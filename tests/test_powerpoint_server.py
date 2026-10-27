"""Native PowerPoint through the real HTTP boundary; no desktop or listening socket.

Reuse the hub tests' native observations and PNGs, not their standalone router app.
Native locking/coalescing, PDF validation, and optional TLS/CLI setup are tested elsewhere.
"""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import Clock, FakeExecutor
from fastapi.testclient import TestClient
from test_presentations import PNG, PRIVATE, SLIDES
from test_presentations import FakePowerPoint as FakeNativeAdapter

from conty.adapters.base import AdapterError
from conty.auth import Sessions
from conty.config import freeze
from conty.presentations import DISCOVERY_SECONDS
from conty.server import BODY_LIMIT, COOKIE, create_app

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def service(snapshot, fake_executor):
    sessions = Sessions(clock=Clock(), output=lambda _: None)
    native = FakeNativeAdapter()
    app = create_app(snapshot, executor=fake_executor, sessions=sessions, powerpoint=native)
    with TestClient(app, base_url=BASE) as client:
        yield app, client, native
    assert native.closed
    assert not fake_executor.calls


class LockedKeyboardExecutor(FakeExecutor):
    """Model the real executor's mutation/close lock, without accessing the desktop."""

    def __init__(self, native):
        super().__init__()
        self._lock = asyncio.Lock()
        self.native = native

    async def execute(self, definition):
        async with self._lock:
            assert definition["type"] == "keyboard"
            return await super().execute(definition)

    async def close(self):
        async with self._lock:
            assert self.native.closed and self.native.active == 0
            await super().close()


def security(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    directives = dict(
        directive.strip().split(" ", 1)
        for directive in response.headers["content-security-policy"].split(";")
    )
    assert directives == {
        "default-src": "'self'",
        "script-src": "'self'",
        "style-src": "'self'",
        "connect-src": "'self'",
        "img-src": "'self'",
        "object-src": "'none'",
        "base-uri": "'none'",
        "frame-ancestors": "'none'",
        "form-action": "'self'",
    }


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert len(response.json()["error"]["correlation_id"]) == 16
    assert PRIVATE not in response.text
    security(response)
    return response.json()["error"]


def pair(app, client):
    response = client.post(
        "/api/pair", json={"code": app.state.sessions.code}, headers={"origin": BASE}
    )
    assert response.status_code == 200, response.text
    security(response)
    return {"origin": BASE, "x-csrf-token": response.json()["csrf_token"]}


def presentation(client):
    response = client.get("/api/presentations")
    assert response.status_code == 200, response.text
    security(response)
    return response.json()["presentations"][0]


def commands(deck):
    return f"/api/presentations/{deck['id']}/commands"


def payload(deck, **overrides):
    return {
        "request_id": "phone-request",
        "revision": deck["revision"],
        "operation": "refresh",
        **overrides,
    }


def submit(client, deck, headers, **overrides):
    return client.post(commands(deck), json=payload(deck, **overrides), headers=headers)


def settled(app, client, deck, accepted):
    assert accepted.status_code == 202, accepted.text
    security(accepted)

    async def join_worker():
        await asyncio.wait_for(asyncio.shield(app.state.presentations._worker), 2)

    # Join on TestClient's event loop instead of timing-dependent HTTP polling.
    client.portal.call(join_worker)
    response = client.get(f"{commands(deck)}/{accepted.json()['id']}")
    assert response.status_code == 200, response.text
    security(response)
    assert response.json()["state"] not in ("queued", "running")
    return response.json()


def wait_native(client, native, method):
    async def started():
        event = native.started.setdefault(method, asyncio.Event())
        await asyncio.wait_for(event.wait(), 2)

    client.portal.call(started)


def loaded(app, client, headers):
    deck = presentation(client)
    result = settled(app, client, deck, submit(client, deck, headers))
    assert result["state"] == "succeeded"
    return presentation(client)


@pytest.mark.parametrize("method,elapsed", [("discover", 6), ("observe", 6), ("refresh", 45)])
def test_real_shared_executor_lock_keeps_keyboard_responsive_during_native_reads(
    snapshot, method, elapsed
):
    snapshot = replace(
        snapshot,
        actions=freeze({"hello": {"type": "keyboard", "key": "right", "timeout_seconds": 5}}),
    )
    native = FakeNativeAdapter()
    executor = LockedKeyboardExecutor(native)
    sessions = Sessions(clock=Clock(), output=lambda _: None)
    app = create_app(snapshot, executor=executor, sessions=sessions, powerpoint=native)
    with TestClient(app, base_url=BASE) as client:
        headers = pair(app, client)
        deck = presentation(client) if method != "discover" else None

        async def scenario():
            gate = native.gates[method] = asyncio.Event()
            started = native.started[method] = asyncio.Event()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE, cookies=client.cookies
            ) as phone:
                if method == "discover":
                    work = asyncio.create_task(phone.get("/api/presentations"))
                else:
                    accepted = await phone.post(commands(deck), json=payload(deck), headers=headers)
                    assert accepted.status_code == 202
                    work = app.state.presentations._worker
                await asyncio.wait_for(started.wait(), 0.5)
                sessions.clock.now += elapsed
                native_work = app.state.presentations._discovery if method == "discover" else work
                try:
                    # Previous/Next/Blackout all dispatch through this same executor lane.
                    response = await asyncio.wait_for(
                        phone.post(
                            "/api/invocations?wait=0.25",
                            json={
                                "request_id": "keyboard-during-read",
                                "action_id": "hello",
                                "revision": snapshot.revision,
                            },
                            headers=headers,
                        ),
                        0.5,
                    )
                    assert response.status_code == 202, response.text
                    assert response.json()["state"] == "succeeded"
                    assert len(executor.calls) == 1 and executor.available
                    assert not gate.is_set() and not native_work.done()
                    assert native.count("command") == 0
                    status = await asyncio.wait_for(phone.get("/api/status"), 0.5)
                    assert status.status_code == 200
                finally:
                    gate.set()
                    await asyncio.wait_for(native_work, 2)
                    await asyncio.wait_for(work, 2)
                if method != "discover":
                    result = await phone.get(f"{commands(deck)}/{accepted.json()['id']}")
                    assert result.json()["state"] == "succeeded"
                assert not app.state.presentations._poisoned

        client.portal.call(scenario)
    assert native.closed and executor.closed and native.active == 0


@pytest.mark.parametrize("method", ["discover", "observe", "refresh"])
def test_native_read_timeout_does_not_poison_shared_keyboard_executor(
    snapshot, monkeypatch, method
):
    import conty.presentations as module

    monkeypatch.setattr(module, "READ_SECONDS", 0.05)
    monkeypatch.setattr(module, "REFRESH_SECONDS", 0.05)
    snapshot = replace(
        snapshot,
        actions=freeze({"hello": {"type": "keyboard", "key": "right", "timeout_seconds": 5}}),
    )
    native = FakeNativeAdapter()
    executor = LockedKeyboardExecutor(native)
    app = create_app(snapshot, executor=executor, powerpoint=native, owner_output=lambda _: None)
    with TestClient(app, base_url=BASE) as client:
        headers = pair(app, client)
        deck = presentation(client) if method != "discover" else None
        native.gates[method] = asyncio.Event()
        if method == "discover":
            assert client.get("/api/presentations").status_code == 503
        else:
            result = settled(app, client, deck, submit(client, deck, headers))
            assert result["state"] == ("outcome_unknown" if method == "refresh" else "failed")
        assert native.cancelled == [method] and app.state.presentations._poisoned
        response = client.post(
            "/api/invocations?wait=0.25",
            json={
                "request_id": "keyboard-after-read-timeout",
                "action_id": "hello",
                "revision": snapshot.revision,
            },
            headers=headers,
        )
        assert response.status_code == 202 and response.json()["state"] == "succeeded"
        assert len(executor.calls) == 1 and executor.available
        assert app.state.coordinator.available and not executor._lock.locked()
    assert native.closed and executor.closed


@pytest.mark.parametrize("cached", [False, True])
def test_slow_http_discovery_returns_bounded_coalesced_pending_then_populates(service, cached):
    app, client, native = service
    pair(app, client)
    previous = [presentation(client)] if cached else []
    native.decks[0]["title"] = "Latest metadata.pptx"
    app.state.sessions.clock.now += 15
    hub = app.state.presentations
    discovered_at = hub._discovered_at
    calls_before = native.count("discover")

    async def scenario():
        gate = native.gates["discover"] = asyncio.Event()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE, cookies=client.cookies
        ) as phone:
            for _ in range(3):
                response = await asyncio.wait_for(phone.get("/api/presentations"), 1)
                assert response.status_code == 200, response.text
                security(response)
                assert response.json() == {"presentations": previous, "discovering": True}
                assert PRIVATE not in response.text and "private-key" not in response.text
                assert not gate.is_set() and not hub._discovery.done()
                assert hub._discovered_at == discovered_at and not hub._poisoned
                assert native.count("discover") == calls_before + 1
                assert native.count("refresh") == native.count("command") == 0
                status = await asyncio.wait_for(phone.get("/api/status"), 0.5)
                assert status.status_code == 200
                app.state.sessions.clock.now += 5
            assert not native.cancelled
            gate.set()
            await asyncio.wait_for(asyncio.shield(hub._discovery), 0.5)
            response = await asyncio.wait_for(phone.get("/api/presentations"), 1)
            assert response.status_code == 200 and not response.json().get("discovering", False)
            found = response.json()["presentations"]
            assert len(found) == 1 and found[0]["title"] == "Latest metadata.pptx"
            if cached:
                assert found[0]["id"] == previous[0]["id"]
            assert native.count("discover") == calls_before + 1

    client.portal.call(scenario)


def test_slow_http_discovery_reports_structured_error_then_rejects_revoked_owner(service):
    app, client, native = service
    pair(app, client)
    native.gates["discover"] = asyncio.Event()
    native.errors["discover"] = AdapterError("permission_denied", PRIVATE)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE, cookies=client.cookies
        ) as phone:
            response = await asyncio.wait_for(phone.get("/api/presentations"), 1)
            assert response.status_code == 200
            assert response.json() == {"presentations": [], "discovering": True}
            native.gates["discover"].set()
            await asyncio.wait_for(asyncio.shield(app.state.presentations._discovery), 0.5)
            error(await phone.get("/api/presentations"), 503, "permission_denied")
            app.state.sessions.records.clear()
            error(await phone.get("/api/presentations"), 401, "pairing_required")
            assert [call[0] for call in native.calls] == ["discover"]
            assert not native.cancelled and not app.state.presentations._poisoned

    client.portal.call(scenario)


@pytest.mark.parametrize("completion", ["pending", "success", "error"])
def test_slow_http_discovery_rechecks_session_before_returning(service, completion):
    app, client, native = service
    pair(app, client)

    async def scenario():
        gate = native.gates["discover"] = asyncio.Event()
        started = native.started["discover"] = asyncio.Event()
        if completion == "error":
            native.errors["discover"] = AdapterError("permission_denied", PRIVATE)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE, cookies=client.cookies
        ) as phone:
            request = asyncio.create_task(phone.get("/api/presentations"))
            await asyncio.wait_for(started.wait(), 0.5)
            app.state.sessions.records.clear()
            if completion != "pending":
                gate.set()
            error(await asyncio.wait_for(request, 1), 401, "pairing_required")
            assert not native.cancelled
            gate.set()
            await asyncio.wait_for(asyncio.shield(app.state.presentations._discovery), 0.5)
            assert native.count("discover") == 1

    client.portal.call(scenario)


def test_pairing_and_discovery_are_read_only_and_private(service, snapshot):
    app, client, native = service
    for method, path in (
        ("GET", "/api/presentations"),
        ("GET", "/api/presentations/unknown/commands/unknown"),
        ("GET", "/api/presentations/unknown/slides/20/preview?revision=unknown"),
        ("POST", "/api/presentations/unknown/commands"),
    ):
        response = client.request(method, path, headers={"origin": BASE}, json={})
        error(response, 401, "pairing_required")
    assert not native.calls
    pair(app, client)
    assert not native.calls
    response = client.get("/api/presentations")
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    deck = response.json()["presentations"][0]
    assert deck["title"] == "Slides.pptx"
    assert deck["id"] != native.decks[0]["key"]
    assert deck["revision"] and deck["connected"]
    assert deck["view"] == "edit" and deck["current_slide_id"] is None
    assert deck["can_control"] is False and deck["needs_refresh"] is True
    assert deck["preview_count"] == 0
    assert deck["slides"] == [dict(slide, page=None, preview_url=None) for slide in SLIDES]
    assert presentation(client) == deck
    for secret in (
        PRIVATE,
        "private-key",
        "handle",
        "private_path",
        "fullname",
        str(snapshot.source),
        "PRIVATE_ARGUMENT",
    ):
        assert secret not in response.text
    assert [call[0] for call in native.calls] == ["discover"]
    security(response)


def test_refresh_returns_202_without_waiting_for_native_and_can_be_polled(service):
    app, client, native = service
    headers = pair(app, client)
    deck = presentation(client)
    native.gates["refresh"] = asyncio.Event()
    response = client.post(commands(deck) + "?wait=0.25", json=payload(deck), headers=headers)
    assert response.status_code == 202
    assert response.json()["state"] == "queued"
    wait_native(client, native, "refresh")
    assert not native.gates["refresh"].is_set()
    status = client.get(f"{commands(deck)}/{response.json()['id']}")
    assert status.status_code == 200 and status.json()["state"] == "running"
    assert client.get("/api/status").status_code == 200
    duplicate = submit(client, deck, headers)
    assert duplicate.status_code == 202 and duplicate.json()["id"] == response.json()["id"]
    client.portal.call(native.gates["refresh"].set)
    result = settled(app, client, deck, response)
    assert result["state"] == "succeeded"
    assert native.count("observe") == native.count("refresh") == 1
    assert native.calls[-1][1] == native.decks[0]["handle"]
    assert PRIVATE not in json.dumps(result)


def test_png_is_cookie_protected_revision_bound_and_maps_hidden_slide_to_explicit_page(service):
    app, client, native = service
    headers = pair(app, client)
    before = presentation(client)
    # Fake PDF page order deliberately differs from slide number and export list order.
    native.previews = [
        {"slide_id": "30", "page": 2, "image": PNG},
        {"slide_id": "20", "page": 3, "image": PNG},
        {"slide_id": "10", "page": 1, "image": PNG},
    ]
    deck = loaded(app, client, headers)
    assert deck["revision"] != before["revision"]
    assert deck["preview_count"] == 3 and not deck["needs_refresh"]
    assert [slide["page"] for slide in deck["slides"]] == [1, 3, 2]
    hidden = deck["slides"][1]
    assert hidden["id"] == "20" and hidden["hidden"] is True
    url = hidden["preview_url"]
    assert url == (f"/api/presentations/{deck['id']}/slides/20/preview?revision={deck['revision']}")
    response = client.get(url)
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert "set-cookie" not in response.headers
    security(response)
    old_url = url.replace(deck["revision"], before["revision"])
    error(client.get(old_url), 404, "preview_not_found")
    error(client.get(url.replace("/slides/20/", "/slides/unknown/")), 404, "preview_not_found")
    cookie = client.cookies.get(COOKIE)
    client.cookies.clear()
    error(client.get(url), 401, "pairing_required")
    error(
        client.get(url, headers={"authorization": f"Bearer {cookie}"}),
        401,
        "pairing_required",
    )
    assert native.count("refresh") == 1 and native.count("command") == 0


def test_command_dedupe_conflicts_and_result_ownership_use_real_sessions(service):
    app, client, native = service
    native.show()
    headers = pair(app, client)
    deck = presentation(client)
    args = {"operation": "show_slide", "slide_id": "20"}
    accepted = submit(client, deck, headers, **args)
    result = settled(app, client, deck, accepted)
    assert result["state"] == "succeeded"
    duplicate = submit(client, deck, headers, **args)
    assert duplicate.status_code == 202 and duplicate.json() == result
    for changed in (
        {"revision": "different"},
        {"operation": "next_slide", "slide_id": None},
        {"slide_id": "30"},
    ):
        data = payload(deck, **(args | changed))
        if data["operation"] == "next_slide":
            data.pop("slide_id")
        error(client.post(commands(deck), json=data, headers=headers), 409, "request_conflict")
    error(
        client.post(
            "/api/presentations/other/commands", json=payload(deck, **args), headers=headers
        ),
        409,
        "request_conflict",
    )
    assert native.count("command") == 1
    other_headers = pair(app, client)
    owned = client.get(f"{commands(deck)}/{result['id']}")
    missing = client.get(f"{commands(deck)}/missing")
    owned_error = error(owned, 404, "command_not_found")
    missing_error = error(missing, 404, "command_not_found")
    assert owned_error["message"] == missing_error["message"]
    other = submit(client, deck, other_headers, **args)
    assert other.json()["id"] != result["id"]
    assert settled(app, client, deck, other)["state"] == "succeeded"
    assert native.count("command") == 2


@pytest.mark.parametrize("invalid_session", ["expired", "revoked"])
def test_invalid_session_denies_listing_preview_results_and_new_work(service, invalid_session):
    app, client, native = service
    headers = pair(app, client)
    deck = loaded(app, client, headers)
    command_id = next(iter(app.state.presentations.commands))
    calls = deepcopy(native.calls)
    if invalid_session == "expired":
        app.state.sessions.clock.now += 8 * 3600
    else:
        app.state.sessions.records.clear()
    for path in (
        "/api/presentations",
        deck["slides"][0]["preview_url"],
        f"{commands(deck)}/{command_id}",
    ):
        error(client.get(path), 401, "pairing_required")
    error(submit(client, deck, headers, request_id="new"), 401, "pairing_required")
    assert native.calls == calls


@pytest.mark.parametrize(
    "overrides,status,code",
    [
        ({"origin": None}, 403, "invalid_origin"),
        ({"origin": "null"}, 403, "invalid_origin"),
        ({"origin": "http://evil.example"}, 403, "invalid_origin"),
        ({"origin": BASE.replace("http:", "https:")}, 403, "invalid_origin"),
        ({"origin": "http://localhost:8765"}, 403, "invalid_origin"),
        ({"origin": [BASE, BASE]}, 403, "invalid_origin"),
        ({"host": "evil.example:8765"}, 400, "invalid_host"),
        ({"host": ["127.0.0.1:8765", "127.0.0.1:8765"]}, 400, "invalid_host"),
        ({"x-csrf-token": None}, 403, "invalid_csrf"),
        ({"x-csrf-token": "wrong"}, 403, "invalid_csrf"),
        ({"x-csrf-token": "x" * 129}, 403, "invalid_csrf"),
        ({"x-csrf-token": ["TOKEN", "TOKEN"]}, 403, "invalid_csrf"),
        ({"content-type": "text/plain"}, 415, "json_required"),
        ({"content-encoding": "gzip"}, 415, "unsupported_encoding"),
    ],
)
def test_command_boundary_refuses_browser_header_ambiguity(service, overrides, status, code):
    app, client, native = service
    valid = pair(app, client)
    deck = presentation(client)
    headers = {"content-type": "application/json", **valid, **overrides}
    pairs = [
        (key, valid["x-csrf-token"] if item == "TOKEN" else item)
        for key, value in headers.items()
        if value is not None
        for item in (value if isinstance(value, list) else [value])
    ]
    error(
        client.post(commands(deck), content=json.dumps(payload(deck)), headers=pairs), status, code
    )
    assert [call[0] for call in native.calls] == ["discover"]
    assert not app.state.presentations.commands


@pytest.mark.parametrize(
    "extra",
    [
        {"operation": "launch"},
        {"operation": "open"},
        {"operation": "authorize"},
        {"operation": "export"},
        {"operation": "load"},
        {"path": PRIVATE},
        {"file": PRIVATE},
        {"handle": {"fullname": PRIVATE}},
        {"argv": [PRIVATE]},
        {"script": PRIVATE},
        {"operation": "show_slide", "slide_id": PRIVATE},
        {"operation": "show_slide"},
        {"operation": "next_slide", "slide_id": "20"},
        {"operation": "refresh", "slide_id": None},
        {"request_id": 12},
        {"revision": "bad?revision"},
    ],
)
def test_unknown_operations_and_path_arguments_never_reach_native(service, extra):
    app, client, native = service
    headers = pair(app, client)
    deck = presentation(client)
    error(submit(client, deck, headers, **extra), 422, "invalid_request")
    assert not app.state.presentations.commands
    assert [call[0] for call in native.calls] == ["discover"]


def test_malformed_json_and_ambiguous_preview_revisions_are_redacted(service):
    app, client, native = service
    headers = pair(app, client)
    deck = presentation(client)
    error(
        client.post(
            commands(deck),
            content=PRIVATE,
            headers={
                **headers,
                "content-type": "application/json",
            },
        ),
        422,
        "invalid_request",
    )
    for query in ("", "?revision=", "?revision=a&revision=b", "?revision=bad%3F"):
        error(
            client.get(f"/api/presentations/{deck['id']}/slides/20/preview{query}"),
            422,
            "invalid_request",
        )
    assert native.count("refresh") == native.count("command") == 0


def test_stale_revision_and_changed_slide_mapping_invalidate_previews(service):
    app, client, native = service
    headers = pair(app, client)
    before = presentation(client)
    deck = loaded(app, client, headers)
    error(submit(client, before, headers, request_id="stale"), 409, "stale_revision")
    old_url = deck["slides"][1]["preview_url"]
    native.decks[0]["slides"] = [deepcopy(SLIDES[index]) for index in (1, 0, 2)]
    for number, slide in enumerate(native.decks[0]["slides"], 1):
        slide["number"] = number
    app.state.sessions.clock.now += DISCOVERY_SECONDS
    changed = presentation(client)
    assert changed["revision"] != deck["revision"]
    assert changed["preview_count"] == 0 and changed["needs_refresh"]
    assert all(s["preview_url"] is None and s["page"] is None for s in changed["slides"])
    error(client.get(old_url), 404, "preview_not_found")
    error(submit(client, deck, headers, request_id="stale-mapping"), 409, "stale_revision")
    assert native.count("refresh") == 1


@pytest.mark.parametrize("change", ["identity", "order", "numbers"])
def test_native_guard_rejects_changed_target_and_invalidates_previews_without_input(
    service, change
):
    app, client, native = service
    headers = pair(app, client)
    loaded(app, client, headers)
    native.show()
    app.state.sessions.clock.now += DISCOVERY_SECONDS
    deck = presentation(client)
    old_url = deck["slides"][1]["preview_url"]
    assert old_url is not None
    observations = native.count("observe")
    if change == "identity":
        native.decks[0]["handle"] = {"key": "replacement"}
    elif change == "order":
        native.decks[0]["slides"].reverse()
    else:
        native.decks[0]["slides"][1]["number"] = 99
    accepted = submit(
        client, deck, headers, operation="show_slide", slide_id="20", request_id="native-guard"
    )
    result = settled(app, client, deck, accepted)
    assert result["state"] == "failed" and PRIVATE not in result["message"]
    assert native.count("command") == 1 and not native.inputs
    assert native.count("observe") == observations
    assert native.count("refresh") == 1 and not app.state.presentations._poisoned
    error(client.get(old_url), 404, "preview_not_found")
    changed = presentation(client)
    assert changed["revision"] != deck["revision"] and changed["preview_count"] == 0


@pytest.mark.parametrize("operation", ["next_slide", "previous_slide"])
@pytest.mark.parametrize("phase", ["admission", "native_guard"])
def test_next_previous_refuse_missing_focus_including_native_guard(service, operation, phase):
    app, client, native = service
    native.show(can_control=phase == "native_guard")
    headers = pair(app, client)
    deck = presentation(client)
    native.show(can_control=False)
    response = submit(client, deck, headers, operation=operation)
    if phase == "admission":
        error(response, 409, "control_unavailable")
        assert native.count("observe") == 0
    else:
        assert settled(app, client, deck, response)["state"] == "failed"
        assert presentation(client)["can_control"] is False
        assert native.count("command") == 1
    assert native.count("observe") == native.count("refresh") == 0
    assert not native.inputs and not app.state.presentations._poisoned


@pytest.mark.parametrize(
    "operation,observed",
    [("next_slide", "10"), ("previous_slide", "30"), ("show_slide", None)],
)
def test_navigation_current_slide_is_native_observation_not_requested_or_inferred(
    service,
    operation,
    observed,
):
    app, client, native = service
    native.show(current="10")
    headers = pair(app, client)
    deck = presentation(client)
    native.navigation_result = {"current_slide_id": observed}
    target = {"slide_id": "20"} if operation == "show_slide" else {}
    accepted = submit(client, deck, headers, operation=operation, **target)
    assert settled(app, client, deck, accepted)["state"] == "succeeded"
    assert presentation(client)["current_slide_id"] == observed
    method, handle, arguments = native.calls[-1]
    assert method == "command" and handle == native.decks[0]["handle"]
    assert arguments == {
        "operation": operation,
        "slide_id": target.get("slide_id"),
        "expected_slides": SLIDES,
    }
    assert native.count("command") == len(native.inputs) == 1
    assert native.count("observe") == native.count("refresh") == 0


def test_listing_observes_external_slideshow_changes_without_navigation_or_export(service):
    app, client, native = service
    native.show(current="10")
    pair(app, client)
    before = presentation(client)
    native.show(current="30", can_control=False)
    app.state.sessions.clock.now += DISCOVERY_SECONDS
    observed = presentation(client)
    assert observed["current_slide_id"] == "30" and observed["can_control"] is False
    assert observed["id"] == before["id"] and observed["revision"] == before["revision"]
    assert [call[0] for call in native.calls] == ["discover", "discover"]


# Includes public assets, authenticated reads/writes, and former large-body exceptions.
@pytest.mark.parametrize(
    "method,path,status",
    [
        ("GET", "/", 200),
        ("GET", "/assets/presentations.js", 200),
        ("GET", "/api/presentations", 200),
        ("GET", "/api/status", 200),
        ("GET", "/api/presentations/{pid}/commands/missing", 404),
        ("GET", "/api/presentations/{pid}/slides/20/preview?revision={revision}", 404),
        ("POST", "/api/presentations/{pid}/commands", 422),
        ("POST", "/api/pair", 422),
        ("GET", "/powerpoint/manifest.xml", 404),
        ("GET", "/powerpoint/taskpane.html", 404),
        ("POST", "/powerpoint/api/connect", 404),
        ("POST", "/powerpoint/api/{pid}/slides", 404),
        ("POST", "/powerpoint/api/{pid}/slides/20/preview", 404),
        ("POST", "/powerpoint/api/connect/nearby", 404),
        ("GET", "/missing", 404),
    ],
)
def test_exact_4096_byte_limit_is_global_without_legacy_body_exemptions(
    service,
    method,
    path,
    status,
):
    app, client, native = service
    headers = pair(app, client) | {"content-type": "application/json"}
    deck = presentation(client)
    path = path.format(pid=deck["id"], revision=deck["revision"])
    assert BODY_LIMIT == 4096
    bounded = b"{}" + b" " * (4096 - 2)
    response = client.request(method, path, content=bounded, headers=headers)
    assert response.status_code == status, response.text
    security(response)
    for content, extra in ((bounded + b" ", {}), (b"{}", {"content-length": "4097"})):
        error(
            client.request(method, path, content=content, headers=headers | extra),
            413,
            "request_too_large",
        )
    assert [call[0] for call in native.calls] == ["discover"]
    assert not app.state.presentations.commands


@pytest.mark.parametrize("declared_length", [None, "0", "4096"])
@pytest.mark.parametrize("size", [4096, 4097])
@pytest.mark.parametrize("legacy", [False, True])
def test_streamed_bodies_cannot_bypass_bounds_or_lie_about_length(
    service,
    declared_length,
    size,
    legacy,
):
    app, client, native = service
    headers = pair(app, client) | {"content-type": "application/json"}
    deck = presentation(client)
    data = json.dumps(payload(deck)).encode()
    data += b" " * (size - len(data))
    if declared_length is not None:
        headers["content-length"] = declared_length
    path = "/powerpoint/api/connect" if legacy else commands(deck)
    credential = client.cookies.get(COOKIE)

    async def scenario():
        async def chunks():
            yield data[:2048]
            await asyncio.sleep(0)
            yield data[2048:]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE,
            cookies={COOKIE: credential},
        ) as streamed:
            return await streamed.post(path, content=chunks(), headers=headers)

    response = client.portal.call(scenario)
    if size > 4096:
        error(response, 413, "request_too_large")
    elif legacy:
        assert response.status_code == 404
        security(response)
    else:
        assert settled(app, client, deck, response)["state"] == "succeeded"
        assert native.count("refresh") == 1
        return
    assert not app.state.presentations.commands
    assert [call[0] for call in native.calls] == ["discover"]


@pytest.mark.parametrize(
    "path",
    [
        "/powerpoint/manifest.xml",
        "/powerpoint/taskpane.html",
        "/powerpoint/taskpane.js",
        "/powerpoint/taskpane.css",
        "/powerpoint/assets/taskpane.js",
        "/powerpoint/api",
        "/powerpoint/api/connect",
        "/powerpoint/api/deck/poll",
        "/powerpoint/api/deck/slides",
        "/powerpoint/api/deck/commands/command/result",
    ],
)
def test_old_addin_routes_are_gone_and_never_relax_global_csp(service, path):
    app, client, native = service
    for method in ("GET", "POST"):
        response = client.request(method, path, json={}, headers={"origin": BASE})
        assert response.status_code == 404, response.text
        assert "set-cookie" not in response.headers
        security(response)
    assert not native.calls


@pytest.mark.parametrize("path", ["/", "/assets/presentations.js", "/api/presentations"])
def test_host_checks_and_strict_csp_apply_to_shell_assets_and_native_api(service, path):
    app, client, _ = service
    pair(app, client)
    response = client.get(path)
    assert response.status_code == 200
    security(response)
    error(client.get(path, headers={"host": "evil.example:8765"}), 400, "invalid_host")


@pytest.mark.parametrize("code,status", [("permission_denied", 503), ("unsupported_identity", 409)])
def test_native_errors_are_redacted_without_permission_prompt_or_export(service, code, status):
    app, client, native = service
    native.errors["discover"] = AdapterError(code, PRIVATE)
    pair(app, client)
    result = error(client.get("/api/presentations"), status, code)
    if code == "unsupported_identity":
        assert result["message"] == (
            "This presentation cannot be targeted unambiguously; save it and scan again"
        )
    assert [call[0] for call in native.calls] == ["discover"]


def test_unexpected_parent_boundary_errors_are_redacted_and_keep_security_headers(
    service, monkeypatch
):
    app, client, _ = service
    pair(app, client)

    def broken_authenticate(_):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(app.state.sessions, "authenticate", broken_authenticate)
    error(client.get("/api/presentations"), 500, "internal_error")


def test_periodic_maintenance_never_performs_native_io_even_after_discovery_expires(
    service,
    monkeypatch,
):
    app, client, native = service
    pair(app, client)
    presentation(client)
    calls = deepcopy(native.calls)
    hub = app.state.presentations
    maintained = asyncio.Event()
    original = hub.maintain

    def maintain():
        original()
        maintained.set()

    monkeypatch.setattr(hub, "maintain", maintain)
    app.state.sessions.clock.now += DISCOVERY_SECONDS + 1
    client.portal.call(asyncio.wait_for, maintained.wait(), 3)
    assert native.calls == calls
    assert native.count("refresh") == native.count("command") == 0


@pytest.mark.parametrize("method", ["refresh", "command"])
def test_shutdown_awaits_hub_and_native_cleanup_and_joins_pending_work_without_warnings(
    snapshot,
    monkeypatch,
    recwarn,
    caplog,
    method,
):
    native = FakeNativeAdapter()
    if method == "command":
        native.show()
    fake_executor = LockedKeyboardExecutor(native)
    native.gates[method] = asyncio.Event()
    operation = "refresh" if method == "refresh" else "next_slide"
    cleanup_finished = False

    async def cleanup():
        nonlocal cleanup_finished
        await asyncio.sleep(0.01)
        await FakeNativeAdapter.close(native)
        cleanup_finished = True

    native.close = AsyncMock(side_effect=cleanup)
    app = create_app(
        snapshot, executor=fake_executor, powerpoint=native, owner_output=lambda _: None
    )
    hub = app.state.presentations
    close = AsyncMock(wraps=hub.close)
    monkeypatch.setattr(hub, "close", close)
    with TestClient(app, base_url=BASE) as client:
        headers = pair(app, client)
        deck = presentation(client)
        accepted = submit(client, deck, headers, operation=operation)
        assert accepted.status_code == 202
        wait_native(client, native, method)
        assert fake_executor._lock.locked() == (method == "command")
        queued = submit(client, deck, headers, operation=operation, request_id="queued")
        assert queued.status_code == 202 and queued.json()["state"] == "queued"
        assert native.count(method) == 1
        worker, discovery = hub._worker, hub._discovery
    close.assert_awaited_once_with()
    native.close.assert_awaited_once_with()
    assert cleanup_finished and native.closed and fake_executor.closed
    assert method in native.cancelled and native.active == 0
    assert not fake_executor._lock.locked()
    assert worker.done() and discovery.done()
    assert hub.closed and not hub.presentations and not hub.commands and not hub.dedupe
    assert not hub._queue and hub.preview_bytes == 0
    assert not app.state.sessions.records
    assert not recwarn.list
    assert not any(
        text in caplog.text.lower()
        for text in ("never awaited", "task was destroyed", "exception was never retrieved")
    )
