"""Bounded native helpers; subprocess waits never run on the HTTP event loop."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time

from conty.adapters.base import ActionResult, AdapterError
from conty.adapters.process import ProcessAdapter

HELPER_OUTPUT_LIMIT = 32 * 1024
HELPER_CODES = frozenset(
    {
        "permission_denied",
        "target_unavailable",
        "target_not_frontmost",
        "capability_unavailable",
        "invalid_action",
        "execution_failed",
        "timed_out",
        "executor_unavailable",
    }
)


class Executor:
    def __init__(self, *, helper_command=None, native_types=(), persistent=None):
        self.process = ProcessAdapter()
        self.helper_command = helper_command or [sys.executable, "-m", "conty.native_worker"]
        self.persistent = helper_command is None if persistent is None else persistent
        self.native_types = tuple(sorted(set(native_types)))
        self.available = True
        self._child = None
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._diagnostics = None
        self._volume_cache = None
        self._volume_task = None
        self._volume_epoch = 0

    async def start(self):
        if self.persistent and self.native_types:
            async with self._lock:
                await self._start_worker()

    async def _start_worker(self):
        if self._child is not None:
            return
        if not self.available:
            raise AdapterError("executor_unavailable", "Restart the executor locally")
        try:
            self._child = await asyncio.create_subprocess_exec(
                *self.helper_command,
                "--persistent",
                "--warm",
                ",".join(self.native_types),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                limit=HELPER_OUTPUT_LIMIT,
            )
            ready = await asyncio.wait_for(self._child.stdout.readline(), 7)
            handshake = json.loads(ready)
            if (
                not isinstance(handshake, dict)
                or set(handshake) != {"ready"}
                or handshake["ready"] is not True
            ):
                raise ValueError("Invalid native worker handshake")
        except BaseException as exc:
            await self._break_worker()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AdapterError("executor_unavailable", "Native worker did not start") from exc

    async def _break_worker(self):
        self.available = False
        child, self._child = self._child, None
        if child is not None:
            await self.terminate(child)

    async def helper(self, payload, timeout, *, probe=False):
        if not self.available:
            raise AdapterError("executor_unavailable", "Restart the executor locally")
        if not self.persistent:
            return await self._oneshot_helper(payload, timeout, probe=probe)
        async with self._lock:
            await self._start_worker()
            child = self._child
            self._sequence += 1
            identity = self._sequence

            async def exchange():
                child.stdin.write(json.dumps({"id": identity, "payload": payload}).encode() + b"\n")
                await child.stdin.drain()
                frame = json.loads(await child.stdout.readline())
                if (
                    not isinstance(frame, dict)
                    or set(frame) != {"id", "result"}
                    or type(frame["id"]) is not int
                    or frame["id"] != identity
                ):
                    raise ValueError("Invalid native worker frame")
                return frame["result"]

            try:
                result = await asyncio.wait_for(exchange(), timeout)
                return self._validate_result(result, payload, probe=probe)
            except AdapterError as exc:
                if exc.code == "executor_unavailable":
                    await self._break_worker()
                raise
            except BaseException as exc:
                await self._break_worker()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AdapterError(
                    "executor_unavailable",
                    "Native worker failed; restart locally",
                    effects_possible=not probe,
                ) from exc

    @staticmethod
    def _validate_result(result, payload, *, probe):
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("Invalid native helper response")
        if not result["ok"]:
            if (
                set(result) - {"ok", "code", "effects_possible"}
                or result.get("code") not in HELPER_CODES
                or type(result.get("effects_possible")) is not bool
            ):
                raise ValueError("Invalid native helper error")
            raise AdapterError(
                result["code"],
                "Native operation failed",
                effects_possible=result["effects_possible"],
            )
        if "read_volume" in payload:
            volume = result.get("volume")
            if (
                set(result) != {"ok", "volume"}
                or not isinstance(volume, dict)
                or set(volume) != {"value", "muted"}
                or type(volume["value"]) is not int
                or not 0 <= volume["value"] <= 100
                or type(volume["muted"]) is not bool
            ):
                raise ValueError("Invalid volume observation")
        elif probe:
            if set(result) != {"ok", "readiness"} or not isinstance(result["readiness"], dict):
                raise ValueError("Invalid readiness response")
            if set(result["readiness"]) != set(payload["check"]):
                raise ValueError("Invalid readiness capabilities")
            for report in result["readiness"].values():
                if (
                    not isinstance(report, dict)
                    or set(report) != {"available", "code"}
                    or type(report["available"]) is not bool
                    or report["code"] not in HELPER_CODES | {None}
                ):
                    raise ValueError("Invalid readiness report")
        elif set(result) != {"ok"}:
            raise ValueError("Invalid action result")
        return result

    async def _oneshot_helper(self, payload, timeout, *, probe=False):
        try:
            child = await asyncio.create_subprocess_exec(
                *self.helper_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise AdapterError(
                "capability_unavailable", "Could not start the native helper"
            ) from exc

        async def exchange():
            child.stdin.write(json.dumps(payload).encode())
            await child.stdin.drain()
            child.stdin.close()
            output = bytearray()
            while chunk := await child.stdout.read(4096):
                output.extend(chunk)
                if len(output) > HELPER_OUTPUT_LIMIT:
                    raise ValueError("Native helper output exceeded its bound")
            await child.wait()
            if child.returncode:
                raise ValueError("Native helper exited unexpectedly")
            return json.loads(output)

        task = asyncio.create_task(exchange())
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout)
            return self._validate_result(result, payload, probe=probe)
        except AdapterError as exc:
            if exc.code == "executor_unavailable":
                self.available = False
                await self.terminate(child)
            raise
        except BaseException as exc:
            # A hard native deadline cannot establish key-up or effect completion.
            self.available = False
            try:
                await self.terminate(child)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AdapterError(
                "executor_unavailable",
                "Native helper failed; restart locally",
                effects_possible=not probe,
            ) from exc

    @staticmethod
    async def terminate(child):
        for sig, grace in ((signal.SIGTERM, 0.2), (signal.SIGKILL, 1.0)):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass
            # Send KILL even if the leader exits: a legacy script may still run.
            await asyncio.sleep(grace if sig == signal.SIGTERM else 0)
        try:
            await asyncio.wait_for(child.wait(), 1)
        except TimeoutError as exc:
            raise AdapterError(
                "executor_unavailable",
                "Native helper cleanup was unconfirmed",
                effects_possible=True,
            ) from exc

    async def execute(self, action):
        if not self.available:
            raise AdapterError("executor_unavailable", "Restart the executor locally")
        timeout = action["timeout_seconds"]
        if action["type"] == "process":
            try:
                return await self.process.execute(
                    action["argv"],
                    timeout=timeout,
                    cwd=action.get("cwd"),
                    env_from=action["env_from"],
                )
            except AdapterError as exc:
                if exc.code == "executor_unavailable":
                    self.available = False
                raise
        # Startup normally happens once before admission; keep cold-start calls
        # bounded by the same host deadline too.
        definition = {**action, "deadline_monotonic": time.monotonic() + timeout}
        is_volume = action["type"] == "volume"
        if is_volume:
            self._volume_epoch += 1
            self._volume_cache = None
        try:
            result = await self.helper({"action": definition}, timeout + 1.5)
        finally:
            if is_volume:
                self._volume_epoch += 1
                self._volume_cache = None
        return ActionResult("Action dispatched", result.get("details", {}))

    async def check(self):
        if not self.native_types:
            return {"process": {"available": True}}
        # Bound each capability separately so one slow check cannot consume
        # another capability's probe deadline.
        readiness = {"process": {"available": True}}
        if self._diagnostics is None or not self._diagnostics.available:
            self._diagnostics = Executor(helper_command=self.helper_command, persistent=False)
        for kind in self.native_types:
            result = await self._diagnostics.helper({"check": [kind]}, 7, probe=True)
            readiness.update(result["readiness"])
        return readiness

    async def close(self):
        self._volume_cache = None
        if self._volume_task is not None:
            self._volume_task.cancel()
            await asyncio.gather(self._volume_task, return_exceptions=True)
            self._volume_task = None
        async with self._lock:
            child, self._child = self._child, None
            self.available = False
            if child is not None:
                child.stdin.close()
                try:
                    await asyncio.wait_for(child.wait(), 1)
                except TimeoutError:
                    await self.terminate(child)

    async def read_volume(self):
        cached = self._volume_cache
        if cached is not None and time.monotonic() - cached[0] < 0.25:
            if cached[2] is not None:
                raise AdapterError(cached[2], "System volume is unavailable")
            return dict(cached[1])
        if self._volume_task is None:
            self._volume_task = asyncio.create_task(self._observe_volume())
            self._volume_task.add_done_callback(self._volume_finished)
        task = self._volume_task
        try:
            return dict(await asyncio.shield(task))
        finally:
            if task.done() and self._volume_task is task:
                self._volume_task = None

    def _volume_finished(self, task):
        if self._volume_task is task:
            self._volume_task = None
        if not task.cancelled():
            task.exception()  # A disconnected reader must not leave an unobserved error.

    async def _observe_volume(self):
        epoch = self._volume_epoch
        if self._diagnostics is None or not self._diagnostics.available:
            self._diagnostics = Executor(helper_command=self.helper_command, persistent=False)
        try:
            result = await self._diagnostics.helper({"read_volume": True}, 2, probe=True)
        except AdapterError as exc:
            if epoch == self._volume_epoch:
                self._volume_cache = (time.monotonic(), None, exc.code)
            raise
        if epoch == self._volume_epoch:
            self._volume_cache = (time.monotonic(), dict(result["volume"]), None)
        return result["volume"]
