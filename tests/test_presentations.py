"""Native presentation hub/router tests; never initialize or access a real desktop."""

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import wraps

import pytest
from conftest import Clock
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conty.adapters.base import AdapterError
from conty.auth import APIError, Sessions
from conty.presentations import (
    COMMAND_SECONDS,
    DISCOVERY_SECONDS,
    MAX_SLIDES,
    PNG_LIMIT,
    REFRESH_SECONDS,
    RETENTION_SECONDS,
    CommandInput,
    PresentationHub,
    presentation_router,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
SLIDES = [
    {"id": "10", "number": 1, "title": "Opening", "hidden": False},
    {"id": "20", "number": 2, "title": "Hidden", "hidden": True},
    {"id": "30", "number": 3, "title": "Closing", "hidden": False},
]
PRIVATE = "/Users/private/Documents/secret.pptx"


def native_deck(key="private-key", title="Slides.pptx"):
    return {
        "key": key,
        "title": title,
        "handle": {"fullname": PRIVATE, "key": key},
        "slides": deepcopy(SLIDES),
        "view": "edit",
        "current_slide_id": None,
        "can_control": False,
        "private_path": PRIVATE,
    }


class FakePowerPoint:
    def __init__(self):
        self.decks = [native_deck()]
        self.calls = []
        self.inputs = []
        self.gates = {}
        self.started = {}
        self.errors = {}
        self.previews = None
        self.navigation_result = None
        self.active = self.peak = 0
        self.closed = False
        self.cancelled = []

    async def _call(self, method, handle=None, **kwargs):
        assert not self.closed
        self.calls.append((method, deepcopy(handle), deepcopy(kwargs)))
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.setdefault(method, asyncio.Event()).set()
        try:
            if method in self.gates:
                await self.gates[method].wait()
            if method in self.errors:
                raise self.errors[method]
            if method == "discover":
                return deepcopy(self.decks)
            deck = next((d for d in self.decks if d["handle"] == handle), None)
            if deck is None:
                raise AdapterError("target_unavailable", PRIVATE)
            if method == "command":
                expected = kwargs["expected_slides"]
                if [(s["id"], s["number"]) for s in expected] != [
                    (s["id"], s["number"]) for s in deck["slides"]
                ]:
                    raise AdapterError("stale_snapshot", PRIVATE)
                if deck["view"] != "read" or not deck["can_control"]:
                    raise AdapterError("target_not_frontmost", PRIVATE)
                if kwargs["operation"] == "show_slide":
                    if kwargs["slide_id"] not in {s["id"] for s in deck["slides"]}:
                        raise AdapterError("stale_snapshot", PRIVATE)
                    if [s["number"] for s in deck["slides"]] != list(
                        range(1, len(deck["slides"]) + 1)
                    ):
                        raise AdapterError("unsupported_show", PRIVATE)
                self.inputs.append((deepcopy(handle), deepcopy(kwargs)))
            result = deepcopy(deck)
            if method == "refresh":
                result["previews"] = (
                    deepcopy(self.previews)
                    if self.previews is not None
                    else [
                        {"slide_id": s["id"], "page": index, "image": PNG}
                        for index, s in enumerate(result["slides"], 1)
                    ]
                )
            if method == "command" and self.navigation_result is not None:
                result.update(deepcopy(self.navigation_result))
                deck.update(deepcopy(self.navigation_result))
            return result
        except asyncio.CancelledError:
            self.cancelled.append(method)
            raise
        finally:
            self.active -= 1

    async def discover(self):
        return await self._call("discover")

    async def observe(self, handle):
        return await self._call("observe", handle)

    async def refresh(self, handle):
        return await self._call("refresh", handle)

    async def command(self, handle, operation, slide_id=None, expected_slides=None):
        return await self._call(
            "command",
            handle,
            operation=operation,
            slide_id=slide_id,
            expected_slides=expected_slides,
        )

    async def authorize(self):
        raise AssertionError("Browser requests must never request permission")

    async def close(self):
        self.closed = True

    def show(self, *, current="10", can_control=True):
        self.decks[0].update(view="read", current_slide_id=current, can_control=can_control)

    def count(self, operation):
        return sum(call[0] == operation for call in self.calls)


class WaitingInputLock(asyncio.Lock):
    """Expose a contended acquisition without relying on native reads or sleeps."""

    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.waiting.set()
        return await super().acquire()


@dataclass
class Environment:
    clock: Clock
    sessions: Sessions
    adapter: FakePowerPoint
    credential: str
    owner: object
    other_credential: str
    other: object

    @asynccontextmanager
    async def hub(self, **kwargs):
        hub = PresentationHub(self.sessions, self.adapter, **kwargs)
        try:
            yield hub
        finally:
            await hub.close()

    def revoke(self, credential=None):
        self.sessions.records.pop(self.sessions.digest(credential or self.credential))


@pytest.fixture
def env():
    clock = Clock()
    sessions = Sessions(clock=clock, output=lambda _: None)
    sessions.rotate()
    credential, owner = sessions.pair(sessions.code, "browser")
    other_credential, other = sessions.pair(sessions.code, "other")
    return Environment(
        clock, sessions, FakePowerPoint(), credential, owner, other_credential, other
    )


def run(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


async def listing(hub, env):
    return (await hub.list_presentations(env.owner.id))["presentations"]


async def deck(hub, env):
    return (await listing(hub, env))[0]


def submit(hub, env, presentation, operation="refresh", request_id="request1", **kwargs):
    data = {"request_id": request_id, "revision": presentation["revision"], "operation": operation}
    if operation == "show_slide":
        data["slide_id"] = "20"
    data.update(kwargs)
    return hub.submit(env.owner.id, presentation["id"], CommandInput(**data))


async def settled(hub, env, presentation, command):
    if hub._worker is not None:
        await asyncio.wait_for(asyncio.shield(hub._worker), 2)
    return hub.lookup(env.owner.id, presentation["id"], command["id"])


def expect_error(code, function, *args, status=None, **kwargs):
    with pytest.raises(APIError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status == status
    assert PRIVATE not in caught.value.message
    return caught.value


def test_default_is_lazy_and_close_does_not_construct_native(env):
    async def scenario():
        hub = PresentationHub(env.sessions)
        assert hub._adapter is None
        hub.maintain()
        await hub.close()
        await hub.close()
        assert hub._adapter is None

    asyncio.run(scenario())


@run
async def test_discovery_is_read_only_coalesced_cached_and_never_periodic(env):
    async with env.hub() as hub:
        env.adapter.gates["discover"] = asyncio.Event()
        calls = [
            asyncio.create_task(hub.list_presentations(owner.id))
            for owner in (env.owner, env.other, env.owner)
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        env.adapter.gates["discover"].set()
        results = await asyncio.gather(*calls)
        assert results[0] == results[1] == results[2]
        assert env.adapter.count("discover") == 1
        first = results[0]["presentations"][0]
        assert first["needs_refresh"] and first["preview_count"] == 0
        assert first["view"] == "edit" and not first["can_control"]
        assert all(s["preview_url"] is None and s["page"] is None for s in first["slides"])
        assert set(first) == {
            "id",
            "title",
            "revision",
            "connected",
            "view",
            "current_slide_id",
            "can_control",
            "slides",
            "preview_count",
            "needs_refresh",
            "observed_at",
        }
        assert PRIVATE not in json.dumps(results)
        assert "private-key" not in json.dumps(results)
        for _ in range(3):
            env.clock.now += 0.2
            await listing(hub, env)
        assert env.adapter.count("discover") == 1
        env.clock.now += 100
        for _ in range(3):
            hub.maintain()
            await asyncio.sleep(0)
        assert env.adapter.count("discover") == 1
        await listing(hub, env)
        assert [c[0] for c in env.adapter.calls] == ["discover", "discover"]


@pytest.mark.parametrize("wait", [0, 0.01])
@run
async def test_bounded_list_wait_returns_pending_without_cancelling_or_finishing_discovery(
    env, wait
):
    async with env.hub() as hub:
        gate = env.adapter.gates["discover"] = asyncio.Event()
        pending = await asyncio.wait_for(hub.list_presentations(env.owner.id, wait=wait), 0.5)
        assert pending == {"presentations": [], "discovering": True}
        task = hub._discovery
        waiting = asyncio.create_task(hub.list_presentations(env.other.id))
        env.clock.now += 15
        for owner in (env.other, env.owner):
            assert await hub.list_presentations(owner.id, wait=wait) == pending
            assert hub._discovery is task
        assert not task.done() and not waiting.done() and not gate.is_set()
        assert hub._discovered_at is None and not hub._poisoned
        assert [call[0] for call in env.adapter.calls] == ["discover"]
        assert not env.adapter.cancelled
        gate.set()
        completed = await asyncio.wait_for(waiting, 0.5)
        assert completed["presentations"][0]["title"] == "Slides.pptx"
        assert not completed.get("discovering", False)
        assert await hub.list_presentations(env.owner.id, wait=wait) == completed
        assert env.adapter.count("discover") == 1


@run
async def test_completed_background_scan_is_published_before_starting_the_next_scan(env):
    async with env.hub() as hub:
        gate = env.adapter.gates["discover"] = asyncio.Event()
        pending = await hub.list_presentations(env.owner.id, wait=0)
        assert pending == {"presentations": [], "discovering": True}
        await asyncio.sleep(0)
        gate.set()
        await hub._discovery
        env.clock.now += DISCOVERY_SECONDS + 0.5  # Phone missed the short cache window.
        completed = await hub.list_presentations(env.owner.id, wait=0)
        assert completed["presentations"] and not completed.get("discovering")
        assert env.adapter.count("discover") == 1
        env.adapter.gates["discover"] = asyncio.Event()
        next_scan = await hub.list_presentations(env.owner.id, wait=0)
        assert next_scan == {**completed, "discovering": True}


@run
async def test_pending_discovery_preserves_cached_public_records_without_refreshing_freshness(env):
    async with env.hub() as hub:
        original = await deck(hub, env)
        await settled(hub, env, original, submit(hub, env, original))
        cached = await hub.list_presentations(env.owner.id)
        loaded = cached["presentations"][0]
        observed_at, discovered_at = loaded["observed_at"], hub._discovered_at
        env.clock.now += 7
        gate = env.adapter.gates["discover"] = asyncio.Event()
        env.adapter.decks[0]["title"] = "Updated title.pptx"
        env.adapter.decks.append(native_deck("second-key", "Second.pptx"))
        for _ in range(2):
            pending = await hub.list_presentations(env.owner.id, wait=0.01)
            assert pending == {**cached, "discovering": True}
            assert pending["presentations"][0]["observed_at"] == observed_at
            assert hub._discovered_at == discovered_at
            assert hub.preview(env.owner.id, loaded["id"], "10", loaded["revision"]) == PNG
        assert PRIVATE not in json.dumps(pending) and "private-key" not in json.dumps(pending)
        assert [call[0] for call in env.adapter.calls] == [
            "discover",
            "observe",
            "refresh",
            "discover",
        ]
        gate.set()
        completed = await hub.list_presentations(env.owner.id)
        assert [deck["title"] for deck in completed["presentations"]] == [
            "Updated title.pptx",
            "Second.pptx",
        ]
        assert completed["presentations"][0]["id"] == loaded["id"]
        assert not completed.get("discovering", False)
        assert env.adapter.count("refresh") == 1


@pytest.mark.parametrize("retry_error", [None, "unsupported_identity"])
@run
async def test_pending_retry_hides_previous_discovery_error_until_new_read_completes(
    env, retry_error
):
    env.adapter.errors["discover"] = AdapterError("permission_denied", PRIVATE)
    async with env.hub() as hub:
        with pytest.raises(APIError) as caught:
            await hub.list_presentations(env.owner.id)
        assert caught.value.code == "permission_denied"
        env.clock.now += DISCOVERY_SECONDS
        gate = env.adapter.gates["discover"] = asyncio.Event()
        if retry_error:
            env.adapter.errors["discover"] = AdapterError(retry_error, PRIVATE)
        else:
            env.adapter.errors.clear()
        assert await hub.list_presentations(env.owner.id, wait=0.01) == {
            "presentations": [],
            "discovering": True,
        }
        assert hub._discovery_error.code == "permission_denied" and not hub._discovery.done()
        gate.set()
        await asyncio.wait_for(hub._discovery, 0.5)
        if retry_error:
            with pytest.raises(APIError) as caught:
                await hub.list_presentations(env.owner.id, wait=0.01)
            assert caught.value.code == retry_error and caught.value.status == 409
            assert PRIVATE not in caught.value.message
        else:
            assert (await hub.list_presentations(env.owner.id, wait=0.01))["presentations"]
        assert env.adapter.count("discover") == 2 and not hub._poisoned


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("completed", [False, True])
@run
async def test_bounded_discovery_reauthenticates_owner_after_wait(env, expired, completed):
    async with env.hub() as hub:
        gate = env.adapter.gates["discover"] = asyncio.Event()
        started = env.adapter.started["discover"] = asyncio.Event()
        call = asyncio.create_task(hub.list_presentations(env.owner.id, wait=0.02))
        await asyncio.wait_for(started.wait(), 0.5)
        if expired:
            key = env.sessions.digest(env.credential)
            env.sessions.records[key] = replace(env.owner, expires=env.clock.now)
        else:
            env.revoke()
        if completed:
            gate.set()
        with pytest.raises(APIError) as caught:
            await asyncio.wait_for(call, 0.5)
        assert caught.value.code == "pairing_required" and caught.value.status == 401
        assert not env.adapter.cancelled and not hub._poisoned
        if not completed:
            assert (await hub.list_presentations(env.other.id, wait=0))["discovering"]
            gate.set()
        assert (await hub.list_presentations(env.other.id))["presentations"]
        assert env.adapter.count("discover") == 1


@pytest.mark.parametrize("wait", [None, 0.2])
@run
async def test_cancelling_one_list_does_not_cancel_shared_discovery(env, wait):
    async with env.hub() as hub:
        env.adapter.gates["discover"] = asyncio.Event()
        one = asyncio.create_task(hub.list_presentations(env.owner.id, wait=wait))
        two = asyncio.create_task(hub.list_presentations(env.other.id))
        await asyncio.sleep(0)
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        env.adapter.gates["discover"].set()
        assert (await two)["presentations"]
        assert env.adapter.count("discover") == 1
        assert not env.adapter.cancelled


@run
async def test_explicit_refresh_maps_hidden_slides_by_id_and_explicit_pdf_page(env):
    async with env.hub() as hub:
        original = await deck(hub, env)
        env.adapter.previews = [
            {"slide_id": "30", "page": 2, "image": PNG + b"closing"},
            {"slide_id": "10", "page": 1, "image": PNG},
            {"slide_id": "20", "page": 3, "image": PNG + b"hidden"},
        ]
        command = submit(hub, env, original)
        assert command["state"] == "queued" and env.adapter.count("refresh") == 0
        assert (await settled(hub, env, original, command))["state"] == "succeeded"
        updated = await deck(hub, env)
        assert updated["revision"] != original["revision"]
        assert updated["preview_count"] == 3 and not updated["needs_refresh"]
        assert [s["page"] for s in updated["slides"]] == [1, 3, 2]
        assert updated["slides"][1]["hidden"]
        assert (
            hub.preview(env.owner.id, updated["id"], "20", updated["revision"]) == PNG + b"hidden"
        )
        expect_error(
            "preview_not_found",
            hub.preview,
            env.owner.id,
            updated["id"],
            "20",
            original["revision"],
        )
        assert [c[0] for c in env.adapter.calls] == ["discover", "observe", "refresh"]
        assert PRIVATE not in json.dumps(updated)


@run
async def test_static_preview_remains_available_without_fresh_live_observation(env):
    async with env.hub() as hub:
        original = await deck(hub, env)
        await settled(hub, env, original, submit(hub, env, original))
        loaded = await deck(hub, env)
        args = env.owner.id, loaded["id"], "10", loaded["revision"]
        calls = deepcopy(env.adapter.calls)
        env.clock.now += 600
        assert hub.preview(*args) == PNG
        assert env.adapter.calls == calls
        cached = hub.presentations[loaded["id"]]
        assert cached.connected and cached.revision == loaded["revision"]
        assert len(cached.previews) == 3 and hub.preview_bytes == 3 * len(PNG)
        assert (await deck(hub, env))["revision"] == loaded["revision"]
        assert hub.preview(*args) == PNG
        assert env.adapter.count("refresh") == 1


@pytest.mark.parametrize("change", ["closed", "reordered", "reopened"])
@run
async def test_static_preview_is_invalidated_when_discovery_observes_deck_change(env, change):
    async with env.hub() as hub:
        original = await deck(hub, env)
        await settled(hub, env, original, submit(hub, env, original))
        loaded = await deck(hub, env)
        args = env.owner.id, loaded["id"], "10", loaded["revision"]
        if change == "closed":
            env.adapter.decks.clear()
        elif change == "reordered":
            env.adapter.decks[0]["slides"].reverse()
        else:
            env.adapter.decks = [native_deck("reopened-key")]
        env.clock.now += 600
        calls = deepcopy(env.adapter.calls)
        assert hub.preview(*args) == PNG  # Cached PDF snapshot, not a live view.
        assert env.adapter.calls == calls
        assert hub.presentations[loaded["id"]].connected
        refreshed = await listing(hub, env)
        expect_error("preview_not_found", hub.preview, *args, status=404)
        if change == "reopened":
            reopened = next(d for d in refreshed if d["connected"])
            assert reopened["id"] != loaded["id"]
            assert reopened["revision"] != loaded["revision"]
        elif change == "reordered":
            assert refreshed[0]["revision"] != loaded["revision"]
        else:
            assert not refreshed[0]["connected"]
        assert env.adapter.count("refresh") == 1


@pytest.mark.parametrize("first_number", [1001, 0, -10, -(2**31), 2**31 - 3])
@run
async def test_discovery_preserves_signed_display_numbers_alongside_standard_deck(
    env, first_number
):
    offset = native_deck("offset-key", "Offset.pptx")
    for index, slide in enumerate(offset["slides"]):
        slide["number"] = first_number + index
    env.adapter.decks.append(offset)
    async with env.hub() as hub:
        standard, offset_public = await listing(hub, env)
        assert standard["slides"] == [dict(s, page=None, preview_url=None) for s in SLIDES]
        assert [s["number"] for s in offset_public["slides"]] == [
            first_number + index for index in range(3)
        ]
        assert standard["connected"] and offset_public["connected"]
        assert env.adapter.count("discover") == 1


@pytest.mark.parametrize("number", [True, False, 1.0, -(2**31) - 1, 2**31])
def test_slide_display_number_rejects_non_int32_values(number):
    value = native_deck()
    value["slides"][0]["number"] = number
    with pytest.raises(AdapterError) as caught:
        PresentationHub._state(value)
    assert caught.value.code == "execution_failed"


@run
async def test_offset_number_jump_is_rejected_by_native_guard_not_discovery(env):
    env.adapter.show()
    for index, slide in enumerate(env.adapter.decks[0]["slides"]):
        slide["number"] = 1001 + index
    env.adapter.errors["command"] = AdapterError("unsupported_show", PRIVATE)
    async with env.hub() as hub:
        original = await deck(hub, env)
        command = submit(hub, env, original, "show_slide")
        result = await settled(hub, env, original, command)
        assert result["state"] == "failed"
        assert "standard all-slides show" in result["message"]
        assert env.adapter.count("command") == 1 and not hub._poisoned


@run
async def test_content_refresh_rotates_revision_but_same_manifest_observation_does_not(env):
    async with env.hub() as hub:
        first = await deck(hub, env)
        await settled(hub, env, first, submit(hub, env, first))
        loaded = await deck(hub, env)
        env.adapter.decks[0]["slides"][0]["title"] = "Edited content"
        env.clock.now += DISCOVERY_SECONDS
        observed = await deck(hub, env)
        assert observed["revision"] == loaded["revision"]
        assert not observed["needs_refresh"]  # No unsupported content-change detection claim.
        await settled(hub, env, observed, submit(hub, env, observed, request_id="new-content"))
        assert (await deck(hub, env))["revision"] != loaded["revision"]


@pytest.mark.parametrize("change", ["order", "id", "number", "hidden"])
@run
async def test_manifest_changes_invalidate_all_previews_and_revision(env, change):
    async with env.hub() as hub:
        original = await deck(hub, env)
        await settled(hub, env, original, submit(hub, env, original))
        loaded = await deck(hub, env)
        slides = env.adapter.decks[0]["slides"]
        if change == "order":
            slides.reverse()
        elif change == "id":
            slides[0]["id"] = "99"
        elif change == "number":
            slides[0]["number"], slides[1]["number"] = 2, 1
        else:
            slides[0]["hidden"] = True
        env.clock.now += DISCOVERY_SECONDS
        changed = await deck(hub, env)
        assert changed["revision"] != loaded["revision"]
        assert changed["needs_refresh"] and changed["preview_count"] == 0
        assert hub.preview_bytes == 0
        expect_error("stale_revision", submit, hub, env, loaded, request_id="stale")


@run
async def test_navigation_reports_native_observation_not_request_or_press_count(env):
    env.adapter.show()
    async with env.hub() as hub:
        original = await deck(hub, env)
        env.adapter.navigation_result = {"current_slide_id": "30"}
        command = submit(hub, env, original, "show_slide")
        result = await settled(hub, env, original, command)
        assert result["state"] == "succeeded" and "dispatched" in result["message"]
        observed = await deck(hub, env)
        assert observed["current_slide_id"] == "30"  # Requested hidden slide 20, observed 30.
        call = next(c for c in env.adapter.calls if c[0] == "command")
        assert call[1] == env.adapter.decks[0]["handle"]
        assert call[2] == {"operation": "show_slide", "slide_id": "20", "expected_slides": SLIDES}
        env.adapter.navigation_result = {"current_slide_id": "30"}
        for operation in ("next_slide", "previous_slide"):
            command = submit(hub, env, observed, operation, request_id=operation)
            assert (await settled(hub, env, observed, command))["state"] == "succeeded"
            assert (await deck(hub, env))["current_slide_id"] == "30"


@run
async def test_submission_and_pre_dispatch_recheck_view_focus_and_revision(env):
    async with env.hub() as hub:
        original = await deck(hub, env)
        expect_error("control_unavailable", submit, hub, env, original, "next_slide")
        env.adapter.show(can_control=False)
        env.clock.now += DISCOVERY_SECONDS
        read = await deck(hub, env)
        expect_error("control_unavailable", submit, hub, env, read, "show_slide")
        expect_error("edit_view_required", submit, hub, env, read)
        env.adapter.show()
        env.clock.now += DISCOVERY_SECONDS
        active = await deck(hub, env)
        expect_error("slide_not_found", submit, hub, env, active, "show_slide", slide_id="missing")
        command = submit(hub, env, active, "next_slide")
        env.adapter.show(can_control=False)
        assert (await settled(hub, env, active, command))["state"] == "failed"
        assert env.adapter.count("command") == 1
        assert not env.adapter.inputs and not hub._poisoned
        assert not (await deck(hub, env))["can_control"]
        env.adapter.decks[0].update(view="edit", current_slide_id=None, can_control=False)
        env.clock.now += DISCOVERY_SECONDS
        edit = await deck(hub, env)
        command = submit(hub, env, edit, request_id="view-race")
        env.adapter.show()
        assert (await settled(hub, env, edit, command))["state"] == "cancelled"
        assert env.adapter.count("refresh") == 0


@run
async def test_native_manifest_guard_rejects_change_before_input(env):
    env.adapter.show()
    async with env.hub() as hub:
        original = await deck(hub, env)
        command = submit(hub, env, original, "show_slide")
        env.adapter.decks[0]["slides"].reverse()
        result = await settled(hub, env, original, command)
        assert result["state"] == "failed"
        assert env.adapter.count("command") == 1 and not env.adapter.inputs
        assert env.adapter.count("observe") == 0 and not hub._poisoned
        assert (await deck(hub, env))["revision"] != original["revision"]


@run
async def test_dedupe_owner_scope_full_fingerprint_and_precedence_over_new_checks(env):
    env.adapter.show()
    async with env.hub(max_commands=1) as hub:
        original = await deck(hub, env)
        first = submit(hub, env, original, "next_slide")
        assert submit(hub, env, original, "next_slide") == first
        expect_error("request_conflict", submit, hub, env, original, "previous_slide", status=409)
        expect_error("request_conflict", submit, hub, env, original, "next_slide", revision="other")
        payload = CommandInput(
            request_id="request1", revision=original["revision"], operation="next_slide"
        )
        expect_error("request_conflict", hub.submit, env.owner.id, "other-deck", payload)
        expect_error("command_not_found", hub.lookup, env.other.id, original["id"], first["id"])
        expect_error("command_not_found", hub.lookup, env.owner.id, "other-deck", first["id"])
        await settled(hub, env, original, first)
        hub._poison()
        assert submit(hub, env, original, "next_slide")["id"] == first["id"]
        assert env.adapter.count("command") == 1


@run
async def test_same_request_id_is_independent_per_owner(env):
    env.adapter.show()
    async with env.hub() as hub:
        original = await deck(hub, env)
        one = submit(hub, env, original, "next_slide")
        payload = CommandInput(
            request_id="request1", revision=original["revision"], operation="next_slide"
        )
        two = hub.submit(env.other.id, original["id"], payload)
        assert one["id"] != two["id"]
        await settled(hub, env, original, one)
        assert hub.lookup(env.other.id, original["id"], two["id"])["state"] == "succeeded"
        assert env.adapter.count("command") == 2


@run
async def test_global_queue_serializes_native_reads_export_and_dispatch(env):
    async with env.hub() as hub:
        first = await deck(hub, env)
        env.adapter.decks.append(native_deck("second-key", "Other.pptx"))
        env.clock.now += DISCOVERY_SECONDS
        second = (await listing(hub, env))[1]
        env.adapter.gates["refresh"] = asyncio.Event()
        one = submit(hub, env, first)
        two = submit(hub, env, second, request_id="second")
        await asyncio.sleep(0)
        await env.adapter.started["refresh"].wait()
        assert env.adapter.count("refresh") == 1
        assert hub.lookup(env.owner.id, second["id"], two["id"])["state"] == "queued"
        env.clock.now += DISCOVERY_SECONDS
        poll = asyncio.create_task(hub.list_presentations(env.other.id))
        await asyncio.sleep(0)
        assert env.adapter.count("discover") == 2
        env.adapter.gates["refresh"].set()
        await settled(hub, env, first, one)
        await poll
        assert env.adapter.count("refresh") == 2
        assert env.adapter.peak == 1


@run
async def test_shared_input_lock_does_not_block_discovery_observation_or_export(env):
    lock = asyncio.Lock()
    async with env.hub(native_lock=lock) as hub:
        async with lock:
            original = await asyncio.wait_for(deck(hub, env), 0.5)
            command = submit(hub, env, original)
            assert (await settled(hub, env, original, command))["state"] == "succeeded"
        assert [c[0] for c in env.adapter.calls] == ["discover", "observe", "refresh"]


@pytest.mark.parametrize("method,elapsed", [("discover", 6), ("observe", 6), ("refresh", 45)])
@run
async def test_gated_native_reads_leave_shared_input_lane_available(env, method, elapsed):
    lock = asyncio.Lock()
    async with env.hub(native_lock=lock) as hub:
        if method != "discover":
            original = await deck(hub, env)
        gate = env.adapter.gates[method] = asyncio.Event()
        started = env.adapter.started[method] = asyncio.Event()
        if method == "discover":
            work = asyncio.create_task(listing(hub, env))
        else:
            command = submit(hub, env, original)
            work = hub._worker
        await asyncio.wait_for(started.wait(), 0.5)
        env.clock.now += elapsed

        async def keyboard_dispatch():
            async with lock:
                assert not gate.is_set()
                assert env.adapter.active == 1

        try:
            # A six-second read / 45-second export must not delay any keyboard action.
            await asyncio.wait_for(keyboard_dispatch(), 0.5)
            assert not work.done() and not gate.is_set()
        finally:
            gate.set()
            await asyncio.wait_for(work, 2)
        if method != "discover":
            assert hub.lookup(env.owner.id, original["id"], command["id"])["state"] == "succeeded"
        assert not hub._poisoned


@pytest.mark.parametrize("operation", ["show_slide", "next_slide", "previous_slide"])
@run
async def test_navigation_alone_holds_shared_input_lock(env, operation):
    env.adapter.show()
    lock = WaitingInputLock()
    async with env.hub(native_lock=lock) as hub:
        original = await deck(hub, env)
        gate = env.adapter.gates["command"] = asyncio.Event()
        started = env.adapter.started["command"] = asyncio.Event()
        async with lock:
            command = submit(hub, env, original, operation)
            await asyncio.wait_for(lock.waiting.wait(), 0.5)
            assert env.adapter.count("command") == 0
        await asyncio.wait_for(started.wait(), 0.5)
        assert lock.locked()

        async def keyboard_dispatch():
            async with lock:
                return "dispatched"

        keyboard = asyncio.create_task(keyboard_dispatch())
        await asyncio.sleep(0)
        assert not keyboard.done()
        gate.set()
        assert (await settled(hub, env, original, command))["state"] == "succeeded"
        assert await asyncio.wait_for(keyboard, 0.5) == "dispatched"
        assert not lock.locked() and env.adapter.peak == 1
        assert env.adapter.count("observe") == 0
        assert env.adapter.count("command") == len(env.adapter.inputs) == 1
        call = env.adapter.calls[-1]
        assert call[1] == env.adapter.decks[0]["handle"]
        assert call[2]["expected_slides"] == SLIDES


@pytest.mark.parametrize("change", ["expiry", "revocation", "deadline", "revision", "offline"])
@run
async def test_navigation_rechecks_after_waiting_for_shared_input_lock(env, change):
    env.adapter.show()
    lock = WaitingInputLock()
    async with env.hub(native_lock=lock) as hub:
        original = await deck(hub, env)
        async with lock:
            command = submit(hub, env, original, "show_slide")
            await asyncio.wait_for(lock.waiting.wait(), 0.5)
            assert env.adapter.count("command") == 0
            if change == "expiry":
                key = env.sessions.digest(env.credential)
                env.sessions.records[key] = replace(env.owner, expires=env.clock.now)
            elif change == "revocation":
                env.revoke()
            elif change == "deadline":
                env.clock.now += COMMAND_SECONDS
            elif change == "revision":
                hub._invalidate(hub.presentations[original["id"]])
            else:
                hub._offline(hub.presentations[original["id"]])
        await asyncio.wait_for(hub._worker, 0.5)
        assert env.adapter.count("command") == 0 and not hub._poisoned
        if change in ("expiry", "revocation"):
            assert command["id"] not in hub.commands
        else:
            assert hub.lookup(env.owner.id, original["id"], command["id"])["state"] == "cancelled"


@pytest.mark.parametrize("operation", ["show_slide", "next_slide", "previous_slide"])
@pytest.mark.parametrize("change", ["identity", "order", "numbers", "focus", "view"])
@run
async def test_navigation_keeps_original_target_for_native_guard_after_input_wait(
    env, operation, change
):
    env.adapter.show()
    lock = WaitingInputLock()
    async with env.hub(native_lock=lock) as hub:
        original = await deck(hub, env)
        handle = deepcopy(env.adapter.decks[0]["handle"])
        async with lock:
            command = submit(hub, env, original, operation)
            await asyncio.wait_for(lock.waiting.wait(), 0.5)
            assert env.adapter.count("command") == 0
            if change == "identity":
                env.adapter.decks[0]["handle"] = {"key": "replacement"}
            elif change == "order":
                env.adapter.decks[0]["slides"].reverse()
            elif change == "numbers":
                env.adapter.decks[0]["slides"][1]["number"] = 99
            elif change == "focus":
                env.adapter.show(can_control=False)
            else:
                env.adapter.decks[0].update(view="edit", current_slide_id=None, can_control=False)
        result = await settled(hub, env, original, command)
        assert result["state"] == "failed" and not hub._poisoned
        assert not env.adapter.inputs
        assert [call[0] for call in env.adapter.calls] == ["discover", "command"]
        assert env.adapter.calls[-1][1:] == (
            handle,
            {
                "operation": operation,
                "slide_id": "20" if operation == "show_slide" else None,
                "expected_slides": SLIDES,
            },
        )


@run
async def test_close_cancels_navigation_waiting_for_input_lock_without_deadlock(env):
    env.adapter.show()
    lock = WaitingInputLock()
    async with env.hub(native_lock=lock) as hub:
        original = await deck(hub, env)
        async with lock:
            submit(hub, env, original, "next_slide")
            await asyncio.wait_for(lock.waiting.wait(), 0.5)
            await asyncio.wait_for(hub.close(), 0.5)
        assert hub._worker.done() and env.adapter.closed
        assert env.adapter.count("command") == 0 and not hub._poisoned
        assert not env.adapter.cancelled and not lock.locked()


@run
async def test_navigation_input_lock_deadline_does_not_start_native_or_poison(env, monkeypatch):
    import conty.presentations as module

    monkeypatch.setattr(module, "COMMAND_SECONDS", 0.05)
    env.adapter.show()
    lock = asyncio.Lock()
    async with env.hub(native_lock=lock) as hub:
        original = await deck(hub, env)
        async with lock:
            command = submit(hub, env, original, "next_slide")
            result = await settled(hub, env, original, command)
        assert result["state"] == "cancelled" and not hub._poisoned
        assert env.adapter.count("observe") == env.adapter.count("command") == 0
        retry = submit(hub, env, original, "next_slide", request_id="retry")
        assert (await settled(hub, env, original, retry))["state"] == "succeeded"


@pytest.mark.parametrize("expired", [False, True])
@run
async def test_expired_or_revoked_queued_session_never_dispatches(env, expired):
    async with env.hub() as hub:
        original = await deck(hub, env)
        command = submit(hub, env, original)
        if expired:
            key = env.sessions.digest(env.credential)
            env.sessions.records[key] = replace(env.owner, expires=env.clock.now)
        else:
            env.revoke()
        hub.maintain()
        await hub._worker
        assert command["id"] not in hub.commands and not hub.dedupe
        assert [c[0] for c in env.adapter.calls] == ["discover"]
        expect_error("pairing_required", hub.lookup, env.owner.id, original["id"], command["id"])


@run
async def test_revocation_during_preflight_prevents_dispatch(env):
    async with env.hub() as hub:
        original = await deck(hub, env)
        env.adapter.gates["observe"] = asyncio.Event()
        submit(hub, env, original)
        await asyncio.sleep(0)
        await env.adapter.started["observe"].wait()
        env.revoke()
        hub.maintain()
        env.adapter.gates["observe"].set()
        await hub._worker
        assert env.adapter.count("refresh") == 0 and not hub.commands


@run
async def test_running_expired_owner_is_hidden_without_replay_or_new_capacity(env):
    env.adapter.show()
    async with env.hub(max_pending=1) as hub:
        original = await deck(hub, env)
        env.adapter.gates["command"] = asyncio.Event()
        command = submit(hub, env, original, "next_slide")
        await asyncio.sleep(0)
        await env.adapter.started["command"].wait()
        env.revoke()
        hub.maintain()
        assert command["id"] not in hub.commands
        payload = CommandInput(
            request_id="other", revision=original["revision"], operation="next_slide"
        )
        expect_error("command_capacity", hub.submit, env.other.id, original["id"], payload)
        env.adapter.gates["command"].set()
        await hub._worker
        assert env.adapter.count("command") == 1 and not hub.commands


@run
async def test_disconnect_reopen_changes_opaque_identity_and_revision_and_cleans_offline(env):
    async with env.hub() as hub:
        original = await deck(hub, env)
        command = submit(hub, env, original)
        await settled(hub, env, original, command)
        loaded = await deck(hub, env)
        private_deck = env.adapter.decks.pop()
        env.clock.now += DISCOVERY_SECONDS
        offline = await deck(hub, env)
        assert not offline["connected"] and offline["view"] is None
        assert not offline["can_control"] and offline["preview_count"] == 0
        expect_error("presentation_offline", submit, hub, env, offline, request_id="offline")
        expect_error(
            "preview_not_found", hub.preview, env.owner.id, original["id"], "10", loaded["revision"]
        )
        env.adapter.decks.append(private_deck)  # Even if the native key is reused.
        env.clock.now += DISCOVERY_SECONDS
        reopened = next(d for d in await listing(hub, env) if d["connected"])
        assert reopened["id"] != original["id"] and reopened["revision"] != loaded["revision"]
        env.clock.now += RETENTION_SECONDS
        calls = len(env.adapter.calls)
        hub.maintain()
        assert original["id"] not in hub.presentations
        assert len(env.adapter.calls) == calls


@run
async def test_command_retention_and_queue_record_caps_refuse_not_evict(env):
    env.adapter.show()
    async with env.hub(max_commands=2, max_pending=1) as hub:
        original = await deck(hub, env)
        one = submit(hub, env, original, "next_slide")
        expect_error("command_capacity", submit, hub, env, original, "next_slide", request_id="two")
        await settled(hub, env, original, one)
        two = submit(hub, env, original, "next_slide", request_id="two")
        await settled(hub, env, original, two)
        expect_error(
            "command_capacity", submit, hub, env, original, "next_slide", request_id="three"
        )
        env.clock.now += RETENTION_SECONDS - 1
        hub.maintain()
        assert len(hub.commands) == 2
        env.clock.now += 1
        hub.maintain()
        assert not hub.commands and not hub.dedupe
        three = submit(hub, env, original, "next_slide", request_id="three")
        assert (await settled(hub, env, original, three))["state"] == "succeeded"


@run
async def test_admitted_navigation_has_separate_bounded_execution_budget(env, monkeypatch):
    import conty.presentations as module

    monkeypatch.setattr(module, "COMMAND_SECONDS", 0.03)
    monkeypatch.setattr(module, "NAVIGATION_SECONDS", 0.4)
    env.adapter.show()
    original_command = env.adapter.command

    async def slow_command(*args, **kwargs):
        await asyncio.sleep(0.08)  # Past admission budget, but within native execution budget.
        return await original_command(*args, **kwargs)

    env.adapter.command = slow_command
    async with env.hub() as hub:
        original = await deck(hub, env)
        command = submit(hub, env, original, "show_slide")
        result = await settled(hub, env, original, command)
        assert result["state"] == "succeeded"
        assert env.adapter.count("command") == 1 and not hub._poisoned


@run
async def test_queued_deadline_cancels_without_native_call(env):
    env.adapter.show()
    async with env.hub() as hub:
        original = await deck(hub, env)
        command = submit(hub, env, original, "next_slide")
        env.clock.now += COMMAND_SECONDS
        hub.maintain()
        assert (await settled(hub, env, original, command))["state"] == "cancelled"
        assert [c[0] for c in env.adapter.calls] == ["discover"]


@run
async def test_refresh_has_longer_deadline_than_navigation(env):
    assert REFRESH_SECONDS >= 90
    async with env.hub() as hub:
        original = await deck(hub, env)
        command = submit(hub, env, original)
        env.clock.now += COMMAND_SECONDS + 1
        hub.maintain()
        assert (await settled(hub, env, original, command))["state"] == "succeeded"


@run
async def test_navigation_timeout_cancels_native_and_permanently_poison_service(env, monkeypatch):
    import conty.presentations as module

    monkeypatch.setattr(module, "COMMAND_SECONDS", 0.02)
    monkeypatch.setattr(module, "NAVIGATION_SECONDS", 0.02)
    env.adapter.show()
    async with env.hub() as hub:
        original = await deck(hub, env)
        env.adapter.gates["command"] = asyncio.Event()
        command = submit(hub, env, original, "next_slide")
        queued = submit(hub, env, original, "next_slide", request_id="queued")
        result = await settled(hub, env, original, command)
        assert result["state"] == "outcome_unknown"
        assert env.adapter.cancelled == ["command"]
        assert hub.lookup(env.owner.id, original["id"], queued["id"])["state"] == "cancelled"
        assert hub._poisoned and env.adapter.active == 0
        expect_error(
            "executor_unavailable", submit, hub, env, original, "next_slide", request_id="blocked"
        )
        env.clock.now += RETENTION_SECONDS
        hub.maintain()
        assert not hub.commands and hub._poisoned
        with pytest.raises(APIError, match="local recovery"):
            await hub.list_presentations(env.owner.id)
        assert env.adapter.count("command") == 1


@run
async def test_read_timeout_fails_without_claiming_dispatch(env, monkeypatch):
    import conty.presentations as module

    monkeypatch.setattr(module, "REFRESH_SECONDS", 0.02)
    async with env.hub() as hub:
        original = await deck(hub, env)
        env.adapter.gates["observe"] = asyncio.Event()
        command = submit(hub, env, original)
        assert (await settled(hub, env, original, command))["state"] == "failed"
        assert hub._poisoned and env.adapter.count("command") == 0
        assert env.adapter.cancelled == ["observe"]


@pytest.mark.parametrize("operation", ["next_slide", "refresh"])
@run
async def test_uncertain_effects_poison_all_decks_and_refresh_cannot_recover(env, operation):
    if operation != "refresh":
        env.adapter.show()
    async with env.hub() as hub:
        original = await deck(hub, env)
        method = "refresh" if operation == "refresh" else "command"
        env.adapter.errors[method] = AdapterError(
            "execution_failed", PRIVATE, effects_possible=True
        )
        command = submit(hub, env, original, operation)
        result = await settled(hub, env, original, command)
        assert result["state"] == "outcome_unknown" and PRIVATE not in result["message"]
        assert hub._poisoned
        assert all(not d.can_control and not d.previews for d in hub.presentations.values())
        env.adapter.errors.clear()
        expect_error(
            "executor_unavailable", submit, hub, env, original, "refresh", request_id="recovery"
        )
        assert env.adapter.count(method) == 1


@pytest.mark.parametrize(
    "code",
    [
        "target_not_frontmost",
        "unsafe_focus",
        "unsupported_show",
        "unsupported_identity",
        "target_unavailable",
        "permission_denied",
        "custom_secret",
    ],
)
@run
async def test_safe_native_errors_no_paths_no_retries(env, code):
    env.adapter.show()
    async with env.hub() as hub:
        original = await deck(hub, env)
        env.adapter.errors["command"] = AdapterError(code, PRIVATE)
        command = submit(hub, env, original, "show_slide")
        result = await settled(hub, env, original, command)
        assert result["state"] == "failed" and PRIVATE not in json.dumps(result)
        assert env.adapter.count("command") == 1 and not hub._poisoned
        if code == "unsupported_identity":
            assert result["message"] == (
                "This presentation cannot be targeted unambiguously; save it and scan again"
            )
        if code == "unsafe_focus":
            assert "menus and dialogs" in result["message"]
        if code == "target_unavailable":
            assert not hub.presentations[original["id"]].connected


@run
async def test_discovery_permission_error_is_structured_cached_and_no_authorize(env):
    env.adapter.errors["discover"] = AdapterError("permission_denied", PRIVATE)
    async with env.hub() as hub:
        for _ in range(2):
            with pytest.raises(APIError) as caught:
                await hub.list_presentations(env.owner.id)
            assert caught.value.code == "permission_denied"
            assert "authorize" in caught.value.message and PRIVATE not in caught.value.message
        assert env.adapter.count("discover") == 1
        env.clock.now += DISCOVERY_SECONDS
        env.adapter.errors.clear()
        assert await listing(hub, env)


@pytest.mark.parametrize(
    "invalid", ["duplicate", "unknown", "bad_page", "duplicate_page", "not_png", "large_image"]
)
@run
async def test_invalid_export_mapping_and_per_image_limits_are_rejected(env, invalid):
    async with env.hub() as hub:
        original = await deck(hub, env)
        previews = [{"slide_id": "10", "page": 1, "image": PNG}]
        if invalid == "duplicate":
            previews.append(dict(previews[0], page=2))
        elif invalid == "unknown":
            previews[0]["slide_id"] = "unknown"
        elif invalid == "bad_page":
            previews[0]["page"] = True
        elif invalid == "duplicate_page":
            previews.append(dict(previews[0], slide_id="20"))
        elif invalid == "not_png":
            previews[0]["image"] = b"not png"
        else:
            previews[0]["image"] = PNG + b"x" * PNG_LIMIT
        env.adapter.previews = previews
        command = submit(hub, env, original)
        assert (await settled(hub, env, original, command))["state"] == "failed"
        assert hub.preview_bytes == 0 and not hub.presentations[original["id"]].previews


@run
async def test_preview_budgets_replacement_global_and_deck_limits(env):
    env.adapter.decks.append(native_deck("second-key"))
    async with env.hub(max_preview_bytes=6 * len(PNG), max_deck_preview_bytes=3 * len(PNG)) as hub:
        first, second = await listing(hub, env)
        for index, presentation in enumerate((first, second)):
            command = submit(hub, env, presentation, request_id=f"load{index}")
            assert (await settled(hub, env, presentation, command))["state"] == "succeeded"
        assert hub.preview_bytes == 6 * len(PNG)
        first = (await listing(hub, env))[0]
        command = submit(hub, env, first, request_id="replace")
        assert (await settled(hub, env, first, command))["state"] == "succeeded"
        assert hub.preview_bytes == 6 * len(PNG)
        first = (await listing(hub, env))[0]
        env.adapter.previews = [{"slide_id": "10", "page": 1, "image": PNG * 4}]
        command = submit(hub, env, first, request_id="overflow")
        assert (await settled(hub, env, first, command))["state"] == "failed"
        assert hub.preview_bytes == 6 * len(PNG)
        hub.max_preview_bytes = 6 * len(PNG) - 1
        command = submit(hub, env, first, request_id="global-overflow")
        env.adapter.previews = None
        assert (await settled(hub, env, first, command))["state"] == "failed"
        assert hub.preview_bytes == 6 * len(PNG)


@run
async def test_deck_slide_capacity_and_offline_eviction(env):
    async with env.hub(max_presentations=1) as hub:
        original = await deck(hub, env)
        env.adapter.decks = [native_deck("replacement")]
        env.clock.now += DISCOVERY_SECONDS
        replacement = await deck(hub, env)
        assert replacement["id"] != original["id"] and len(hub.presentations) == 1
        env.adapter.decks.append(native_deck("overflow"))
        env.clock.now += DISCOVERY_SECONDS
        with pytest.raises(APIError) as caught:
            await listing(hub, env)
        assert caught.value.code == "limit_exceeded"
        env.adapter.decks = [native_deck()]
        env.adapter.decks[0]["slides"] = [
            dict(SLIDES[0], id=str(i), number=i + 1) for i in range(MAX_SLIDES + 1)
        ]
        env.clock.now += DISCOVERY_SECONDS
        with pytest.raises(APIError) as caught:
            await listing(hub, env)
        assert caught.value.code == "limit_exceeded"


@run
async def test_close_cancels_running_and_queued_tasks_and_closes_adapter(env):
    env.adapter.show()
    hub = PresentationHub(env.sessions, env.adapter)
    original = await deck(hub, env)
    env.adapter.gates["command"] = asyncio.Event()
    submit(hub, env, original, "next_slide")
    submit(hub, env, original, "next_slide", request_id="queued")
    await asyncio.sleep(0)
    await env.adapter.started["command"].wait()
    await asyncio.wait_for(hub.close(), 1)
    assert hub.closed and env.adapter.closed and env.adapter.cancelled == ["command"]
    assert env.adapter.count("command") == 1
    assert hub._worker.done() and not hub.commands and not hub.presentations
    assert hub.preview_bytes == 0
    await hub.close()


@run
async def test_close_during_discovery_cleans_shared_task(env):
    hub = PresentationHub(env.sessions, env.adapter)
    env.adapter.gates["discover"] = asyncio.Event()
    poll = asyncio.create_task(hub.list_presentations(env.owner.id))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.wait_for(hub.close(), 1)
    with pytest.raises((asyncio.CancelledError, APIError)):
        await poll
    assert env.adapter.closed and hub._discovery.done()


@pytest.fixture
def service(env):
    hub = PresentationHub(env.sessions, env.adapter)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await hub.close()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status
        )

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        # Origin/Host/body limits belong to the parent server, not this standalone router.
        if request.url.path.startswith("/api/"):
            try:
                session = env.sessions.authenticate(request.cookies.get("browser"))
                request.state.session = session
                if request.method != "GET" and request.headers.get("x-csrf-token") != session.csrf:
                    raise APIError(403, "invalid_csrf", "Session request token is invalid")
            except APIError as exc:
                return await api_error(request, exc)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(presentation_router(hub))
    with TestClient(app) as client:
        client.cookies.set("browser", env.credential)
        yield client, hub


def http_submit(client, env, presentation, **overrides):
    payload = {
        "request_id": "http-request",
        "revision": presentation["revision"],
        "operation": "refresh",
    }
    payload.update(overrides)
    return client.post(
        f"/api/presentations/{presentation['id']}/commands",
        json=payload,
        headers={"x-csrf-token": env.owner.csrf},
    )


def http_result(client, presentation, command):
    for _ in range(200):
        response = client.get(f"/api/presentations/{presentation['id']}/commands/{command['id']}")
        if response.json()["state"] not in ("queued", "running"):
            return response
        time.sleep(0.002)
    pytest.fail("Fake native command did not settle")


def test_router_explicit_refresh_202_poll_preview_and_private_projection(service, env):
    client, _ = service
    response = client.get("/api/presentations")
    assert response.status_code == 200 and "set-cookie" not in response.headers
    presentation = response.json()["presentations"][0]
    assert env.adapter.count("refresh") == 0
    accepted = http_submit(client, env, presentation)
    assert accepted.status_code == 202
    assert http_result(client, presentation, accepted.json()).json()["state"] == "succeeded"
    updated = client.get("/api/presentations").json()["presentations"][0]
    preview = client.get(updated["slides"][1]["preview_url"])
    assert preview.status_code == 200 and preview.content == PNG
    assert preview.headers["content-type"] == "image/png"
    assert preview.headers["cache-control"] == "no-store"
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in preview.headers
    assert PRIVATE not in json.dumps(updated) and "handle" not in json.dumps(updated)
    assert (
        client.get(
            f"/api/presentations/{updated['id']}/slides/20/preview?revision={presentation['revision']}"
        ).status_code
        == 404
    )
    assert http_submit(client, env, presentation).json()["id"] == accepted.json()["id"]
    assert env.adapter.count("refresh") == 1
    assert client.post("/powerpoint/api/connect", json={}).status_code == 404
    assert client.post("/powerpoint/api/anything/poll", json={}).status_code == 404


def test_router_navigation_accepts_native_operations(service, env):
    client, _ = service
    env.adapter.show()
    presentation = client.get("/api/presentations").json()["presentations"][0]
    for operation in ("show_slide", "next_slide", "previous_slide"):
        kwargs = {"slide_id": "20"} if operation == "show_slide" else {}
        response = http_submit(
            client, env, presentation, operation=operation, request_id=operation, **kwargs
        )
        assert response.status_code == 202
        assert http_result(client, presentation, response.json()).json()["state"] == "succeeded"


def test_router_cookie_auth_csrf_and_result_owner_scope(service, env):
    client, _ = service
    presentation = client.get("/api/presentations").json()["presentations"][0]
    url = f"/api/presentations/{presentation['id']}/commands"
    assert client.post(url, json={}).status_code == 403
    accepted = http_submit(client, env, presentation)
    http_result(client, presentation, accepted.json())
    client.cookies.set("browser", env.other_credential)
    assert client.get(f"{url}/{accepted.json()['id']}").status_code == 404
    client.cookies.clear()
    assert client.get("/api/presentations").status_code == 401
    assert env.adapter.count("discover") == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"operation": "refresh", "slide_id": None},
        {"operation": "next_slide", "slide_id": "10"},
        {"operation": "previous_slide", "slide_id": None},
        {"operation": "show_slide"},
        {"operation": "launch"},
        {"operation": "refresh", "extra": PRIVATE},
        {"request_id": 123},
        {"revision": "bad?revision"},
    ],
)
def test_router_strict_command_validation_redacts_input(service, env, extra):
    client, _ = service
    presentation = client.get("/api/presentations").json()["presentations"][0]
    response = http_submit(client, env, presentation, **extra)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Request fields are invalid"}
    }
    assert env.adapter.count("refresh") == 0 and env.adapter.count("command") == 0


def test_router_malformed_json_and_revision_queries_are_redacted(service, env):
    client, _ = service
    presentation = client.get("/api/presentations").json()["presentations"][0]
    response = client.post(
        f"/api/presentations/{presentation['id']}/commands",
        content=PRIVATE,
        headers={"x-csrf-token": env.owner.csrf, "content-type": "application/json"},
    )
    assert response.status_code == 422 and PRIVATE not in response.text
    for query in ("", "?revision=", "?revision=a&revision=b", "?revision=bad%3F"):
        response = client.get(f"/api/presentations/{presentation['id']}/slides/10/preview{query}")
        assert response.status_code == 422 and PRIVATE not in response.text


def test_router_without_host_session_state_fails_closed(env):
    app = FastAPI()
    hub = PresentationHub(env.sessions, env.adapter)

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)

    app.include_router(presentation_router(hub))
    with TestClient(app) as client:
        assert client.get("/api/presentations").status_code == 401
        assert client.post("/api/presentations/unknown/commands", json={}).status_code == 401
        assert (
            client.get("/api/presentations/unknown/slides/10/preview?revision=a").status_code == 401
        )
    assert not env.adapter.calls


@pytest.mark.parametrize(
    "kwargs", [{"max_commands": 0}, {"max_pending": True}, {"max_preview_bytes": -1}]
)
def test_invalid_capacity_configuration(env, kwargs):
    with pytest.raises(ValueError):
        PresentationHub(env.sessions, env.adapter, **kwargs)


def test_command_model_rejects_target_on_non_show_operations():
    with pytest.raises(ValidationError):
        CommandInput(request_id="r", revision="v", operation="refresh", slide_id=None)
