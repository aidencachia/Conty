"""Fake-only tests: never import PyObjC or touch the desktop."""

import math
import unittest
from unittest.mock import patch

from conty.adapters.base import AdapterError
from conty.adapters.macos import keyboard


class FakeNative:
    def __init__(self):
        self.calls = []
        self.focused = True
        self.allowed = True
        self.running = True
        self.target_pid = 123
        self.activation_ok = True
        self.activation_focuses = True
        self.focus_checks = 0
        self.lose_focus = False
        self.trust_checks = 0
        self.revoke = False
        self.resolves = 0
        self.restart = False
        self.fail_down = False
        self.fail_up = False

    def trusted(self):
        self.trust_checks += 1
        return self.allowed and not (self.revoke and self.trust_checks > 1)

    def request_accessibility(self):
        self.calls.append(("request_accessibility",))
        return self.allowed

    def resolve(self, app_id):
        self.calls.append(("resolve", app_id))
        self.resolves += 1
        if not self.running:
            raise AdapterError("target_unavailable", "not running")
        return self.target_pid + (1 if self.restart and self.resolves > 1 else 0)

    def current_application(self):
        self.calls.append(("current_application",))
        if not self.running:
            raise AdapterError("target_unavailable", "no frontmost app")
        return self.target_pid

    def is_running(self, app):
        return self.running

    def pid(self, app):
        return app

    def frontmost(self, pid):
        self.focus_checks += 1
        return self.focused and not (self.lose_focus and self.focus_checks > 1)

    def activate(self, app):
        self.calls.append(("activate", app))
        if self.activation_focuses:
            self.focused = True
        return self.activation_ok

    def events(self, key, modifiers):
        self.calls.append(("events", key, modifiers))
        return "down", "up"

    def post(self, pid, event):
        self.calls.append(("post", pid, event))
        if (event == "down" and self.fail_down) or (event == "up" and self.fail_up):
            raise RuntimeError("post failed")


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeNative()
        self.loader = patch.object(keyboard, "_Native", return_value=self.native)
        self.load = self.loader.start()
        self.addCleanup(self.loader.stop)
        self.adapter = keyboard.KeyboardAdapter(bridge=self.native)

    def test_injected_bridge_does_not_load_native_dependencies(self):
        self.assertTrue(self.adapter.check()["available"])
        self.adapter.execute("space")
        self.load.assert_not_called()

    def test_default_bridge_is_loaded_lazily(self):
        adapter = keyboard.KeyboardAdapter()
        self.load.assert_not_called()
        self.assertTrue(adapter.check()["available"])
        self.load.assert_called_once_with()

    def test_prepared_bridge_is_reused_but_live_permission_is_rechecked(self):
        adapter = keyboard.KeyboardAdapter()
        adapter.prepare()
        self.assertTrue(adapter.check()["available"])
        adapter.execute("space")
        self.load.assert_called_once_with()
        self.native.allowed = False
        self.assertFalse(adapter.check()["available"])

    def assert_error(self, code, *, effects=False, **kwargs):
        with self.assertRaises(AdapterError) as caught:
            self.adapter.execute(**{"key": "right", "app_id": "org.example.Editor", **kwargs})
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.effects_possible, effects)
        return caught.exception

    def test_validates_everything_before_native_calls(self):
        cases = [
            {"key": "unsupported"},
            {"key": []},
            {"app_id": "not a bundle"},
            {"app_id": ""},
            {"app_id": []},
            {"focus": "launch"},
            {"focus": []},
            {"app_id": None, "focus": "activate"},
            {"app_id": None, "focus": "require_frontmost"},
            {"modifiers": ["shift"]},
            {"modifiers": ("shift", "shift")},
            {"modifiers": ("alt",)},
            {"modifiers": ([],)},
            *({"timeout": value} for value in (0, -1, True, math.inf, math.nan, "5")),
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self.assert_error("invalid_action", **kwargs)
        self.load.assert_not_called()

    def test_all_supported_keys_and_modifiers_are_paired_and_targeted(self):
        for key in keyboard.SUPPORTED_KEYS:
            with self.subTest(key=key):
                self.native.calls.clear()
                result = self.adapter.execute(
                    key,
                    app_id="org.example.Editor",
                    modifiers=("shift", "control", "option", "command"),
                )
                posts = [c for c in self.native.calls if c[0] == "post"]
                self.assertEqual(posts, [("post", 123, "down"), ("post", 123, "up")])
                self.assertEqual(result.message, "Input dispatched")
                self.assertEqual(result.details["effect_confirmation"], "dispatch_only")
                self.assertNotIn("activate", [c[0] for c in self.native.calls])

    def test_untargeted_input_snapshots_frontmost_without_activation_or_bundle_lookup(self):
        result = self.adapter.execute("s", modifiers=("command",))
        self.assertEqual(result.details["target"], "frontmost")
        self.assertIsNone(result.details["app_id"])
        self.assertEqual(result.details["pid"], 123)
        self.assertEqual(
            self.native.calls,
            [
                ("current_application",),
                ("events", "s", ("command",)),
                ("post", 123, "down"),
                ("post", 123, "up"),
            ],
        )

    def test_untargeted_focus_changes_never_activate_or_retarget(self):
        for initially_focused in (True, False):
            with self.subTest(initially_focused=initially_focused):
                self.native = FakeNative()
                self.adapter = keyboard.KeyboardAdapter(bridge=self.native)
                self.native.focused = initially_focused
                self.native.lose_focus = True
                self.assert_error("target_not_frontmost", app_id=None)
                self.assertFalse(
                    any(c[0] in ("activate", "resolve", "post") for c in self.native.calls)
                )

    def test_untargeted_missing_or_exited_app_fails_without_input(self):
        self.native.running = False
        self.assert_error("target_unavailable", app_id=None)
        self.native.running = True
        with patch.object(self.native, "is_running", return_value=False):
            self.assert_error("target_unavailable", app_id=None)
        self.assertFalse(any(c[0] in ("activate", "post") for c in self.native.calls))

    def test_untargeted_permission_revocation_fails_without_input(self):
        self.native.revoke = True
        self.assert_error("permission_denied", app_id=None)
        self.assertFalse(any(c[0] == "post" for c in self.native.calls))

    def test_missing_permission_or_target_does_not_activate_or_post(self):
        self.native.allowed = False
        self.assert_error("permission_denied")
        self.assertEqual(self.native.calls, [])
        self.native.allowed = True
        self.native.running = False
        self.assert_error("target_unavailable")
        self.assertFalse(any(c[0] in ("post", "activate") for c in self.native.calls))

    def test_require_frontmost_never_activates(self):
        self.native.focused = False
        self.assert_error("target_not_frontmost", focus="require_frontmost")
        self.assertFalse(any(c[0] in ("post", "activate") for c in self.native.calls))

    def test_activate_confirms_focus_and_reports_activation_as_possible_effect(self):
        self.native.focused = False
        self.adapter.execute("left", app_id="org.example.Editor")
        self.assertIn(("activate", 123), self.native.calls)
        self.assertGreaterEqual(self.native.focus_checks, 3)
        self.native.focused = False
        self.native.activation_ok = False
        self.assert_error("target_not_frontmost", effects=True)

    def test_focus_confirmation_has_a_deadline(self):
        self.native.focused = False
        self.native.activation_focuses = False
        clock = iter((0, 0, 0, 2))
        with (
            patch.object(keyboard.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(keyboard.time, "sleep"),
        ):
            self.assert_error("timed_out", timeout=1, effects=True)
        self.assertFalse(any(c[0] == "post" for c in self.native.calls))

    def test_final_rechecks_fail_closed(self):
        for attribute, code in (
            ("lose_focus", "target_not_frontmost"),
            ("restart", "target_unavailable"),
            ("revoke", "permission_denied"),
        ):
            with self.subTest(attribute=attribute):
                self.native = FakeNative()
                setattr(self.native, attribute, True)
                self.adapter = keyboard.KeyboardAdapter(bridge=self.native)
                self.assert_error(code)
                self.assertFalse(any(c[0] == "post" for c in self.native.calls))

    def test_deadline_rechecked_before_dispatch(self):
        with patch.object(keyboard.time, "monotonic", side_effect=(0, 0, 2)):
            self.assert_error("timed_out", timeout=1)
        self.assertFalse(any(c[0] == "post" for c in self.native.calls))

    def test_release_attempted_even_when_down_raises(self):
        self.native.fail_down = True
        self.assert_error("execution_failed", effects=True)
        self.assertEqual(self.native.calls[-2:], [("post", 123, "down"), ("post", 123, "up")])

    def test_release_failure_is_not_reported_as_success(self):
        self.native.fail_up = True
        self.assert_error("execution_failed", effects=True)

    def test_check_is_non_effectful_and_reports_permission(self):
        self.assertTrue(self.adapter.check()["available"])
        self.assertEqual(self.native.calls, [])
        self.native.allowed = False
        self.assertEqual(self.adapter.check()["code"], "permission_denied")

    def test_explicit_check_requests_missing_permission_without_input(self):
        self.native.allowed = False
        report = self.adapter.check(request_permission=True)
        self.assertEqual(report["code"], "permission_denied")
        self.assertTrue(report["prompt_requested"])
        self.assertIn("prompt was requested from macOS", report["message"])
        self.assertNotIn("No prompt was requested", report["message"])
        self.assertEqual(self.native.calls, [("request_accessibility",)])

    def test_explicit_check_does_not_prompt_when_already_trusted(self):
        self.assertTrue(self.adapter.check(request_permission=True)["available"])
        self.assertEqual(self.native.calls, [])

    def test_explicit_check_reports_access_if_granted_during_request(self):
        self.native.allowed = False
        with patch.object(self.native, "request_accessibility", return_value=True) as request:
            self.assertTrue(self.adapter.check(request_permission=True)["available"])
        request.assert_called_once_with()

    def test_permission_errors_explain_how_to_find_and_enable_access(self):
        self.native.allowed = False
        report = self.adapter.check()
        error = self.assert_error("permission_denied")
        self.assertEqual(report["message"], str(error))
        for instruction in (
            "Privacy & Security",
            "scroll down to Accessibility",
            "not the main Accessibility sidebar page",
            "Privacy_Accessibility",
            "Terminal, iTerm, or Zed",
            "+ button",
            "No prompt was requested",
        ):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, report["message"])
        self.assertEqual(self.native.calls, [])

    def test_revoked_permission_includes_setup_guidance(self):
        self.native.revoke = True
        error = self.assert_error("permission_denied")
        self.assertIn("changed before dispatch", str(error))
        self.assertIn("Privacy_Accessibility", str(error))
        self.assertFalse(any(c[0] == "post" for c in self.native.calls))

    def test_platform_handling_is_lazy_and_no_import_is_attempted(self):
        self.adapter = keyboard.KeyboardAdapter()
        self.loader.stop()
        with (
            patch.object(keyboard.sys, "platform", "linux"),
            patch("builtins.__import__", wraps=__import__) as imports,
        ):
            self.assertEqual(self.adapter.check()["code"], "capability_unavailable")
            self.assert_error("capability_unavailable")
            self.assertFalse(
                any(
                    c.args[0] in ("AppKit", "Quartz", "ApplicationServices")
                    for c in imports.call_args_list
                )
            )


class NativeTranslationTests(unittest.TestCase):
    def test_representative_physical_key_codes(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        native = object.__new__(keyboard._Native)
        native.cg = SimpleNamespace(
            CGEventCreateKeyboardEvent=Mock(side_effect=lambda source, code, down: (code, down)),
            CGEventSetFlags=Mock(),
        )
        expected = {
            "a": 0,
            "s": 1,
            "z": 6,
            "0": 29,
            "9": 25,
            "semicolon": 41,
            "space": 49,
            "return": 36,
            "tab": 48,
            "escape": 53,
            "backspace": 51,
            "delete": 117,
            "up": 126,
            "home": 115,
            "page_down": 121,
            "f1": 122,
            "f12": 111,
            "f20": 90,
            "keypad_0": 82,
            "keypad_9": 92,
            "keypad_enter": 76,
        }
        for key, code in expected.items():
            with self.subTest(key=key):
                self.assertEqual(native.events(key, ()), ((code, True), (code, False)))
        self.assertTrue(set("abcdefghijklmnopqrstuvwxyz0123456789") <= set(keyboard.SUPPORTED_KEYS))
        self.assertTrue({f"f{i}" for i in range(1, 21)} <= set(keyboard.SUPPORTED_KEYS))

    def test_frontmost_snapshot_requires_a_running_application(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        native = object.__new__(keyboard._Native)
        workspace = Mock()
        native._refresh = Mock()
        native.apps = SimpleNamespace(
            NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)
        )
        for app in (None, Mock(isTerminated=Mock(return_value=True))):
            workspace.frontmostApplication.return_value = app
            with self.assertRaises(AdapterError) as caught:
                native.current_application()
            self.assertEqual(caught.exception.code, "target_unavailable")
        app = Mock(isTerminated=Mock(return_value=False))
        workspace.frontmostApplication.return_value = app
        self.assertIs(native.current_application(), app)
        self.assertTrue(native.is_running(app))

    def test_run_loop_is_pumped_without_waiting_before_native_state_reads(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        native = object.__new__(keyboard._Native)
        loop = Mock()
        native.apps = SimpleNamespace(
            NSRunLoop=SimpleNamespace(currentRunLoop=lambda: loop),
            NSDefaultRunLoopMode="default",
            NSDate=SimpleNamespace(date=lambda: "now"),
        )
        native._refresh()
        loop.runMode_beforeDate_.assert_called_once_with("default", "now")

    def test_accessibility_request_uses_native_prompt_option(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        native = object.__new__(keyboard._Native)
        native.ax = SimpleNamespace(
            AXIsProcessTrustedWithOptions=Mock(return_value=False),
            kAXTrustedCheckOptionPrompt="AXTrustedCheckOptionPrompt",
        )
        self.assertFalse(native.request_accessibility())
        native.ax.AXIsProcessTrustedWithOptions.assert_called_once_with(
            {"AXTrustedCheckOptionPrompt": True}
        )

    def test_native_events_have_flags_and_pid_delivery_only(self):
        # Construct without __init__: no native imports, even on macOS.
        from types import SimpleNamespace
        from unittest.mock import Mock

        native = object.__new__(keyboard._Native)
        native.cg = SimpleNamespace(
            CGEventCreateKeyboardEvent=Mock(side_effect=("down", "up")),
            CGEventSetFlags=Mock(),
            CGEventPostToPid=Mock(),
            kCGEventFlagMaskShift=1,
            kCGEventFlagMaskControl=2,
            kCGEventFlagMaskAlternate=4,
            kCGEventFlagMaskCommand=8,
        )
        self.assertEqual(native.events("b", ("shift", "command")), ("down", "up"))
        self.assertEqual(
            native.cg.CGEventCreateKeyboardEvent.call_args_list[0].args, (None, 11, True)
        )
        self.assertEqual(
            [c.args for c in native.cg.CGEventSetFlags.call_args_list], [("down", 9), ("up", 0)]
        )
        native.post(123, "down")
        native.cg.CGEventPostToPid.assert_called_once_with(123, "down")


if __name__ == "__main__":
    unittest.main()
