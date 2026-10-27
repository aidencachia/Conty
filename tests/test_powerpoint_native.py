"""Fake application/process tests; never resolve an app or send desktop input.

Safe real-framework synthetic render/descriptor tests are in test_powerpoint_render.py.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from conty.adapters.base import AdapterError
from conty.adapters.macos import powerpoint as p
from conty.adapters.macos.powerpoint_scripts import native as n
from conty.adapters.macos.powerpoint_scripts import render as r
from conty.adapters.macos.powerpoint_scripts.focus import JumpFocusGuard

PNG = b"\x89PNG\r\n\x1a\nFAKE"


class FakeKeyboard:
    def __init__(self):
        self.focus = True
        self.allowed = True
        self.created = []

    def frontmost(self, pid):
        assert pid == 123
        return self.focus

    def trusted(self):
        return self.allowed

    def events(self, key, modifiers):
        assert modifiers == ()
        self.created.append(key)
        return key + ":down", key + ":up"


class AXNode:
    def __init__(self, role, **attrs):
        self.pid = 123
        self.attrs = {"AXRole": role, **attrs}


class FakeAX:
    """Only PID-scoped, read-only AX APIs exist on this fake."""

    def __init__(self):
        self.window = AXNode(
            "AXWindow", AXSubrole="AXStandardWindow", AXModal=False, AXChildren=[], AXSheets=[]
        )
        self.group = AXNode("AXGroup", AXWindow=self.window, AXParent=self.window)
        self.canvas = AXNode("AXImage", AXWindow=self.window, AXParent=self.group)
        self.window.attrs["AXChildren"] = [self.group]
        self.item = AXNode("AXMenuBarItem", AXSelected=False, AXExpanded=False, AXChildren=[])
        self.bar = AXNode("AXMenuBar", AXSelectedChildren=[], AXChildren=[self.item])
        self.app = AXNode(
            "AXApplication",
            AXFocusedWindow=self.window,
            AXFocusedUIElement=self.canvas,
            AXMenuBar=self.bar,
        )
        self.errors = {}
        self.names_error = False
        self.calls, self.created_pids = [], []

    def AXUIElementCreateApplication(self, pid):
        self.created_pids.append(pid)
        return self.app

    def AXUIElementGetPid(self, element, _out):
        return 0, element.pid

    def AXUIElementCopyAttributeNames(self, element, _out):
        self.calls.append((element, "attribute_names"))
        return (-25200, None) if self.names_error else (0, list(element.attrs))

    def AXUIElementCopyAttributeValue(self, element, name, _out):
        self.calls.append((element, name))
        if (element, name) in self.errors:
            return self.errors[(element, name)], None
        return (0, element.attrs[name]) if name in element.attrs else (-25205, None)

    @staticmethod
    def CFEqual(first, second):
        return first is second


def deck(window=10, *, count=3, path="/PRIVATE/Deck.pptx"):
    slides = [
        {"SlID": 100 + i * 7, "SIdX": i, "SldN": i, "PSsT": {"Thid": i == 2}, "placeholders": []}
        for i in range(1, count + 1)
    ]
    result = {
        "windows": [window],
        "fNam": path,
        "pnam": "Deck.pptx",
        "slides": slides,
        "PtPo": {"POpT": 901, "SRgT": 902, "POpH": False},
        "pPgS": {"PsF#": 1},
        "SSSt": {"SRgT": n.SHOW_ALL},
    }
    show = {
        "ID  ": window + 1000,
        "Actv": True,
        "pptP": result,
        "PSSv": {"SinS": False, "pSLD": slides[-1] if slides else None},
    }
    result["pSSw"] = show
    return result


class FakeBridge:
    def __init__(self, decks=None, **_kwargs):
        self.pid, self.lifetime = 123, "1000.123456789"
        self.keyboard = FakeKeyboard()
        self.ax = FakeAX()
        self.focus_checks = []
        self.gets = []
        self.decks = [deck()] if decks is None else decks
        self.effects = False
        self.unknown = False
        self.deadline = time.monotonic() + 100
        self.posts, self.sent, self.settings, self.permissions = [], [], [], []
        self.post_hook = self.set_hook = self.export_hook = self.read_hook = None
        self.running = True
        self.root = None
        self.export_count = 0

    def guard(self):
        if not self.running:
            raise p._error("target_unavailable")
        if time.monotonic() >= self.deadline:
            raise p._error("timed_out")

    def permission(self, suite, event_id, *, ask=False):
        self.permissions.append((suite, event_id, ask))

    def prop(self, code, container):
        return "prop", code, container

    def all(self, cls, container=None):
        return "all", cls, container

    def at(self, cls, index, container=None):
        return "at", cls, container, index

    def by_id(self, cls, identity):
        return "id", cls, identity

    def node(self, spec):
        if spec is None:
            return None
        form, code, owner, *index = spec
        if form == "id":
            for d in self.decks:
                if code == "pDWn" and owner in d["windows"]:
                    return {"pptP": d, "ID  ": owner}
                show = d["pSSw"]
                if code == "pSSw" and show is not None and owner == show["ID  "]:
                    return show
            raise p._error("target_unavailable")
        parent = self.node(owner)
        if form == "prop":
            if isinstance(parent, list):
                return [item[code] for item in parent]
            return parent[code]
        if form in ("all", "at"):
            collection = parent[{"pSLD": "slides", "cD05": "placeholders"}[code]]
            return collection if form == "all" else collection[index[0] - 1]
        raise AssertionError(spec)

    def ids(self, cls, container=None, *, limit=64):
        if cls == "pDWn":
            ids = (
                self.node(container)["windows"]
                if container
                else [w for d in self.decks for w in d["windows"]]
            )
        else:
            ids = [d["pSSw"]["ID  "] for d in self.decks if d["pSSw"] is not None]
        if len(ids) > limit:
            raise p._error("limit_exceeded")
        return list(ids)

    def count(self, cls, container=None):
        return len(self.node(self.all(cls, container)))

    def read(self, code, container):
        if self.read_hook:
            self.read_hook(code, container)
        return self.node(self.prop(code, container))

    def get(self, spec):
        self.gets.append(spec)
        return copy.deepcopy(self.node(spec))

    def value(self, value):
        return value

    def enum(self, value):
        return value

    def boolean(self, value):
        return value

    def set(self, spec, value):
        self.effects = True
        _, code, owner = spec
        self.settings.append((code, value))
        if self.set_hook:
            self.set_hook(code, value)
        self.node(owner)[code] = value

    def export(self, presentation, path):
        self.effects = True
        self.export_count += 1
        if self.export_hook:
            self.export_hook(self.node(presentation), path)
        path.write_bytes(b"%PDF-FAKE")

    def jump_focus_safe(self, pid):
        assert pid == self.pid == 123
        self.focus_checks.append((pid, len(self.posts)))
        return JumpFocusGuard(self.ax, deadline=self.deadline).safe(pid)

    def post(self, event):
        self.effects = True
        self.posts.append((self.pid, event))
        if self.post_hook:
            self.post_hook(event)

    def send(self, suite, event_id, direct, parameters=None, *, mutation=False):
        self.effects |= mutation
        self.sent.append((suite, event_id, direct, parameters, mutation))


def edit_bridge():
    b = FakeBridge()
    b.decks[0]["pSSw"] = None
    return b


def handle(b, index=0):
    d = b.decks[index]
    return {
        "pid": b.pid,
        "lifetime": b.lifetime,
        "window_id": d["windows"][0],
        "fullname": d["fNam"],
    }


def ids(b):
    return [str(s["SlID"]) for s in b.decks[0]["slides"]]


def invoke(b, operation, **kwargs):
    return n.execute(
        {"operation": operation, "handle": handle(b), **kwargs}, bridge_factory=lambda **_kwargs: b
    )


def assert_code(code, call, *, effects=None):
    with pytest.raises(AdapterError) as caught:
        call()
    assert caught.value.code == code
    if effects is not None:
        assert caught.value.effects_possible == effects
    return caught.value


def test_discovery_stable_process_window_key_private_path_and_deduplicated_windows():
    b = FakeBridge()
    b.decks[0]["windows"].append(11)
    values = n.DeckSession(b).discover()
    assert len(values) == 1
    adapter = p.NativePowerPoint()
    with patch.object(adapter, "_run", return_value=values):
        first, second = adapter._discover()[0], adapter._discover()[0]
    assert first["key"] == second["key"]
    assert first["handle"]["fullname"] == "/PRIVATE/Deck.pptx"
    assert "/PRIVATE" not in json.dumps({k: v for k, v in first.items() if k != "handle"})
    changed = copy.deepcopy(values)
    changed[0]["handle"]["lifetime"] = "NEW-LIFETIME"
    with patch.object(adapter, "_run", return_value=changed):
        assert adapter._discover()[0]["key"] != first["key"]


class SavedPathBridge(FakeBridge):
    """Runtime variant: document windows have indices, not persistent IDs."""

    def ids(self, cls, container=None, *, limit=64):
        raise p._error("unsupported_identity")

    presentation_paths = n.AppleEvents.presentation_paths

    def node(self, spec):
        if spec is not None:
            form, code, owner, *index = spec
            assert form != "name", "filename selectors must never target saved paths"
            if form in ("all", "at") and owner is None and code in ("pptP", "pSSw"):
                collection = (
                    self.decks
                    if code == "pptP"
                    else [d["pSSw"] for d in self.decks if d["pSSw"] is not None]
                )
                return collection if form == "all" else collection[index[0] - 1]
        return super().node(spec)


def saved_handle(b, index=0):
    return {
        "pid": b.pid,
        "lifetime": b.lifetime,
        "fullname": b.decks[index]["fNam"],
        "identity_kind": "saved_path",
    }


def invoke_saved(b, operation, h=None, **kwargs):
    return n.execute(
        {"operation": operation, "handle": h or saved_handle(b), **kwargs},
        bridge_factory=lambda **_kw: b,
    )


def test_saved_path_discovery_is_explicit_and_private():
    b = SavedPathBridge()
    values = n.DeckSession(b).discover()
    assert len(values) == 1
    value = values[0]
    assert value["handle"] == {
        "pid": b.pid,
        "lifetime": b.lifetime,
        "fullname": b.decks[0]["fNam"],
        "identity_kind": "saved_path",
    }
    assert value["view"] == "read"
    assert value["can_control"] is True
    assert value["show_id"] is None
    assert value["show_token"]["kind"] == "saved_path_show"
    adapter = p.NativePowerPoint()
    with patch.object(adapter, "_run", return_value=values):
        first = adapter._discover()[0]
        b.decks[0]["windows"].reverse()
        assert adapter._discover()[0]["key"] == first["key"]
    assert "/PRIVATE" not in json.dumps({k: v for k, v in first.items() if k != "handle"})
    changed = copy.deepcopy(values)
    changed[0]["handle"]["fullname"] = "/PRIVATE/Other.pptx"
    with patch.object(adapter, "_run", return_value=changed):
        assert adapter._discover()[0]["key"] != first["key"]
    assert not b.effects


def test_saved_path_resolves_by_full_path_not_name_or_root_order():
    a, other = deck(10), deck(20, path="/PRIVATE/Other.pptx")
    other["pnam"] = "Other.pptx"
    other["slides"][0]["SlID"] = 999
    b = SavedPathBridge([a, other])
    session = n.DeckSession(b)
    h = session.discover()[0]["handle"]
    b.decks.reverse()
    assert session.snapshot(h)["slides"][0]["id"] == str(a["slides"][0]["SlID"])
    a["fNam"] = "/PRIVATE/Moved.pptx"
    assert_code("target_unavailable", lambda: session.snapshot(h))


@pytest.mark.parametrize("change", ["path", "unsaved"])
def test_saved_path_ambiguous_or_unsaved_identity_is_never_guessed(change):
    a, other = deck(10), deck(20, path="/PRIVATE/Other.pptx")
    other["pnam"] = "Other.pptx"
    if change == "path":
        other["fNam"] = a["fNam"]

    else:
        a["fNam"], other["fNam"] = "Presentation1", "Presentation2"
    b = SavedPathBridge([a, other])
    assert_code("unsupported_identity", lambda: n.DeckSession(b).discover())
    assert not b.effects


@pytest.mark.parametrize("unsaved_path", ["Presentation2", None])
def test_unsaved_presentation_does_not_erase_supported_saved_picker_entry(unsaved_path):
    a, unsaved = deck(10), deck(20, path=unsaved_path)
    unsaved["pnam"] = "Presentation2"
    b = SavedPathBridge([a, unsaved])
    assert len(n.DeckSession(b).discover()) == 1


@pytest.mark.parametrize("field,value", [("pid", 124), ("lifetime", "reused")])
def test_saved_path_rechecks_process_identity(field, value):
    b = SavedPathBridge()
    session = n.DeckSession(b)
    h = {**session.discover()[0]["handle"], field: value}
    assert_code("target_unavailable", lambda: session.snapshot(h))


def test_saved_path_rechecks_index_fullname_after_inventory():
    a, other = deck(10), deck(20, path="/OTHER/Deck.pptx")
    b = SavedPathBridge([a, other])
    paths = b.presentation_paths

    def reorder_after_inventory():
        result = paths()
        b.decks.reverse()
        return result

    b.presentation_paths = reorder_after_inventory
    assert_code("target_unavailable", lambda: n.DeckSession(b).resolve(saved_handle(b)))
    assert not b.effects


@pytest.mark.parametrize("operation", ["export", "next_slide", "previous_slide", "show_slide"])
def test_saved_path_duplicate_fullpath_rejected_before_effects(operation, tmp_path):
    b = SavedPathBridge([deck(10), deck(20)])
    response = invoke_saved(b, operation, pdf=str(tmp_path / "preview.pdf"))
    assert response == {
        "ok": False,
        "code": "unsupported_identity",
        "effects_possible": False,
        "poison": False,
    }
    assert not b.effects and not b.settings and not b.sent and not b.posts
    assert b.export_count == 0


def test_saved_path_same_basename_in_different_directories_remains_distinct():
    a, other = deck(10), deck(20, path="/OTHER/Deck.pptx")
    other["slides"][-1]["SlID"] = 999
    b = SavedPathBridge([a, other])
    session = n.DeckSession(b)
    values = session.discover()
    assert len(values) == 2
    assert values[0]["current_slide_id"] == "121"
    assert values[1]["current_slide_id"] == "999"
    h = {**saved_handle(b), "fullname": "/NONEXISTENT/Deck.pptx"}
    response = invoke_saved(b, "next_slide", h)
    assert response["code"] == "target_unavailable"
    assert not response["effects_possible"] and not b.sent


def test_saved_path_discovery_reads_one_manifest_without_shape_traversal():
    b = SavedPathBridge()
    session = n.DeckSession(b)
    session.manifest = Mock(wraps=session.manifest)
    session.title = Mock(side_effect=AssertionError("shape traversal"))
    values = session.discover()
    assert session.manifest.call_count == 1
    assert len(values[0]["slides"]) == 3
    session.snapshot(values[0]["handle"])
    assert session.manifest.call_count == 3  # Observe still verifies concurrency.
    session.title.assert_not_called()


def test_saved_path_index_reorder_before_operation_targets_the_same_file(tmp_path):
    a, other = deck(10), deck(20, path="/OTHER/Deck.pptx")
    a["pSSw"] = other["pSSw"] = None
    other["PtPo"]["POpT"] = 444
    b = SavedPathBridge([a, other])
    h = saved_handle(b)
    b.decks.reverse()
    original = copy.deepcopy(other["PtPo"])
    targets = []
    b.export_hook = lambda target, _path: targets.append(target["fNam"])
    result = invoke_saved(b, "export", h, pdf=str(tmp_path / "preview.pdf"))
    assert result["ok"]
    assert targets == [a["fNam"]]
    assert other["PtPo"] == original
    assert a["PtPo"] == {"POpT": 901, "SRgT": 902, "POpH": False}
    assert len(b.settings) == 6


@pytest.mark.parametrize("operation,event", [("next_slide", "SnxS"), ("previous_slide", "SpvS")])
def test_saved_path_control_targets_associated_show_not_root_show_index(operation, event):
    a, other = deck(10), deck(20, path="/OTHER/Deck.pptx")
    b = SavedPathBridge([a, other])
    h = saved_handle(b)
    b.decks.reverse()
    result = invoke_saved(b, operation, h)
    assert result["ok"]
    assert b.sent == [("sPPT", event, b.prop("PSSv", b.prop("pSSw", b.at("pptP", 2))), None, True)]
    assert not b.posts


@pytest.mark.parametrize("change", ["duplicate_show", "associated_owner"])
def test_saved_path_show_ownership_must_be_unique_and_exact(change):
    a, other = deck(10), deck(20, path="/OTHER/Deck.pptx")
    if change == "duplicate_show":
        other["pSSw"]["pptP"] = a
    else:
        a["pSSw"]["pptP"] = other
        other["pSSw"]["pptP"] = a
    b = SavedPathBridge([a, other])
    result = invoke_saved(b, "next_slide")
    assert result["code"] == "target_unavailable"
    assert not result["effects_possible"] and not b.sent


def test_saved_path_reorder_during_settings_capture_aborts_before_first_mutation(tmp_path):
    a, other = deck(10), deck(20, path="/OTHER/Deck.pptx")
    a["pSSw"] = other["pSSw"] = None
    b = SavedPathBridge([a, other])
    get = b.get

    def captured(spec):
        value = get(spec)
        if len(b.gets) == 3:
            b.decks.reverse()
        return value

    b.get = captured
    result = invoke_saved(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result["code"] == "target_unavailable"
    assert not result["effects_possible"] and not result["poison"]
    assert not b.settings and b.export_count == 0


def test_saved_path_every_set_save_and_restore_has_an_immediate_fullname_check(tmp_path):
    b = SavedPathBridge()
    b.decks[0]["pSSw"] = None
    reads, mutations = [], []
    b.read_hook = lambda code, target: reads.append((code, target))
    set_value, export = b.set, b.export

    def checked_set(spec, value):
        assert reads[-1] == ("fNam", b.at("pptP", 1))
        mutations.append("set")
        set_value(spec, value)

    def checked_export(presentation, path):
        assert reads[-1] == ("fNam", presentation)
        mutations.append("save")
        export(presentation, path)

    b.set, b.export = checked_set, checked_export
    result = invoke_saved(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result["ok"]
    assert mutations == ["set", "set", "set", "save", "set", "set", "set"]


def test_saved_path_command_postflight_rejects_membership_change_after_send():
    b = SavedPathBridge([deck(10), deck(20, path="/OTHER/Deck.pptx")])
    send = b.send

    def changing_send(*args, **kwargs):
        send(*args, **kwargs)
        b.decks.reverse()

    b.send = changing_send
    result = invoke_saved(b, "next_slide")
    assert result["code"] == "target_unavailable"
    assert result["effects_possible"] and result["poison"]
    assert len(b.sent) == 1


def test_saved_path_unsaved_target_has_clear_error_without_effects():
    b = SavedPathBridge([deck(path="Presentation1")])
    result = invoke_saved(b, "next_slide")
    assert result["code"] == "unsupported_identity"
    assert not result["effects_possible"] and not result["poison"]
    assert not b.sent and not b.posts


def test_saved_path_post_verification_reorder_is_uncertain_and_never_restores_by_old_index(
    tmp_path,
):
    a, other = deck(10), deck(20, path="/OTHER/Deck.pptx")
    a["pSSw"] = other["pSSw"] = None
    b = SavedPathBridge([a, other])
    b.set_hook = lambda _code, _value: b.decks.reverse()
    result = invoke_saved(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert not result["ok"]
    assert result["effects_possible"] and result["poison"]
    assert len(b.settings) == 1
    assert b.export_count == 0


@pytest.mark.parametrize("stage", ["set", "save", "restore"])
def test_saved_path_rename_after_any_effect_refuses_further_targets_and_poisons(stage, tmp_path):
    b = SavedPathBridge()
    b.decks[0]["pSSw"] = None

    def rename(code, _value):
        if (stage == "set" and len(b.settings) == 1) or (
            stage == "restore" and len(b.settings) == 4
        ):
            b.decks[0]["fNam"] = "/MOVED/Deck.pptx"

    if stage == "save":
        b.export_hook = lambda target, _path: target.update(fNam="/MOVED/Deck.pptx")
    else:
        b.set_hook = rename
    result = invoke_saved(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result["code"] == "executor_unavailable"
    assert result["effects_possible"] and result["poison"]
    assert len(b.settings) == {"set": 1, "save": 3, "restore": 4}[stage]


def test_saved_path_failed_export_restores_each_setting_on_the_revalidated_path(tmp_path):
    b = SavedPathBridge()
    b.decks[0]["pSSw"] = None
    original = dict(b.decks[0]["PtPo"])
    b.export_hook = Mock(side_effect=p._error("export_failed"))
    result = invoke_saved(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result["code"] == "export_failed"
    assert result["effects_possible"] and result["poison"]
    assert b.decks[0]["PtPo"] == original
    assert b.settings[-3:] == [("POpH", False), ("SRgT", 902), ("POpT", 901)]


@pytest.mark.parametrize(
    "change", ["path", "membership", "show", "focus", "permission", "active", "lifetime", "exit"]
)
@pytest.mark.parametrize("after_event", [1, 2, 3, 4])
def test_saved_path_jump_defers_deck_checks_but_rechecks_input_safety_each_pair(
    change, after_event
):
    b = SavedPathBridge([deck(), deck(20, path="/OTHER/Deck.pptx")])
    h, expected = saved_handle(b), ids(b)

    def mutate(_event):
        if len(b.posts) != after_event:
            return
        if change == "path":
            b.decks[0]["fNam"] = "/OTHER/Deck.pptx"
        elif change == "membership":
            b.decks.append(deck(30, path="/THIRD/Deck.pptx"))
        elif change == "show":
            # Preserve presentation membership and the selected active show, but
            # change its saved-path show token by removing the other owner's show.
            b.decks[1]["pSSw"] = None
        elif change == "focus":
            b.keyboard.focus = False
        elif change == "permission":
            b.keyboard.allowed = False
        elif change == "active":
            b.decks[0]["pSSw"]["Actv"] = False
        elif change == "lifetime":
            b.lifetime = "replacement"
        else:
            b.running = False

    b.post_hook = mutate
    result = invoke_saved(b, "show_slide", h, slide_id=expected[0], expected_slides=expected)
    assert not result["ok"]
    assert result["effects_possible"] and result["poison"]
    deferred = change in ("path", "membership", "show", "active")
    assert len(b.posts) == (4 if deferred else after_event + after_event % 2)
    assert b.posts[-1][1].endswith(":up")
    assert all(pid == 123 for pid, _ in b.posts)
    if deferred:
        assert b.posts[-2:] == [(123, "return:down"), (123, "return:up")]
    elif after_event <= 2:
        assert not any("return" in event for _, event in b.posts)


@pytest.mark.parametrize("operation", ["export", "next_slide"])
def test_saved_path_final_snapshot_must_preserve_operation_membership(operation, tmp_path):
    b = SavedPathBridge()
    if operation == "export":
        b.decks[0]["pSSw"] = None
    original = n.DeckSession.snapshot
    calls = 0

    def snapshot(session, *args, **kwargs):
        nonlocal calls
        calls += 1
        value = original(session, *args, **kwargs)
        if calls == (3 if operation == "export" else 2):
            value["target_signature"] = {**value["target_signature"], "index": 2}
        return value

    with patch.object(n.DeckSession, "snapshot", snapshot):
        response = invoke_saved(b, operation, pdf=str(tmp_path / "preview.pdf"))
    assert not response["ok"]
    assert response["effects_possible"] and response["poison"]


def test_saved_path_restore_failure_attempts_remaining_settings_and_poisons(tmp_path):
    b = SavedPathBridge()
    b.decks[0]["pSSw"] = None

    def fail(code, value):
        if code == "POpH" and value is False:
            raise p._error("permission_denied")

    b.set_hook = fail
    result = invoke_saved(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result["code"] == "executor_unavailable"
    assert result["effects_possible"] and result["poison"]
    assert b.settings[-2:] == [("SRgT", 902), ("POpT", 901)]


def test_saved_path_jump_has_no_native_getter_between_down_and_up():
    b = SavedPathBridge()

    def reading(_code, _container):
        assert len(b.posts) % 2 == 0

    b.read_hook = reading
    result = invoke_saved(b, "show_slide", slide_id=ids(b)[0], expected_slides=ids(b))
    assert result["ok"]
    assert b.posts == [(123, "1:down"), (123, "1:up"), (123, "return:down"), (123, "return:up")]
    assert len(b.focus_checks) == 3  # Once per pair, then postflight; never between down/up.


@pytest.mark.parametrize(
    "extra",
    [
        {"identity_kind": "other"},
        {"identity_kind": "saved_path"},
        {"identity_kind": "saved_path", "window_id": None},
    ],
)
def test_private_handle_rejects_unknown_or_mixed_identity(extra):
    assert_code("target_unavailable", lambda: p._handle({**handle(FakeBridge()), **extra}))


def test_discovery_sixteen_decks_maximum():
    b = FakeBridge([deck(i) for i in range(16)])
    assert len(n.DeckSession(b).discover()) == 16
    b.decks.append(deck(99))
    assert_code("limit_exceeded", lambda: n.DeckSession(b).discover())


@pytest.mark.parametrize("count,target", [(81, 1), (81, 81), (150, 123)])
def test_number_jump_hidden_metadata_cost_is_independent_of_digit_count(count, target):
    b = FakeBridge([deck(count=count)])
    original_read = b.read
    hidden_reads = []

    def read(code, container):
        if code == "Thid" and container[2][0] == "all":
            return [n.fourcc("msng")] * count
        if code == "Thid":
            hidden_reads.append(len(b.posts))
        return original_read(code, container)

    b.read = read
    expected = ids(b)
    value = n.DeckSession(b).command(handle(b), "show_slide", expected[target - 1], expected)
    assert value["slides"][1]["hidden"] is True
    assert len(hidden_reads) == count  # Legacy callers read once; postflight reuses metadata.
    assert set(hidden_reads) == {0}
    pairs = len(str(target)) + 1
    assert len(b.posts) == 2 * pairs
    assert all(pid == 123 for pid, _ in b.posts)


def test_legacy_jump_rejects_mid_jump_numbering_change_only_after_all_pairs():
    b = FakeBridge([deck(count=81)])
    expected = ids(b)

    def changed(_event):
        if len(b.posts) == 2:
            b.decks[0]["slides"][0]["SldN"] = 1001

    b.post_hook = changed
    response = invoke(b, "show_slide", slide_id=expected[80], expected_slides=expected)
    assert response["code"] == "stale_snapshot"
    assert response["effects_possible"] and response["poison"]
    assert b.posts == [
        (123, f"{key}:{state}") for key in ["8", "1", "return"] for state in ("down", "up")
    ]


def test_manifest_cap_and_no_truncation():
    b = FakeBridge([deck(count=500)])
    assert len(n.DeckSession(b).snapshot(handle(b))["slides"]) == 500
    b.decks = [deck(count=501)]
    assert_code("limit_exceeded", lambda: n.DeckSession(b).snapshot(handle(b)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", 124),
        ("lifetime", "reused-pid"),
        ("window_id", 11),
        ("fullname", "/elsewhere/Deck.pptx"),
    ],
)
def test_observe_requires_exact_identity(field, value):
    b = FakeBridge()
    h = {**handle(b), field: value}
    assert_code("target_unavailable", lambda: n.DeckSession(b).snapshot(h))
    assert not b.effects


def test_duplicate_window_identity_rejected():
    b = FakeBridge([deck(10), deck(10)])
    assert_code("target_unavailable", lambda: n.DeckSession(b).snapshot(handle(b)))


def test_two_same_named_presentations_do_not_share_live_slide():
    a, other = deck(10), deck(20)
    other["slides"][-1]["SlID"] = 999
    b = FakeBridge([a, other])
    session = n.DeckSession(b)
    assert a["fNam"] == other["fNam"]
    assert session.snapshot(handle(b, 0))["current_slide_id"] == "121"
    assert session.snapshot(handle(b, 1))["current_slide_id"] == "999"
    a["pSSw"] = None
    value = session.snapshot(handle(b, 0))
    assert value["view"] == "edit"
    assert value["current_slide_id"] is None  # Never editor selection/currentShowPosition.
    assert value["can_control"] is False


def test_selected_show_relationship_must_match_presentation_property():
    b = FakeBridge()
    original = b.read

    def read(code, container):
        if code == "ID  " and container[0:2] == ("prop", "pSSw"):
            return 999
        return original(code, container)

    b.read = read
    assert_code("target_unavailable", lambda: n.DeckSession(b).snapshot(handle(b)))


@pytest.mark.parametrize("active,focus", [(False, True), (True, False)])
def test_control_readiness_is_selected_show_active_and_frontmost(active, focus):
    b = FakeBridge()
    b.decks[0]["pSSw"]["Actv"] = active
    b.keyboard.focus = focus
    assert n.DeckSession(b).snapshot(handle(b))["can_control"] is False
    result = invoke(b, "next_slide")
    assert result["code"] == "target_not_frontmost"
    assert not result["effects_possible"]
    assert not b.posts and not b.sent


def test_slide_title_comes_from_title_placeholder_and_is_bounded():
    b = FakeBridge()
    b.decks[0]["slides"][0]["placeholders"] = [
        {"PHtY": 0x00DB0002},
        {"PHtY": 0x00DB0001, "TxTF": {"TObj": {"pTdT": "  Hello\r\nWorld "}}},
    ]
    assert n.DeckSession(b).snapshot(handle(b), titles=True)["slides"][0]["title"] == "Hello World"
    b.decks[0]["slides"][0]["placeholders"][1]["TxTF"]["TObj"]["pTdT"] = "x" * 1000
    assert len(n.DeckSession(b).snapshot(handle(b), titles=True)["slides"][0]["title"]) == 256


def test_snapshot_detects_concurrent_reorder():
    b = FakeBridge()
    session = n.DeckSession(b)
    original = session.manifest

    calls = 0

    def manifest(presentation, *, titles=False):
        nonlocal calls
        calls += 1
        result = original(presentation, titles=titles)
        return result if calls == 1 else list(reversed(result))

    session.manifest = manifest
    assert_code("stale_snapshot", lambda: session.snapshot(handle(b)))


def test_discover_and_observe_never_traverse_shapes_or_collect_titles():
    b = FakeBridge()
    session = n.DeckSession(b)
    session.title = Mock(side_effect=AssertionError("title traversal during polling"))
    values = session.discover()
    assert [s["title"] for s in values[0]["slides"]] == ["Slide 1", "Slide 2", "Slide 3"]
    assert session.snapshot(handle(b))["slides"] == values[0]["slides"]
    session.title.assert_not_called()


def test_missing_bulk_transition_hidden_flags_use_real_individual_booleans():
    b = FakeBridge()
    read = b.read
    individual = []

    def runtime_read(code, container):
        if code == "Thid" and container[2][0] == "all":
            return [n.fourcc("msng")] * len(b.decks[0]["slides"])
        if code == "Thid":
            individual.append(container)
        return read(code, container)

    b.read = runtime_read
    slides = n.DeckSession(b).snapshot(handle(b))["slides"]
    assert [s["hidden"] for s in slides] == [False, True, False]
    assert len(individual) == 6  # Initial manifest plus concurrency verification.
    assert not b.effects


def test_invalid_hidden_flags_are_not_coerced_or_assumed_visible():
    b = FakeBridge()
    read = b.read
    b.read = lambda code, container: [0, 0, 0] if code == "Thid" else read(code, container)
    assert_code("stale_snapshot", lambda: n.DeckSession(b).snapshot(handle(b)))


def test_duplicate_slide_ids_and_non_index_order_rejected():
    for field in ("SlID", "SIdX"):
        b = FakeBridge()
        b.decks[0]["slides"][1][field] = b.decks[0]["slides"][0][field]
        assert_code("stale_snapshot", lambda: n.DeckSession(b).snapshot(handle(b)))


def test_export_all_including_hidden_and_restore_without_saved_property(tmp_path):
    b = edit_bridge()
    original = dict(b.decks[0]["PtPo"])

    def exporting(d, _path):
        assert d["PtPo"] == {"POpT": n.PRINT_SLIDES, "SRgT": n.PRINT_ALL, "POpH": True}

    b.export_hook = exporting
    result = n.DeckSession(b).export(handle(b), tmp_path / "preview.pdf")
    assert result["slides"][1]["hidden"] is True
    assert b.decks[0]["PtPo"] == original
    assert b.settings == [
        ("POpT", n.PRINT_SLIDES),
        ("SRgT", n.PRINT_ALL),
        ("POpH", True),
        ("POpH", False),
        ("SRgT", 902),
        ("POpT", 901),
    ]
    assert b.export_count == 1
    assert all(code != "save" for code, _ in b.settings)


@pytest.mark.parametrize("failure", ["export", "first_set", "second_set", "snapshot"])
def test_export_restores_on_every_failure(tmp_path, failure):
    b = edit_bridge()
    original = dict(b.decks[0]["PtPo"])
    if failure == "export":
        b.export_hook = Mock(side_effect=p._error("export_failed"))
    elif failure in ("first_set", "second_set"):
        fail_code = "POpT" if failure == "first_set" else "SRgT"
        raised = False

        def fail(code, value):
            nonlocal raised
            if code == fail_code and not raised:
                raised = True
                raise p._error("execution_failed")

        b.set_hook = fail
    else:

        def change(_code, _value):
            b.decks[0]["slides"][0]["PSsT"]["Thid"] = True

        b.set_hook = change
    with pytest.raises(AdapterError):
        n.DeckSession(b).export(handle(b), tmp_path / "preview.pdf")
    assert b.decks[0]["PtPo"] == original
    assert b.settings[-3:] == [("POpH", False), ("SRgT", 902), ("POpT", 901)]


def test_export_restore_failure_attempts_remaining_settings_and_poisons(tmp_path):
    b = edit_bridge()

    def fail(code, value):
        if code == "POpH" and value is False:
            raise p._error("permission_denied")

    b.set_hook = fail
    result = invoke(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result == {
        "ok": False,
        "code": "executor_unavailable",
        "effects_possible": True,
        "poison": True,
    }
    assert b.settings[-2:] == [("SRgT", 902), ("POpT", 901)]


@pytest.mark.parametrize("change", ["path", "manifest"])
def test_export_never_returns_if_original_identity_or_manifest_changed(tmp_path, change):
    b = edit_bridge()

    def mutate(d, _path):
        if change == "path":
            d["fNam"] = "/PRIVATE/MOVED.pptx"
        else:
            d["slides"].reverse()

    b.export_hook = mutate
    result = invoke(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result["ok"] is False
    assert result["poison"] is True
    assert "/PRIVATE" not in json.dumps(result)
    assert b.decks[0]["PtPo"] == {"POpT": 901, "SRgT": 902, "POpH": False}


def test_export_initial_read_mode_refuses_before_settings_read_or_mutation(tmp_path):
    b = FakeBridge()
    result = invoke(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result == {
        "ok": False,
        "code": "invalid_action",
        "effects_possible": False,
        "poison": False,
    }
    assert not b.gets and not b.settings and b.export_count == 0
    assert not (tmp_path / "preview.pdf").exists()


@pytest.mark.parametrize("stage", ["settings", "export", "restore"])
def test_export_mode_change_restores_options_and_never_returns_preview(tmp_path, stage):
    b = edit_bridge()
    original = dict(b.decks[0]["PtPo"])

    def start_show():
        d = b.decks[0]
        show = deck()["pSSw"]
        show["pptP"] = d
        show["PSSv"]["pSLD"] = d["slides"][-1]
        d["pSSw"] = show

    def change_on_set(code, value):
        if code == "POpH" and (
            (stage == "settings" and value is True) or (stage == "restore" and value is False)
        ):
            start_show()

    b.set_hook = change_on_set
    if stage == "export":
        b.export_hook = lambda _presentation, _path: start_show()
    result = invoke(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result["code"] == "stale_snapshot"
    assert result["effects_possible"] and result["poison"]
    assert b.decks[0]["PtPo"] == original
    assert b.settings[-3:] == [("POpH", False), ("SRgT", 902), ("POpT", 901)]
    assert b.export_count == (0 if stage == "settings" else 1)


def test_export_refuses_empty_deck_without_changing_options(tmp_path):
    b = FakeBridge([deck(count=0)])
    b.decks[0]["pSSw"] = None
    result = invoke(b, "export", pdf=str(tmp_path / "preview.pdf"))
    assert result["code"] == "export_failed"
    assert not b.settings


@pytest.mark.parametrize("operation,event", [("next_slide", "SnxS"), ("previous_slide", "SpvS")])
def test_next_previous_target_selected_slideshow_view_no_keyboard(operation, event):
    b = FakeBridge()
    result = invoke(b, operation, expected_slides=ids(b))
    assert result["ok"]
    assert len(b.sent) == 1
    assert b.sent[0] == ("sPPT", event, b.prop("PSSv", b.by_id("pSSw", 1010)), None, True)
    assert not b.posts


def test_jump_maps_stable_slide_id_to_current_index_not_id_or_show_position():
    b = FakeBridge([deck(count=12)])
    result = invoke(b, "show_slide", slide_id=ids(b)[11], expected_slides=ids(b))
    assert result["ok"]
    assert b.posts == [
        (123, "1:down"),
        (123, "1:up"),
        (123, "2:down"),
        (123, "2:up"),
        (123, "return:down"),
        (123, "return:up"),
    ]
    assert b.keyboard.created == ["1", "2", "return"]
    assert result["result"]["effect_confirmation"] == "dispatch_only"


@pytest.mark.parametrize("operation", ["show_slide", "next_slide", "previous_slide"])
def test_stale_expected_order_rejected_before_any_input(operation):
    b = FakeBridge()
    result = invoke(
        b,
        operation,
        slide_id=ids(b)[0] if operation == "show_slide" else None,
        expected_slides=list(reversed(ids(b))),
    )
    assert result["code"] == "stale_snapshot"
    assert not result["effects_possible"]
    assert not b.posts and not b.sent


@pytest.mark.parametrize("change", ["named", "range", "first_number", "slide_number"])
def test_number_jump_rejects_custom_and_nonstandard_numbering(change):
    b = FakeBridge()
    d = b.decks[0]
    if change == "named":
        d["pSSw"]["PSSv"]["SinS"] = True
    elif change == "range":
        d["SSSt"]["SRgT"] = 0x00D60003
    elif change == "first_number":
        d["pPgS"]["PsF#"] = 20
    else:
        d["slides"][0]["SldN"] = 5
    result = invoke(b, "show_slide", slide_id=ids(b)[0], expected_slides=ids(b))
    assert result["code"] == "unsupported_show"
    assert not result["effects_possible"]
    assert not b.posts
    # Named shows still support direct view-scoped next/previous.
    assert invoke(b, "next_slide")["ok"]


@pytest.mark.parametrize(
    "change",
    ["focus", "permission", "pid", "lifetime", "exit", "ax", "active", "show", "order", "deadline"],
)
@pytest.mark.parametrize("after_event", [1, 2, 3, 4, 5, 6])
def test_jump_defers_deck_checks_but_rechecks_input_safety_after_release(change, after_event):
    b = FakeBridge([deck(count=12)])
    h = handle(b)
    expected = ids(b)

    def mutate(_event):
        if len(b.posts) != after_event:
            return
        if change == "focus":
            b.keyboard.focus = False
        elif change == "permission":
            b.keyboard.allowed = False
        elif change == "pid":
            b.pid += 1
        elif change == "lifetime":
            b.lifetime = "replacement"
        elif change == "exit":
            b.running = False
        elif change == "ax":
            b.ax.canvas.attrs["AXRole"] = "AXTextField"
        elif change == "active":
            b.decks[0]["pSSw"]["Actv"] = False
        elif change == "show":
            b.decks[0]["pSSw"]["ID  "] = 8000
        elif change == "order":
            b.decks[0]["slides"].reverse()
        else:
            b.deadline = 0

    b.post_hook = mutate
    result = n.execute(
        {
            "operation": "show_slide",
            "handle": h,
            "slide_id": expected[11],
            "expected_slides": expected,
        },
        bridge_factory=lambda **_kw: b,
    )
    assert result["ok"] is False
    assert result["effects_possible"] is True
    assert result["poison"] is True
    # Input-safety losses abort before the next pair, after unconditional release.
    # Deck/show changes are an accepted uncertainty until the final snapshot.
    deferred = change in ("active", "show", "order")
    assert len(b.posts) == (6 if deferred else after_event + after_event % 2)
    assert b.posts[-1][1].endswith(":up")
    assert not any("escape" in event for _, event in b.posts)
    if deferred:
        assert b.posts[-2:] == [(123, "return:down"), (123, "return:up")]
    elif after_event <= 4:
        assert not any("return" in event for _, event in b.posts)
    if change != "pid":
        assert all(pid == 123 for pid, _ in b.posts)


@pytest.mark.parametrize("event", ["1:down", "1:up", "return:down", "return:up"])
def test_post_errors_still_attempt_release_and_poison_without_cleanup_input(event):
    b = FakeBridge()

    def fail(posted):
        if posted == event:
            raise RuntimeError("post may have delivered before failing")

    b.post_hook = fail
    result = invoke(b, "show_slide", slide_id=ids(b)[0], expected_slides=ids(b))
    assert result["code"] == "execution_failed"
    assert result["effects_possible"] is True
    assert result["poison"] is True
    assert b.posts == (
        [(123, "1:down"), (123, "1:up")]
        + ([(123, "return:down"), (123, "return:up")] if event.startswith("return") else [])
    )


@pytest.mark.parametrize("bridge_type", [FakeBridge, SavedPathBridge])
@pytest.mark.parametrize("cached", [False, True])
def test_jump_builds_all_events_before_single_full_guard_then_uses_input_guards(
    bridge_type, cached
):
    b = bridge_type([deck(count=12)])
    session = n.DeckSession(b)
    value = session.discover()[0]
    sequence = []
    guard, input_guard, post = session.command_guard, session.jump_input_guard, b.post

    def guarded(*args, **kwargs):
        assert b.keyboard.created == ["1", "2", "return"]
        assert not b.posts
        assert kwargs["jump"] is True
        sequence.append("full_guard")
        return guard(*args, **kwargs)

    def input_guarded(*args, **kwargs):
        sequence.append("input_guard")
        return input_guard(*args, **kwargs)

    def posted(event):
        sequence.append(event)
        post(event)

    session.command_guard = Mock(side_effect=guarded)
    session.jump_input_guard = Mock(side_effect=input_guarded)
    b.post = posted
    session.command(
        value["handle"], "show_slide", ids(b)[11], ids(b), value["slides"] if cached else None
    )
    assert session.command_guard.call_count == 1
    assert sequence == [
        "full_guard",
        "input_guard",  # Final input-safety step inside the initial full guard.
        "1:down",
        "1:up",
        "input_guard",
        "2:down",
        "2:up",
        "input_guard",
        "return:down",
        "return:up",
        "input_guard",
    ]
    assert session.jump_input_guard.call_count == 4
    for call in session.jump_input_guard.call_args_list:
        assert call.args == (value["handle"],)
        assert not call.kwargs
    assert b.focus_checks == [(123, 0), (123, 2), (123, 4), (123, 6)]


@pytest.mark.parametrize("bridge_type", [FakeBridge, SavedPathBridge])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("target", [81, 181])
def test_jump_native_reads_only_before_first_or_after_last_event(bridge_type, cached, target):
    b = bridge_type([deck(count=target)])
    session = n.DeckSession(b)
    value = session.discover()[0]
    total_events = 2 * (len(str(target)) + 1)
    reads = []

    def checked_read(method):
        def reading(*args, **kwargs):
            reads.append(len(b.posts))
            assert len(b.posts) in (0, total_events)
            return method(*args, **kwargs)

        return reading

    # Instrument all fake Apple Event read entry points, not just property reads.
    for name in ("read", "get", "ids", "count"):
        setattr(b, name, checked_read(getattr(b, name)))
    result = session.command(
        value["handle"],
        "show_slide",
        ids(b)[target - 1],
        ids(b),
        value["slides"] if cached else None,
    )
    assert result["effect_confirmation"] == "dispatch_only"
    assert set(reads) == {0, total_events}
    assert b.posts == [
        (123, f"{key}:{state}") for key in [*str(target), "return"] for state in ("down", "up")
    ]
    assert b.focus_checks == [(123, index) for index in range(0, total_events + 1, 2)]


@pytest.mark.parametrize("bridge_type", [FakeBridge, SavedPathBridge])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("change", ["show_token", "inactive"])
def test_jump_final_snapshot_rejects_changed_show_token_or_inactive_show(
    bridge_type, cached, change
):
    b = bridge_type([deck(count=12), deck(20, path="/OTHER/Deck.pptx")])
    session = n.DeckSession(b)
    value = session.discover()[0]
    after = []
    snapshot = session.snapshot

    def observed_snapshot(*args, **kwargs):
        result = snapshot(*args, **kwargs)
        if b.effects:
            after.append(result)
        return result

    def mutate(_event):
        if len(b.posts) != 2:
            return
        if change == "inactive":
            b.decks[0]["pSSw"]["Actv"] = False
        elif bridge_type is SavedPathBridge:
            b.decks[1]["pSSw"] = None
        else:
            b.decks[0]["pSSw"]["ID  "] = 8000

    session.snapshot = observed_snapshot
    b.post_hook = mutate
    with pytest.raises(AdapterError) as caught:
        session.command(
            value["handle"],
            "show_slide",
            ids(b)[11],
            ids(b),
            value["slides"] if cached else None,
        )
    assert caught.value.code == (
        "target_not_frontmost" if change == "inactive" else "target_unavailable"
    )
    assert b.effects
    assert b.posts == [
        (123, f"{key}:{state}") for key in ["1", "2", "return"] for state in ("down", "up")
    ]
    assert len(after) == 1  # Rejection must use the final snapshot, not a per-key deck read.
    assert after[0]["can_control"] is (change != "inactive")
    assert b.focus_checks == [(123, 0), (123, 2), (123, 4), (123, 6)]


def test_reorder_after_final_event_still_rejects_command_result():
    b = FakeBridge()
    expected = ids(b)

    def reorder(_event):
        if len(b.posts) == 4:
            b.decks[0]["slides"].reverse()
            for index, slide in enumerate(b.decks[0]["slides"], 1):
                slide["SIdX"] = slide["SldN"] = index

    b.post_hook = reorder
    result = invoke(b, "show_slide", slide_id=expected[0], expected_slides=expected)
    assert result["code"] == "stale_snapshot"
    assert result["effects_possible"] and result["poison"]
    assert len(b.posts) == 4


def ax_safe(ax):
    return JumpFocusGuard(ax, deadline=time.monotonic() + 5).safe(123)


@pytest.mark.parametrize(
    "role",
    ["AXWindow", "AXGroup", "AXSplitGroup", "AXLayoutArea", "AXScrollArea", "AXImage", "AXUnknown"],
)
def test_ax_explicit_container_allowlist_with_same_window_ancestry(role):
    ax = FakeAX()
    if role == "AXWindow":
        ax.app.attrs["AXFocusedUIElement"] = ax.window
    else:
        ax.canvas.attrs["AXRole"] = role
    assert ax_safe(ax)
    assert ax.created_pids == [123]
    assert all(element.pid == 123 for element, _attr in ax.calls)


@pytest.mark.parametrize(
    "role",
    [
        "AXMenu",
        "AXMenuItem",
        "AXMenuBar",
        "AXMenuBarItem",
        "AXSheet",
        "AXDialog",
        "AXSystemDialog",
        "AXTextField",
        "AXTextArea",
        "AXComboBox",
        "AXButton",
        "AXCheckBox",
        "AXPopUpButton",
        "AXUnexpected",
    ],
)
@pytest.mark.parametrize("node", ["canvas", "group"])
def test_ax_rejects_controls_notes_menus_and_dialogs_in_focus_ancestry(role, node):
    ax = FakeAX()
    getattr(ax, node).attrs["AXRole"] = role
    assert not ax_safe(ax)


@pytest.mark.parametrize(
    "failure",
    [
        "modal",
        "dialog_subrole",
        "sheet_role",
        "sheet_child",
        "sheets_attribute",
        "foreign_window",
        "foreign_pid",
        "cyclic_parent",
        "missing_parent",
        "unknown_modal_value",
    ],
)
def test_ax_window_membership_and_nonmodal_state_must_be_proven(failure):
    ax = FakeAX()
    if failure == "modal":
        ax.window.attrs["AXModal"] = True
    elif failure == "dialog_subrole":
        ax.window.attrs["AXSubrole"] = "AXDialog"
    elif failure == "sheet_role":
        ax.window.attrs["AXRole"] = "AXSheet"
    elif failure == "sheet_child":
        ax.window.attrs["AXChildren"].append(AXNode("AXSheet"))
    elif failure == "sheets_attribute":
        ax.window.attrs["AXSheets"] = [AXNode("AXSheet")]
    elif failure == "foreign_window":
        ax.canvas.attrs["AXWindow"] = AXNode("AXWindow")
    elif failure == "foreign_pid":
        ax.canvas.pid = 999
    elif failure == "cyclic_parent":
        ax.group.attrs["AXParent"] = ax.canvas
    elif failure == "missing_parent":
        del ax.canvas.attrs["AXParent"]
    else:
        ax.window.attrs["AXModal"] = 0  # An untyped/unknown value is not proof of false.
    assert not ax_safe(ax)


@pytest.mark.parametrize(
    "node,attribute",
    [
        ("app", "AXFocusedWindow"),
        ("app", "AXFocusedUIElement"),
        ("app", "AXMenuBar"),
        ("window", "AXRole"),
        ("window", "AXSubrole"),
        ("window", "AXModal"),
        ("window", "AXChildren"),
        ("window", "AXSheets"),
        ("canvas", "AXRole"),
        ("canvas", "AXWindow"),
        ("canvas", "AXParent"),
        ("bar", "AXSelectedChildren"),
        ("bar", "AXChildren"),
        ("item", "AXExpanded"),
    ],
)
def test_ax_advertised_or_required_queries_fail_closed_on_unsupported(node, attribute):
    ax = FakeAX()
    ax.errors[(getattr(ax, node), attribute)] = -25205
    assert not ax_safe(ax)


def test_ax_attribute_enumeration_failure_and_excessive_ancestry_fail_closed():
    ax = FakeAX()
    ax.names_error = True
    assert not ax_safe(ax)
    ax = FakeAX()
    parent = ax.window
    for _ in range(33):
        parent = AXNode("AXGroup", AXWindow=ax.window, AXParent=parent)
    ax.canvas.attrs["AXParent"] = parent
    assert not ax_safe(ax)


@pytest.mark.parametrize(
    "signal",
    [
        "bar_selected",
        "item_selected",
        "item_expanded",
        "visible_menu",
        "menu_selected",
        "unknown_menu_visibility",
        "no_closed_state_indicators",
    ],
)
def test_ax_open_or_unverifiable_menu_rejected_without_changing_frontmost(signal):
    ax = FakeAX()
    if signal == "bar_selected":
        ax.bar.attrs["AXSelectedChildren"] = [ax.item]
    elif signal == "item_selected":
        ax.item.attrs["AXSelected"] = True
    elif signal == "item_expanded":
        ax.item.attrs["AXExpanded"] = True
    elif signal == "no_closed_state_indicators":
        del ax.bar.attrs["AXSelectedChildren"]
        del ax.item.attrs["AXSelected"]
        del ax.item.attrs["AXExpanded"]
    else:
        menu = AXNode("AXMenu", AXVisible=signal == "visible_menu", AXSelectedChildren=[])
        ax.item.attrs["AXChildren"] = [menu]
        if signal == "menu_selected":
            menu.attrs["AXSelectedChildren"] = [AXNode("AXMenuItem")]
        if signal == "unknown_menu_visibility":
            del menu.attrs["AXVisible"]
            del ax.item.attrs["AXExpanded"]
            del ax.bar.attrs["AXSelectedChildren"]
    assert not ax_safe(ax)


@pytest.mark.parametrize("change", ["focused_element", "focused_window", "open_menu"])
def test_ax_state_change_during_preflight_is_rejected(change):
    ax = FakeAX()
    read = ax.AXUIElementCopyAttributeValue
    changed = False

    def mutate(element, name, out):
        nonlocal changed
        if name == "AXParent" and not changed:
            changed = True
            if change == "focused_element":
                ax.app.attrs["AXFocusedUIElement"] = AXNode("AXTextField")
            elif change == "focused_window":
                ax.app.attrs["AXFocusedWindow"] = AXNode("AXWindow")
            else:
                ax.item.attrs["AXExpanded"] = True
        return read(element, name, out)

    ax.AXUIElementCopyAttributeValue = mutate
    assert not ax_safe(ax)
    assert changed


def test_ax_deadline_or_missing_native_api_does_not_assume_safety():
    ax = FakeAX()
    assert not JumpFocusGuard(ax, deadline=0).safe(123)
    assert not ax.calls
    ax.CFEqual = None
    assert not ax_safe(ax)


def test_ax_closed_menu_objects_require_positive_closed_state_evidence():
    ax = FakeAX()
    menu = AXNode("AXMenu", AXVisible=False)
    ax.item.attrs["AXChildren"] = [menu]
    assert ax_safe(ax)
    del menu.attrs["AXVisible"]
    assert ax_safe(ax)  # Parent explicitly reports AXExpanded=False.
    del ax.item.attrs["AXExpanded"]
    assert ax_safe(ax)  # PowerPoint's explicitly empty menu-bar selection is sufficient.
    del ax.bar.attrs["AXSelectedChildren"]
    assert not ax_safe(ax)


def test_jump_accepts_powerpoint_closed_menu_accessibility_shape():
    b = FakeBridge()
    b.ax.group.attrs.update(AXRole="AXSplitGroup")
    hosting = AXNode(
        "AXGroup", AXSubrole="AXHostingView", AXWindow=b.ax.window, AXParent=b.ax.window
    )
    b.ax.group.attrs["AXParent"] = hosting
    menu = AXNode("AXMenu")  # Real PowerPoint has no visibility/expanded attributes here.
    b.ax.item.attrs["AXChildren"] = [menu]
    del b.ax.item.attrs["AXExpanded"]
    result = invoke(b, "show_slide", slide_id=ids(b)[0], expected_slides=ids(b))
    assert result["ok"]
    assert b.posts == [(123, "1:down"), (123, "1:up"), (123, "return:down"), (123, "return:up")]


@pytest.mark.parametrize(
    "role,subrole",
    [("AXWindow", "AXHostingView"), ("AXGroup", "AXDialog"), ("AXGroup", "AXSystemDialog")],
)
def test_hosting_view_compatibility_does_not_admit_unsafe_windows_or_groups(role, subrole):
    ax = FakeAX()
    node = ax.window if role == "AXWindow" else ax.group
    node.attrs.update(AXRole=role, AXSubrole=subrole)
    assert not ax_safe(ax)


def test_jump_menu_while_same_pid_frontmost_and_show_active_has_no_input():
    b = FakeBridge()
    b.ax.bar.attrs["AXSelectedChildren"] = [b.ax.item]
    result = invoke(b, "show_slide", slide_id=ids(b)[0], expected_slides=ids(b))
    assert result == {
        "ok": False,
        "code": "unsafe_focus",
        "effects_possible": False,
        "poison": False,
    }
    assert b.keyboard.focus and b.decks[0]["pSSw"]["Actv"]
    assert not b.posts
    # View-scoped native commands do not inject keys into the menu or need AX.
    assert invoke(b, "next_slide")["ok"]


@pytest.mark.parametrize("after_event", [1, 2, 3, 4])
def test_menu_appears_between_jump_pairs_releases_up_and_aborts_before_return(after_event):
    b = FakeBridge([deck(count=12)])

    def open_menu(_event):
        if len(b.posts) == after_event:
            b.ax.item.attrs["AXExpanded"] = True

    b.post_hook = open_menu
    result = invoke(b, "show_slide", slide_id=ids(b)[11], expected_slides=ids(b))
    assert result["code"] == "unsafe_focus"
    assert result["effects_possible"] and result["poison"]
    assert b.keyboard.focus and b.decks[0]["pSSw"]["Actv"]
    assert len(b.posts) == after_event + after_event % 2
    assert b.posts[-1][1].endswith(":up")
    assert all(
        pid == 123 and "return" not in event and "escape" not in event for pid, event in b.posts
    )
    assert all(pid == 123 for pid in b.ax.created_pids)


def test_worker_jump_focus_guard_never_resolves_another_pid():
    b = n.AppleEvents.__new__(n.AppleEvents)
    ax = FakeAX()
    b.pid, b.deadline = 123, time.monotonic() + 5
    b.keyboard, b.guard = SimpleNamespace(ax=ax), Mock()
    assert not b.jump_focus_safe(999)
    assert not ax.created_pids
    assert b.jump_focus_safe(123)
    assert ax.created_pids == [123]


def test_accessibility_denied_before_jump_has_no_effects_or_prompt():
    b = FakeBridge()
    b.keyboard.allowed = False
    result = invoke(b, "show_slide", slide_id=ids(b)[0], expected_slides=ids(b))
    assert result["code"] == "permission_denied"
    assert not result["effects_possible"]
    assert not b.posts


def test_discovery_absence_is_empty_but_ambiguous_processes_are_errors():
    def missing(**_kwargs):
        raise AdapterError("target_unavailable", "not running", details={"not_running": True})

    assert n.execute({"operation": "discover"}, bridge_factory=missing) == {
        "ok": True,
        "result": [],
    }

    def ambiguous(**_kwargs):
        raise p._error("target_unavailable")

    result = n.execute({"operation": "discover"}, bridge_factory=ambiguous)
    assert result["ok"] is False
    assert result["code"] == "target_unavailable"
    assert not result["effects_possible"]


def test_raw_64_bit_descriptor_does_not_assume_nonexistent_int64value_method():
    b = n.AppleEvents.__new__(n.AppleEvents)
    descriptor = Mock(spec=["descriptorType", "data"])
    descriptor.descriptorType.return_value = n.fourcc("comp")
    descriptor.data.return_value = (2**40).to_bytes(8, n.sys.byteorder, signed=True)
    assert b.value(descriptor) == 2**40


def test_authorize_only_permission_preflight_no_deck_read_or_input():
    b = FakeBridge()
    b.read = Mock(side_effect=AssertionError("must not inspect decks"))
    result = invoke(b, "authorize")
    assert result["ok"]
    assert result["result"]["permission"] == "automation"
    assert b.permissions == [
        (suite, event, True)
        for suite, event in (
            ("core", "getd"),
            ("core", "cnte"),
            ("core", "setd"),
            ("core", "save"),
            ("sPPT", "SnxS"),
            ("sPPT", "SpvS"),
        )
    ]
    assert not b.effects and not b.sent and not b.posts


@pytest.mark.parametrize("bridge_type", [FakeBridge, SavedPathBridge])
@pytest.mark.parametrize("count,target", [(81, 81), (181, 181)])
def test_cached_jump_reads_no_hidden_or_title_metadata(bridge_type, count, target):
    b = bridge_type([deck(count=count)])
    session = n.DeckSession(b)
    value = session.discover()[0]
    slides = value["slides"]
    slides[0]["title"] = "Cached preview title"
    reads = []

    def reading(code, container):
        assert code not in ("Thid", "PHtY", "pTdT")
        if code == "SlID" and container[0:2] == ("prop", "pSLD"):
            reads.append(code)

    b.read_hook = reading
    b.decks[0]["pSSw"]["PSSv"]["pSLD"] = b.decks[0]["slides"][0]
    # Hidden edits do not alter the absolute jump number or snapshot badges.
    b.decks[0]["slides"][1]["PSsT"]["Thid"] = False
    result = session.command(
        value["handle"], "show_slide", slides[target - 1]["id"], ids(b), slides
    )
    assert result["slides"] == slides
    assert result["slides"][1]["hidden"] is True
    assert result["current_slide_id"] == ids(b)[0]  # Actual show read, not target/guess.
    assert len(reads) == 1  # Only post-dispatch position is needed.
    assert b.keyboard.created == [*str(target), "return"]
    assert len(b.focus_checks) == len(str(target)) + 2


@pytest.mark.parametrize("bridge_type", [FakeBridge, SavedPathBridge])
@pytest.mark.parametrize("operation", ["show_slide", "next_slide", "previous_slide"])
@pytest.mark.parametrize("change", ["id", "reorder", "number"])
def test_cached_command_rejects_mapping_changes_before_any_input(bridge_type, operation, change):
    b = bridge_type([deck(count=81)])
    value = n.DeckSession(b).discover()[0]
    expected = ids(b)
    if change == "id":
        b.decks[0]["slides"][0]["SlID"] = 9999
    elif change == "number":
        b.decks[0]["slides"][0]["SldN"] = 1001
    else:
        b.decks[0]["slides"].reverse()
        for index, slide in enumerate(b.decks[0]["slides"], 1):
            slide["SIdX"] = slide["SldN"] = index
    result = invoke(
        b,
        operation,
        handle=value["handle"],
        slide_id=expected[0] if operation == "show_slide" else None,
        expected_slides=expected,
        snapshot_slides=value["slides"],
    )
    assert result["code"] == "stale_snapshot"
    assert not result["effects_possible"]
    assert not b.posts and not b.sent


@pytest.mark.parametrize("bridge_type", [FakeBridge, SavedPathBridge])
@pytest.mark.parametrize("operation", ["show_slide", "next_slide", "previous_slide"])
def test_cached_navigation_reads_snapshot_only_after_dispatch(bridge_type, operation):
    b = bridge_type()
    session = n.DeckSession(b)
    value = session.discover()[0]
    session.snapshot = Mock(wraps=session.snapshot)
    session.navigation_manifest = Mock(wraps=session.navigation_manifest)
    snapshot_effects = []

    def snapshot(*args, **kwargs):
        snapshot_effects.append(b.effects)
        return n.DeckSession.snapshot(session, *args, **kwargs)

    session.snapshot.side_effect = snapshot
    result = session.command(
        value["handle"],
        operation,
        value["slides"][0]["id"] if operation == "show_slide" else None,
        ids(b),
        value["slides"],
    )
    assert result["effect_confirmation"] == "dispatch_only"
    assert snapshot_effects == [True]
    assert session.snapshot.call_count == 1
    # One full command guard, then the final snapshot; no mapping reads per key.
    assert session.navigation_manifest.call_count == 2


@pytest.mark.parametrize(
    "failure", ["path", "pid", "lifetime", "show", "focus", "permission", "ax"]
)
def test_cached_jump_freshly_checks_safety_without_observe(failure):
    b = FakeBridge()
    value = n.DeckSession(b).discover()[0]
    if failure == "path":
        b.decks[0]["fNam"] = "/PRIVATE/Other.pptx"
    elif failure == "pid":
        b.pid += 1
    elif failure == "lifetime":
        b.lifetime = "new"
    elif failure == "show":
        b.decks[0]["pSSw"] = None
    elif failure == "focus":
        b.keyboard.focus = False
    elif failure == "permission":
        b.keyboard.allowed = False
    else:
        b.ax.canvas.attrs["AXRole"] = "AXTextField"
    response = n.execute(
        {
            "operation": "show_slide",
            "handle": value["handle"],
            "slide_id": value["slides"][0]["id"],
            "expected_slides": [s["id"] for s in value["slides"]],
            "snapshot_slides": value["slides"],
        },
        bridge_factory=lambda **_kwargs: b,
    )
    assert not response["ok"] and not response["effects_possible"]
    assert not b.posts and not b.sent


@pytest.mark.parametrize("bridge_type", [FakeBridge, SavedPathBridge])
def test_public_warm_discovery_reuses_metadata_but_observe_updates_hidden_edits(bridge_type):
    b = bridge_type([deck(count=81)])
    adapter = p.NativePowerPoint()
    requests, hidden_reads = [], []

    def run(_helper, request, _timeout, _effects):
        requests.append(copy.deepcopy(request))
        result = n.execute(request, bridge_factory=lambda **_kwargs: b)
        assert result["ok"]
        return result["result"]

    b.read_hook = lambda code, _container: hidden_reads.append(code) if code == "Thid" else None
    with patch.object(adapter, "_run", side_effect=run):
        first = asyncio.run(adapter.discover())[0]
        assert hidden_reads
        hidden_reads.clear()
        second = asyncio.run(adapter.discover())[0]
        assert not hidden_reads
        assert first == second
        assert "cached_decks" not in requests[0]
        assert requests[1]["cached_decks"][0]["handle"] == first["handle"]
        b.decks[0]["slides"][1]["PSsT"]["Thid"] = False
        assert asyncio.run(adapter.discover())[0]["slides"][1]["hidden"] is True
        observed = asyncio.run(adapter.observe(first["handle"]))
        assert hidden_reads and observed["slides"][1]["hidden"] is False
        hidden_reads.clear()
        assert asyncio.run(adapter.discover())[0]["slides"] == observed["slides"]
        assert not hidden_reads
    assert not b.effects and not b.posts and not b.sent and not b.permissions


def test_refresh_full_reads_hidden_edits_and_replaces_cached_flags(tmp_path):
    b = edit_bridge()
    adapter = p.NativePowerPoint(export_directory=tmp_path / "previews")
    requests, hidden_reads = [], []
    b.read_hook = lambda code, _container: hidden_reads.append(code) if code == "Thid" else None

    def run(helper, request, _timeout, _effects):
        requests.append(request)
        if helper == "render":
            return [{"page": i, "image": base64.b64encode(PNG).decode()} for i in range(1, 4)]
        result = n.execute(request, bridge_factory=lambda **_kwargs: b)
        assert result["ok"]
        return result["result"]

    with patch.object(adapter, "_run", side_effect=run):
        original = asyncio.run(adapter.discover())[0]
        b.decks[0]["slides"][1]["PSsT"]["Thid"] = False
        hidden_reads.clear()
        refreshed = asyncio.run(adapter.refresh(original["handle"]))
        assert hidden_reads
        assert refreshed["slides"][1]["hidden"] is False
        assert adapter._cached_decks()[0]["slides"] == refreshed["slides"]
        assert all(
            "cached_decks" not in req for req in requests if req.get("operation") == "export"
        )
        hidden_reads.clear()
        assert asyncio.run(adapter.discover())[0]["slides"] == refreshed["slides"]
        assert not hidden_reads


@pytest.mark.parametrize("bridge_type", [FakeBridge, SavedPathBridge])
def test_cached_numbering_change_during_jump_is_rejected_after_all_pairs(bridge_type):
    b = bridge_type([deck(count=181)])
    value = n.DeckSession(b).discover()[0]

    def changed(_event):
        if len(b.posts) == 2:
            b.decks[0]["slides"][0]["SldN"] = 1001

    b.post_hook = changed
    result = invoke(
        b,
        "show_slide",
        handle=value["handle"],
        slide_id=ids(b)[-1],
        expected_slides=ids(b),
        snapshot_slides=value["slides"],
    )
    assert result["code"] == "stale_snapshot"
    assert result["effects_possible"] and result["poison"]
    assert b.posts == [
        (123, f"{key}:{state}") for key in ["1", "8", "1", "return"] for state in ("down", "up")
    ]


@pytest.mark.parametrize("change", ["id", "reorder", "number"])
def test_discovery_invalidates_cached_mapping_on_fresh_bulk_change(change):
    b = FakeBridge()
    session = n.DeckSession(b)
    value = session.discover()[0]
    if change == "id":
        b.decks[0]["slides"][0]["SlID"] = 9999
    elif change == "number":
        b.decks[0]["slides"][0]["SldN"] = 1001
    else:
        b.decks[0]["slides"].reverse()
        for index, slide in enumerate(b.decks[0]["slides"], 1):
            slide["SIdX"] = slide["SldN"] = index
    session.manifest = Mock(wraps=session.manifest)
    result = session.discover([{"handle": value["handle"], "slides": value["slides"]}])[0]
    assert session.manifest.call_count
    assert result["slides"] != value["slides"]


@pytest.mark.parametrize("field", ["pid", "lifetime", "fullname", "window_id", "identity_kind"])
def test_discovery_never_reuses_metadata_from_another_private_identity(field):
    b = FakeBridge()
    session = n.DeckSession(b)
    value = session.discover()[0]
    cached_handle = {**value["handle"], field: "foreign"}
    slides = copy.deepcopy(value["slides"])
    slides[0]["title"] = "Foreign cached title"
    session.manifest = Mock(wraps=session.manifest)
    result = session.discover([{"handle": cached_handle, "slides": slides}])[0]
    assert session.manifest.call_count
    assert result["slides"][0]["title"] != slides[0]["title"]


def test_metadata_cache_bounds_compact_payload_and_skips_oversized_entry():
    adapter = p.NativePowerPoint()
    b = FakeBridge([deck(count=500)])
    slides = n.DeckSession(b).discover()[0]["slides"]
    for index in range(p.MAX_DECKS + 3):
        adapter._remember({**handle(b), "window_id": index}, slides)
    assert len(adapter._metadata_cache) == p.MAX_DECKS
    assert adapter._cache_key({**handle(b), "window_id": 0}) not in adapter._metadata_cache
    oversized = [{**slide, "title": "\U0001f600" * 256} for slide in slides]
    huge_handle = {**handle(b), "window_id": 999}
    adapter._remember(huge_handle, oversized)
    payload = adapter._cached_decks()
    assert payload  # Oversized newest entry does not exclude smaller entries.
    assert all(entry["handle"] != huge_handle for entry in payload)
    assert len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()) <= 65536
    assert all(len(entry["slides"]) <= p.MAX_SLIDES for entry in payload)
    assert len(payload) <= p.MAX_DECKS


class PublicAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="conty-test-powerpoint-")
        self.addCleanup(temporary.cleanup)
        self.export_directory = Path(temporary.name) / "PowerPoint Previews"
        self.adapter = p.NativePowerPoint(export_directory=self.export_directory)
        self.b = FakeBridge()
        self.h = handle(self.b)
        self.state = p._state(n.DeckSession(self.b).snapshot(self.h))

    async def asyncTearDown(self):
        await self.adapter.close()

    async def test_metadata_only_observe_and_command_confirmation(self):
        with patch.object(self.adapter, "_run", return_value={**self.state, "fullname": "SECRET"}):
            value = await self.adapter.observe(self.h)
            self.assertEqual(value, self.state)
            result = await self.adapter.command(self.h, "next_slide")
            self.assertEqual(result, {**self.state, "effect_confirmation": "dispatch_only"})

    async def test_saved_path_dispatches_refresh_and_control_to_supervised_worker(self):
        h = saved_handle(SavedPathBridge())
        with patch.object(self.adapter, "_refresh", return_value=self.state) as refresh:
            self.assertEqual(await self.adapter.refresh(h), self.state)
            refresh.assert_called_once_with(h)
        with patch.object(self.adapter, "_command", return_value=self.state) as command:
            self.assertEqual(await self.adapter.command(h, "next_slide"), self.state)
            command.assert_called_once_with(h, "next_slide", None, None, None)
        self.assertFalse(self.adapter._poisoned)

    async def test_command_carries_full_metadata_and_legacy_ids_remain_supported(self):
        with patch.object(self.adapter, "_run", return_value=self.state) as run:
            await self.adapter.command(self.h, "show_slide", "107", self.state["slides"])
            payload = run.call_args.args[1]
            self.assertEqual(payload["snapshot_slides"], self.state["slides"])
            self.assertEqual(payload["expected_slides"], ids(self.b))
            await self.adapter.command(self.h, "show_slide", "107", ids(self.b))
            self.assertNotIn("snapshot_slides", run.call_args.args[1])

    async def test_validation_before_worker(self):
        with patch.object(self.adapter, "_run") as run:
            cases = [
                ({}, "bad", None, None),
                (self.h, "show_slide", "107", None),
                (self.h, "show_slide", "missing", ids(self.b)),
                (self.h, "show_slide", "107", ["107", "107"]),
                (self.h, "show_slide", "107", [["107"]]),
                (self.h, "next_slide", "107", None),
            ]
            for h, operation, slide, expected in cases:
                with self.assertRaises(AdapterError):
                    await self.adapter.command(h, operation, slide, expected)
            run.assert_not_called()

    async def test_refresh_private_stable_folder_0700_explicit_mapping_bytes_cleanup(self):
        paths = []

        def run(helper, request, timeout, effects):
            if request.get("operation") == "export":
                path = Path(request["pdf"])
                paths.append(path)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(timeout, p.EXPORT_TIMEOUT)
                path.write_bytes(b"%PDF-FAKE")
                return self.state
            if helper == "render":
                self.assertEqual(timeout, p.RENDER_TIMEOUT)
                return [{"page": i, "image": base64.b64encode(PNG).decode()} for i in range(1, 4)]
            return self.state

        with patch.object(self.adapter, "_run", side_effect=run):
            value = await self.adapter.refresh(self.h)
        self.assertEqual(value["slides"], self.state["slides"])
        self.assertEqual(self.adapter._cached_decks()[0]["slides"], self.state["slides"])
        self.assertEqual(
            value["previews"],
            [
                {"slide_id": slide["id"], "page": i, "image": PNG}
                for i, slide in enumerate(self.state["slides"], 1)
            ],
        )
        self.assertTrue(paths[0].parent.is_dir())
        self.assertFalse(paths[0].exists())
        self.assertEqual(paths[0], self.export_directory / "preview.pdf")
        self.assertNotIn("fullname", value)

    async def test_refresh_failure_cleans_pdf_but_preserves_granted_folder(self):
        paths = []

        def run(helper, request, timeout, effects):
            if helper == "native":
                path = Path(request["pdf"])
                paths.append(path)
                path.write_bytes(b"%PDF-FAKE")
                return self.state
            raise p._error("export_failed")

        with patch.object(self.adapter, "_run", side_effect=run):
            with self.assertRaises(AdapterError):
                await self.adapter.refresh(self.h)
        self.assertTrue(paths[0].parent.is_dir())
        self.assertFalse(paths[0].exists())

    async def test_refresh_rejects_wrong_count_page_mapping_and_bad_image(self):
        for pages in (
            [],
            [{"page": 2, "image": ""}] * 3,
            [{"page": i, "image": "not-base64"} for i in range(1, 4)],
            [{"page": i, "image": base64.b64encode(b"notpng").decode()} for i in range(1, 4)],
        ):

            def run(helper, request, timeout, effects):
                if request.get("operation") == "export":
                    Path(request["pdf"]).write_bytes(b"%PDF-FAKE")
                    return self.state
                return pages

            with patch.object(self.adapter, "_run", side_effect=run):
                with self.assertRaises(AdapterError):
                    await self.adapter.refresh(self.h)

    async def test_refresh_detects_deck_changes_during_render(self):
        changed = copy.deepcopy(self.state)
        changed["slides"].reverse()

        def run(helper, request, timeout, effects):
            if request.get("operation") == "export":
                Path(request["pdf"]).write_bytes(b"%PDF-FAKE")
                return self.state
            if helper == "render":
                return [{"page": i, "image": base64.b64encode(PNG).decode()} for i in range(1, 4)]
            return changed

        with patch.object(self.adapter, "_run", side_effect=run):
            with self.assertRaises(AdapterError) as caught:
                await self.adapter.refresh(self.h)
        self.assertEqual(caught.exception.code, "stale_snapshot")

    async def test_pdf_image_and_deck_byte_limits(self):
        for limit_name, limit in (("MAX_PDF", 4), ("MAX_IMAGE", 4), ("MAX_PREVIEWS", 20)):

            def run(helper, request, timeout, effects):
                if request.get("operation") == "export":
                    Path(request["pdf"]).write_bytes(b"%PDF-FAKE")
                    return self.state
                return [{"page": i, "image": base64.b64encode(PNG).decode()} for i in range(1, 4)]

            with (
                patch.object(p, limit_name, limit),
                patch.object(self.adapter, "_run", side_effect=run),
            ):
                with self.assertRaises(AdapterError):
                    await self.adapter.refresh(self.h)

    async def test_serialization_including_whole_jump(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def run(_helper, request, _timeout, _effects):
            calls.append(request["operation"])
            if len(calls) == 1:
                entered.set()
                release.wait(1)
            return self.state

        with patch.object(self.adapter, "_run", side_effect=run):
            first = asyncio.create_task(
                self.adapter.command(self.h, "show_slide", "107", ids(self.b))
            )
            await asyncio.to_thread(entered.wait, 1)
            second = asyncio.create_task(self.adapter.observe(self.h))
            await asyncio.sleep(0.02)
            self.assertEqual(calls, ["show_slide"])
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(calls, ["show_slide", "observe"])

    async def test_cancel_waits_for_worker_cleanup_before_unlock(self):
        entered, cleaned = threading.Event(), threading.Event()

        def run(*_args):
            entered.set()
            self.adapter._cancel.wait(1)
            time.sleep(0.02)
            cleaned.set()
            raise p._error("executor_unavailable", effects=True)

        with patch.object(self.adapter, "_run", side_effect=run):
            task = asyncio.create_task(self.adapter.command(self.h, "next_slide"))
            await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cleaned.is_set())
            with self.assertRaises(AdapterError):
                await self.adapter.command(self.h, "next_slide")

    async def test_close_with_fake_process_kills_and_joins_actual_worker_loop(self):
        process = FakeProcess(hanging=True)
        with (
            patch.object(p.sys, "platform", "darwin"),
            patch.object(p.subprocess, "Popen", return_value=process) as spawn,
            patch.object(p.os, "killpg") as kill,
        ):
            task = asyncio.create_task(self.adapter.command(self.h, "next_slide"))
            for _ in range(100):
                if spawn.called:
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(spawn.called)
            await self.adapter.close()
            with self.assertRaises(AdapterError):
                await task
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            self.assertEqual(kill.call_args_list[-1].args, (987654, signal.SIGKILL))
            self.assertTrue(self.adapter._poisoned)

    async def test_close_signals_running_worker_and_refuses_future_work(self):
        entered = threading.Event()

        def run(*_args):
            entered.set()
            self.adapter._cancel.wait(1)
            raise p._error("executor_unavailable")

        with patch.object(self.adapter, "_run", side_effect=run):
            task = asyncio.create_task(self.adapter.observe(self.h))
            await asyncio.to_thread(entered.wait, 1)
            await self.adapter.close()
            with self.assertRaises(AdapterError):
                await task
            with self.assertRaises(AdapterError):
                await self.adapter.observe(self.h)


class FakeProcess:
    """Real local pipes, but no child process, Apple Events, or framework calls."""

    def __init__(self, payload=b"", *, hanging=False):
        self.pid = 987654
        self.returncode = None if hanging else 0
        self.hanging = hanging
        self.waits = []
        self.stdout = self.pipe(payload)
        self.stderr = self.pipe(b"PRIVATE raw diagnostics")

    @staticmethod
    def pipe(payload):
        read, write = os.pipe()
        os.write(write, payload)
        os.close(write)
        return os.fdopen(read, "rb", buffering=0)

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.hanging and len(self.waits) == 1:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = 0
        return 0


def envelope(result):
    return json.dumps({"ok": True, "result": result}).encode()


def fake_run_process(adapter, process, timeout=0.2, effects=False):
    with (
        patch.object(p.sys, "platform", "darwin"),
        patch.object(p.subprocess, "Popen", return_value=process),
        patch.object(p.os, "killpg"),
    ):
        return adapter._run("native", {"operation": "observe"}, timeout, effects)


def test_runner_fixed_module_and_json_argv_no_shell_interpolation():
    adapter = p.NativePowerPoint()
    process = FakeProcess(envelope([]))
    request = {"operation": "observe", "handle": {"fullname": '"; activate; /PRIVATE/\n'}}
    with (
        patch.object(p.sys, "platform", "darwin"),
        patch.object(p.subprocess, "Popen", return_value=process) as spawn,
    ):
        assert adapter._run("native", request, 1, False) == []
    argv = spawn.call_args.args[0]
    assert argv[:3] == [p.sys.executable, "-m", "conty.adapters.macos.powerpoint_scripts.native"]
    assert json.loads(argv[3]) == request
    assert "shell" not in spawn.call_args.kwargs
    assert spawn.call_args.kwargs["start_new_session"] is True
    assert spawn.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert "PRIVATE" not in argv[2]


@pytest.mark.parametrize("payload", [b"garbage /PRIVATE", b"[]", b'{"ok":true}', b'{"ok":truee}'])
def test_runner_redacts_unexpected_response_and_poisons_unknown_command_effects(payload):
    adapter = p.NativePowerPoint()
    error = assert_code(
        "execution_failed",
        lambda: fake_run_process(adapter, FakeProcess(payload), effects=True),
        effects=True,
    )
    assert "PRIVATE" not in str(error)
    assert adapter._poisoned


def test_runner_whitelisted_permission_error_does_not_poison_before_effects():
    adapter = p.NativePowerPoint()
    process = FakeProcess(
        json.dumps(
            {
                "ok": False,
                "code": "permission_denied",
                "message": "/PRIVATE",
                "effects_possible": False,
            }
        ).encode()
    )
    error = assert_code("permission_denied", lambda: fake_run_process(adapter, process))
    assert "authorize" in str(error)
    assert "PRIVATE" not in str(error)
    assert not adapter._poisoned


def test_runner_unknown_error_code_is_redacted():
    adapter = p.NativePowerPoint()
    process = FakeProcess(b'{"ok":false,"code":"/PRIVATE/file.pptx"}')
    error = assert_code("execution_failed", lambda: fake_run_process(adapter, process))
    assert "PRIVATE" not in str(error)


def test_runner_timeout_kills_group_reaps_and_poisons_even_read_hang():
    adapter = p.NativePowerPoint()
    process = FakeProcess(hanging=True)
    with (
        patch.object(p.sys, "platform", "darwin"),
        patch.object(p.subprocess, "Popen", return_value=process),
        patch.object(p.os, "killpg") as kill,
    ):
        assert_code("timed_out", lambda: adapter._run("native", {}, 0.01, False))
    assert kill.call_args_list[0].args == (987654, signal.SIGTERM)
    assert kill.call_args_list[1].args == (987654, signal.SIGKILL)
    assert process.waits == [0.2, 1]
    assert adapter._poisoned
    assert process.stdout.closed and process.stderr.closed


def test_runner_output_capture_bounded_and_cleaned():
    adapter = p.NativePowerPoint()
    with patch.object(p, "OUTPUT_LIMIT", 16):
        assert_code("limit_exceeded", lambda: fake_run_process(adapter, FakeProcess(b"x" * 100)))


def test_runner_unconfirmed_cleanup_is_poisoned():
    adapter = p.NativePowerPoint()
    process = FakeProcess(hanging=True)
    process.wait = Mock(side_effect=subprocess.TimeoutExpired("fake", 1))
    assert_code("executor_unavailable", lambda: fake_run_process(adapter, process, timeout=0.01))
    assert adapter._poisoned


def test_runner_reaps_even_if_process_group_already_exited():
    adapter = p.NativePowerPoint()
    process = FakeProcess(b"not-json")
    with (
        patch.object(p.sys, "platform", "darwin"),
        patch.object(p.subprocess, "Popen", return_value=process),
        patch.object(p.os, "killpg", side_effect=ProcessLookupError),
    ):
        assert_code("execution_failed", lambda: adapter._run("native", {}, 1, False))
    assert process.waits
    assert process.stdout.closed


def test_runner_spawn_error_is_redacted_and_effect_free():
    adapter = p.NativePowerPoint()
    with (
        patch.object(p.sys, "platform", "darwin"),
        patch.object(p.subprocess, "Popen", side_effect=OSError("/PRIVATE")),
    ):
        error = assert_code(
            "capability_unavailable", lambda: adapter._run("native", {}, 1, True), effects=False
        )
    assert "PRIVATE" not in str(error)
    assert not adapter._poisoned


def test_runner_no_spawn_on_non_macos_or_prior_cancel():
    adapter = p.NativePowerPoint()
    with patch.object(p.sys, "platform", "linux"), patch.object(p.subprocess, "Popen") as spawn:
        assert_code("capability_unavailable", lambda: adapter._run("native", {}, 1, False))
        spawn.assert_not_called()
    adapter._cancel.set()
    with patch.object(p.subprocess, "Popen") as spawn:
        assert_code("executor_unavailable", lambda: adapter._run("native", {}, 1, False))
        spawn.assert_not_called()


@pytest.mark.parametrize("native_bytes", [b" lla", b"all "])
def test_all_selector_preserves_cocoa_enum_data_with_absolute_ordinal_type(native_bytes):
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.D, b.specifier = Mock(), Mock(return_value="all slides")
    enum = b.D.descriptorWithEnumCode_.return_value
    enum.data.return_value = native_bytes
    assert b.all("pSLD", "presentation") == "all slides"
    constructor = b.D.descriptorWithDescriptorType_bytes_length_
    constructor.assert_called_once_with(n.fourcc("abso"), native_bytes, 4)
    b.specifier.assert_called_once_with("pSLD", "indx", constructor.return_value, "presentation")
    b.D.descriptorWithEnumCode_.assert_called_once_with(n.fourcc("all "))
    enum.coerceToDescriptorType_.assert_not_called()


@pytest.mark.parametrize("container", [None, "presentation"])
def test_count_sends_container_with_type_code_kocl_not_all_specifier(container):
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.D, b.root = Mock(), object()
    b.send, b.value, b.all = Mock(), Mock(return_value=1), Mock()
    assert b.count("pDWn", container) == 1
    b.D.descriptorWithTypeCode_.assert_called_once_with(n.fourcc("pDWn"))
    b.send.assert_called_once_with(
        "core",
        "cnte",
        b.root if container is None else container,
        {"kocl": b.D.descriptorWithTypeCode_.return_value},
    )
    b.all.assert_not_called()


def test_real_cocoa_all_and_count_descriptors_without_sending():
    apps = pytest.importorskip("AppKit")
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.D = apps.NSAppleEventDescriptor
    b.root = b.D.nullDescriptor()
    selected = b.all("pDWn").descriptorForKeyword_(n.fourcc("seld"))
    assert selected.descriptorType() == n.fourcc("abso")
    assert bytes(selected.data()) == bytes(b.enum(n.fourcc("all ")).data())
    b.send = Mock(return_value=b.D.descriptorWithInt32_(1))
    for container in (None, b.at("pptP", 1)):
        assert b.count("pDWn", container) == 1
        suite, event, direct, params = b.send.call_args.args
        assert (suite, event) == ("core", "cnte")
        assert direct == (b.root if container is None else container)
        assert params["kocl"].descriptorType() == n.fourcc("type")
        assert params["kocl"].typeCodeValue() == n.fourcc("pDWn")


class RawDescriptor:
    def __init__(self, kind, value=None, *, fields=None, items=()):
        self.kind, self.payload, self.fields, self.items = kind, value, fields or {}, items

    def descriptorType(self):
        return n.fourcc(self.kind)

    def descriptorForKeyword_(self, key):
        return self.fields.get(key)

    def typeCodeValue(self):
        return n.fourcc(self.payload)

    def enumCodeValue(self):
        return n.fourcc(self.payload)

    def int32Value(self):
        return self.payload

    def stringValue(self):
        return self.payload

    def numberOfItems(self):
        return len(self.items)

    def descriptorAtIndex_(self, index):
        return self.items[index - 1]


def raw_reference(cls, form, key, *, container=None):
    return RawDescriptor(
        "obj ",
        fields={
            n.fourcc("want"): RawDescriptor("type", cls),
            n.fourcc("form"): RawDescriptor("enum", form),
            n.fourcc("seld"): key,
            n.fourcc("from"): container or RawDescriptor("null"),
        },
    )


def raw_id_bridge(form="indx", key=None):
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.count = Mock(return_value=1)
    b.all, b.prop = Mock(), Mock()
    ref = raw_reference("pDWn", form, key or RawDescriptor("long", 1))
    b.get = Mock(
        side_effect=[
            RawDescriptor("list", items=[ref]),
            RawDescriptor("list", items=[RawDescriptor("type", "msng")]),
        ]
    )
    return b


def test_fullpath_inventory_is_bulk_and_preserves_missing_entries_as_nonidentities():
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.count = Mock(return_value=3)
    b.all = Mock(return_value="all presentations")
    b.read = Mock(return_value=["/PRIVATE/Deck.pptx", n.fourcc("msng"), "Presentation1"])
    assert b.presentation_paths() == ["/PRIVATE/Deck.pptx", None, "Presentation1"]
    b.read.assert_called_once_with("fNam", "all presentations")
    b.read.return_value = ["/PRIVATE/Deck.pptx"]
    assert_code("target_unavailable", b.presentation_paths)


def test_runtime_index_window_reference_and_missing_id_are_explicitly_unsupported():
    b = raw_id_bridge()
    assert_code("unsupported_identity", lambda: b.ids("pDWn"))
    assert b.get.call_count == 2


def test_runtime_unique_id_reference_needs_no_inherited_id_property():
    b = raw_id_bridge("ID  ", RawDescriptor("long", 4321))
    assert b.ids("pDWn") == [4321]
    assert b.get.call_count == 1
    b.prop.assert_not_called()


def test_reference_index_and_nonroot_name_are_not_stable_identity():
    b = raw_id_bridge()
    assert (
        b.reference_key(raw_reference("pDWn", "indx", RawDescriptor("long", 1)), "pDWn", "ID  ")
        is None
    )
    ref = raw_reference(
        "pptP", "name", RawDescriptor("utxt", "PRIVATE"), container=RawDescriptor("obj ")
    )
    assert b.reference_key(ref, "pptP", "name") is None
    ref = raw_reference("pptP", "name", RawDescriptor("utxt", "PRIVATE"))
    assert b.reference_key(ref, "pptP", "name") == "PRIVATE"


def test_runtime_unsupported_id_property_error_allows_only_explicit_identity_fallback():
    b = raw_id_bridge()
    error = p._error("target_unavailable")
    error.details["native_code"] = -1728
    first_reply = next(b.get.side_effect)
    b.get.side_effect = [first_reply, error]
    assert_code("unsupported_identity", lambda: b.ids("pDWn"))
    b = raw_id_bridge()
    b.get.side_effect = p._error("permission_denied")
    assert_code("permission_denied", lambda: b.ids("pDWn"))


def test_raw_ae_send_checks_permission_and_uses_pid_descriptor_no_prompt_flags():
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.D = Mock()
    b.target = object()
    b.deadline = time.monotonic() + 10
    b.effects, b.unknown = False, False
    b.permission = Mock()
    b.guard = Mock()
    reply = Mock()
    reply.paramDescriptorForKeyword_.return_value = None
    event = (
        b.D.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_.return_value
    )
    event.sendEventWithOptions_timeout_error_.return_value = reply, None
    b.send("sPPT", "SnxS", "selected-view", mutation=True)
    b.permission.assert_called_once_with("sPPT", "SnxS")
    args = (
        b.D.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_.call_args.args
    )
    assert args == (n.fourcc("sPPT"), n.fourcc("SnxS"), b.target, -1, 0)
    assert event.sendEventWithOptions_timeout_error_.call_args.args[0] == 0x00080013
    event.setParamDescriptor_forKeyword_.assert_called_once_with("selected-view", n.fourcc("----"))
    assert b.effects


def test_raw_permission_defaults_to_no_prompt_exact_c_pid_descriptor_and_disposes():
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.pid = 123
    b.guard = Mock()
    b.carbon = Mock()
    b.carbon.AECreateDesc.return_value = 0
    b.carbon.AEDeterminePermissionToAutomateTarget.return_value = 0
    b.permission("core", "getd")
    args = b.carbon.AECreateDesc.call_args.args
    assert args[0] == n.fourcc("kpid")
    assert args[1]._obj.value == 123
    assert args[2] == 4
    assert b.carbon.AEDeterminePermissionToAutomateTarget.call_args.args[-1] is False
    b.carbon.AEDisposeDesc.assert_called_once()


def test_raw_guard_rejects_same_pid_different_lifetime():
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.pid, b.lifetime = 123, "1000.000000000"
    b.deadline = time.monotonic() + 1
    b.app = object()
    b.keyboard = Mock()
    b.keyboard.is_running.return_value = True
    b.keyboard.pid.return_value = 123
    current = Mock()
    current.isTerminated.return_value = False
    current.bundleIdentifier.return_value = n.BUNDLE
    current.launchDate.return_value.timeIntervalSince1970.return_value = 2000
    b.apps = Mock()
    b.apps.NSRunningApplication.runningApplicationWithProcessIdentifier_.return_value = current
    assert_code("target_unavailable", b.guard)
    b.apps.NSRunningApplication.runningApplicationWithProcessIdentifier_.assert_called_once_with(
        123
    )


class FakeQuartz:
    kCGPDFMediaBox = 0
    kCGImageAlphaNoneSkipLast = 5

    def __init__(self, pages=3, png=PNG):
        self.pages = pages
        self.png = png
        self.requested, self.sizes = [], []
        self.output = None

    def CGPDFDocumentCreateWithURL(self, url):
        return "pdf"

    def CGPDFDocumentIsEncrypted(self, doc):
        return False

    def CGPDFDocumentGetNumberOfPages(self, doc):
        return self.pages

    def CGPDFDocumentGetPage(self, doc, index):
        self.requested.append(index)
        return index

    def CGPDFPageGetBoxRect(self, page, box):
        return SimpleNamespace(size=SimpleNamespace(width=960, height=540))

    def CGPDFPageGetRotationAngle(self, page):
        return 0

    def CGBitmapContextCreate(self, _, w, h, *_args):
        self.sizes.append((w, h))
        return "context"

    def CGColorSpaceCreateDeviceRGB(self):
        return "rgb"

    def CGRectMake(self, *args):
        return args

    def CGContextSetRGBFillColor(self, *_args):
        pass

    def CGContextFillRect(self, *_args):
        pass

    def CGPDFPageGetDrawingTransform(self, *_args):
        return "transform"

    def CGContextConcatCTM(self, *_args):
        pass

    def CGContextDrawPDFPage(self, *_args):
        pass

    def CGBitmapContextCreateImage(self, *_args):
        return "image"

    def CGImageDestinationCreateWithData(self, data, kind, count, options):
        assert kind == "public.png" and count == 1 and options is None
        self.output = data
        return "destination"

    def CGImageDestinationAddImage(self, *_args):
        pass

    def CGImageDestinationFinalize(self, *_args):
        self.output.extend(self.png)
        return True


def fake_foundation():
    return SimpleNamespace(
        NSURL=SimpleNamespace(fileURLWithPath_=lambda p: p),
        NSMutableData=SimpleNamespace(data=bytearray),
    )


def test_renderer_page_count_and_one_based_mapping(tmp_path):
    pdf = tmp_path / "preview.pdf"
    pdf.write_bytes(b"%PDF-FAKE")
    q = FakeQuartz()
    result = r.render(pdf, 3, quartz=q, foundation=fake_foundation())
    assert q.requested == [1, 2, 3]
    assert [page["page"] for page in result] == [1, 2, 3]
    assert all(base64.b64decode(page["image"]) == PNG for page in result)
    assert all(max(size) <= 1024 for size in q.sizes)
    q.pages = 2
    assert_code("export_failed", lambda: r.render(pdf, 3, quartz=q, foundation=fake_foundation()))


def test_renderer_downsizes_to_per_page_and_total_budget(tmp_path):
    pdf = tmp_path / "preview.pdf"
    pdf.write_bytes(b"%PDF-FAKE")
    q = FakeQuartz(png=PNG * 5)
    original = q.CGImageDestinationFinalize

    def finalize(*args):
        q.png = PNG if q.sizes[-1][0] <= 768 else PNG * 5
        return original(*args)

    q.CGImageDestinationFinalize = finalize
    with patch.object(r, "MAX_PREVIEWS", len(PNG) * 3), patch.object(r, "MAX_IMAGE", len(PNG) * 2):
        result = r.render(pdf, 3, quartz=q, foundation=fake_foundation())
    assert q.sizes[:2] == [(1024, 576), (768, 432)]
    assert sum(len(base64.b64decode(page["image"])) for page in result) == len(PNG) * 3


def test_renderer_rejects_unrenderable_page_limits_and_rotation(tmp_path):
    pdf = tmp_path / "preview.pdf"
    pdf.write_bytes(b"%PDF-FAKE")
    q = FakeQuartz()
    for count in (0, 501, True):
        assert_code(
            "limit_exceeded", lambda: r.render(pdf, count, quartz=q, foundation=fake_foundation())
        )
    q.CGPDFPageGetRotationAngle = lambda _page: 1
    assert_code("export_failed", lambda: r.render(pdf, 3, quartz=q, foundation=fake_foundation()))


def test_renderer_refuses_encryption_and_oversized_pdf_before_drawing(tmp_path):
    pdf = tmp_path / "preview.pdf"
    pdf.write_bytes(b"%PDF-FAKE")
    q = FakeQuartz()
    q.CGPDFDocumentIsEncrypted = lambda _document: True
    assert_code("export_failed", lambda: r.render(pdf, 3, quartz=q, foundation=fake_foundation()))
    assert not q.requested
    q.CGPDFDocumentIsEncrypted = lambda _document: False
    with patch.object(r, "MAX_PDF", 4):
        assert_code(
            "export_failed", lambda: r.render(pdf, 3, quartz=q, foundation=fake_foundation())
        )
    assert not q.requested


def test_renderer_cannot_exceed_png_limit_at_minimum_resolution(tmp_path):
    pdf = tmp_path / "preview.pdf"
    pdf.write_bytes(b"%PDF-FAKE")
    q = FakeQuartz(png=PNG * 4)
    with patch.object(r, "MAX_IMAGE", len(PNG)):
        assert_code(
            "limit_exceeded", lambda: r.render(pdf, 3, quartz=q, foundation=fake_foundation())
        )
    assert q.sizes[-1][0] == 64
    assert len(q.sizes) < 20


def test_resource_sources_have_no_launch_activation_global_input_or_extra_pdf_dependency():
    # Inspect source only: importing these modules does not import PyObjC or
    # instantiate a bridge. Guard against accidental unsafe future shortcuts.
    import inspect

    source = inspect.getsource(n) + inspect.getsource(r)
    for forbidden in ("ScriptingBridge", "PDFKit", "System Events"):
        assert f"import {forbidden}" not in source
    assert ".activate(" not in source
    assert ".activateWithOptions_" not in source
    assert "CGEventPost(" not in source
    assert 'send("core", "clos"' not in source
    assert 'send("aevt", "odoc"' not in source
    assert 'read("ScSp"' not in source
    assert 'prop("AAPr"' not in source
