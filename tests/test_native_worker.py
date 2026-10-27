"""Private-protocol routing tests with fake adapters only."""

import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from conty import native_worker


@pytest.mark.parametrize(
    "target,selected",
    [(None, "media"), ("system", "media"), ({"app_id": "com.apple.Music"}, "music")],
)
def test_media_routes_only_to_the_configured_adapter(monkeypatch, target, selected):
    monkeypatch.setattr(native_worker.time, "monotonic", lambda: 10)
    adapters = {"media": Mock(), "music": Mock()}
    action = {"type": "media", "command": "play_pause", "deadline_monotonic": 15}
    if target is not None:
        action["target"] = target
    assert native_worker.handle({"action": action}, adapters) == {"ok": True}
    adapters[selected].execute.assert_called_once_with("play_pause", timeout=5)
    adapters["music" if selected == "media" else "media"].execute.assert_not_called()


@pytest.mark.parametrize("target", ["spotify", {"app_id": "com.spotify.client"}, None])
def test_invalid_explicit_media_targets_do_not_fall_back(monkeypatch, target):
    monkeypatch.setattr(native_worker.time, "monotonic", lambda: 10)
    adapters = {"media": Mock(), "music": Mock()}
    action = {"type": "media", "command": "play_pause", "target": target, "deadline_monotonic": 15}
    result = native_worker.handle({"action": action}, adapters)
    assert result == {"ok": False, "code": "invalid_action", "effects_possible": False}
    for adapter in adapters.values():
        adapter.execute.assert_not_called()


def test_generic_media_is_preloaded_without_dispatch_or_probe(monkeypatch, capsys):
    factory = Mock()
    monkeypatch.setattr(native_worker, "MediaAdapter", factory)
    monkeypatch.setattr(
        native_worker.sys, "argv", ["native_worker", "--persistent", "--warm", "media"]
    )
    monkeypatch.setattr(native_worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"")))
    native_worker.main()
    assert capsys.readouterr().out == '{"ready": true}\n'
    factory.return_value.prepare.assert_called_once_with()
    factory.return_value.check.assert_not_called()
    factory.return_value.execute.assert_not_called()
