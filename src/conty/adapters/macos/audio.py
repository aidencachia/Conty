"""Milestone-1 Core Audio system-output controls (stdlib only).

Supported mapping: kAudioHardwarePropertyDefaultOutputDevice ('dOut') on the
system object; kAudioDevicePropertyVolumeScalar ('volm', Float32) and
kAudioDevicePropertyMute ('mute', UInt32) on output scope ('outp'), master
(main) element 0. BOTH controls must exist and be settable. Channel-only,
fixed-volume and mute-less devices are deliberately unsupported; no fallback
changes channel gains or emulates mute by overwriting volume.

Checks are passive and loading is lazy. Calls belong on Conty's serialized
worker. Device-alive/default-device checks bracket the write, followed by
readback of both controls. Readback confirms an observation, not a system-wide
transaction: external controls and route changes can still race native calls.
No rollback is attempted on failure (it could overwrite another user's change).
Core Audio calls have no Python-enforceable timeout. Real-device acceptance is
still required, including hardware quantization and sleep/disconnection cases.

An injected bridge implements default_output(), alive(device),
has_property(device, name), settable(device, name), read(device, name), and
write(device, name, value), where name is 'volume' or 'mute'.
"""

from __future__ import annotations

import ctypes
import math
import sys
import time

from conty.adapters.base import ActionResult, AdapterError, validate_timeout


def _fourcc(value: str) -> int:
    return int.from_bytes(value.encode("ascii"), "big")


class _Address(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


_PROPERTIES = {"volume": ("volm", ctypes.c_float), "mute": ("mute", ctypes.c_uint32)}


class _Native:
    def __init__(self):
        if sys.platform != "darwin":
            raise AdapterError("capability_unavailable", "System audio control requires macOS")
        try:
            self.lib = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            address = ctypes.POINTER(_Address)
            self.lib.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, address]
            self.lib.AudioObjectHasProperty.restype = ctypes.c_ubyte
            self.lib.AudioObjectIsPropertySettable.argtypes = [
                ctypes.c_uint32,
                address,
                ctypes.POINTER(ctypes.c_ubyte),
            ]
            self.lib.AudioObjectIsPropertySettable.restype = ctypes.c_int32
            self.lib.AudioObjectGetPropertyData.argtypes = [
                ctypes.c_uint32,
                address,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ]
            self.lib.AudioObjectGetPropertyData.restype = ctypes.c_int32
            self.lib.AudioObjectSetPropertyData.argtypes = [
                ctypes.c_uint32,
                address,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            self.lib.AudioObjectSetPropertyData.restype = ctypes.c_int32
        except (OSError, AttributeError) as exc:
            raise AdapterError("capability_unavailable", "Core Audio is unavailable") from exc

    @staticmethod
    def _address(name):
        return _Address(_fourcc(_PROPERTIES[name][0]), _fourcc("outp"), 0)

    @staticmethod
    def _status(status):
        if not status:
            return
        if status in (_fourcc("!obj"), _fourcc("!dev")):
            raise AdapterError("target_unavailable", "The audio device is no longer available")
        if status in (_fourcc("who?"), _fourcc("nope")):
            raise AdapterError(
                "capability_unavailable", "The audio property is unsupported or not writable"
            )
        raise AdapterError(
            "execution_failed", "Core Audio could not complete the property operation"
        )

    def _read(self, device, address, kind):
        value = kind()
        size = ctypes.c_uint32(ctypes.sizeof(value))
        self._status(
            self.lib.AudioObjectGetPropertyData(
                device, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(value)
            )
        )
        if size.value != ctypes.sizeof(value):
            raise AdapterError(
                "execution_failed", "Core Audio returned an unexpected property size"
            )
        return value.value

    def default_output(self):
        device = self._read(1, _Address(_fourcc("dOut"), _fourcc("glob"), 0), ctypes.c_uint32)
        if not device:
            raise AdapterError("target_unavailable", "No default output device is selected")
        return device

    def alive(self, device):
        return (
            self._read(device, _Address(_fourcc("livn"), _fourcc("glob"), 0), ctypes.c_uint32) == 1
        )

    def has_property(self, device, name):
        return bool(self.lib.AudioObjectHasProperty(device, ctypes.byref(self._address(name))))

    def settable(self, device, name):
        value = ctypes.c_ubyte()
        self._status(
            self.lib.AudioObjectIsPropertySettable(
                device, ctypes.byref(self._address(name)), ctypes.byref(value)
            )
        )
        return bool(value.value)

    def read(self, device, name):
        return self._read(device, self._address(name), _PROPERTIES[name][1])

    def write(self, device, name, value):
        data = _PROPERTIES[name][1](value)
        self._status(
            self.lib.AudioObjectSetPropertyData(
                device,
                ctypes.byref(self._address(name)),
                0,
                None,
                ctypes.sizeof(data),
                ctypes.byref(data),
            )
        )


class AudioAdapter:
    def __init__(self, bridge=None) -> None:
        self._bridge = bridge

    def _native(self):
        if self._bridge is None:
            self._bridge = _Native()
        return self._bridge

    def prepare(self):
        """Load Core Audio without reading or changing a device."""
        self._native()

    @staticmethod
    def _same_device(bridge, device):
        if not device or bridge.default_output() != device or not bridge.alive(device):
            raise AdapterError(
                "target_unavailable", "The selected output device changed or disappeared"
            )

    @staticmethod
    def _capabilities(bridge, device):
        properties = {}
        for name in _PROPERTIES:
            supported = bool(bridge.has_property(device, name))
            properties[name] = {
                "supported": supported,
                "settable": supported and bool(bridge.settable(device, name)),
            }
        return properties

    @staticmethod
    def _require_capabilities(properties):
        if not all(p["supported"] and p["settable"] for p in properties.values()):
            raise AdapterError(
                "capability_unavailable",
                "Output device requires writable master scalar volume and mute; no channel fallback",
            )

    @staticmethod
    def _state(bridge, device):
        volume = bridge.read(device, "volume")
        mute = bridge.read(device, "mute")
        if (
            isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or not math.isfinite(volume)
            or not 0 <= volume <= 1
            or not isinstance(mute, (int, bool))
            or mute not in (0, 1)
        ):
            raise AdapterError(
                "execution_failed", "Core Audio returned invalid volume or mute state"
            )
        return volume, bool(mute)

    def check(self) -> dict:
        """Inspect the current route and both master properties without writing."""
        details = {"mapping": "output_master_scalar_and_mute"}
        try:
            bridge = self._native()
            device = bridge.default_output()
            self._same_device(bridge, device)
            details["device_id"] = device
            properties = self._capabilities(bridge, device)
            details["properties"] = properties
            self._require_capabilities(properties)
            self._state(bridge, device)
            self._same_device(bridge, device)
            return {"available": True, **details}
        except AdapterError as exc:
            return {"available": False, "code": exc.code, "message": str(exc), **details}
        except Exception:
            return {
                "available": False,
                "code": "capability_unavailable",
                "message": "macOS audio capability could not be established",
                **details,
            }

    def execute(
        self,
        operation: str,
        delta: int | None = None,
        *,
        timeout: float | None = None,
        value: int | None = None,
    ) -> ActionResult:
        if not isinstance(operation, str) or operation not in ("adjust", "toggle_mute", "set"):
            raise AdapterError(
                "invalid_action", "Supported operations are adjust, set and toggle_mute"
            )
        if operation == "adjust":
            if type(delta) is not int or not -100 <= delta <= 100:
                raise AdapterError(
                    "invalid_action", "adjust requires an integer delta from -100 to 100"
                )
        elif delta is not None:
            raise AdapterError("invalid_action", "Only adjust accepts delta")
        if operation == "set":
            if type(value) is not int or not 0 <= value <= 100:
                raise AdapterError("invalid_action", "set requires an integer value from 0 to 100")
        elif value is not None:
            raise AdapterError("invalid_action", "Only set accepts value")

        if timeout is not None:
            validate_timeout(timeout)
        deadline = time.monotonic() + timeout if timeout is not None else None

        def check_deadline():
            if deadline is not None and time.monotonic() >= deadline:
                raise AdapterError("timed_out", "Audio deadline expired")

        effects = False
        try:
            check_deadline()
            bridge = self._native()
            device = bridge.default_output()
            self._same_device(bridge, device)
            self._require_capabilities(self._capabilities(bridge, device))
            volume, mute = self._state(bridge, device)
            if operation in ("adjust", "set"):
                new_value = (
                    value / 100 if operation == "set" else min(1.0, max(0.0, volume + delta / 100))
                )
                name, value = "volume", new_value
                expected_volume, expected_mute = new_value, mute
            else:
                name, value = "mute", int(not mute)
                expected_volume, expected_mute = volume, not mute
            self._same_device(bridge, device)
            check_deadline()
            effects = True  # Even a failed setter may have reached the device.
            bridge.write(device, name, value)
            self._same_device(bridge, device)
            actual_volume, actual_mute = self._state(bridge, device)
            self._same_device(bridge, device)
            check_deadline()
            # Float32 roundoff only, not a promise to accept arbitrary device steps.
            if (
                not math.isclose(actual_volume, expected_volume, rel_tol=0, abs_tol=1e-6)
                or actual_mute != expected_mute
            ):
                raise AdapterError(
                    "execution_failed",
                    "Audio readback did not confirm the requested state and preserved control",
                )
            return ActionResult(
                "Output mute toggled" if operation == "toggle_mute" else "Output volume adjusted",
                {
                    "device_id": device,
                    "operation": operation,
                    "volume": actual_volume * 100,
                    "muted": actual_mute,
                    "effect_confirmation": "readback",
                },
            )
        except AdapterError as exc:
            raise AdapterError(
                exc.code, str(exc), effects_possible=effects or exc.effects_possible
            ) from exc
        except Exception as exc:
            raise AdapterError(
                "execution_failed", "macOS audio operation failed", effects_possible=effects
            ) from exc

    def read(self) -> dict:
        """Observe the selected output without changing it or prompting."""
        try:
            bridge = self._native()
            device = bridge.default_output()
            self._same_device(bridge, device)
            self._require_capabilities(self._capabilities(bridge, device))
            volume, mute = self._state(bridge, device)
            self._same_device(bridge, device)
            return {"value": round(volume * 100), "muted": bool(mute)}
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("capability_unavailable", "System volume could not be read") from exc
