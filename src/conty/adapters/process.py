"""Direct child execution with bounded capture and POSIX process-group cleanup."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path

from conty.adapters.base import ActionResult, AdapterError, validate_timeout

OUTPUT_LIMIT = 16 * 1024  # Per stream, in bytes; excess is drained, not retained.


class ProcessAdapter:
    def __init__(self) -> None:
        self._unavailable = False

    async def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 10,
        cwd: str | None = None,
        env_from: Mapping[str, str] | None = None,
    ) -> ActionResult:
        if self._unavailable:
            raise AdapterError(
                "executor_unavailable",
                "Previous process cleanup failed; restart after local recovery",
            )
        if (
            isinstance(argv, (str, bytes))
            or not isinstance(argv, Sequence)
            or not argv
            or any(not isinstance(a, str) or "\0" in a for a in argv)
            or not Path(argv[0]).is_absolute()
        ):
            raise AdapterError(
                "invalid_action", "argv must contain an absolute executable and string arguments"
            )
        validate_timeout(timeout)
        if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
            raise AdapterError("invalid_action", "cwd must be an absolute directory")
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}
        if env_from is not None:
            if not isinstance(env_from, Mapping):
                raise AdapterError(
                    "invalid_action",
                    "env_from must map child variable names to host variable names",
                )
            for destination, source in env_from.items():
                if (
                    not isinstance(destination, str)
                    or not destination
                    or "=" in destination
                    or "\0" in destination
                    or not isinstance(source, str)
                    or not source
                ):
                    raise AdapterError("invalid_action", "Invalid environment mapping")
                if source not in os.environ:
                    raise AdapterError(
                        "invalid_action", f"Host environment variable {source!r} is not defined"
                    )
                env[destination] = os.environ[source]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise AdapterError(
                "execution_failed", "Could not start the configured executable"
            ) from exc

        captures = [bytearray(), bytearray()]
        truncated = [False, False]

        async def drain(stream, index):
            while chunk := await stream.read(8192):
                room = OUTPUT_LIMIT - len(captures[index])
                captures[index].extend(chunk[:room])
                if len(chunk) > room:
                    truncated[index] = True

        tasks = [
            asyncio.create_task(drain(process.stdout, 0)),
            asyncio.create_task(drain(process.stderr, 1)),
            asyncio.create_task(process.wait()),
        ]
        completion = asyncio.gather(*tasks)

        def send(sig):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass

        async def cleanup():
            send(signal.SIGTERM)
            # Even if the leader has exited, descendants may ignore TERM.
            await asyncio.sleep(0.2)
            send(signal.SIGKILL)
            await asyncio.wait_for(asyncio.shield(completion), 1)

        try:
            await asyncio.wait_for(asyncio.shield(completion), timeout)
        except BaseException as exc:
            try:
                await cleanup()
            except (OSError, asyncio.TimeoutError) as cleanup_error:
                self._unavailable = True
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise AdapterError(
                    "executor_unavailable",
                    "Process cleanup could not be confirmed; stop dispatching",
                    effects_possible=True,
                    details={"cleanup": "unconfirmed"},
                ) from cleanup_error
            if isinstance(exc, asyncio.TimeoutError):
                raise AdapterError(
                    "timed_out",
                    "Process timed out; group termination attempted and child reaped; external effects may remain",
                    effects_possible=True,
                    details={"cleanup": "group_terminated_child_reaped"},
                ) from exc
            raise
        details = {
            "returncode": process.returncode,
            "stdout": captures[0].decode("utf-8", errors="replace"),
            "stderr": captures[1].decode("utf-8", errors="replace"),
            "stdout_truncated": truncated[0],
            "stderr_truncated": truncated[1],
            "cleanup": "child_reaped",
        }
        if process.returncode != 0:
            raise AdapterError(
                "execution_failed",
                f"Process exited with status {process.returncode}",
                effects_possible=True,
                details=details,
            )
        return ActionResult("Configured process exited successfully", details)
