"""CLI boundary tests with fake adapters; no desktop or child-process effects."""

import json
import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from conty import cli
from conty.adapters.base import ActionResult, AdapterError
from conty.adapters.macos import keyboard


@pytest.fixture
def adapters(monkeypatch):
    factories = {}
    for name in ("KeyboardAdapter", "MusicAdapter", "AudioAdapter", "ProcessAdapter"):
        factory = Mock(name=name)
        adapter = factory.return_value
        adapter.check.return_value = {"available": True}
        result = ActionResult("Action completed", {"value": 'quoted "text"\nnext line'})
        if name == "ProcessAdapter":
            adapter.execute = AsyncMock(return_value=result)
        else:
            adapter.execute.return_value = result
        monkeypatch.setattr(cli, name, factory)
        factories[name] = factory
    return factories


def test_validate_command_is_passive_and_reports_errors(config_file, adapters, capsys):
    assert cli.main(["validate", str(config_file)]) == 0
    assert "valid" in capsys.readouterr().out
    config_file.write_text("version: invalid")
    assert cli.main(["validate", str(config_file)]) == 1
    assert "version" in capsys.readouterr().err
    for factory in adapters.values():
        factory.assert_not_called()


def test_serve_forwards_the_owner_config_without_invoking_native_adapters(
    config_file, adapters, monkeypatch
):
    from conty import service

    serve = Mock()
    monkeypatch.setattr(service, "serve", serve)
    assert cli.main(["serve", str(config_file)]) == 0
    serve.assert_called_once_with(str(config_file))
    for factory in adapters.values():
        factory.assert_not_called()


@pytest.mark.parametrize(
    "argv,key,app_id,focus,modifiers,timeout",
    [
        (["keyboard", "space"], "space", None, None, (), 5),
        (
            ["keyboard", "space", "--app-id", "org.example.Editor"],
            "space",
            "org.example.Editor",
            None,
            (),
            5,
        ),
        (
            [
                "keyboard",
                "s",
                "--modifier",
                "command",
                "--modifier",
                "shift",
                "--app-id",
                "org.example.Editor",
                "--focus",
                "require_frontmost",
                "--timeout",
                "2.5",
            ],
            "s",
            "org.example.Editor",
            "require_frontmost",
            ("command", "shift"),
            2.5,
        ),
        (
            ["keyboard", "return", "--app-id", "org.example.Editor", "--focus", "activate"],
            "return",
            "org.example.Editor",
            "activate",
            (),
            5,
        ),
    ],
)
def test_keyboard_arguments_are_forwarded(
    adapters, capsys, argv, key, app_id, focus, modifiers, timeout
):
    assert cli.main(argv) == 0
    adapters["KeyboardAdapter"].return_value.execute.assert_called_once_with(
        key, app_id=app_id, focus=focus, modifiers=modifiers, timeout=timeout
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("focus", ["activate", "require_frontmost"])
def test_focus_without_target_fails_before_native_calls(adapters, monkeypatch, capsys, focus):
    # Exercise real validation, but fail closed if it tries to load a native bridge.
    native = Mock(side_effect=AssertionError("Native access is forbidden"))
    monkeypatch.setattr(keyboard, "_Native", native)
    monkeypatch.setattr(cli, "KeyboardAdapter", keyboard.KeyboardAdapter)

    assert cli.main(["keyboard", "space", "--focus", focus]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error == {
        "status": "failed",
        "code": "invalid_action",
        "message": "focus requires an explicit app_id",
        "effects_possible": False,
        "details": {},
    }
    native.assert_not_called()
    for factory in adapters.values():
        factory.assert_not_called()


@pytest.mark.parametrize("options,should_prompt", [([], True), (["--no-prompt"], False)])
@pytest.mark.parametrize("unavailable", [None, "KeyboardAdapter", "MusicAdapter", "AudioAdapter"])
def test_doctor_reports_all_capabilities(adapters, capsys, options, should_prompt, unavailable):
    if unavailable is not None:
        adapters[unavailable].return_value.check.return_value = {
            "available": False,
            "code": "capability_unavailable",
            "message": "Not installed",
        }

    assert cli.main(["doctor", *options]) == (0 if unavailable is None else 1)
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    assert report["python"] == sys.executable
    assert report["pid"] == os.getpid()
    assert report["platform"] == sys.platform
    assert "launch" in report["permission_identity"].lower()
    for name, field in (
        ("KeyboardAdapter", "keyboard"),
        ("MusicAdapter", "music"),
        ("AudioAdapter", "audio"),
    ):
        factory = adapters[name]
        factory.assert_called_once_with()
        assert report[field] == factory.return_value.check.return_value
        if field == "keyboard":
            factory.return_value.check.assert_called_once_with(request_permission=should_prompt)
        else:
            factory.return_value.check.assert_called_once_with()
        factory.return_value.execute.assert_not_called()
    adapters["ProcessAdapter"].assert_not_called()


ACTIONS = [
    (
        ["keyboard", "space"],
        "KeyboardAdapter",
        ("space",),
        {"app_id": None, "focus": None, "modifiers": (), "timeout": 5},
    ),
    (["music", "play_pause"], "MusicAdapter", ("play_pause",), {"timeout": 5}),
    (
        ["music", "next_track", "--timeout", "1.5"],
        "MusicAdapter",
        ("next_track",),
        {"timeout": 1.5},
    ),
    (["volume", "adjust", "--delta", "-7"], "AudioAdapter", ("adjust",), {"delta": -7}),
    (["volume", "toggle_mute"], "AudioAdapter", ("toggle_mute",), {"delta": None}),
    (
        ["process", "--", "/fake/executable"],
        "ProcessAdapter",
        (["/fake/executable"],),
        {"timeout": 10, "cwd": None, "env_from": {}},
    ),
]


@pytest.mark.parametrize("argv,name,args,kwargs", ACTIONS)
def test_action_success_json_and_forwarding(adapters, capsys, argv, name, args, kwargs):
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = adapters[name].return_value.execute.return_value
    assert json.loads(captured.out) == {
        "status": "succeeded",
        "message": result.message,
        "details": result.details,
    }
    adapters[name].assert_called_once_with()
    execute = adapters[name].return_value.execute
    if name == "ProcessAdapter":
        execute.assert_awaited_once_with(*args, **kwargs)
    else:
        execute.assert_called_once_with(*args, **kwargs)
    for other_name, factory in adapters.items():
        factory.return_value.check.assert_not_called()
        if other_name != name:
            factory.assert_not_called()


@pytest.mark.parametrize("argv,name,args,kwargs", ACTIONS)
@pytest.mark.parametrize("effects_possible", [False, True])
def test_adapter_error_json_and_exit_code(
    adapters, capsys, argv, name, args, kwargs, effects_possible
):
    error = AdapterError(
        "execution_failed",
        'Could not complete "action"\nRetry later',
        effects_possible=effects_possible,
        details={"reason": "fake failure"},
    )
    execute = adapters[name].return_value.execute
    execute.side_effect = error

    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "failed",
        "code": error.code,
        "message": str(error),
        "effects_possible": effects_possible,
        "details": error.details,
    }
    if name == "ProcessAdapter":
        execute.assert_awaited_once_with(*args, **kwargs)
    else:
        execute.assert_called_once_with(*args, **kwargs)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["unknown"],
        ["doctor", "--unknown"],
        ["keyboard"],
        ["keyboard", "unsupported"],
        ["keyboard", "space", "--focus", "launch"],
        ["keyboard", "space", "--modifier", "alt"],
        ["keyboard", "space", "--timeout", "not-a-number"],
        ["keyboard", "space", "--app-id"],
        ["music", "stop"],
        ["volume", "adjust", "--delta", "1.5"],
        ["process", "--timeout", "bad"],
        ["process", "--env-from"],
    ],
)
def test_syntax_errors_exit_two_without_adapters(adapters, capsys, argv):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err
    assert "error:" in captured.err
    for factory in adapters.values():
        factory.assert_not_called()


@pytest.mark.parametrize("separator", [[], ["--"]])
def test_process_preserves_literal_arguments(adapters, capsys, separator):
    arguments = [
        "/fake/executable",
        "two words",
        "",
        "$(touch marker)",
        "a;b",
        "*.txt",
        "$HOME",
        "--timeout",
        "99",
        "--",
        "--env-from",
        "A=B",
    ]
    assert (
        cli.main(["process", "--timeout", "2.5", "--cwd", "/fake/work", *separator, *arguments])
        == 0
    )
    adapters["ProcessAdapter"].return_value.execute.assert_awaited_once_with(
        arguments, timeout=2.5, cwd="/fake/work", env_from={}
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "mappings,expected",
    [
        ([], {}),
        (["CHILD=HOST"], {"CHILD": "HOST"}),
        (["FIRST=SHARED", "SECOND=SHARED"], {"FIRST": "SHARED", "SECOND": "SHARED"}),
        (["FIRST=HOST_ONE", "SECOND=HOST_TWO"], {"FIRST": "HOST_ONE", "SECOND": "HOST_TWO"}),
        (["CHILD=HOST=SUFFIX"], {"CHILD": "HOST=SUFFIX"}),
    ],
)
def test_process_environment_mappings_are_forwarded(adapters, capsys, mappings, expected):
    options = [item for mapping in mappings for item in ("--env-from", mapping)]
    assert cli.main(["process", *options, "--", "/fake/executable"]) == 0
    adapters["ProcessAdapter"].return_value.execute.assert_awaited_once_with(
        ["/fake/executable"], timeout=10, cwd=None, env_from=expected
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "mappings",
    [
        ["MISSING_EQUALS"],
        [""],
        ["VALID=HOST", "MALFORMED"],
        ["CHILD=HOST", "CHILD=HOST"],
        ["CHILD=FIRST", "CHILD=SECOND"],
    ],
)
def test_invalid_environment_mappings_fail_without_process_call(adapters, capsys, mappings):
    options = [item for mapping in mappings for item in ("--env-from", mapping)]
    assert cli.main(["process", *options, "--", "/fake/executable"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "failed",
        "code": "invalid_action",
        "message": "Use unique --env-from CHILD=HOST mappings",
        "effects_possible": False,
        "details": {},
    }
    for factory in adapters.values():
        factory.assert_not_called()
    adapters["ProcessAdapter"].return_value.execute.assert_not_awaited()
