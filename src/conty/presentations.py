"""Native Mac PowerPoint discovery, explicit PDF previews, and at-most-once commands.

The host supplies browser authentication/CSRF and APIError rendering. Mount
``presentation_router(hub)``, call synchronous ``maintain()`` for housekeeping,
and await ``close()`` at shutdown. Only a requested list performs discovery;
there is no timer, Office add-in, authorization prompt, or fallback. Content edits
that preserve slide IDs/order/number/hidden status require an explicit Refresh.
All methods run on the host event loop; native I/O shares one service lock.
Only navigation mutations also acquire the host's shared input lock.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections import deque
from dataclasses import dataclass, field
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from conty.adapters.base import AdapterError
from conty.auth import APIError, Sessions

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
DISCOVERY_SECONDS = 1
DISCOVERY_RESULT_SECONDS = 6

READ_SECONDS = 35
COMMAND_SECONDS = 10
NAVIGATION_SECONDS = 30
REFRESH_SECONDS = 90
RETENTION_SECONDS = 120
PNG_LIMIT = 512 * 1024
MAX_SLIDES = 500
_SAFE_ERRORS = {
    "permission_denied": (
        "PowerPoint Automation access is required. Run `conty powerpoint authorize` locally "
        "from the same host and check System Settings > Privacy & Security > Automation. "
        "Slide jumps also require Accessibility access."
    ),
    "target_unavailable": "The selected PowerPoint presentation is no longer available",
    "target_not_frontmost": "The selected PowerPoint slideshow must be frontmost and active",
    "unsafe_focus": (
        "Dismiss PowerPoint menus and dialogs, then focus the slideshow canvas before jumping"
    ),
    "capability_unavailable": "Native macOS PowerPoint support is unavailable",
    "invalid_action": "The PowerPoint operation or slide target is invalid",
    "stale_snapshot": "The presentation changed; reload the slide list before retrying",
    "unsupported_show": "Slide jumps require a standard all-slides show numbered from one",
    "unsupported_identity": (
        "This presentation cannot be targeted unambiguously; save it and scan again"
    ),
    "limit_exceeded": "The presentation or its previews exceed the supported resource limits",
    "export_failed": "PowerPoint PDF previews could not be verified",
    "export_busy": "Another PowerPoint preview export is in progress; wait for it to finish",
    "execution_failed": "The native PowerPoint operation failed",
    "timed_out": "PowerPoint exceeded its deadline; control is disabled until local recovery",
    "executor_unavailable": "PowerPoint control is disabled; restart after local recovery",
}


class CommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: Identifier
    revision: Identifier
    operation: Literal["refresh", "show_slide", "next_slide", "previous_slide"]
    slide_id: Identifier | None = None

    @model_validator(mode="after")
    def operation_target(self):
        if self.operation == "show_slide" and self.slide_id is None:
            raise ValueError("A slide is required")
        if self.operation != "show_slide" and "slide_id" in self.model_fields_set:
            raise ValueError("Only show_slide accepts a slide target")
        return self


def _invalid():
    return APIError(422, "invalid_request", "Request fields are invalid")


def _native_error(exc: AdapterError):
    code = exc.code if exc.code in _SAFE_ERRORS else "execution_failed"
    status = (
        409
        if code
        in (
            "target_not_frontmost",
            "unsafe_focus",
            "stale_snapshot",
            "unsupported_show",
            "unsupported_identity",
        )
        else 503
    )
    return APIError(status, code, _SAFE_ERRORS[code])


@dataclass
class _Command:
    id: str
    owner: str
    request_id: str
    presentation_id: str
    revision: str
    operation: str
    slide_id: str | None
    created: float
    state: str = "queued"
    message: str = "Waiting for PowerPoint"
    finished: float | None = None

    def public(self):
        return {
            key: getattr(self, key)
            for key in (
                "id",
                "state",
                "operation",
                "presentation_id",
                "slide_id",
                "revision",
                "message",
            )
        }

    def fingerprint(self):
        return self.presentation_id, self.revision, self.operation, self.slide_id

    @property
    def deadline(self):
        return self.created + (REFRESH_SECONDS if self.operation == "refresh" else COMMAND_SECONDS)


@dataclass
class _Presentation:
    id: str
    key: str
    handle: object
    title: str
    revision: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    connected: bool = True
    slides: list[dict] = field(default_factory=list)
    view: str | None = None
    current_slide_id: str | None = None
    can_control: bool = False
    previews: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    preview_bytes: int = 0
    needs_refresh: bool = True
    observed_at: float | None = None
    offline_at: float | None = None


class PresentationHub:
    def __init__(
        self,
        sessions: Sessions,
        adapter=None,
        *,
        native_lock: asyncio.Lock | None = None,
        max_presentations: int = 16,
        max_commands: int = 2048,
        max_pending: int = 32,
        max_pending_per_deck: int = 32,
        max_preview_bytes: int = 32 * 1024 * 1024,
        max_deck_preview_bytes: int = 8 * 1024 * 1024,
    ):
        limits = (
            max_presentations,
            max_commands,
            max_pending,
            max_pending_per_deck,
            max_preview_bytes,
            max_deck_preview_bytes,
        )
        if any(type(limit) is not int or limit < 1 for limit in limits):
            raise ValueError("Presentation capacity limits must be positive integers")
        self.sessions, self.clock = sessions, sessions.clock
        self._adapter = adapter
        self._lock = asyncio.Lock()
        self._input_lock = native_lock if native_lock is not None else asyncio.Lock()
        self.max_presentations = min(max_presentations, 16)
        self.max_commands = min(max_commands, 2048)
        self.max_pending = min(max_pending, 32)
        self.max_pending_per_deck = min(max_pending_per_deck, 32)
        self.max_preview_bytes = min(max_preview_bytes, 32 * 1024 * 1024)
        self.max_deck_preview_bytes = min(max_deck_preview_bytes, 8 * 1024 * 1024)
        self.presentations: dict[str, _Presentation] = {}
        self.commands: dict[str, _Command] = {}
        self.dedupe: dict[tuple[str, str], str] = {}
        self.preview_bytes = 0
        self.closed = False
        self._poisoned = False
        self._keys: dict[str, str] = {}
        self._queue: deque[str] = deque()
        self._worker: asyncio.Task | None = None
        self._running: _Command | None = None
        self._discovery: asyncio.Task | None = None
        self._discovered_at: float | None = None
        self._discovery_error: APIError | None = None
        self._discovery_unpublished = False

    def _native(self):
        if self._adapter is None:
            # Importing/constructing the hub never initializes native support.
            from conty.adapters.macos.powerpoint import NativePowerPoint

            self._adapter = NativePowerPoint()
        return self._adapter

    def _browser(self, owner):
        self.maintain()
        if not self.sessions.active(owner):
            raise APIError(401, "pairing_required", "Pair this browser with the host")
        if self.closed:
            raise APIError(503, "executor_unavailable", _SAFE_ERRORS["executor_unavailable"])

    def _available(self):
        if self.closed or self._poisoned:
            raise APIError(503, "executor_unavailable", _SAFE_ERRORS["executor_unavailable"])

    def _deck(self, identity):
        deck = self.presentations.get(identity)
        if deck is None:
            raise APIError(404, "presentation_not_found", "Presentation is not available")
        return deck

    def _finish(self, command, state, message):
        command.state, command.message = state, message
        command.finished = self.clock()

    def _clear_previews(self, deck):
        self.preview_bytes -= deck.preview_bytes
        deck.preview_bytes = 0
        deck.previews.clear()
        deck.needs_refresh = True

    def _invalidate(self, deck):
        self._clear_previews(deck)
        deck.revision = secrets.token_urlsafe(24)

    def _offline(self, deck):
        if deck.connected:
            deck.connected = False
            deck.offline_at = self.clock()
            if self._keys.get(deck.key) == deck.id:
                del self._keys[deck.key]
            self._invalidate(deck)
        deck.view = deck.current_slide_id = None
        deck.can_control = False

    def _poison(self):
        # A new snapshot or successful export cannot resolve uncertain native effects.
        self._poisoned = True
        for deck in self.presentations.values():
            deck.can_control = False
            self._clear_previews(deck)
        for command in self.commands.values():
            if command.state == "queued":
                self._finish(command, "cancelled", _SAFE_ERRORS["executor_unavailable"])

    def _adapter_failed(self, exc, deck=None):
        if exc.effects_possible or exc.code in ("timed_out", "executor_unavailable"):
            self._poison()
        if deck is not None:
            if exc.code == "target_unavailable":
                self._offline(deck)
            elif exc.code == "stale_snapshot":
                self._invalidate(deck)
            elif exc.code == "target_not_frontmost":
                deck.can_control = False
        self._discovered_at = None
        return _native_error(exc)

    def maintain(self):
        """Synchronous housekeeping only; never read, export, or control PowerPoint."""
        now = self.clock()
        for command in list(self.commands.values()):
            active = self.sessions.active(command.owner)
            if command.state == "queued":
                deck = self.presentations.get(command.presentation_id)
                if not active or self.closed:
                    self._finish(command, "cancelled", "Browser session expired or service closed")
                elif now >= command.deadline:
                    self._finish(command, "cancelled", "Command expired before dispatch")
                elif deck is None or not deck.connected or deck.revision != command.revision:
                    self._finish(command, "cancelled", "Presentation changed or is offline")
            if not active or (
                command.finished is not None and now - command.finished >= RETENTION_SECONDS
            ):
                self.commands.pop(command.id, None)
                self.dedupe.pop((command.owner, command.request_id), None)
        self._queue = deque(
            cid
            for cid in self._queue
            if cid in self.commands and self.commands[cid].state == "queued"
        )
        for deck in list(self.presentations.values()):
            if not deck.connected and now - deck.offline_at >= RETENTION_SECONDS:
                self.presentations.pop(deck.id)

    @staticmethod
    def _state(value):
        """Validate resource bounds and copy only public metadata from the native boundary."""
        if not isinstance(value, dict):
            raise AdapterError("execution_failed", "Invalid native observation")
        slides = value.get("slides")
        if not isinstance(slides, list) or len(slides) > MAX_SLIDES:
            raise AdapterError("limit_exceeded", "Invalid native slide count")
        result = []
        for slide in slides:
            if (
                not isinstance(slide, dict)
                or not isinstance(slide.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", slide["id"])
                or not isinstance(slide.get("title"), str)
                or len(slide["title"]) > 256
                or type(slide.get("number")) is not int
                or not -(2**31) <= slide["number"] < 2**31
                or type(slide.get("hidden")) is not bool
            ):
                raise AdapterError("execution_failed", "Invalid native slide metadata")
            result.append({key: slide[key] for key in ("id", "title", "number", "hidden")})
        ids = {slide["id"] for slide in result}
        if (
            len(ids) != len(result)
            or value.get("view") not in ("read", "edit")
            or type(value.get("can_control")) is not bool
            or (value.get("current_slide_id") is not None and value["current_slide_id"] not in ids)
            or (
                value["view"] == "edit"
                and (value.get("current_slide_id") is not None or value["can_control"])
            )
        ):
            raise AdapterError("stale_snapshot", "Invalid native observation")
        return result, value["view"], value.get("current_slide_id"), value["can_control"]

    def _apply(self, deck, value):
        slides, view, current, can_control = self._state(value)

        def manifest(items):
            return [(s["id"], s["number"], s["hidden"]) for s in items]

        if manifest(deck.slides) != manifest(slides):
            self._invalidate(deck)
        deck.slides, deck.view = slides, view
        deck.current_slide_id = current
        deck.can_control = can_control and not self._poisoned
        deck.observed_at = self.clock()

    async def _discover(self):
        try:
            async with self._lock:
                self._available()
                values = await asyncio.wait_for(self._native().discover(), READ_SECONDS)
                if not isinstance(values, list) or len(values) > self.max_presentations:
                    raise AdapterError("limit_exceeded", "Too many native presentations")
                keys = set()
                for value in values:
                    self._state(value)
                    if (
                        not isinstance(value.get("key"), str)
                        or not value["key"]
                        or value["key"] in keys
                        or "handle" not in value
                        or not isinstance(value.get("title"), str)
                        or len(value["title"]) > 256
                    ):
                        raise AdapterError("execution_failed", "Invalid native presentation")
                    keys.add(value["key"])
                for deck in list(self.presentations.values()):
                    if deck.connected and deck.key not in keys:
                        self._offline(deck)
                for value in values:
                    identity = self._keys.get(value["key"])
                    if identity is None:
                        while len(self.presentations) >= self.max_presentations:
                            offline = next(
                                (d for d in self.presentations.values() if not d.connected), None
                            )
                            if offline is None:
                                raise AdapterError(
                                    "limit_exceeded", "Presentation capacity reached"
                                )
                            self.presentations.pop(offline.id)
                        deck = _Presentation(
                            secrets.token_urlsafe(24), value["key"], value["handle"], value["title"]
                        )
                        self.presentations[deck.id] = deck
                        self._keys[deck.key] = deck.id
                    else:
                        deck = self.presentations[identity]
                        deck.handle, deck.title = value["handle"], value["title"]
                    self._apply(deck, value)
                self._discovery_error = None
        except AdapterError as exc:
            self._discovery_error = self._adapter_failed(exc)
            # Failed observation must not leave formerly live controls/previews available.
            for deck in self.presentations.values():
                self._offline(deck)
        except TimeoutError:
            self._poison()
            self._discovery_error = _native_error(AdapterError("timed_out", ""))
        except APIError as exc:
            self._discovery_error = exc
        except asyncio.CancelledError:
            raise
        except Exception:
            self._discovery_error = _native_error(AdapterError("execution_failed", ""))
            for deck in self.presentations.values():
                self._offline(deck)
        finally:
            self._discovered_at = self.clock()
            self._discovery_unpublished = True

    async def list_presentations(self, owner: str, *, wait: float | None = None):
        """List metadata, optionally bounding only this caller's wait for shared discovery."""
        self._browser(owner)
        self._available()
        discovering = False
        # A scan can finish between two-second phone polls, after the one-second
        # cache window. Publish its recent result once before starting another
        # scan, otherwise every response can remain "discovering" indefinitely.
        unpublished = (
            self._discovery_unpublished
            and self._discovered_at is not None
            and self.clock() - self._discovered_at < DISCOVERY_RESULT_SECONDS
        )
        if not unpublished and (
            self._discovered_at is None or self.clock() - self._discovered_at >= DISCOVERY_SECONDS
        ):
            if self._discovery is None or self._discovery.done():
                self._discovery = asyncio.create_task(self._discover())
            task = self._discovery
            # Neither a disconnected caller nor its response budget cancels the shared read.
            if wait is None:
                await asyncio.shield(task)
            else:
                await asyncio.wait({task}, timeout=wait)
            discovering = not task.done()
        self._browser(owner)
        self._available()
        if not discovering:
            self._discovery_unpublished = False
            if self._discovery_error is not None:
                raise self._discovery_error
        result = {"presentations": [self._public(deck) for deck in self.presentations.values()]}
        if discovering:
            result["discovering"] = True
        return result

    def _public(self, deck):
        slides = []
        for slide in deck.slides:
            preview = deck.previews.get(slide["id"]) if deck.connected else None
            slides.append(
                {
                    **slide,
                    "page": preview[0] if preview else None,
                    "preview_url": (
                        f"/api/presentations/{deck.id}/slides/{slide['id']}/preview"
                        f"?revision={deck.revision}"
                        if preview
                        else None
                    ),
                }
            )
        return {
            "id": deck.id,
            "title": deck.title,
            "revision": deck.revision,
            "connected": deck.connected,
            "view": deck.view,
            "current_slide_id": deck.current_slide_id,
            "can_control": deck.connected and deck.can_control and not self._poisoned,
            "slides": slides,
            "preview_count": len(deck.previews),
            "needs_refresh": deck.needs_refresh,
            "observed_at": deck.observed_at,
        }

    def preview(self, owner: str, identity: str, slide_id: str, revision: str):
        self._browser(owner)
        deck = self._deck(identity)
        # PNGs are a revisioned PDF snapshot, not live state. Lazy image reads
        # remain valid while discovery is slow; observed deck changes still
        # invalidate the revision/cache and every read authenticates its owner.
        if not deck.connected or revision != deck.revision or slide_id not in deck.previews:
            raise APIError(404, "preview_not_found", "Preview is not available")
        return deck.previews[slide_id][1]

    def _eligible(self, deck, command):
        self._available()
        if not deck.connected:
            raise APIError(409, "presentation_offline", "Presentation is offline")
        if command.revision != deck.revision:
            raise APIError(409, "stale_revision", "Presentation revision has changed")
        if command.operation == "refresh":
            if deck.view != "edit":
                raise APIError(
                    409, "edit_view_required", "Stop the slideshow before loading previews"
                )
        else:
            if deck.view != "read" or not deck.can_control:
                raise APIError(
                    409,
                    "control_unavailable",
                    "The selected slideshow must be active and frontmost",
                )
            if command.operation == "show_slide" and command.slide_id not in {
                slide["id"] for slide in deck.slides
            }:
                raise APIError(404, "slide_not_found", "Slide is not available")

    def submit(self, owner: str, identity: str, payload: CommandInput):
        """Accept synchronously; return a queued record without waiting for any native I/O."""
        self._browser(owner)
        previous = self.dedupe.get((owner, payload.request_id))
        fingerprint = identity, payload.revision, payload.operation, payload.slide_id
        if previous is not None:
            command = self.commands[previous]
            if command.fingerprint() != fingerprint:
                raise APIError(409, "request_conflict", "Request identifier was already used")
            return command.public()
        command = _Command(
            secrets.token_urlsafe(24),
            owner,
            payload.request_id,
            identity,
            payload.revision,
            payload.operation,
            payload.slide_id,
            self.clock(),
        )
        deck = self._deck(identity)
        self._eligible(deck, command)
        pending = [c for c in self.commands.values() if c.state in ("queued", "running")]
        if self._running is not None and self._running not in pending:
            pending.append(self._running)
        if (
            len(self.commands) >= self.max_commands
            or len(pending) >= self.max_pending
            or sum(c.presentation_id == identity for c in pending) >= self.max_pending_per_deck
        ):
            raise APIError(503, "command_capacity", "Presentation command capacity reached")
        self.commands[command.id] = command
        self.dedupe[(owner, payload.request_id)] = command.id
        self._queue.append(command.id)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())
        return command.public()

    def _store_previews(self, deck, value):
        previews = value.get("previews")
        if not isinstance(previews, list) or len(previews) > len(deck.slides):
            raise AdapterError("export_failed", "Invalid preview mapping")
        ids = {s["id"] for s in deck.slides}
        result, pages, total = {}, set(), 0
        for preview in previews:
            if not isinstance(preview, dict):
                raise AdapterError("export_failed", "Invalid preview mapping")
            sid, page, image = preview.get("slide_id"), preview.get("page"), preview.get("image")
            if (
                not isinstance(sid, str)
                or sid not in ids
                or sid in result
                or type(page) is not int
                or not 1 <= page <= MAX_SLIDES
                or page in pages
                or not isinstance(image, bytes)
                or not image.startswith(b"\x89PNG\r\n\x1a\n")
            ):
                raise AdapterError("export_failed", "Invalid preview mapping")
            total += len(image)
            if len(image) > PNG_LIMIT or total > self.max_deck_preview_bytes:
                raise AdapterError("limit_exceeded", "Preview capacity reached")
            result[sid] = (page, image)
            pages.add(page)
        if self.preview_bytes - deck.preview_bytes + total > self.max_preview_bytes:
            raise AdapterError("limit_exceeded", "Preview capacity reached")
        self._invalidate(deck)
        deck.previews, deck.preview_bytes = result, total
        self.preview_bytes += total
        deck.needs_refresh = len(result) != len(deck.slides)

    async def _work(self):
        while self._queue and not self.closed:
            command = self.commands.get(self._queue.popleft())
            if command is None or command.state != "queued":
                continue
            self._running = command
            try:
                await self._execute(command)
            finally:
                self._running = None

    async def _execute(self, command):
        native_started = mutation_started = False
        deck = None
        try:
            remaining = command.deadline - self.clock()
            if remaining <= 0:
                self._finish(command, "cancelled", "Command expired before dispatch")
                return
            async with asyncio.timeout(remaining) as deadline:
                async with self._lock:
                    if command.state != "queued":
                        return
                    if not self.sessions.active(command.owner):
                        self._finish(command, "cancelled", "Browser session expired")
                        return
                    deck = self._deck(command.presentation_id)
                    self._eligible(deck, command)
                    command.state, command.message = "running", "Checking PowerPoint"
                    if command.operation == "refresh":
                        native_started = True
                        self._apply(deck, await self._native().observe(deck.handle))
                        native_started = False
                        self._eligible(deck, command)
                    if not self.sessions.active(command.owner):
                        self._finish(command, "cancelled", "Browser session expired")
                        return
                    if self.clock() >= command.deadline:
                        self._finish(command, "cancelled", "Command expired before dispatch")
                        return
                    if command.operation == "refresh":
                        native_started = mutation_started = True
                        command.message = "Loading PowerPoint PDF previews"
                        deck.needs_refresh = True
                        value = await self._native().refresh(deck.handle)
                        self._apply(deck, value)
                        self._store_previews(deck, value)
                        message = "Previews loaded"
                    else:
                        async with self._input_lock:
                            if not self.sessions.active(command.owner):
                                self._finish(command, "cancelled", "Browser session expired")
                                return
                            deck = self._deck(command.presentation_id)
                            self._eligible(deck, command)
                            if self.clock() >= command.deadline:
                                self._finish(
                                    command, "cancelled", "Command expired before dispatch"
                                )
                                return
                            # Queue/input-lock waits have a short admission deadline. Once admitted,
                            # allow the bounded native operation its own execution budget; large
                            # decks must not time out merely because validation took a few seconds.
                            deadline.reschedule(
                                asyncio.get_running_loop().time() + NAVIGATION_SECONDS
                            )
                            # The native command validates this handle and snapshot against fresh
                            # identity/order/numbers/focus before input; do not retarget via observe.
                            command.message = "Dispatching PowerPoint navigation"
                            native_started = mutation_started = True
                            value = await self._native().command(
                                deck.handle,
                                command.operation,
                                slide_id=command.slide_id,
                                expected_slides=[dict(s) for s in deck.slides],
                            )
                        self._apply(deck, value)
                        message = "Navigation dispatched; position reflects the latest observation"
                    if self.sessions.active(command.owner):
                        self._finish(command, "succeeded", message)
                    else:
                        self._finish(
                            command, "outcome_unknown", "Browser session expired after dispatch"
                        )
        except APIError as exc:
            self._finish(command, "cancelled", exc.message)
        except AdapterError as exc:
            error = self._adapter_failed(exc, deck)
            self._finish(
                command, "outcome_unknown" if exc.effects_possible else "failed", error.message
            )
        except TimeoutError:
            if native_started:
                self._poison()
            self._finish(
                command,
                "outcome_unknown"
                if mutation_started
                else ("failed" if native_started else "cancelled"),
                _SAFE_ERRORS["timed_out"] if native_started else "Command expired before dispatch",
            )
        except asyncio.CancelledError:
            if native_started:
                self._poison()
            self._finish(
                command,
                "outcome_unknown" if mutation_started else "cancelled",
                "Service closed after dispatch" if mutation_started else "Service closed",
            )
            raise
        except Exception:
            if mutation_started:
                self._poison()
            self._finish(
                command,
                "outcome_unknown" if mutation_started else "failed",
                _SAFE_ERRORS["execution_failed"],
            )
        finally:
            self.maintain()

    def lookup(self, owner: str, identity: str, command_id: str):
        self._browser(owner)
        command = self.commands.get(command_id)
        if command is None or command.owner != owner or command.presentation_id != identity:
            raise APIError(404, "command_not_found", "Command is not available")
        return command.public()

    async def close(self):
        """Cancel queued work and join supervised native cleanup, without starting native support."""
        if self.closed:
            return
        self.closed = True
        for command in self.commands.values():
            if command.state == "queued":
                self._finish(command, "cancelled", "Service closed")
        tasks = [
            task for task in (self._worker, self._discovery) if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if self._adapter is not None:
            tasks.append(asyncio.create_task(self._adapter.close()))
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1)
            # Retrieve exceptions without allowing one failed cleanup to skip the rest.
            for task in tasks:
                if task.done() and not task.cancelled():
                    task.exception()
        self.presentations.clear()
        self._keys.clear()
        self.commands.clear()
        self.dedupe.clear()
        self._queue.clear()
        self.preview_bytes = 0


async def _body(request: Request):
    try:
        return CommandInput.model_validate(await request.json())
    except (ValueError, ValidationError, RecursionError):
        raise _invalid() from None


def _owner(request: Request):
    session = getattr(request.state, "session", None)
    if session is None:
        raise APIError(401, "pairing_required", "Pair this browser with the host")
    return session.id


def presentation_router(hub: PresentationHub) -> APIRouter:
    router = APIRouter()

    @router.get("/api/presentations")
    async def presentations(request: Request):
        return await hub.list_presentations(_owner(request), wait=0.2)

    @router.get("/api/presentations/{pid}/slides/{slide_id}/preview")
    async def preview(pid: str, slide_id: str, request: Request):
        owner = _owner(request)
        revisions = request.query_params.getlist("revision")
        if len(revisions) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", revisions[0]):
            raise _invalid()
        data = hub.preview(owner, pid, slide_id, revisions[0])
        return Response(
            data,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/api/presentations/{pid}/commands", status_code=202)
    async def submit(pid: str, request: Request):
        owner = _owner(request)
        return hub.submit(owner, pid, await _body(request))

    @router.get("/api/presentations/{pid}/commands/{cid}")
    async def command(pid: str, cid: str, request: Request):
        return hub.lookup(_owner(request), pid, cid)

    return router
