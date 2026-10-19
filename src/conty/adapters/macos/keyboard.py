"""Milestone-1, PID-directed macOS keyboard input.

Optional dependencies: pyobjc-framework-Cocoa, pyobjc-framework-Quartz and
pyobjc-framework-ApplicationServices. Imports and permission checks are lazy;
Checks are non-prompting unless explicitly requested by doctor. Grant Accessibility to the responsible
terminal/editor or host in System Settings > Privacy & Security > Accessibility.

This synchronous adapter belongs on a serialized, dedicated execution context,
not the HTTP event loop. The deadline bounds focus polling, not an individual
PyObjC call (which Python cannot cancel). A hard wall-clock/native-hang guarantee
requires a supervised native helper before production use. Do not time out a
thread and allow another invocation to overlap it.

CGEventPostToPid avoids global event routing, but does not acknowledge delivery
or prove the application handled the key. Focus/termination can still change
after our final check. Application-specific hardware acceptance remains mandatory.
"""

from __future__ import annotations

import re
import sys
import time

from conty.adapters.base import ActionResult, AdapterError, validate_timeout

# macOS virtual key codes describe physical ANSI positions, not Unicode text.
_KEYS = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "equal": 24,
    "9": 25,
    "7": 26,
    "minus": 27,
    "8": 28,
    "0": 29,
    "right_bracket": 30,
    "o": 31,
    "u": 32,
    "left_bracket": 33,
    "i": 34,
    "p": 35,
    "return": 36,
    "l": 37,
    "j": 38,
    "quote": 39,
    "k": 40,
    "semicolon": 41,
    "backslash": 42,
    "comma": 43,
    "slash": 44,
    "n": 45,
    "m": 46,
    "period": 47,
    "tab": 48,
    "space": 49,
    "grave": 50,
    "backspace": 51,
    "escape": 53,
    "keypad_decimal": 65,
    "keypad_multiply": 67,
    "keypad_plus": 69,
    "keypad_clear": 71,
    "keypad_divide": 75,
    "keypad_enter": 76,
    "keypad_minus": 78,
    "keypad_equal": 81,
    "keypad_0": 82,
    "keypad_1": 83,
    "keypad_2": 84,
    "keypad_3": 85,
    "keypad_4": 86,
    "keypad_5": 87,
    "keypad_6": 88,
    "keypad_7": 89,
    "keypad_8": 91,
    "keypad_9": 92,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
    "f13": 105,
    "f14": 107,
    "f15": 113,
    "f16": 106,
    "f17": 64,
    "f18": 79,
    "f19": 80,
    "f20": 90,
    "home": 115,
    "page_up": 116,
    "delete": 117,
    "end": 119,
    "page_down": 121,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
}
SUPPORTED_KEYS = tuple(sorted(_KEYS))
_MODIFIERS = {"shift", "control", "option", "command"}
_BUNDLE_ID = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\Z")
_ACCESSIBILITY_HELP = (
    "Accessibility permission is required. Open System Settings > Privacy & Security, "
    "then scroll down to Accessibility (not the main Accessibility sidebar page). "
    "Or run: /usr/bin/open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'. "
    "Enable the app used to launch Conty (for example Terminal, iTerm, or Zed); "
    "if missing, use the + button to add it. macOS permission identity depends on "
    "the launch method and may instead be the Python executable reported by doctor. "
    "Restart the launching app if needed, then rerun Conty from the same app. "
    "No prompt was requested."
)


class _Native:
    def __init__(self):
        if sys.platform != "darwin":
            raise AdapterError("capability_unavailable", "Keyboard input requires macOS")
        try:
            import AppKit
            import ApplicationServices
            import Quartz

            # Refuse a global-event fallback if this bridge lacks PID delivery.
            if not callable(getattr(Quartz, "CGEventPostToPid", None)):
                raise ImportError("CGEventPostToPid is unavailable")
        except (ImportError, OSError) as exc:
            raise AdapterError(
                "capability_unavailable", "Install the optional macOS PyObjC frameworks"
            ) from exc
        self.apps, self.ax, self.cg = AppKit, ApplicationServices, Quartz

    def trusted(self):
        return bool(self.ax.AXIsProcessTrusted())

    def request_accessibility(self):
        # macOS displays consent asynchronously; the return value is current trust.
        return bool(
            self.ax.AXIsProcessTrustedWithOptions({self.ax.kAXTrustedCheckOptionPrompt: True})
        )

    def resolve(self, app_id):
        apps = [
            app
            for app in self.apps.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
                app_id
            )
            if not app.isTerminated()
        ]
        if len(apps) != 1:
            raise AdapterError(
                "target_unavailable",
                "Expected one running target application; no application was launched",
            )
        return apps[0]

    def current_application(self):
        app = self.apps.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None or app.isTerminated():
            raise AdapterError("target_unavailable", "No running frontmost application")
        return app

    def is_running(self, app):
        return not app.isTerminated()

    def pid(self, app):
        return int(app.processIdentifier())

    def frontmost(self, pid):
        app = self.apps.NSWorkspace.sharedWorkspace().frontmostApplication()
        return app is not None and self.pid(app) == pid

    def activate(self, app):
        return bool(app.activateWithOptions_(self.apps.NSApplicationActivateIgnoringOtherApps))

    def events(self, key, modifiers):
        flags = 0
        names = {
            "shift": "kCGEventFlagMaskShift",
            "control": "kCGEventFlagMaskControl",
            "option": "kCGEventFlagMaskAlternate",
            "command": "kCGEventFlagMaskCommand",
        }
        for modifier in modifiers:
            flags |= getattr(self.cg, names[modifier])
        down = self.cg.CGEventCreateKeyboardEvent(None, _KEYS[key], True)
        up = self.cg.CGEventCreateKeyboardEvent(None, _KEYS[key], False)
        if down is None or up is None:
            raise AdapterError("capability_unavailable", "Could not create keyboard events")
        # Flags are attached to this event, not separate modifier key presses.
        # The paired key-up clears synthetic flags; no physical key is released.
        self.cg.CGEventSetFlags(down, flags)
        self.cg.CGEventSetFlags(up, 0)
        return down, up

    def post(self, pid, event):
        self.cg.CGEventPostToPid(pid, event)


class KeyboardAdapter:
    def __init__(self, bridge=None) -> None:
        self._bridge = bridge

    def _native(self):
        return self._bridge if self._bridge is not None else _Native()

    def check(self, *, request_permission: bool = False) -> dict:
        """Check capability; optionally request consent, never send input."""
        try:
            native = self._native()
            trusted = native.trusted()
            if not trusted and request_permission:
                trusted = native.request_accessibility()
                if not trusted:
                    return {
                        "available": False,
                        "code": "permission_denied",
                        "prompt_requested": True,
                        "message": _ACCESSIBILITY_HELP.removesuffix("No prompt was requested.")
                        + "An Accessibility prompt was requested from macOS; macOS may suppress "
                        "repeat prompts. Enable access in System Settings, then rerun doctor.",
                    }
            if not trusted:
                raise AdapterError("permission_denied", _ACCESSIBILITY_HELP)
            return {
                "available": True,
                "permission": "accessibility",
                "delivery": "pid",
                "effect_confirmation": "dispatch_only",
            }
        except AdapterError as exc:
            return {"available": False, "code": exc.code, "message": str(exc)}
        except Exception:
            return {
                "available": False,
                "code": "capability_unavailable",
                "message": "macOS keyboard capability could not be established",
            }

    def execute(
        self,
        key: str,
        app_id: str | None = None,
        focus: str | None = None,
        modifiers: tuple[str, ...] = (),
        timeout: float = 5,
    ) -> ActionResult:
        # All configuration is validated before importing/calling a native API.
        if not isinstance(key, str) or key not in _KEYS:
            raise AdapterError(
                "invalid_action", "Unsupported physical key; see SUPPORTED_KEYS or keyboard --help"
            )
        if app_id is not None and (not isinstance(app_id, str) or not _BUNDLE_ID.fullmatch(app_id)):
            raise AdapterError("invalid_action", "app_id must be a bundle identifier")
        if focus is not None and focus not in ("activate", "require_frontmost"):
            raise AdapterError("invalid_action", "Unsupported focus policy")
        if app_id is None and focus is not None:
            raise AdapterError("invalid_action", "focus requires an explicit app_id")
        focus = focus or ("activate" if app_id is not None else "require_frontmost")
        if (
            not isinstance(modifiers, tuple)
            or any(not isinstance(m, str) or m not in _MODIFIERS for m in modifiers)
            or len(set(modifiers)) != len(modifiers)
        ):
            raise AdapterError(
                "invalid_action", "Use unique shift, control, option or command modifiers"
            )
        validate_timeout(timeout)
        deadline = time.monotonic() + timeout
        effects = False
        try:
            native = self._native()
            if not native.trusted():
                raise AdapterError("permission_denied", _ACCESSIBILITY_HELP)
            app = native.resolve(app_id) if app_id is not None else native.current_application()
            pid = native.pid(app)
            if pid <= 0:
                raise AdapterError("target_unavailable", "Target has no valid running PID")
            down, up = native.events(key, modifiers)
            if time.monotonic() >= deadline:
                raise AdapterError("timed_out", "Keyboard deadline expired before activation")
            if not native.frontmost(pid):
                if focus == "require_frontmost":
                    raise AdapterError(
                        "target_not_frontmost", "Target application is not frontmost"
                    )
                effects = True  # Activation itself is a desktop side effect.
                if not native.activate(app):
                    raise AdapterError(
                        "target_not_frontmost", "Target application refused activation"
                    )
                while not native.frontmost(pid):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdapterError(
                            "timed_out", "Target did not become frontmost before the deadline"
                        )
                    time.sleep(min(0.01, remaining))
            # Never retarget an untargeted action if the captured app exits or loses focus.
            if not native.is_running(app):
                raise AdapterError("target_unavailable", "Target exited before dispatch")
            if app_id is not None and native.pid(native.resolve(app_id)) != pid:
                raise AdapterError("target_unavailable", "Target process changed before dispatch")
            if not native.trusted():
                raise AdapterError(
                    "permission_denied",
                    "Accessibility permission changed before dispatch. " + _ACCESSIBILITY_HELP,
                )
            if not native.frontmost(pid):
                raise AdapterError("target_not_frontmost", "Target lost focus before dispatch")
            if time.monotonic() >= deadline:
                raise AdapterError("timed_out", "Keyboard deadline expired before dispatch")
            effects = True
            try:
                native.post(pid, down)
            finally:
                # Attempt release even if posting down raised after dispatch.
                # Cleanup must run even if the action deadline has elapsed.
                native.post(pid, up)
            return ActionResult(
                "Input dispatched",
                details={
                    "app_id": app_id,
                    "pid": pid,
                    "key": key,
                    "target": "application" if app_id is not None else "frontmost",
                    "focus": focus,
                    "modifiers": modifiers,
                    "delivery": "pid",
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
                "execution_failed", "macOS keyboard operation failed", effects_possible=effects
            ) from exc
