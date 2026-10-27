"""Native-only PowerPoint snapshots/control. No launch, activation, or scripting fallback.

All native calls (including keyboard and Quartz rendering) live in supervised
Python children. Cancellation joins bounded process-group cleanup before releasing
our service lock. A native timeout or uncertain mutation poisons this instance;
constructing another instance is not a substitute for deliberate local recovery.
Handles are PRIVATE: they contain the original full name, PID/lifetime and window
identity, or saved-path identity on versions without window IDs. Saved paths
identify the same saved file within a PID/lifetime, not a provable window instance:
closing/reopening that file is indistinguishable. Fresh index/path checks are
non-atomic; do not open, close, reorder or Save As decks during an operation.
Only the explicit public metadata below may be published by a caller.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from conty.adapters.base import AdapterError
from conty.adapters.macos.powerpoint_workspace import export_workspace, secure_pdf

MAX_DECKS = 16
MAX_CACHED_DECKS_BYTES = 64 * 1024
MAX_SLIDES = 500
MAX_PDF = 64 * 1024 * 1024
MAX_IMAGE = 512 * 1024
MAX_PREVIEWS = 8 * 1024 * 1024
READ_TIMEOUT = 30
EXPORT_TIMEOUT = 45
RENDER_TIMEOUT = 30
OUTPUT_LIMIT = 16 * 1024 * 1024
_ERRORS = {
    "permission_denied": (
        "PowerPoint Automation access is required. Run `conty powerpoint authorize` locally "
        "from the same host, then check System Settings > Privacy & Security > Automation. "
        "Number jumps also require Accessibility access for that host. Normal operations "
        "never request Automation or Accessibility consent."
    ),
    "target_unavailable": "The exact PowerPoint process/window/presentation is no longer available",
    "target_not_frontmost": "The selected PowerPoint slideshow must already be frontmost and active",
    "unsupported_identity": (
        "PowerPoint requires a unique full saved path to identify this presentation. "
        "Save unsaved presentations and close duplicate full-path matches before retrying."
    ),
    "capability_unavailable": "The native macOS PowerPoint bridge is unavailable",
    "invalid_action": "Invalid PowerPoint operation or slide target",
    "stale_snapshot": "The deck changed; refresh the slide snapshot before retrying",
    "unsupported_show": "Number jumps require a standard, all-slides show numbered from one",
    "unsafe_focus": (
        "Number jumps require safe slideshow-container focus. Dismiss PowerPoint menus, "
        "dialogs and sheets, then focus the slideshow canvas (not notes or controls). "
        "If Accessibility cannot verify that focus, number jumps remain unavailable."
    ),
    "limit_exceeded": "The PowerPoint deck or preview exceeds the supported resource limits",
    "export_failed": "PowerPoint PDF export could not be verified",
    "execution_failed": "The native PowerPoint operation failed",
    "timed_out": "The native PowerPoint helper exceeded its deadline; restart after local recovery",
    "executor_unavailable": "PowerPoint control is disabled; restart after local recovery",
}


def _error(code, *, effects=False):
    return AdapterError(code, _ERRORS[code], effects_possible=effects)


def _slides(value):
    if not isinstance(value, list) or len(value) > MAX_SLIDES:
        raise _error("limit_exceeded")
    result = []
    for slide in value:
        if (
            not isinstance(slide, dict)
            or not isinstance(slide.get("id"), str)
            or not slide["id"].isdigit()
            or len(slide["id"]) > 20
            or type(slide.get("number")) is not int
            or not isinstance(slide.get("title"), str)
            or len(slide["title"]) > 256
            or type(slide.get("hidden")) is not bool
        ):
            raise _error("execution_failed")
        result.append({k: slide[k] for k in ("id", "number", "title", "hidden")})
    if len({s["id"] for s in result}) != len(result):
        raise _error("stale_snapshot")
    return result


def _state(value):
    if not isinstance(value, dict):
        raise _error("execution_failed")
    slides = _slides(value.get("slides"))
    current = value.get("current_slide_id")
    if (
        value.get("view") not in ("read", "edit")
        or type(value.get("can_control")) is not bool
        or (
            current is not None
            and (not isinstance(current, str) or current not in {s["id"] for s in slides})
        )
        or (value["view"] == "edit" and (current is not None or value["can_control"]))
    ):
        raise _error("stale_snapshot")
    return {
        "slides": slides,
        "view": value["view"],
        "current_slide_id": current,
        "can_control": value["can_control"],
    }


def _handle(value):
    if (
        not isinstance(value, dict)
        or type(value.get("pid")) is not int
        or value["pid"] <= 0
        or not isinstance(value.get("lifetime"), str)
        or not 0 < len(value["lifetime"]) <= 128
        or not isinstance(value.get("fullname"), str)
        or len(value["fullname"]) > 16384
    ):
        raise _error("target_unavailable")
    kind = value.get("identity_kind", "window_id")
    if kind == "saved_path":
        if "window_id" in value:
            raise _error("target_unavailable")
        if not Path(value["fullname"]).is_absolute():
            raise _error("unsupported_identity")
        return {k: value[k] for k in ("pid", "lifetime", "fullname", "identity_kind")}
    if kind != "window_id" or type(value.get("window_id")) is not int:
        raise _error("target_unavailable")
    return {k: value[k] for k in ("pid", "lifetime", "window_id", "fullname")}


class NativePowerPoint:
    """Async API with one serialized worker and killable native children.

    ``expected_slides`` accepts the snapshot metadata list (or its ordered ID
    list). For show_slide it is mandatory: a number jump may not use stale order.
    ``authorize`` returns permission readiness and deliberately requests only
    Automation, never Accessibility, input, or a document mutation.
    """

    def __init__(self, *, export_directory=None):
        self._export_directory = export_directory
        self._lock = asyncio.Lock()
        self._cancel = threading.Event()
        self._poisoned = False
        self._closed = False
        self._worker = None
        self._metadata_cache = {}

    @staticmethod
    def _cache_key(handle):
        return json.dumps(_handle(handle), sort_keys=True, separators=(",", ":"))

    def _remember(self, handle, slides):
        key = self._cache_key(handle)
        self._metadata_cache.pop(key, None)
        self._metadata_cache[key] = {"handle": _handle(handle), "slides": _slides(slides)}
        while len(self._metadata_cache) > MAX_DECKS:
            del self._metadata_cache[next(iter(self._metadata_cache))]

    def _cached_decks(self):
        # Bound the serialized argv payload, including JSON escaping and framing.
        result = []
        for entry in reversed(list(self._metadata_cache.values())):
            candidate = [*result, entry]
            if len(json.dumps(candidate, ensure_ascii=True, separators=(",", ":")).encode()) <= (
                MAX_CACHED_DECKS_BYTES
            ):
                result = candidate
        return result

    async def _call(self, function, *args):
        async with self._lock:
            if self._closed or self._poisoned:
                raise _error("executor_unavailable")
            self._cancel.clear()
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            self._worker = task
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                self._cancel.set()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 3)
                except (Exception, asyncio.CancelledError):
                    self._poisoned = True
                raise
            except AdapterError:
                raise
            except Exception:
                raise _error("execution_failed") from None
            finally:
                if task.done():
                    self._worker = None

    async def discover(self) -> list[dict]:
        return await self._call(self._discover)

    async def observe(self, handle) -> dict:
        return await self._call(self._observe, _handle(handle))

    async def refresh(self, handle) -> dict:
        return await self._call(self._refresh, _handle(handle))

    async def command(self, handle, operation, slide_id=None, expected_slides=None) -> dict:
        if not isinstance(operation, str) or operation not in (
            "show_slide",
            "next_slide",
            "previous_slide",
        ):
            raise _error("invalid_action")
        expected = None
        snapshot_slides = None
        if expected_slides is not None:
            if not isinstance(expected_slides, list) or len(expected_slides) > MAX_SLIDES:
                raise _error("invalid_action")
            expected = [s.get("id") if isinstance(s, dict) else s for s in expected_slides]
            if any(not isinstance(s, str) for s in expected) or len(set(expected)) != len(expected):
                raise _error("invalid_action")
            if all(isinstance(s, dict) for s in expected_slides):
                snapshot_slides = _slides(expected_slides)
        if operation == "show_slide":
            if not isinstance(slide_id, str) or expected is None or slide_id not in expected:
                raise _error("invalid_action")
        elif slide_id is not None:
            raise _error("invalid_action")
        return await self._call(
            self._command, _handle(handle), operation, slide_id, expected, snapshot_slides
        )

    async def authorize(self) -> dict:
        return await self._call(self._run, "native", {"operation": "authorize"}, 30, False)

    async def close(self):
        self._closed = True
        self._cancel.set()
        task = self._worker
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), 3)
            except (Exception, asyncio.CancelledError):
                self._poisoned = True

    def _discover(self):
        payload = {"operation": "discover"}
        cached = self._cached_decks()
        if cached:
            payload["cached_decks"] = cached
        values = self._run("native", payload, READ_TIMEOUT, False)
        if not isinstance(values, list) or len(values) > MAX_DECKS:
            raise _error("limit_exceeded")
        result = []
        for value in values:
            if not isinstance(value, dict):
                raise _error("execution_failed")
            handle = _handle(value.get("handle"))
            state = _state(value)
            title = value.get("title")
            if not isinstance(title, str) or len(title) > 256:
                raise _error("execution_failed")
            if handle.get("identity_kind") == "saved_path":
                token = json.dumps(
                    [handle["pid"], handle["lifetime"], "saved_path", handle["fullname"]]
                )

            else:
                token = f"{handle['pid']}:{handle['lifetime']}:{handle['window_id']}"
            result.append(
                {
                    "key": hashlib.sha256(token.encode()).hexdigest(),
                    "title": title,
                    **state,
                    "handle": handle,
                }
            )
        if len({d["key"] for d in result}) != len(result):
            raise _error("target_unavailable")
        for value in result:
            self._remember(value["handle"], value["slides"])
        return result

    def _observe(self, handle):
        state = _state(
            self._run("native", {"operation": "observe", "handle": handle}, READ_TIMEOUT, False)
        )
        self._remember(handle, state["slides"])
        return state

    def _command(self, handle, operation, slide_id, expected, snapshot_slides=None):
        payload = {
            "operation": operation,
            "handle": handle,
            "slide_id": slide_id,
            "expected_slides": expected,
        }
        if snapshot_slides is not None:
            payload["snapshot_slides"] = snapshot_slides
        value = self._run(
            "native",
            payload,
            READ_TIMEOUT,
            True,
        )
        try:
            state = _state(value)
        except AdapterError as exc:
            self._poisoned = True
            raise _error(exc.code, effects=True) from None
        return {**state, "effect_confirmation": "dispatch_only"}

    def _refresh(self, handle):
        # Reuse the same folder and filename so PowerPoint can retain its folder grant.
        # The workspace removes the PDF, but preserves the private directory and lock.
        with export_workspace(self._export_directory) as pdf:
            exported = self._run(
                "native",
                {
                    "operation": "export",
                    "handle": handle,
                    "pdf": str(pdf),
                },
                EXPORT_TIMEOUT,
                True,
            )
            state = _state(exported)
            secure_pdf(pdf, MAX_PDF)
            pages = self._run(
                "render",
                {
                    "pdf": str(pdf),
                    "count": len(state["slides"]),
                },
                RENDER_TIMEOUT,
                False,
            )
            if not isinstance(pages, list) or len(pages) != len(state["slides"]):
                raise _error("export_failed")
            previews, total = [], 0
            for index, (slide, page) in enumerate(zip(state["slides"], pages), 1):
                if (
                    not isinstance(page, dict)
                    or type(page.get("page")) is not int
                    or page["page"] != index
                ):
                    raise _error("export_failed")
                text = page.get("image")
                if not isinstance(text, str) or len(text) > 4 * ((MAX_IMAGE + 2) // 3):
                    raise _error("limit_exceeded")
                try:
                    image = base64.b64decode(text, validate=True)
                except (ValueError, binascii.Error):
                    raise _error("export_failed") from None
                total += len(image)
                if len(image) > MAX_IMAGE or total > MAX_PREVIEWS:
                    raise _error("limit_exceeded")
                if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise _error("export_failed")
                previews.append({"slide_id": slide["id"], "page": index, "image": image})
            # Rendering may take time; reject changes made while it ran as well.
            after = self._observe(handle)
            if after["slides"] != state["slides"]:
                raise _error("stale_snapshot")
            return {**after, "slides": state["slides"], "previews": previews}

    def _stop(self, process):
        # Kill the session even when the leader exited: inherited pipes may be
        # held by descendants. No broad kill-by-name or target-app termination.
        for sig, grace in ((signal.SIGTERM, 0.2), (signal.SIGKILL, 1)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
            if sig == signal.SIGKILL:
                return
            time.sleep(0.02)
        if process.poll() is None:
            raise _error("executor_unavailable", effects=True)

    def _run(self, helper, request, timeout, effects):
        if self._cancel.is_set():
            raise _error("executor_unavailable")
        if sys.platform != "darwin":
            raise _error("capability_unavailable")
        deadline = time.monotonic() + timeout
        argv = [
            sys.executable,
            "-m",
            f"conty.adapters.macos.powerpoint_scripts.{helper}",
            json.dumps(request, ensure_ascii=True, separators=(",", ":")),
        ]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "en_US.UTF-8",
                    "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                },
            )
        except OSError:
            raise _error("capability_unavailable") from None
        captures = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                for name in captures:
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map() or process.poll() is None:
                    if self._cancel.is_set():
                        raise _error("executor_unavailable", effects=effects)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _error("timed_out", effects=effects)
                    for key, _ in selector.select(min(remaining, 0.05)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        capture = captures[key.data]
                        limit = OUTPUT_LIMIT if key.data == "stdout" else 4096
                        if key.data == "stdout" and len(capture) + len(chunk) > limit:
                            raise _error("limit_exceeded", effects=effects)
                        capture.extend(chunk[: max(0, limit - len(capture))])
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if process.returncode != 0:
                raise _error("execution_failed", effects=effects)
            try:
                envelope = json.loads(captures["stdout"])
                if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
                    raise ValueError()
                if not envelope["ok"]:
                    code = envelope.get("code")
                    if code not in _ERRORS:
                        code = "execution_failed"
                    if envelope.get("poison") is True:
                        self._poisoned = True
                    raise _error(code, effects=envelope.get("effects_possible") is True)
                return envelope["result"]
            except (ValueError, KeyError, TypeError):
                raise _error("execution_failed", effects=effects) from None
        except BaseException as exc:
            if (
                not isinstance(exc, AdapterError)
                or exc.effects_possible
                or exc.code in ("timed_out", "executor_unavailable")
            ):
                self._poisoned = True
            try:
                self._stop(process)
            except (OSError, subprocess.TimeoutExpired, AdapterError):
                self._poisoned = True
                raise _error("executor_unavailable", effects=effects) from None
            if isinstance(exc, AdapterError):
                raise
            raise _error("execution_failed", effects=effects) from None
        finally:
            process.stdout.close()
            process.stderr.close()
