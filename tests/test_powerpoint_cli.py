"""Local authorization/startup boundaries; no native effects or network listeners."""

import asyncio
import json
import sys
from contextlib import nullcontext
from types import ModuleType
from unittest.mock import AsyncMock, Mock, call

import pytest

from conty import cli
from conty.adapters.base import AdapterError
from conty.config import ConfigurationError


@pytest.fixture(autouse=True)
def native(monkeypatch):
    # Replace the import itself, so these tests cannot load the real native adapter.
    module = ModuleType("conty.adapters.macos.powerpoint")
    factory = Mock(name="NativePowerPoint")
    adapter = factory.return_value
    adapter.authorize = AsyncMock(return_value={"available": True})
    adapter.close = AsyncMock()
    for name in ("discover", "observe", "refresh", "command"):
        setattr(adapter, name, AsyncMock(side_effect=AssertionError("Unexpected native call")))
    module.NativePowerPoint = factory
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return factory


def test_authorize_awaits_local_request_then_closes_on_same_loop(native, capsys):
    loops = []

    async def authorize():
        loops.append(asyncio.get_running_loop())
        return {
            "available": True,
            "fullname": "/private/deck.pptx",
            "python": "/private/python",
            "handle": {"pid": 123, "window_id": 456},
            "permission": "automation",
            "prompt_requested": True,
        }

    async def close():
        loops.append(asyncio.get_running_loop())

    adapter = native.return_value
    adapter.authorize.side_effect = authorize
    adapter.close.side_effect = close
    assert cli.main(["powerpoint", "authorize"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"available": True}
    native.assert_called_once_with()
    adapter.authorize.assert_awaited_once_with()
    adapter.close.assert_awaited_once_with()
    assert adapter.mock_calls == [call.authorize(), call.close()]
    assert len(loops) == 2 and loops[0] is loops[1]


@pytest.mark.parametrize("value", [False, None, "yes", 1])
def test_authorize_only_reports_explicit_boolean_availability(native, capsys, value):
    native.return_value.authorize.return_value = {"available": value, "path": "/private/deck"}
    assert cli.main(["powerpoint", "authorize"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"available": False}
    native.return_value.close.assert_awaited_once_with()


@pytest.mark.parametrize("code", ["permission_denied", "target_unavailable", "timed_out"])
def test_authorize_closes_on_adapter_error_and_uses_existing_error_json(native, capsys, code):
    error = AdapterError(code, "Safe native error", effects_possible=code == "timed_out")
    adapter = native.return_value
    adapter.authorize.side_effect = error
    assert cli.main(["powerpoint", "authorize"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "failed",
        "code": code,
        "message": str(error),
        "effects_possible": error.effects_possible,
        "details": {},
    }
    adapter.authorize.assert_awaited_once_with()
    adapter.close.assert_awaited_once_with()
    assert adapter.mock_calls == [call.authorize(), call.close()]


def test_authorize_closes_even_on_unexpected_error(native):
    native.return_value.authorize.side_effect = RuntimeError("fake unexpected failure")
    with pytest.raises(RuntimeError, match="fake unexpected failure"):
        cli.main(["powerpoint", "authorize"])
    native.return_value.close.assert_awaited_once_with()


def test_authorize_closes_on_cancellation(native):
    native.return_value.authorize.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli._authorize_powerpoint())
    native.return_value.close.assert_awaited_once_with()


@pytest.mark.parametrize("options", [[], ["--no-prompt"]])
def test_doctor_never_requests_powerpoint_automation(native, monkeypatch, capsys, options):
    for name in ("KeyboardAdapter", "MediaAdapter", "AudioAdapter"):
        factory = Mock()
        factory.return_value.check.return_value = {"available": True}
        monkeypatch.setattr(cli, name, factory)
    assert cli.main(["doctor", *options]) == 0
    assert capsys.readouterr().err == ""
    native.assert_not_called()


def test_validate_never_requests_powerpoint_automation(native, config_file, capsys):
    assert cli.main(["validate", str(config_file)]) == 0
    assert "valid" in capsys.readouterr().out
    native.assert_not_called()


@pytest.mark.parametrize(
    "options,forwarded",
    [
        ([], {}),
        (
            ["--ssl-certfile", "cert.pem", "--ssl-keyfile", "key.pem"],
            {"ssl_certfile": "cert.pem", "ssl_keyfile": "key.pem"},
        ),
        (["--ssl-certfile", "cert.pem"], {"ssl_certfile": "cert.pem", "ssl_keyfile": None}),
        (["--ssl-keyfile", "key.pem"], {"ssl_certfile": None, "ssl_keyfile": "key.pem"}),
    ],
)
def test_serve_cli_preserves_tls_forwarding_without_authorizing(
    native, monkeypatch, config_file, capsys, options, forwarded
):
    from conty import service

    serve = Mock()
    monkeypatch.setattr(service, "serve", serve)
    assert cli.main(["serve", str(config_file), *options]) == 0
    serve.assert_called_once_with(str(config_file), **forwarded)
    assert capsys.readouterr().out == ""
    native.assert_not_called()


@pytest.mark.parametrize("argv", [["powerpoint"], ["powerpoint", "refresh"]])
def test_powerpoint_only_exposes_explicit_local_authorization(native, capsys, argv):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    native.assert_not_called()


def test_serve_help_describes_optional_https(native, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--help"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "optional HTTPS" in output
    assert "--ssl-certfile" in output and "--ssl-keyfile" in output
    assert "add-in" not in output.lower()
    native.assert_not_called()


@pytest.fixture
def startup(monkeypatch):
    import qrcode
    import uvicorn

    from conty import server, service

    logger = service.logging.getLogger("conty")
    for name in ("handlers", "level", "propagate"):
        monkeypatch.setattr(logger, name, getattr(logger, name))
    create = Mock(return_value=object())
    run = Mock()
    context = Mock()
    ssl_context = Mock(return_value=context)
    lock = Mock(side_effect=nullcontext)
    qr = Mock()
    monkeypatch.setattr(server, "create_app", create)
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(service.ssl, "SSLContext", ssl_context)
    monkeypatch.setattr(service, "instance_lock", lock)
    monkeypatch.setattr(qrcode, "QRCode", Mock(return_value=qr))
    return service, create, run, context, ssl_context, lock, qr


@pytest.mark.parametrize("tls", [False, True])
def test_startup_is_nonprompting_and_has_no_office_setup_output(
    native, startup, config_file, tmp_path, capsys, tls
):
    service, create, run, context, ssl_context, lock, qr = startup
    options = {}
    if tls:
        cert = str(tmp_path / "cert.pem")
        key = str(tmp_path / "key.pem")
        options = {"ssl_certfile": cert, "ssl_keyfile": key}
    service.serve(config_file, **options)
    native.assert_not_called()
    lock.assert_called_once_with()
    create.assert_called_once()
    run.assert_called_once()
    assert run.call_args.args == (create.return_value,)
    arguments = run.call_args.kwargs
    assert arguments["workers"] == 1
    assert arguments["proxy_headers"] is False
    assert arguments["ssl_certfile"] == options.get("ssl_certfile")
    assert arguments["ssl_keyfile"] == options.get("ssl_keyfile")
    url = f"{'https' if tls else 'http'}://127.0.0.1:8765"
    qr.add_data.assert_called_once_with(url)
    output = capsys.readouterr().out
    assert f"Dashboard: {url}" in output
    assert "revokes all sessions" in output
    assert not any(word in output.lower() for word in ("manifest", "office", "add-in", "prompt"))
    if tls:
        ssl_context.assert_called_once_with(service.ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain.assert_called_once()
        assert context.load_cert_chain.call_args.args == (cert, key)
        assert context.load_cert_chain.call_args.kwargs["password"]() == ""
    else:
        ssl_context.assert_not_called()
        assert "Trusted-LAN HTTP: traffic is unencrypted" in output
        assert "requires HTTPS" not in output


@pytest.mark.parametrize("flag", ["ssl_certfile", "ssl_keyfile"])
def test_incomplete_tls_still_fails_before_startup(native, startup, config_file, capsys, flag):
    service, create, run, _, ssl_context, lock, _ = startup
    with pytest.raises(ConfigurationError, match="Use both"):
        service.serve(config_file, **{flag: "unused.pem"})
    assert capsys.readouterr().out == ""
    for mocked in (native, create, run, ssl_context, lock):
        mocked.assert_not_called()


def test_invalid_tls_still_fails_before_advertising(native, startup, config_file, capsys):
    service, create, run, context, _, lock, _ = startup
    context.load_cert_chain.side_effect = OSError("Invalid test certificate")
    with pytest.raises(ConfigurationError, match="Invalid test certificate"):
        service.serve(config_file, ssl_certfile="unused-cert.pem", ssl_keyfile="unused-key.pem")
    assert capsys.readouterr().out == ""
    for mocked in (native, create, run, lock):
        mocked.assert_not_called()
