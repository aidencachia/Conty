import asyncio
import sys
from pathlib import Path

import pytest

from conty.adapters.base import AdapterError
from conty.adapters.process import OUTPUT_LIMIT, ProcessAdapter


def run(code, **kwargs):
    return asyncio.run(ProcessAdapter().execute([sys.executable, "-c", code], **kwargs))


def test_direct_arguments_no_shell(tmp_path):
    marker = tmp_path / "not-created"
    argument = f"; touch {marker}"
    result = asyncio.run(
        ProcessAdapter().execute([sys.executable, "-c", "import sys; print(sys.argv[1])", argument])
    )
    assert result.details["stdout"].strip() == argument
    assert not marker.exists()


def test_both_streams_bounded_and_drained():
    result = run('import os; os.write(1,b"x"*200000); os.write(2,b"y"*200000)')
    assert len(result.details["stdout"]) == OUTPUT_LIMIT
    assert len(result.details["stderr"]) == OUTPUT_LIMIT
    assert result.details["stdout_truncated"]
    assert result.details["stderr_truncated"]


def test_environment_is_minimal_and_explicit(monkeypatch):
    monkeypatch.setenv("CONTY_PRIVATE_TEST", "secret")
    assert (
        run('import os; print(os.getenv("CONTY_PRIVATE_TEST"))').details["stdout"].strip() == "None"
    )
    assert (
        run(
            'import os; print(os.getenv("CHILD_TEST"))',
            env_from={"CHILD_TEST": "CONTY_PRIVATE_TEST"},
        )
        .details["stdout"]
        .strip()
        == "secret"
    )


def test_working_directory(tmp_path):
    result = run("import os; print(os.getcwd())", cwd=str(tmp_path))
    assert Path(result.details["stdout"].strip()) == tmp_path.resolve()


def test_nonzero_exit():
    with pytest.raises(AdapterError) as caught:
        run('import sys; print("failure",file=sys.stderr); sys.exit(7)')
    assert caught.value.code == "execution_failed"
    assert caught.value.details["returncode"] == 7
    assert caught.value.effects_possible


def test_missing_executable():
    with pytest.raises(AdapterError) as caught:
        asyncio.run(ProcessAdapter().execute(["/not/a/conty/executable"]))
    assert caught.value.code == "execution_failed"
    assert not caught.value.effects_possible


@pytest.mark.parametrize(
    "arguments,timeout",
    [
        ([], 1),
        (["relative"], 1),
        ("/bin/echo", 1),
        (["/bin/echo", 4], 1),
        (["/bin/echo"], 0),
        (["/bin/echo"], float("nan")),
    ],
)
def test_invalid_action(arguments, timeout):
    with pytest.raises(AdapterError) as caught:
        asyncio.run(ProcessAdapter().execute(arguments, timeout=timeout))
    assert caught.value.code == "invalid_action"


def test_timeout_kills_group_and_remains_responsive(tmp_path):
    pidfile = tmp_path / "pid"
    # Descendant ignores TERM and holds pipes open, requiring group KILL.
    code = (
        "import subprocess,sys,time; "
        'p=subprocess.Popen([sys.executable,"-c",'
        '"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"]); '
        f'open({str(pidfile)!r},"w").write(str(p.pid)); time.sleep(60)'
    )

    async def scenario():
        adapter = ProcessAdapter()
        with pytest.raises(AdapterError) as caught:
            await adapter.execute([sys.executable, "-c", code], timeout=0.5)
        assert caught.value.code == "timed_out"
        assert caught.value.details["cleanup"] == "group_terminated_child_reaped"
        result = await adapter.execute([sys.executable, "-c", 'print("ready")'])
        assert result.details["stdout"].strip() == "ready"

    asyncio.run(scenario())
    assert pidfile.exists()


def test_cancel_reaps_child(tmp_path):
    async def scenario():
        task = asyncio.create_task(
            ProcessAdapter().execute([sys.executable, "-c", "import time; time.sleep(60)"])
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
