"""Same-origin HTTP transport; action definitions never cross this boundary."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import secrets
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from conty.adapters.base import AdapterError
from conty.auth import APIError, Limiter, Sessions
from conty.config import Identifier, Model, Snapshot
from conty.executor import Executor
from conty.presentations import PresentationHub, presentation_router
from conty.runtime import Coordinator

logger = logging.getLogger(__name__)
COOKIE = "conty_session"
BODY_LIMIT = 4096


class PairInput(Model):
    code: Annotated[str, Field(pattern=r"^[0-9]{6}$")]


class InvocationInput(Model):
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
    action_id: Identifier
    revision: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
    value: Annotated[int, Field(ge=0, le=100)] | None = None


def authorities(snapshot):
    server = snapshot.server
    address = ipaddress.ip_address(server["bind"])
    hosts = set(server["allowed_hosts"])
    if not address.is_unspecified:
        hosts.add(server["bind"])
    if address.is_loopback:
        hosts.update(("localhost", "127.0.0.1", "::1"))
    result = set()
    for host in hosts:
        host = f"[{host}]" if ":" in host else host.lower()
        result.add(f"{host}:{server['port']}")
        if server["port"] in (80, 443):
            result.add(host)
    return result


def connection_urls(snapshot, *, scheme="http"):
    server = snapshot.server
    address = ipaddress.ip_address(server["bind"])
    hosts = list(server["allowed_hosts"]) if address.is_unspecified else [server["bind"]]
    return [
        f"{scheme}://{'[' + host + ']' if ':' in host else host}:{server['port']}" for host in hosts
    ]


def error_response(exc: APIError):
    headers = {"Retry-After": "2"} if exc.status in (429, 503) else None
    return JSONResponse(
        {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "correlation_id": secrets.token_hex(8),
            }
        },
        status_code=exc.status,
        headers=headers,
    )


def create_app(
    snapshot: Snapshot, *, executor=None, sessions=None, powerpoint=None, owner_output=print
):
    sessions = sessions if sessions is not None else Sessions(output=owner_output)
    executor = (
        executor
        if executor is not None
        else Executor(
            native_types=[a["type"] for a in snapshot.actions.values() if a["type"] != "process"]
        )
    )
    coordinator = Coordinator(snapshot, executor, sessions)
    presentations = PresentationHub(
        sessions, adapter=powerpoint, native_lock=getattr(executor, "_lock", None)
    )
    traffic = Limiter(80, 40)
    global_traffic = Limiter(400, 200, capacity=1)
    accepted = authorities(snapshot)

    async def maintenance():
        while True:
            await asyncio.sleep(1)
            sessions.maintain()
            coordinator.prune()
            presentations.maintain()

    @asynccontextmanager
    async def lifespan(app):
        sessions.rotate()
        coordinator.start()
        timer = asyncio.create_task(maintenance(), name="conty-session-maintenance")
        try:
            yield
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            await presentations.close()
            await coordinator.stop()
            sessions.records.clear()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.coordinator, app.state.sessions = coordinator, sessions
    app.state.presentations = presentations
    app.include_router(presentation_router(presentations))

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response(APIError(422, "invalid_request", "Request fields are invalid"))

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        try:
            hosts = request.headers.getlist("host")
            if len(hosts) != 1 or hosts[0].lower() not in accepted:
                raise APIError(400, "invalid_host", "Host is not approved for this service")
            source = request.client.host if request.client else "unknown"
            if not global_traffic.allow("host") or not traffic.allow(source):
                raise APIError(429, "request_rate_limited", "Too many requests")
            body_limit = BODY_LIMIT
            content_length = request.headers.get("content-length", "0")
            if not re.fullmatch(r"[0-9]{1,10}", content_length) or int(content_length) > body_limit:
                raise APIError(413, "request_too_large", "Request body exceeds the limit")
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                expected_origin = f"{request.url.scheme}://{hosts[0].lower()}"
                if request.headers.getlist("origin") != [expected_origin]:
                    raise APIError(403, "invalid_origin", "Request origin is not allowed")
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    raise APIError(415, "json_required", "Use an application/json request")
                if request.headers.get("content-encoding", "identity") != "identity":
                    raise APIError(
                        415, "unsupported_encoding", "Compressed requests are unsupported"
                    )
            if request.url.path.startswith("/api/") and request.url.path != "/api/pair":
                session = sessions.authenticate(request.cookies.get(COOKIE))
                request.state.session = session
                if request.method != "GET":
                    token = request.headers.get("x-csrf-token", "")
                    if (
                        len(request.headers.getlist("x-csrf-token")) != 1
                        or len(token) > 128
                        or not token.isascii()
                        or not secrets.compare_digest(token, session.csrf)
                    ):
                        raise APIError(
                            403, "invalid_csrf", "Session request token is missing or invalid"
                        )

            async def bounded_body():
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > body_limit:
                        raise APIError(413, "request_too_large", "Request body exceeds the limit")
                request._body = bytes(body)

            try:
                await asyncio.wait_for(bounded_body(), 5)
            except TimeoutError as exc:
                raise APIError(408, "request_timeout", "Request body took too long") from exc
            response = await call_next(request)
        except APIError as exc:
            response = error_response(exc)
        except Exception:
            correlation = secrets.token_hex(8)
            # Do not log exception strings: framework/native input can contain secrets.
            logger.error("request_failed correlation=%s", correlation)
            response = JSONResponse(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Host request failed",
                        "correlation_id": correlation,
                    }
                },
                status_code=500,
            )

        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            }
        )

        return response

    @app.post("/api/pair")
    async def pair(payload: PairInput, request: Request):
        credential, session = sessions.pair(
            payload.code, request.client.host if request.client else "unknown"
        )
        response = JSONResponse({"csrf_token": session.csrf, "expires_in_seconds": 8 * 3600})
        response.set_cookie(
            COOKIE,
            credential,
            max_age=8 * 3600,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @app.get("/api/dashboard")
    async def dashboard():
        return snapshot.public()

    @app.get("/api/status")
    async def status(request: Request):
        return {
            "revision": snapshot.revision,
            "executor_available": coordinator.available,
            "readiness": coordinator.readiness,
            "csrf_token": request.state.session.csrf,
        }

    @app.post("/api/invocations")
    async def invoke(
        payload: InvocationInput,
        request: Request,
        wait: Annotated[float, Query(ge=0, le=0.25)] = 0,
    ):
        result, created = coordinator.submit(
            request.state.session.id,
            payload.request_id,
            payload.action_id,
            payload.revision,
            value=payload.value,
        )
        if wait:
            result = await coordinator.wait_for_completion(
                result["id"], request.state.session.id, wait=wait
            )
            sessions.authenticate(request.cookies.get(COOKIE))
        return JSONResponse(result, status_code=202 if created else 200)

    @app.get("/api/volume")
    async def volume(request: Request):
        if not any(
            a["type"] == "volume" and a["operation"] == "set" for a in snapshot.actions.values()
        ):
            raise APIError(404, "volume_not_configured", "Volume slider is not configured")
        try:
            observed = await executor.read_volume()
            sessions.authenticate(request.cookies.get(COOKIE))
            return {"value": observed["value"], "muted": observed["muted"]}
        except AdapterError as exc:
            raise APIError(503, "volume_unavailable", "System volume is unavailable") from exc

    @app.get("/api/invocations/{identity}")
    async def invocation(identity: str, request: Request):
        if not 1 <= len(identity) <= 128:
            raise APIError(404, "invocation_not_found", "Invocation is not available")
        return coordinator.lookup(identity, request.state.session.id)

    @app.get("/api/invocations")
    async def invocations(
        request: Request,
        ids: Annotated[
            list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]],
            Query(min_length=1, max_length=64),
        ],
        wait: Annotated[float, Query(ge=0, le=2)] = 0,
    ):
        result = await coordinator.lookup_many(ids, request.state.session.id, wait=wait)
        # A bounded read may span session expiry; recheck before revealing data.
        sessions.authenticate(request.cookies.get(COOKIE))
        return result

    @app.get("/")
    async def index():
        return Response(
            files("conty").joinpath("web/index.html").read_bytes(), media_type="text/html"
        )

    @app.get("/assets/{name}")
    async def asset(name: Literal["app.js", "presentations.js", "style.css", "icon.svg"]):
        return Response(
            files("conty").joinpath("web", name).read_bytes(),
            media_type={
                "app.js": "text/javascript",
                "presentations.js": "text/javascript",
                "style.css": "text/css",
                "icon.svg": "image/svg+xml",
            }[name],
        )

    return app
