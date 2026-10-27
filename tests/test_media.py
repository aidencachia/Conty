"""Fake-only generic media tests; no keys, player launches or native writes."""

import math
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from conty.adapters.base import AdapterError
from conty.adapters.macos import media


@pytest.fixture
def native():
    bridge = Mock(spec=["trusted", "events", "post"])
    bridge.trusted.return_value = True
    bridge.events.return_value = ("down", "up")
    return bridge


@pytest.mark.parametrize("command", ["previous_track", "play_pause", "next_track"])
def test_generic_commands_post_a_pair_without_app_targeting(native, command):
    result = media.MediaAdapter(native).execute(command)
    native.events.assert_called_once_with(command)
    assert [call.args for call in native.post.call_args_list] == [("down",), ("up",)]
    assert native.trusted.call_count == 2
    assert result.details == {
        "target": "system",
        "command": command,
        "delivery": "system_media_keys",
        "effect_confirmation": "dispatch_only",
        "cleanup": "key_up_dispatched",
    }


@pytest.mark.parametrize("command", ["stop", "play_pause; quit", None, [], True])
def test_invalid_command_never_loads_a_native_bridge(monkeypatch, command):
    loader = Mock()
    monkeypatch.setattr(media, "_Native", loader)
    with pytest.raises(AdapterError) as caught:
        media.MediaAdapter().execute(command)
    assert caught.value.code == "invalid_action" and not caught.value.effects_possible
    loader.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, True, math.inf, math.nan, "5"])
def test_invalid_timeouts_have_no_effects(native, timeout):
    with pytest.raises(AdapterError) as caught:
        media.MediaAdapter(native).execute("play_pause", timeout=timeout)
    assert caught.value.code == "invalid_action"
    native.trusted.assert_not_called()
    native.post.assert_not_called()


def test_prepare_is_lazy_and_check_is_passive(monkeypatch, native):
    loader = Mock(return_value=native)
    monkeypatch.setattr(media, "_Native", loader)
    adapter = media.MediaAdapter()
    loader.assert_not_called()
    adapter.prepare()
    native.trusted.assert_not_called()
    report = adapter.check()
    assert report["available"] and report["target"] == "system"
    assert report["effect_confirmation"] == "dispatch_only"
    adapter.prepare()
    loader.assert_called_once_with()
    native.events.assert_not_called()
    native.post.assert_not_called()


@pytest.mark.parametrize("permissions", [[False], [True, False]])
def test_missing_or_revoked_permission_never_posts(native, permissions):
    native.trusted.side_effect = permissions
    with pytest.raises(AdapterError) as caught:
        media.MediaAdapter(native).execute("play_pause")
    assert caught.value.code == "permission_denied" and not caught.value.effects_possible
    native.post.assert_not_called()


def test_passive_permission_failure_and_unexpected_errors_are_sanitized(native):
    adapter = media.MediaAdapter(native)
    native.trusted.return_value = False
    assert adapter.check()["code"] == "permission_denied"
    native.trusted.side_effect = RuntimeError("PRIVATE NATIVE MESSAGE")
    assert adapter.check()["code"] == "capability_unavailable"
    assert "PRIVATE" not in str(adapter.check())
    native.post.assert_not_called()


@pytest.mark.parametrize("clock,effects,posted", [([0, 2], False, 0), ([0, 0, 2], True, 2)])
def test_deadline_is_checked_before_and_after_dispatch(monkeypatch, native, clock, effects, posted):
    monkeypatch.setattr(media.time, "monotonic", Mock(side_effect=clock))
    with pytest.raises(AdapterError) as caught:
        media.MediaAdapter(native).execute("next_track", timeout=1)
    assert caught.value.code == "timed_out" and caught.value.effects_possible is effects
    assert native.post.call_count == posted


def test_down_failure_still_releases_the_key(native):
    native.post.side_effect = [RuntimeError("PRIVATE"), None]
    with pytest.raises(AdapterError) as caught:
        media.MediaAdapter(native).execute("next_track")
    assert caught.value.code == "execution_failed" and caught.value.effects_possible
    assert "PRIVATE" not in str(caught.value)
    assert [call.args for call in native.post.call_args_list] == [("down",), ("up",)]


def test_failed_release_latches_unavailable_until_restart(native):
    adapter = media.MediaAdapter(native)
    native.post.side_effect = [None, RuntimeError("PRIVATE")]
    with pytest.raises(AdapterError) as caught:
        adapter.execute("play_pause")
    assert caught.value.code == "executor_unavailable" and caught.value.effects_possible
    with pytest.raises(AdapterError) as caught:
        adapter.execute("next_track")
    assert caught.value.code == "executor_unavailable" and not caught.value.effects_possible
    assert adapter.check()["code"] == "executor_unavailable"
    assert native.post.call_count == 2


def test_non_macos_platform_fails_without_importing_bridges(monkeypatch):
    monkeypatch.setattr(media.sys, "platform", "linux")
    assert media.MediaAdapter().check()["code"] == "capability_unavailable"


def test_missing_event_posting_api_fails_closed(monkeypatch):
    monkeypatch.setattr(media.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())
    assert media.MediaAdapter().check()["code"] == "capability_unavailable"


@pytest.mark.parametrize(
    "command,code", [("play_pause", 16), ("next_track", 17), ("previous_track", 18)]
)
def test_native_event_encoding_and_posting_are_exact(monkeypatch, command, code):
    make_event = Mock(
        side_effect=[SimpleNamespace(CGEvent=lambda: "down"), SimpleNamespace(CGEvent=lambda: "up")]
    )
    apps = SimpleNamespace(
        NSEvent=SimpleNamespace(
            otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_=make_event
        ),
        NSEventTypeSystemDefined=14,
    )
    quartz = SimpleNamespace(
        CGPreflightPostEventAccess=Mock(return_value=True), CGEventPost=Mock(), kCGHIDEventTap=0
    )
    monkeypatch.setattr(media.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", apps)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    bridge = media._Native()
    assert bridge.trusted()
    assert bridge.events(command) == ("down", "up")
    assert [call.args for call in make_event.call_args_list] == [
        (14, (0, 0), 0xA00, 0, 0, None, 8, (code << 16) | 0xA00, -1),
        (14, (0, 0), 0xB00, 0, 0, None, 8, (code << 16) | 0xB00, -1),
    ]
    quartz.CGEventPost.assert_not_called()
    bridge.post("down")
    quartz.CGEventPost.assert_called_once_with(0, "down")


@pytest.mark.parametrize("failure", [None, SimpleNamespace(CGEvent=lambda: None)])
def test_event_creation_failure_does_not_post(failure):
    bridge = object.__new__(media._Native)
    bridge.apps = SimpleNamespace(
        NSEventTypeSystemDefined=14,
        NSEvent=SimpleNamespace(
            otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_=lambda *args: failure
        ),
    )
    with pytest.raises(AdapterError) as caught:
        bridge.events("play_pause")
    assert caught.value.code == "capability_unavailable"
