"""System-wide macOS media keys, routed by the OS rather than to a named app.

Checks/prepare are passive and never prompt or send input. Dispatch is a paired
auxiliary-control event through the supervised native worker; it does not prove
that a player handled it. No private MediaRemote APIs or scripting processes.
"""

from __future__ import annotations

import sys
import time

from conty.adapters.base import ActionResult, AdapterError, validate_timeout
from conty.adapters.macos.keyboard import _ACCESSIBILITY_HELP

# Apple's IOHIDSystem/IOKit/hidsystem/ev_keymap.h and IOLLEvent.h:
# NX_KEYTYPE_PLAY/NEXT/PREVIOUS; NX_SUBTYPE_AUX_CONTROL_BUTTONS = 8.
_KEYS = {"previous_track": 18, "play_pause": 16, "next_track": 17}


class _Native:
    def __init__(self):
        if sys.platform != "darwin":
            raise AdapterError("capability_unavailable", "System media control requires macOS")
        try:
            import AppKit
            import Quartz

            if not all(
                callable(getattr(Quartz, name, None))
                for name in ("CGEventPost", "CGPreflightPostEventAccess")
            ) or not callable(
                getattr(
                    AppKit.NSEvent,
                    "otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_",
                    None,
                )
            ):
                raise ImportError("System media-event bridge is unavailable")
        except (ImportError, OSError, AttributeError) as exc:
            raise AdapterError(
                "capability_unavailable", "Install the optional macOS PyObjC frameworks"
            ) from exc
        self.apps, self.cg = AppKit, Quartz

    def trusted(self):
        return bool(self.cg.CGPreflightPostEventAccess())

    def events(self, command):
        events = []
        for state in (0xA, 0xB):  # Key down/up, without repeats or held modifiers.
            event = self.apps.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                self.apps.NSEventTypeSystemDefined,
                (0, 0),
                state << 8,
                0,
                0,
                None,
                8,
                (_KEYS[command] << 16) | (state << 8),
                -1,
            )
            cg_event = event.CGEvent() if event is not None else None
            if cg_event is None:
                raise AdapterError("capability_unavailable", "Could not create system media events")
            events.append(cg_event)
        return tuple(events)

    def post(self, event):
        self.cg.CGEventPost(self.cg.kCGHIDEventTap, event)


class MediaAdapter:
    def __init__(self, bridge=None):
        self._bridge = bridge
        self._unavailable = False

    def _native(self):
        if self._unavailable:
            raise AdapterError("executor_unavailable", "Media-key release failed; restart locally")
        if self._bridge is None:
            self._bridge = _Native()
        return self._bridge

    def prepare(self):
        """Preload bridges without sending a key or requesting permission."""
        self._native()

    def check(self):
        try:
            if not self._native().trusted():
                raise AdapterError("permission_denied", _ACCESSIBILITY_HELP)
            return {
                "available": True,
                "target": "system",
                "delivery": "system_media_keys",
                "permission": "accessibility",
                "commands": list(_KEYS),
                "effect_confirmation": "dispatch_only",
            }
        except AdapterError as exc:
            return {"available": False, "code": exc.code, "message": str(exc)}
        except Exception:
            return {
                "available": False,
                "code": "capability_unavailable",
                "message": "System media capability could not be established",
            }

    def execute(self, command: str, timeout: float = 5):
        if not isinstance(command, str) or command not in _KEYS:
            raise AdapterError(
                "invalid_action",
                "Supported media commands are previous_track, play_pause and next_track",
            )
        validate_timeout(timeout)
        deadline = time.monotonic() + timeout
        effects = False
        try:
            native = self._native()
            if not native.trusted():
                raise AdapterError("permission_denied", _ACCESSIBILITY_HELP)
            down, up = native.events(command)
            if not native.trusted():
                raise AdapterError("permission_denied", _ACCESSIBILITY_HELP)
            if time.monotonic() >= deadline:
                raise AdapterError("timed_out", "Media deadline expired before dispatch")
            effects = True
            try:
                native.post(down)
            finally:
                try:
                    native.post(up)
                except Exception as exc:
                    self._unavailable = True
                    raise AdapterError(
                        "executor_unavailable",
                        "Media-key release failed; restart locally",
                        effects_possible=True,
                    ) from exc
            if time.monotonic() >= deadline:
                raise AdapterError("timed_out", "Media deadline expired", effects_possible=True)
            return ActionResult(
                "System media command dispatched",
                {
                    "target": "system",
                    "command": command,
                    "delivery": "system_media_keys",
                    "effect_confirmation": "dispatch_only",
                    "cleanup": "key_up_dispatched",
                },
            )
        except AdapterError as exc:
            if effects and not exc.effects_possible:
                raise AdapterError(exc.code, str(exc), effects_possible=True) from exc
            raise
        except Exception as exc:
            raise AdapterError(
                "execution_failed", "System media operation failed", effects_possible=effects
            ) from exc
