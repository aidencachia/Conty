"""Fake-only Apple Music tests; no osascript process is ever started."""

import math
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

from conty.adapters.base import AdapterError
from conty.adapters.macos import music


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.adapter = music.MusicAdapter()
        self.process = Mock(pid=4321, returncode=0)
        self.process.communicate.return_value = (b"dispatched\n", b"")
        self.platform = patch.object(music.sys, "platform", "darwin")
        self.popen = patch.object(music.subprocess, "Popen", return_value=self.process)
        self.kill = patch.object(music.os, "killpg")
        self.platform.start()
        self.spawn = self.popen.start()
        self.killpg = self.kill.start()
        self.addCleanup(self.platform.stop)
        self.addCleanup(self.popen.stop)
        self.addCleanup(self.kill.stop)

    def assert_error(self, code, *, effects=False, **kwargs):
        with self.assertRaises(AdapterError) as caught:
            self.adapter.execute(**{"command": "next_track", **kwargs})
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.effects_possible, effects)
        return caught.exception

    def test_validation_precedes_any_helper(self):
        for command in ("stop", "play_pause; quit", "com.other.Player", None, []):
            self.assert_error("invalid_action", command=command)
        for timeout in (0, -1, True, math.inf, math.nan, "5"):
            self.assert_error("invalid_action", timeout=timeout)
        self.spawn.assert_not_called()

    def test_fixed_commands_use_direct_argv_and_no_player_activation(self):
        for command in ("previous_track", "play_pause", "next_track"):
            with self.subTest(command=command):
                result = self.adapter.execute(command)
                args, kwargs = self.spawn.call_args
                argv = args[0]
                self.assertEqual(argv[:4], ["/usr/bin/osascript", "-l", "JavaScript", "-e"])
                self.assertEqual(argv[4], music._SCRIPTS[command])
                self.assertTrue(kwargs["start_new_session"])
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertNotIn("shell", kwargs)
                self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"})
                self.assertEqual(result.details["app_id"], "com.apple.Music")
                self.assertEqual(result.details["command"], command)
                self.assertEqual(result.details["cleanup"], "helper_reaped")
                self.assertEqual(result.details["effect_confirmation"], "dispatch_only")
                self.killpg.assert_not_called()

    def test_native_template_is_pid_addressed_and_no_prompt(self):
        for script in music._SCRIPTS.values():
            self.assertIn("descriptorWithProcessIdentifier(pid)", script)
            self.assertIn("target.aeDesc, suite, eventID, false", script)
            self.assertIn("0x00080013", script)
            self.assertIn("reply.isNil()", script)
            self.assertIn("!failure.isNil()", script)
            self.assertIn("AEDeterminePermissionToAutomateTarget", script)
            self.assertIn("com.apple.Music", script)
            self.assertNotIn("Application(", script)
            self.assertNotIn("activate", script)
            self.assertNotIn("launch", script)
            self.assertNotIn("EVENT_ID", script)
        self.assertNotIn("sendEventWithOptions", music._CHECK_SCRIPT)

    def test_preflight_errors_are_known_to_have_no_playback_effects(self):
        for code in ("permission_denied", "target_unavailable", "capability_unavailable"):
            with self.subTest(code=code):
                self.process.communicate.return_value = (("preflight:" + code).encode(), b"")
                self.assert_error(code)

    def test_errors_after_dispatch_are_uncertain(self):
        for code in (
            "permission_denied",
            "target_unavailable",
            "timed_out",
            "execution_failed",
            "capability_unavailable",
        ):
            with self.subTest(code=code):
                self.process.communicate.return_value = (code.encode(), b"")
                self.assert_error(code, effects=True)

    def test_check_only_checks_capabilities_and_permission(self):
        self.process.communicate.return_value = (b"ready", b"")
        result = self.adapter.check()
        self.assertTrue(result["available"])
        self.assertEqual(self.spawn.call_args.args[0][4], music._CHECK_SCRIPT)
        self.process.communicate.return_value = (b"preflight:permission_denied", b"")
        self.assertEqual(self.adapter.check()["code"], "permission_denied")

    def test_platform_is_checked_before_spawning(self):
        with patch.object(music.sys, "platform", "linux"):
            self.assert_error("capability_unavailable")
            self.assertEqual(self.adapter.check()["code"], "capability_unavailable")
        self.spawn.assert_not_called()

    def test_helper_start_failure_has_no_effects(self):
        self.spawn.side_effect = FileNotFoundError()
        self.assert_error("capability_unavailable")

    def test_unrecognized_or_failed_helper_is_not_success(self):
        for output in (b"ready", b"", b"garbage", b"\xff"):
            self.process.communicate.return_value = (output, b"private diagnostics")
            error = self.assert_error("execution_failed", effects=True)
            self.assertNotIn("private diagnostics", str(error))
        self.process.returncode = 1
        self.assert_error("capability_unavailable", effects=True)

    def test_timeout_terminates_session_and_reaps(self):
        self.process.communicate.side_effect = [
            subprocess.TimeoutExpired("osascript", 0.1),
            (b"", b""),
        ]
        self.assert_error("timed_out", timeout=0.1, effects=True)
        self.killpg.assert_called_once_with(4321, signal.SIGTERM)
        self.assertEqual(self.process.communicate.call_args.kwargs, {"timeout": 0.2})

    def test_supervised_music_joins_native_helper_group(self):
        adapter = music.MusicAdapter(supervised=True)
        adapter.execute("play_pause")
        self.assertFalse(self.spawn.call_args.kwargs["start_new_session"])
        self.process.communicate.side_effect = [
            subprocess.TimeoutExpired("osascript", 0.1),
            subprocess.TimeoutExpired("osascript", 0.2),
            (b"", b""),
        ]
        with self.assertRaises(AdapterError) as caught:
            adapter.execute("play_pause", timeout=0.1)
        self.assertEqual(caught.exception.code, "timed_out")
        self.killpg.assert_not_called()
        self.assertEqual(
            [call.args for call in self.process.send_signal.call_args_list],
            [(signal.SIGTERM,), (signal.SIGKILL,)],
        )

    def test_timeout_escalates_to_kill_then_reaps(self):
        self.process.communicate.side_effect = [
            subprocess.TimeoutExpired("osascript", 0.1),
            subprocess.TimeoutExpired("osascript", 0.2),
            (b"", b""),
        ]
        self.assert_error("timed_out", timeout=0.1, effects=True)
        self.assertEqual(
            [c.args for c in self.killpg.call_args_list],
            [(4321, signal.SIGTERM), (4321, signal.SIGKILL)],
        )
        self.assertEqual(self.process.communicate.call_args.kwargs, {"timeout": 1})

    def test_unconfirmed_cleanup_poisons_adapter_until_local_recovery(self):
        self.process.communicate.side_effect = subprocess.TimeoutExpired("osascript", 0.1)
        self.assert_error("executor_unavailable", timeout=0.1, effects=True)
        self.assert_error("executor_unavailable")
        self.assertEqual(self.adapter.check()["code"], "executor_unavailable")
        self.assertEqual(self.spawn.call_count, 1)

    def test_check_timeout_has_no_playback_effects(self):
        self.process.communicate.side_effect = [
            subprocess.TimeoutExpired("osascript", 5),
            (b"", b""),
        ]
        self.assertEqual(self.adapter.check()["code"], "timed_out")
        self.killpg.assert_called_once_with(4321, signal.SIGTERM)

    def test_deadline_includes_helper_startup(self):
        with patch.object(music.time, "monotonic", side_effect=(10, 12)):
            self.adapter.execute("play_pause", timeout=3)
        self.assertEqual(self.process.communicate.call_args.kwargs, {"timeout": 1})

    def test_already_gone_helper_is_still_reaped(self):
        self.killpg.side_effect = ProcessLookupError()
        self.process.communicate.side_effect = [
            subprocess.TimeoutExpired("osascript", 0.1),
            (b"", b""),
        ]
        self.assert_error("timed_out", timeout=0.1, effects=True)
        self.assertEqual(self.process.communicate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
