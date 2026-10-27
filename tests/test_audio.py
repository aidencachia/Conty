"""Fake-only Core Audio tests: no framework is loaded and no audio is changed."""

import ctypes
import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from conty.adapters.base import AdapterError
from conty.adapters.macos import audio


class FakeBridge:
    def __init__(self):
        self.device = 42
        self.living = True
        self.supported = {"volume": True, "mute": True}
        self.writable = dict(self.supported)
        self.volume = 0.37
        self.mute = 0
        self.writes = []
        self.resolves = 0
        self.change_at = None
        self.disappear_at = None
        self.read_error = None
        self.write_error = None
        self.ignore_write = False
        self.couple_controls = False

    def default_output(self):
        self.resolves += 1
        if self.resolves == self.change_at:
            self.device += 1
        if self.resolves == self.disappear_at:
            self.living = False
        return self.device

    def alive(self, device):
        return self.living

    def has_property(self, device, name):
        return self.supported[name]

    def settable(self, device, name):
        return self.writable[name]

    def read(self, device, name):
        if self.read_error:
            raise self.read_error
        return getattr(self, name)

    def write(self, device, name, value):
        self.writes.append((device, name, value))
        if not self.ignore_write:
            setattr(self, name, value)
        if self.couple_controls:
            if name == "volume":
                self.mute = int(not self.mute)
            else:
                self.volume = 0
        if self.write_error:
            raise self.write_error


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.adapter = audio.AudioAdapter(self.bridge)

    def assert_error(self, code, operation="adjust", delta=5, *, effects=False):
        with self.assertRaises(AdapterError) as caught:
            self.adapter.execute(operation, delta)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.effects_possible, effects)

    def test_validation_precedes_loading_or_bridge_calls(self):
        self.adapter = audio.AudioAdapter()
        with patch.object(audio, "_Native") as native:
            for operation, delta in [
                (None, 5),
                ([], 5),
                ("set", 5),
                ("toggle_mute", 0),
                ("toggle_mute", False),
            ]:
                self.assert_error("invalid_action", operation, delta)
            for delta in (None, True, False, 1.5, "5", [], -101, 101, math.inf, math.nan):
                self.assert_error("invalid_action", delta=delta)
            native.assert_not_called()
        self.assertEqual(self.bridge.resolves, 0)

    def test_adjust_uses_actual_fractional_volume_and_preserves_mute(self):
        for muted in (0, 1):
            with self.subTest(muted=muted):
                self.bridge.mute = muted
                self.bridge.volume = 0.373
                result = self.adapter.execute("adjust", 5)
                self.assertAlmostEqual(self.bridge.volume, 0.423)
                self.assertEqual(self.bridge.mute, muted)
                self.assertEqual(self.bridge.writes[-1][1], "volume")
                self.assertAlmostEqual(result.details["volume"], 42.3)
                self.assertEqual(result.details["muted"], bool(muted))
                self.assertEqual(result.details["effect_confirmation"], "readback")
        self.assertTrue(all(w[1] == "volume" for w in self.bridge.writes))

    def test_deadline_expiry_during_preflight_prevents_the_write(self):
        with patch.object(audio.time, "monotonic", side_effect=[0, 0, 2]):
            with self.assertRaises(AdapterError) as caught:
                self.adapter.execute("adjust", 5, timeout=1)
        self.assertEqual(caught.exception.code, "timed_out")
        self.assertFalse(caught.exception.effects_possible)
        self.assertEqual(self.bridge.writes, [])

    def test_deadline_expiry_after_write_reports_possible_effects(self):
        with patch.object(audio.time, "monotonic", side_effect=[0, 0, 0, 2]):
            with self.assertRaises(AdapterError) as caught:
                self.adapter.execute("adjust", 5, timeout=1)
        self.assertEqual(caught.exception.code, "timed_out")
        self.assertTrue(caught.exception.effects_possible)
        self.assertEqual(len(self.bridge.writes), 1)

    def test_set_volume_preserves_actual_mute_and_readback(self):
        for muted in (0, 1):
            self.bridge.mute = muted
            for value in (0, 37, 100):
                result = self.adapter.execute("set", value=value)
                self.assertAlmostEqual(self.bridge.volume, value / 100)
                self.assertEqual(self.bridge.mute, muted)
                self.assertEqual(result.details["muted"], bool(muted))

    def test_invalid_set_values_fail_before_native_access(self):
        for value in (None, True, False, -1, 101, 2.5, "50"):
            with self.assertRaises(AdapterError) as caught:
                self.adapter.execute("set", value=value)
            self.assertEqual(caught.exception.code, "invalid_action")
        self.assertEqual(self.bridge.resolves, 0)
        self.assertEqual(self.bridge.writes, [])

    def test_read_is_observed_and_never_writes(self):
        self.bridge.volume = 0.63
        self.bridge.mute = 1
        self.assertEqual(self.adapter.read(), {"value": 63, "muted": True})
        self.assertEqual(self.bridge.writes, [])
        self.bridge.change_at = self.bridge.resolves + 2
        with self.assertRaises(AdapterError):
            self.adapter.read()

    def test_clamp_and_zero_delta(self):
        for initial, delta, expected in (
            (0.97, 5, 1),
            (0.02, -5, 0),
            (0.4, 100, 1),
            (0.4, -100, 0),
            (0.373, 0, 0.373),
        ):
            self.bridge.volume = initial
            self.adapter.execute("adjust", delta)
            self.assertAlmostEqual(self.bridge.volume, expected)

    def test_toggle_reads_actual_mute_and_never_writes_volume(self):
        for initial in (0, 1, 1):
            self.bridge.mute = initial
            self.bridge.volume = 0.373
            result = self.adapter.execute("toggle_mute")
            self.assertEqual(self.bridge.mute, int(not initial))
            self.assertEqual(self.bridge.volume, 0.373)
            self.assertEqual(result.details["muted"], not initial)
        self.assertEqual([w[1] for w in self.bridge.writes], ["mute"] * 3)

    def test_each_execute_resolves_new_route_and_reads_external_changes(self):
        self.adapter.execute("adjust", 5)
        self.bridge.device = 99
        self.bridge.volume = 0.6
        self.bridge.mute = 1
        result = self.adapter.execute("adjust", -5)
        self.assertEqual(result.details["device_id"], 99)
        self.assertAlmostEqual(result.details["volume"], 55)
        self.assertTrue(result.details["muted"])
        self.assertEqual([w[0] for w in self.bridge.writes], [42, 99])

    def test_check_is_passive_and_reports_both_properties(self):
        result = self.adapter.check()
        self.assertTrue(result["available"])
        self.assertEqual(result["device_id"], 42)
        self.assertEqual(
            result["properties"],
            {
                "volume": {"supported": True, "settable": True},
                "mute": {"supported": True, "settable": True},
            },
        )
        self.assertEqual(self.bridge.writes, [])

    def test_both_controls_must_be_supported_and_writable_for_either_action(self):
        for attribute in ("supported", "writable"):
            for name in ("volume", "mute"):
                with self.subTest(attribute=attribute, name=name):
                    self.bridge = FakeBridge()
                    self.adapter = audio.AudioAdapter(self.bridge)
                    getattr(self.bridge, attribute)[name] = False
                    result = self.adapter.check()
                    self.assertEqual(result["code"], "capability_unavailable")
                    self.assertFalse(result["properties"][name]["settable"])
                    self.assert_error("capability_unavailable")
                    self.assert_error("capability_unavailable", "toggle_mute", None)
                    self.assertEqual(self.bridge.writes, [])

    def test_device_changes_and_disappearance_before_and_after_write(self):
        # Resolution 1 starts action, 2 checks initial route, 3 precedes write,
        # 4 follows write, 5 follows readback.
        for attribute in ("change_at", "disappear_at"):
            for stage in (2, 3, 4, 5):
                with self.subTest(attribute=attribute, stage=stage):
                    self.bridge = FakeBridge()
                    self.adapter = audio.AudioAdapter(self.bridge)
                    setattr(self.bridge, attribute, stage)
                    self.assert_error("target_unavailable", effects=stage >= 4)
                    self.assertEqual(len(self.bridge.writes), int(stage >= 4))

    def test_missing_default_and_dead_device_fail_without_writes(self):
        for device, living in ((0, True), (42, False)):
            self.bridge.device, self.bridge.living = device, living
            self.assert_error("target_unavailable")
            self.assertEqual(self.adapter.check()["code"], "target_unavailable")
        self.assertEqual(self.bridge.writes, [])

    def test_invalid_native_state_rejected(self):
        for name, values in (
            ("volume", (-0.1, 1.1, math.nan, math.inf, True, "0.5")),
            ("mute", (2, -1, 0.5, None)),
        ):
            for value in values:
                self.bridge = FakeBridge()
                self.adapter = audio.AudioAdapter(self.bridge)
                setattr(self.bridge, name, value)
                self.assert_error("execution_failed")
                self.assertEqual(self.adapter.check()["code"], "execution_failed")
                self.assertEqual(self.bridge.writes, [])

    def test_read_and_write_failures_are_structured_with_effect_uncertainty(self):
        for error, code in (
            (RuntimeError("private native error"), "execution_failed"),
            (AdapterError("target_unavailable", "gone"), "target_unavailable"),
            (AdapterError("capability_unavailable", "not writable"), "capability_unavailable"),
        ):
            self.bridge.read_error = error
            self.assert_error(code)
            self.bridge.read_error = None
            self.bridge.write_error = error
            self.assert_error(code, effects=True)
            self.bridge.write_error = None

    def test_readback_failure_reports_possible_effects(self):
        write = self.bridge.write

        def fail_after_write(device, name, value):
            write(device, name, value)
            self.bridge.read_error = AdapterError("target_unavailable", "disconnected")

        self.bridge.write = fail_after_write
        self.assert_error("target_unavailable", effects=True)
        self.assertEqual(len(self.bridge.writes), 1)

    def test_ignored_write_or_modified_preserved_control_never_claims_success(self):
        for attribute in ("ignore_write", "couple_controls"):
            for operation, delta in (("adjust", 5), ("toggle_mute", None)):
                self.bridge = FakeBridge()
                self.adapter = audio.AudioAdapter(self.bridge)
                setattr(self.bridge, attribute, True)
                self.assert_error("execution_failed", operation, delta, effects=True)
                self.assertEqual(len(self.bridge.writes), 1)  # No unsafe rollback.

    def test_check_handles_bridge_exception_and_route_change(self):
        self.bridge.read_error = RuntimeError("native failure")
        self.assertEqual(self.adapter.check()["code"], "capability_unavailable")
        self.bridge.read_error = None
        self.bridge.resolves = 0
        self.bridge.change_at = 3
        self.assertEqual(self.adapter.check()["code"], "target_unavailable")
        self.assertEqual(self.bridge.writes, [])

    def test_lazy_platform_and_framework_failure(self):
        with patch.object(audio.ctypes, "CDLL") as load:
            adapter = audio.AudioAdapter()
            load.assert_not_called()
            with patch.object(audio.sys, "platform", "linux"):
                self.assertEqual(adapter.check()["code"], "capability_unavailable")
                with self.assertRaises(AdapterError) as caught:
                    adapter.execute("adjust", 5)
                self.assertFalse(caught.exception.effects_possible)
            load.assert_not_called()
            with patch.object(audio.sys, "platform", "darwin"):
                load.side_effect = OSError("missing framework")
                self.assertEqual(adapter.check()["code"], "capability_unavailable")


class NativeBridgeTests(unittest.TestCase):
    def test_abi_is_declared_without_loading_core_audio(self):
        lib = SimpleNamespace(
            **{
                name: Mock(return_value=0)
                for name in (
                    "AudioObjectHasProperty",
                    "AudioObjectIsPropertySettable",
                    "AudioObjectGetPropertyData",
                    "AudioObjectSetPropertyData",
                )
            }
        )
        with (
            patch.object(audio.sys, "platform", "darwin"),
            patch.object(audio.ctypes, "CDLL", return_value=lib),
        ):
            audio._Native()
        self.assertEqual(ctypes.sizeof(audio._Address), 12)
        self.assertIs(lib.AudioObjectHasProperty.restype, ctypes.c_ubyte)
        self.assertIs(lib.AudioObjectGetPropertyData.restype, ctypes.c_int32)
        self.assertEqual(len(lib.AudioObjectSetPropertyData.argtypes), 6)

    def test_native_translation_uses_only_master_output_properties(self):
        native = object.__new__(audio._Native)
        native.lib = Mock()
        native.lib.AudioObjectHasProperty.return_value = 1
        native.lib.AudioObjectSetPropertyData.return_value = 0
        writes = []

        def write(device, address, qualifier_size, qualifier, size, data):
            address = address._obj
            writes.append(
                (device, address.selector, address.scope, address.element, size, data._obj.value)
            )
            return 0

        native.lib.AudioObjectSetPropertyData.side_effect = write
        self.assertTrue(native.has_property(42, "volume"))
        native.write(42, "volume", 0.375)
        native.write(42, "mute", 1)
        self.assertEqual(
            writes,
            [
                (42, audio._fourcc("volm"), audio._fourcc("outp"), 0, 4, 0.375),
                (42, audio._fourcc("mute"), audio._fourcc("outp"), 0, 4, 1),
            ],
        )
        reads = []

        def read(device, address, qualifier_size, qualifier, size, data):
            address = address._obj
            reads.append((device, address.selector, address.scope, address.element))
            data._obj.value = 42 if address.selector == audio._fourcc("dOut") else 1
            return 0

        native.lib.AudioObjectGetPropertyData.side_effect = read
        self.assertEqual(native.default_output(), 42)
        self.assertTrue(native.alive(42))
        self.assertEqual(native.read(42, "volume"), 1)
        self.assertEqual(
            reads,
            [
                (1, audio._fourcc("dOut"), audio._fourcc("glob"), 0),
                (42, audio._fourcc("livn"), audio._fourcc("glob"), 0),
                (42, audio._fourcc("volm"), audio._fourcc("outp"), 0),
            ],
        )

    def test_native_settable_uses_boolean_out_parameter_and_checks_status(self):
        native = object.__new__(audio._Native)
        native.lib = Mock()

        def settable(device, address, value):
            self.assertEqual(device, 42)
            self.assertEqual(address._obj.selector, audio._fourcc("mute"))
            value._obj.value = 1
            return 0

        native.lib.AudioObjectIsPropertySettable.side_effect = settable
        self.assertTrue(native.settable(42, "mute"))
        native.lib.AudioObjectIsPropertySettable.side_effect = None
        native.lib.AudioObjectIsPropertySettable.return_value = audio._fourcc("!obj")
        with self.assertRaises(AdapterError) as caught:
            native.settable(42, "mute")
        self.assertEqual(caught.exception.code, "target_unavailable")
        native.lib.AudioObjectSetPropertyData.return_value = -1
        with self.assertRaises(AdapterError) as caught:
            native.write(42, "mute", 1)
        self.assertEqual(caught.exception.code, "execution_failed")

    def test_status_and_unexpected_property_size_never_succeed(self):
        for status, code in (
            ("!obj", "target_unavailable"),
            ("!dev", "target_unavailable"),
            ("who?", "capability_unavailable"),
            ("nope", "capability_unavailable"),
            ("what", "execution_failed"),
        ):
            with self.assertRaises(AdapterError) as caught:
                audio._Native._status(audio._fourcc(status))
            self.assertEqual(caught.exception.code, code)
        native = object.__new__(audio._Native)
        native.lib = Mock()

        def short_read(device, address, qualifier_size, qualifier, size, data):
            size._obj.value = 0
            return 0

        native.lib.AudioObjectGetPropertyData.side_effect = short_read
        with self.assertRaises(AdapterError) as caught:
            native.read(42, "volume")
        self.assertEqual(caught.exception.code, "execution_failed")


if __name__ == "__main__":
    unittest.main()
