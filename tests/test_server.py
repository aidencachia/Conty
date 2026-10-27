import asyncio
import json
import sys
import time
from dataclasses import replace

import httpx
import pytest
from conftest import Clock
from fastapi.testclient import TestClient

from conty.adapters.base import AdapterError
from conty.auth import Sessions
from conty.config import freeze
from conty.executor import Executor
from conty.server import COOKIE, authorities, connection_urls, create_app

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def service(snapshot, fake_executor):
    app = create_app(snapshot, executor=fake_executor, owner_output=lambda _: None)
    with TestClient(app, base_url=BASE) as client:
        yield app, client


def pair(app, client):
    response = client.post(
        "/api/pair", json={"code": app.state.sessions.code}, headers={"origin": BASE}
    )
    assert response.status_code == 200, response.text
    return {"origin": BASE, "x-csrf-token": response.json()["csrf_token"]}


def submit(client, snapshot, headers, **overrides):
    return client.post(
        "/api/invocations",
        json={
            "request_id": "request1",
            "action_id": "hello",
            "revision": snapshot.revision,
            **overrides,
        },
        headers=headers,
    )


def test_public_shell_has_no_dashboard_and_unpaired_api_is_denied(service):
    app, client = service
    response = client.get("/")
    assert response.status_code == 200
    assert "PRIVATE_ARGUMENT" not in response.text and "Second" not in response.text
    assert "Content-Security-Policy" in response.headers
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/style.css").status_code == 200
    for path in ("/api/dashboard", "/api/status", "/api/invocations/missing"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "pairing_required"
    assert client.post("/api/invocations", json={}, headers={"origin": BASE}).status_code == 401
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_pair_cookie_csrf_and_public_projection(service, snapshot):
    app, client = service
    headers = pair(app, client)
    cookie = client.cookies.get(COOKIE)
    assert cookie and cookie not in app.state.sessions.records
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    assert response.json()["revision"] == snapshot.revision
    assert response.headers["cache-control"] == "no-store"
    status = client.get("/api/status").json()
    assert status["csrf_token"] == headers["x-csrf-token"]
    for secret in (
        "PRIVATE_ARGUMENT",
        "env_from",
        "/bin/echo",
        "HOST_SECRET",
        str(snapshot.source),
    ):
        assert secret not in json.dumps([response.json(), status])
    denied = submit(client, snapshot, {"origin": BASE})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "invalid_csrf"
    assert not app.state.coordinator.records


def test_pairing_response_cookie_attributes(service):
    app, client = service
    response = client.post(
        "/api/pair", json={"code": app.state.sessions.code}, headers={"origin": BASE}
    )
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert "Domain=" not in cookie and "Secure" not in cookie


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"origin": "http://evil.example"}, 403),
        ({}, 403),
        ({"origin": "null"}, 403),
        ({"origin": BASE, "host": "evil.example:8765"}, 400),
        ({"origin": BASE, "content-type": "text/plain"}, 415),
        ({"origin": BASE, "content-encoding": "gzip"}, 415),
    ],
)
def test_host_origin_and_content_type(service, headers, status):
    app, client = service
    response = client.post(
        "/api/pair",
        content='{"code":"123456"}',
        headers={"content-type": "application/json", **headers},
    )
    assert response.status_code == status
    assert not app.state.sessions.records


def test_bounded_body_and_safe_validation_errors(service):
    app, client = service
    headers = {"origin": BASE, "content-type": "application/json"}
    assert client.post("/api/pair", content="x" * 4097, headers=headers).status_code == 413
    response = client.post("/api/pair", content='{"code":"PRIVATE_INVALID"}', headers=headers)
    assert response.status_code == 422
    assert "PRIVATE_INVALID" not in response.text
    response = client.post("/api/pair", content="invalid json SECRET", headers=headers)
    assert response.status_code == 422 and "SECRET" not in response.text


def test_invocation_acceptance_duplicate_status_and_redaction(service, snapshot, fake_executor):
    app, client = service
    headers = pair(app, client)
    response = submit(client, snapshot, headers)
    assert response.status_code == 202
    identity = response.json()["id"]
    duplicate = submit(client, snapshot, headers)
    assert duplicate.status_code == 200 and duplicate.json()["id"] == identity
    for _ in range(30):
        record = client.get(f"/api/invocations/{identity}").json()
        if record["state"] == "succeeded":
            break
        time.sleep(0.01)
    assert record["state"] == "succeeded"
    assert len(fake_executor.calls) == 1
    for secret in (
        "PRIVATE_ARGUMENT",
        "PRIVATE_OUTPUT",
        "SECRET MESSAGE",
        "argv",
        "env_from",
        "pid",
    ):
        assert secret not in json.dumps(record)
    assert submit(client, snapshot, headers, revision="different").status_code == 409
    assert (
        submit(client, snapshot, headers, request_id="another", revision="old").status_code == 409
    )
    assert (
        submit(client, snapshot, headers, request_id="another", extra="PRIVATE").status_code == 422
    )


def test_cross_session_invocations_are_indistinguishable_from_missing(service, snapshot):
    app, client = service
    headers = pair(app, client)
    identity = submit(client, snapshot, headers).json()["id"]
    pair(app, client)
    for path in (f"/api/invocations/{identity}", "/api/invocations/does-not-exist"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "invocation_not_found"


def test_expired_session_denies_reads(snapshot, fake_executor):
    clock = Clock()
    sessions = Sessions(clock=clock, output=lambda _: None)
    app = create_app(snapshot, executor=fake_executor, sessions=sessions)
    with TestClient(app, base_url=BASE) as client:
        pair(app, client)
        clock.now += 8 * 3600
        assert client.get("/api/dashboard").status_code == 401


def test_failure_messages_do_not_leak_details(service, snapshot, fake_executor):
    app, client = service
    fake_executor.error = AdapterError(
        "execution_failed",
        "PRIVATE native script",
        details={"stderr": "PRIVATE output"},
        effects_possible=True,
    )
    headers = pair(app, client)
    identity = submit(client, snapshot, headers).json()["id"]
    for _ in range(30):
        response = client.get(f"/api/invocations/{identity}")
        if response.json()["state"] == "failed":
            break
        time.sleep(0.01)
    assert response.json()["state"] == "failed"
    assert response.json()["result"]["effects_possible"]
    assert "PRIVATE" not in response.text


def test_connection_urls_and_loopback_authorities(snapshot):
    assert connection_urls(snapshot) == [BASE]
    assert "localhost:8765" in authorities(snapshot)
    assert "evil.example:8765" not in authorities(snapshot)


def test_chunked_body_limit_cannot_be_bypassed(snapshot, fake_executor):
    async def scenario():
        app = create_app(snapshot, executor=fake_executor, owner_output=lambda _: None)

        async def chunks():
            yield b"x" * 2048
            yield b"x" * 2049

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as client:
            response = await client.post(
                "/api/pair",
                content=chunks(),
                headers={"origin": BASE, "content-type": "application/json"},
            )
        assert response.status_code == 413
        assert not app.state.sessions.records

    asyncio.run(scenario())


def test_cookie_is_secure_in_https_context(snapshot, fake_executor):
    app = create_app(snapshot, executor=fake_executor, owner_output=lambda _: None)
    with TestClient(app, base_url=BASE.replace("http:", "https:")) as client:
        response = client.post(
            "/api/pair",
            json={"code": app.state.sessions.code},
            headers={"origin": BASE.replace("http:", "https:")},
        )
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]


def test_hung_helper_leaves_status_responsive_and_cancels_later_work(snapshot):
    snapshot = replace(
        snapshot, actions=freeze({"hello": {"type": "keyboard", "timeout_seconds": 0.05}})
    )
    executor = Executor(
        helper_command=[sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"]
    )
    app = create_app(snapshot, executor=executor, owner_output=lambda _: None)
    with TestClient(app, base_url=BASE) as client:
        headers = pair(app, client)
        one = submit(client, snapshot, headers).json()["id"]
        two = submit(client, snapshot, headers, request_id="second").json()["id"]
        start = time.monotonic()
        assert client.get("/api/status").status_code == 200
        assert time.monotonic() - start < 0.5
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status = client.get("/api/invocations/" + one).json()
            if status["state"] == "failed":
                break
            time.sleep(0.08)
        assert status["result"]["code"] == "executor_unavailable"
        assert client.get("/api/invocations/" + two).json()["state"] == "cancelled"
        assert client.get("/api/status").json()["executor_available"] is False
        assert submit(client, snapshot, headers, request_id="third").status_code == 503


def test_batched_status_is_bounded_authenticated_and_owned(service, snapshot):
    app, client = service
    assert client.get("/api/invocations", params={"ids": "missing"}).status_code == 401
    headers = pair(app, client)
    identity = submit(client, snapshot, headers).json()["id"]
    response = client.get("/api/invocations", params=[("ids", identity), ("ids", "missing")])
    assert response.status_code == 200
    result = response.json()
    assert result["invocations"][0]["id"] == identity
    assert result["missing_ids"] == ["missing"]
    for secret in ("PRIVATE", "env_from", "stdout", "/bin/echo"):
        assert secret not in response.text
    for params in (
        {},
        {"ids": "!invalid"},
        {"ids": "missing", "wait": 3},
        {"ids": "missing", "wait": -1},
        [("ids", "missing")] * 65,
    ):
        assert client.get("/api/invocations", params=params).status_code == 422
    pair(app, client)
    foreign = client.get("/api/invocations", params={"ids": identity, "wait": 2}).json()
    assert foreign == {"invocations": [], "missing_ids": [identity]}


def test_volume_observation_is_paired_and_redacted(volume_snapshot, fake_executor):
    app = create_app(volume_snapshot, executor=fake_executor, owner_output=lambda _: None)
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/api/volume").status_code == 401
        pair(app, client)
        response = client.get("/api/volume")
        assert response.json() == {"value": 37, "muted": False}
        assert "PRIVATE" not in response.text
        assert not fake_executor.calls
        fake_executor.volume_error = AdapterError("capability_unavailable", "PRIVATE DEVICE")
        response = client.get("/api/volume")
        assert response.status_code == 503 and "PRIVATE" not in response.text


def test_live_slider_can_receive_completion_in_the_admission_response(
    volume_snapshot, fake_executor
):
    app = create_app(volume_snapshot, executor=fake_executor, owner_output=lambda _: None)
    with TestClient(app, base_url=BASE) as client:
        headers = pair(app, client)
        payload = {
            "request_id": "live",
            "action_id": "volume_level",
            "revision": volume_snapshot.revision,
            "value": 68,
        }
        response = client.post("/api/invocations?wait=0.25", json=payload, headers=headers)
        assert response.status_code == 202
        assert response.json()["state"] == "succeeded"
        assert fake_executor.volume == 68
        assert "PRIVATE" not in response.text
        duplicate = client.post("/api/invocations?wait=0.25", json=payload, headers=headers)
        assert duplicate.status_code == 200 and duplicate.json() == response.json()
        assert len(fake_executor.calls) == 1


@pytest.mark.parametrize("wait", [-1, 0.251, "invalid", "nan", "inf"])
def test_invalid_completion_wait_accepts_no_work(service, snapshot, wait):
    app, client = service
    headers = pair(app, client)
    response = client.post(
        "/api/invocations",
        params={"wait": wait},
        json={"request_id": "wait", "action_id": "hello", "revision": snapshot.revision},
        headers=headers,
    )
    assert response.status_code == 422 and not app.state.coordinator.records


def test_waited_admission_rechecks_session_before_returning(volume_snapshot, fake_executor):
    clock = Clock()
    sessions = Sessions(clock=clock, output=lambda _: None)
    original = fake_executor.execute

    async def execute(action):
        clock.now += 8 * 3600 + 1
        return await original(action)

    fake_executor.execute = execute
    app = create_app(
        volume_snapshot, executor=fake_executor, sessions=sessions, owner_output=lambda _: None
    )
    with TestClient(app, base_url=BASE) as client:
        headers = pair(app, client)
        response = client.post(
            "/api/invocations?wait=0.25",
            json={
                "request_id": "wait",
                "action_id": "volume_level",
                "revision": volume_snapshot.revision,
                "value": 62,
            },
            headers=headers,
        )
        assert response.status_code == 401
        assert len(fake_executor.calls) == 1 and fake_executor.volume == 62


def test_volume_reads_require_an_explicit_configured_action(service, fake_executor):
    app, client = service
    pair(app, client)
    assert client.get("/api/volume").status_code == 404
    assert fake_executor.volume_reads == 0


@pytest.mark.parametrize("value", [True, False, -1, 101, 2.5, "50", None])
def test_invalid_slider_values_accept_no_work(volume_snapshot, fake_executor, value):
    app = create_app(volume_snapshot, executor=fake_executor, owner_output=lambda _: None)
    with TestClient(app, base_url=BASE) as client:
        headers = pair(app, client)
        response = submit(client, volume_snapshot, headers, action_id="volume_level", value=value)
        assert response.status_code == 422
        assert not app.state.coordinator.records and not fake_executor.calls


def test_slider_values_deduplicate_and_cannot_parameterize_fixed_actions(
    volume_snapshot, fake_executor
):
    app = create_app(volume_snapshot, executor=fake_executor, owner_output=lambda _: None)
    with TestClient(app, base_url=BASE) as client:
        headers = pair(app, client)
        response = submit(client, volume_snapshot, headers, action_id="volume_level", value=68)
        assert response.status_code == 202
        original = response.json()["id"]
        duplicate = submit(client, volume_snapshot, headers, action_id="volume_level", value=68)
        assert duplicate.status_code == 200 and duplicate.json()["id"] == original
        assert (
            submit(client, volume_snapshot, headers, action_id="volume_level", value=69).status_code
            == 409
        )
        assert (
            submit(client, volume_snapshot, headers, request_id="fixed", value=68).status_code
            == 422
        )
        for _ in range(5):
            result = client.get("/api/invocations", params={"ids": original, "wait": 2}).json()
            if result["invocations"][0]["state"] == "succeeded":
                break
        assert result["invocations"][0]["state"] == "succeeded"
        assert fake_executor.volume == 68 and len(fake_executor.calls) == 1
        assert client.get("/api/volume").json()["value"] == 68
