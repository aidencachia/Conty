import asyncio
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conty.adapters.base import AdapterError
from conty.executor import Executor
from conty.service import instance_lock


def test_volume_observations_are_shared_and_invalidated_after_writes():
    async def scenario():
        calls = []

        async def observe(payload, timeout, *, probe):
            calls.append(payload)
            await asyncio.sleep(0.01)
            return {"ok": True, "volume": {"value": 37 if len(calls) == 1 else 68, "muted": False}}

        executor = Executor()
        executor._diagnostics = SimpleNamespace(available=True, helper=observe)
        results = await asyncio.gather(*(executor.read_volume() for _ in range(10)))
        assert len(calls) == 1 and all(item["value"] == 37 for item in results)
        results[0]["value"] = 99
        assert (await executor.read_volume())["value"] == 37
        executor.helper = AsyncMock(return_value={"ok": True})
        await executor.execute(
            {"type": "volume", "operation": "set", "value": 68, "timeout_seconds": 5}
        )
        assert (await executor.read_volume())["value"] == 68
        assert len(calls) == 2
        await executor.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "observation",
    [
        {"value": True, "muted": False},
        {"value": 101, "muted": False},
        {"value": 50, "muted": 1},
        {"value": 50, "muted": False, "device_id": "PRIVATE"},
    ],
)
def test_volume_protocol_rejects_invalid_or_private_fields(observation):
    with pytest.raises(ValueError):
        Executor._validate_result(
            {"ok": True, "volume": observation}, {"read_volume": True}, probe=True
        )


def test_native_helper_protocol_without_desktop_effects():
    async def scenario():
        executor = Executor(
            helper_command=[
                sys.executable,
                "-c",
                "import sys,json; p=json.load(sys.stdin); print(json.dumps({'ok':True}))",
            ]
        )
        result = await executor.helper({"action": {}}, 2)
        assert result == {"ok": True} and executor.available
        await executor.close()

    asyncio.run(scenario())


def test_real_native_helper_rejects_expired_deadline_before_any_desktop_access():
    async def scenario():
        executor = Executor()
        with pytest.raises(AdapterError) as caught:
            await executor.helper({"action": {"type": "keyboard", "deadline_monotonic": -1.0}}, 2)
        assert caught.value.code == "timed_out"
        assert not caught.value.effects_possible
        assert executor.available
        await executor.close()

    asyncio.run(scenario())


def test_persistent_worker_reuses_one_process_and_recovers_from_ordinary_action_error():
    async def scenario():
        executor = Executor()
        try:
            children = []
            for _ in range(3):
                with pytest.raises(AdapterError) as caught:
                    await executor.helper(
                        {"action": {"type": "keyboard", "deadline_monotonic": -1}}, 2
                    )
                assert caught.value.code == "timed_out"
                children.append(executor._child)
            assert children[0] is children[1] is children[2]
            pid = children[0].pid
        finally:
            await executor.close()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        "invalid",
        '{"id":999,"result":{"ok":true}}',
        '{"id":1,"result":{"ok":"yes"}}',
        pytest.param("x" * 40000, id="oversized-frame"),
    ],
)
def test_persistent_protocol_corruption_kills_worker_and_prevents_reuse(response):
    async def scenario():
        code = "import sys; print('{\"ready\":true}',flush=True); sys.stdin.readline(); print(sys.argv[1],flush=True); sys.stdin.read()"
        executor = Executor(helper_command=[sys.executable, "-c", code, response], persistent=True)
        with pytest.raises(AdapterError) as caught:
            await executor.helper({}, 2)
        assert caught.value.code == "executor_unavailable"
        assert not executor.available and executor._child is None
        with pytest.raises(AdapterError):
            await executor.helper({}, 2)
        await executor.close()

    asyncio.run(scenario())


def test_persistent_timeout_kills_worker_and_rejects_later_input():
    async def scenario():
        code = "import sys,time; print('{\"ready\":true}',flush=True); sys.stdin.readline(); time.sleep(30)"
        executor = Executor(helper_command=[sys.executable, "-c", code], persistent=True)
        async with executor._lock:
            await executor._start_worker()
        pid = executor._child.pid
        with pytest.raises(AdapterError) as caught:
            await executor.helper({}, 0.05)
        assert caught.value.effects_possible
        assert not executor.available
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with pytest.raises(AdapterError):
            await executor.helper({}, 2)
        await executor.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code",
    [
        "print('invalid')",
        "print('x' * 40000)",
        "raise SystemExit(1)",
        'print(\'{"ok":"yes"}\')',
        'print(\'{"ok":true,"unexpected":true}\')',
        'print(\'{"ok":false,"code":"unknown","effects_possible":false}\')',
    ],
)
def test_invalid_helper_response_latches_executor(code):
    async def scenario():
        executor = Executor(helper_command=[sys.executable, "-c", code])
        with pytest.raises(AdapterError) as caught:
            await executor.helper({}, 2)
        assert caught.value.code == "executor_unavailable"
        assert not executor.available

    asyncio.run(scenario())


def test_hung_native_group_is_killed_and_http_loop_remains_responsive(tmp_path):
    pid_file = tmp_path / "helper.pid"
    code = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); sys.stdin.read(); time.sleep(30)"

    async def scenario():
        executor = Executor(helper_command=[sys.executable, "-c", code, str(pid_file)])
        ticks = []

        async def heartbeat():
            for _ in range(10):
                await asyncio.sleep(0.02)
                ticks.append(True)

        started = time.monotonic()
        task = asyncio.create_task(heartbeat())
        with pytest.raises(AdapterError) as caught:
            await executor.helper({}, 0.2)
        await task
        assert caught.value.code == "executor_unavailable"
        assert caught.value.effects_possible and not executor.available
        assert len(ticks) == 10 and time.monotonic() - started < 2
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    asyncio.run(scenario())


def test_process_execution_keeps_output_local(tmp_path):
    async def scenario():
        executor = Executor()
        result = await executor.execute(
            {
                "type": "process",
                "argv": [sys.executable, "-c", "print('local only')"],
                "cwd": str(tmp_path),
                "env_from": {},
                "timeout_seconds": 2,
            }
        )
        assert result.details["stdout"] == "local only\n"
        assert await executor.check() == {"process": {"available": True}}

    asyncio.run(scenario())


def test_unexpected_idle_worker_exit_is_not_silently_restarted():
    async def scenario():
        code = 'import sys; print(\'{"ready":true}\',flush=True); sys.stdin.readline(); print(\'{"id":1,"result":{"ok":true}}\',flush=True)'
        executor = Executor(helper_command=[sys.executable, "-c", code], persistent=True)
        assert await executor.helper({}, 2) == {"ok": True}
        child = executor._child
        await child.wait()
        with pytest.raises(AdapterError) as caught:
            await executor.helper({}, 2)
        assert caught.value.code == "executor_unavailable"
        assert not executor.available and executor._child is None
        await executor.close()

    asyncio.run(scenario())


def test_probe_timeout_does_not_poison_the_persistent_action_worker():
    async def scenario():
        code = """import json,sys,time
if '--persistent' in sys.argv:
    print(json.dumps({'ready':True}),flush=True)
    for line in sys.stdin:
        frame=json.loads(line)
        print(json.dumps({'id':frame['id'],'result':{'ok':True}}),flush=True)
else:
    sys.stdin.read()
    time.sleep(30)
"""
        command = [sys.executable, "-c", code]
        executor = Executor(helper_command=command, native_types=("keyboard",), persistent=True)
        diagnostics = Executor(helper_command=command, persistent=False)
        original = diagnostics.helper

        async def bounded_probe(payload, timeout, *, probe):
            return await original(payload, 0.05, probe=probe)

        diagnostics.helper = bounded_probe
        executor._diagnostics = diagnostics
        try:
            await executor.start()
            child = executor._child
            with pytest.raises(AdapterError):
                await executor.check()
            assert not diagnostics.available and executor.available
            assert await executor.helper({}, 1) == {"ok": True}
            assert executor._child is child
        finally:
            await executor.close()

    asyncio.run(scenario())


def test_instance_lock_prevents_duplicates_and_releases(tmp_path):
    directory = tmp_path / "runtime"
    with instance_lock(directory):
        assert directory.stat().st_mode & 0o777 == 0o700
        with pytest.raises(OSError, match="already running"):
            with instance_lock(directory):
                pytest.fail("Duplicate instance admitted")
    with instance_lock(directory):
        pass


def test_instance_lock_rejects_shared_directory_and_symlinks(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(OSError, match="owner-only"):
        with instance_lock(shared):
            pass
    link = tmp_path / "link"
    link.symlink_to(shared)
    with pytest.raises(OSError):
        with instance_lock(link):
            pass
