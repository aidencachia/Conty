import json
from copy import deepcopy

import pytest
from ruamel.yaml import YAML

from conty.config import MAX_BYTES, ConfigurationError, load_config


def edit(path, change):
    yaml = YAML()
    data = yaml.load(path)
    change(data)
    with path.open("w") as stream:
        yaml.dump(data, stream)
    return path


def test_defaults_projection_order_and_immutability(config_file):
    snapshot = load_config(config_file)
    assert snapshot.server["bind"] == "127.0.0.1"
    assert snapshot.actions["hello"]["timeout_seconds"] == 5
    widgets = snapshot.public()["dashboards"][0]["sections"][0]["widgets"]
    assert [w["id"] for w in widgets] == ["first", "second"]
    assert widgets[0]["position"]["row_span"] == 1
    serialized = json.dumps(snapshot.public())
    for secret in ("PRIVATE_ARGUMENT", "env_from", "/bin/echo", "HOST_SECRET", str(config_file)):
        assert secret not in serialized
    with pytest.raises(TypeError):
        snapshot.actions["hello"]["argv"] = []
    widgets.clear()
    assert len(snapshot.public()["dashboards"][0]["sections"][0]["widgets"]) == 2
    assert load_config(config_file).revision != snapshot.revision


@pytest.mark.parametrize("server", [None, {}, {"port": 9000}])
def test_omitted_bind_detects_host_ip(config_file, monkeypatch, server):
    monkeypatch.setattr("conty.config.detect_host_ip", lambda: "192.168.20.15")

    def change(data):
        data.pop("server")
        if server is not None:
            data["server"] = server

    edit(config_file, change)
    snapshot = load_config(config_file)
    assert snapshot.server["bind"] == "192.168.20.15"
    assert snapshot.server["port"] == (9000 if server and "port" in server else 8765)
    from conty.server import authorities, connection_urls

    port = snapshot.server["port"]
    assert connection_urls(snapshot) == [f"http://192.168.20.15:{port}"]
    assert authorities(snapshot) == {f"192.168.20.15:{port}"}


@pytest.mark.parametrize(
    "server",
    [
        {"bind": "127.0.0.1"},
        {"bind": "192.168.20.30"},
        {"bind": "::1"},
        {"bind": "0.0.0.0", "allowed_hosts": ["192.168.20.30"]},
    ],
)
def test_explicit_bind_skips_detection(config_file, monkeypatch, server):
    def unexpected_detection():
        pytest.fail("Explicit bind must not perform IP discovery")

    monkeypatch.setattr("conty.config.detect_host_ip", unexpected_detection)
    edit(config_file, lambda data: data.update(server=server))
    assert load_config(config_file).server["bind"] == server["bind"]


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\nversion: 1\n",
        "version: 1\na: &a []\nb: *a\n",
        "version: !!int 1",
        "!!python/object:subprocess.Popen {}",
        "---\nversion: 1\n---\nversion: 1",
        "a: {<<: {x: 1}}",
        "{1: value}",
        "",
        "[invalid",
        "version: 1\na: " + "[" * 33 + "0" + "]" * 33,
        "a: [" + ",".join("0" for _ in range(10_001)) + "]",
    ],
)
def test_unsafe_or_invalid_yaml(config_file, text):
    config_file.write_text(text)
    with pytest.raises(ConfigurationError):
        load_config(config_file)


def test_oversized_and_missing_file(config_file):
    config_file.write_bytes(b" " * (MAX_BYTES + 1))
    with pytest.raises(ConfigurationError, match="1 MiB"):
        load_config(config_file)
    with pytest.raises(ConfigurationError):
        load_config(config_file.parent / "missing")


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 1.0),
        ("version", "1"),
        ("version", 2),
        ("unrecognized", "typo"),
        ("dashboards", []),
        ("defaults", {"action_timeout_seconds": True}),
        ("defaults", {"action_timeout_seconds": 0}),
        ("defaults", {"action_timeout_seconds": float("inf")}),
        ("server", {"port": True}),
        ("server", {"bind": "0.0.0.0"}),
        ("server", {"allowed_hosts": ["evil/path"]}),
        ("server", {"allowed_hosts": ["*.local"]}),
    ],
)
def test_strict_schema(config_file, field, value):
    edit(config_file, lambda data: data.update({field: value}))
    with pytest.raises(ConfigurationError):
        load_config(config_file)


@pytest.mark.parametrize(
    "change,expected",
    [
        (lambda d: d["dashboards"].append(deepcopy(d["dashboards"][0])), "duplicate ID"),
        (
            lambda d: d["dashboards"][0]["sections"].append(
                deepcopy(d["dashboards"][0]["sections"][0])
            ),
            "duplicate ID",
        ),
        (
            lambda d: d["dashboards"][0]["sections"][0]["widgets"][0].update(id="first"),
            "duplicate ID",
        ),
        (
            lambda d: d["dashboards"][0]["sections"][0]["widgets"][0].update(action="missing"),
            "unknown action",
        ),
        (
            lambda d: d["dashboards"][0]["sections"][0]["widgets"][0]["position"].update(
                row=1, column=1
            ),
            "overlaps widget 'second'",
        ),
        (
            lambda d: d["dashboards"][0]["sections"][0]["widgets"][0]["position"].update(
                column_span=4
            ),
            "exceeds section",
        ),
        (
            lambda d: d["dashboards"][0]["sections"][0]["widgets"][0]["position"].update(row=True),
            "valid integer",
        ),
    ],
)
def test_references_ids_and_grids(config_file, change, expected):
    edit(config_file, change)
    with pytest.raises(ConfigurationError, match=expected) as caught:
        load_config(config_file)
    assert caught.value.diagnostics[0].path.startswith("dashboards[")
    assert caught.value.diagnostics[0].line is not None


@pytest.mark.parametrize(
    "action",
    [
        {"type": "keyboard", "key": "a", "target": {"focus": "activate"}},
        {"type": "keyboard", "key": "unicode"},
        {"type": "keyboard", "key": "a", "modifiers": ["shift", "shift"]},
        {"type": "media", "target": {"app_id": "com.other.Player"}, "command": "play_pause"},
        {"type": "volume", "target": "system", "operation": "adjust"},
        {"type": "volume", "target": "system", "operation": "adjust", "delta": True},
        {"type": "volume", "target": "system", "operation": "adjust", "delta": 101},
        {"type": "volume", "target": "system", "operation": "toggle_mute", "delta": None},
        {"type": "process", "argv": "echo hello"},
        {"type": "process", "argv": ["echo", "hello"]},
        {"type": "process", "argv": ["/bin/echo", "\0"]},
        {"type": "process", "argv": ["/bin/echo"], "env_from": {"BAD=NAME": "HOST"}},
    ],
)
def test_action_validation(config_file, action):
    edit(config_file, lambda data: data["actions"].update(hello=action))
    with pytest.raises(ConfigurationError):
        load_config(config_file, platform="darwin")


def test_explicit_relative_paths_do_not_reinterpret_strings(config_file):
    edit(
        config_file,
        lambda data: data["actions"].update(
            hello={
                "type": "process",
                "argv": ["/bin/sh", {"path": "scripts/task.sh"}, "literal/file"],
                "cwd": "elsewhere",
                "timeout_seconds": 2,
            }
        ),
    )
    action = load_config(config_file).actions["hello"]
    assert action["argv"] == (
        "/bin/sh",
        str(config_file.parent / "scripts/task.sh"),
        "literal/file",
    )
    assert action["cwd"] == str(config_file.parent / "elsewhere")
    assert action["timeout_seconds"] == 2


def test_support_checks_are_separate_from_readiness(config_file, monkeypatch):
    from conty.adapters.macos.keyboard import KeyboardAdapter

    monkeypatch.setattr(KeyboardAdapter, "check", lambda *a, **k: pytest.fail("No native checks"))
    edit(config_file, lambda data: data["actions"].update(hello={"type": "keyboard", "key": "a"}))
    load_config(config_file, platform="darwin")
    with pytest.raises(ConfigurationError, match="requires macOS"):
        load_config(config_file, platform="linux")


def test_examples(project_root):
    snapshot = load_config(project_root / "examples/conty.yaml", platform="darwin")
    assert all(
        action["target"] == "system"
        for action in snapshot.actions.values()
        if action["type"] == "media"
    )
    load_config(project_root / "examples/script.yaml")


@pytest.mark.parametrize("target", [None, "system", {"app_id": "com.apple.Music"}])
def test_media_defaults_to_system_and_preserves_explicit_legacy_target(config_file, target):
    action = {"type": "media", "command": "play_pause"}
    if target is not None:
        action["target"] = target
    edit(config_file, lambda data: data["actions"].update(hello=action))
    compiled = load_config(config_file, platform="darwin").actions["hello"]
    assert compiled["target"] == (target or "system")


def test_hosts_are_canonicalized_and_wildcard_destinations_rejected(config_file):
    edit(
        config_file,
        lambda data: data.update(
            server={"bind": "::", "allowed_hosts": ["0:0:0:0:0:0:0:1", "MAC.LOCAL"]}
        ),
    )
    snapshot = load_config(config_file)
    assert snapshot.server["allowed_hosts"] == ("::1", "mac.local")
    for host in ("0.0.0.0", "::", "224.0.0.1", "fe80::1%en0"):
        edit(
            config_file,
            lambda data: data.update(server={"bind": "0.0.0.0", "allowed_hosts": [host]}),
        )
        with pytest.raises(ConfigurationError):
            load_config(config_file)


def test_direct_grid_symbols_and_cell_sizes(config_file):
    def change(data):
        page = data["dashboards"][0]
        section = page.pop("sections")[0]
        page.update(layout=section["layout"], widgets=section["widgets"])
        widget = page["widgets"][0]
        widget["position"].pop("column_span")
        widget.update(symbol="←", size={"width": 2, "height": 1})

    edit(config_file, change)
    public = load_config(config_file).public()
    widgets = public["dashboards"][0]["sections"][0]["widgets"]
    assert widgets[1]["position"]["column_span"] == 2
    assert widgets[1]["symbol"] == "←"


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["dashboards"][0]["sections"][0]["widgets"][0].update(size={"width": 1}),
        lambda d: d["dashboards"][0]["sections"][0]["widgets"][0].update(size={"width": True}),
        lambda d: d["dashboards"][0].update(layout={"rows": 1, "columns": 1}),
        lambda d: d["dashboards"][0]["sections"][0]["widgets"][0].update(type="slider"),
    ],
)
def test_invalid_sizes_grid_forms_and_slider_bindings(config_file, change):
    edit(config_file, change)
    with pytest.raises(ConfigurationError):
        load_config(config_file)


@pytest.mark.parametrize("style", [None, "standard", "ios"])
def test_slider_requires_volume_set_and_validates_spans(config_file, style):
    def change(data):
        data["actions"]["volume_level"] = {"type": "volume", "target": "system", "operation": "set"}
        data["dashboards"][0]["sections"][0]["widgets"].append(
            {
                "id": "volume",
                "type": "slider",
                "label": "Volume",
                "symbol": "♫",
                "action": "volume_level",
                "position": {"row": 1, "column": 3},
                "size": {"width": 1, "height": 2},
            }
        )
        if style is not None:
            data["dashboards"][0]["sections"][0]["widgets"][-1]["style"] = style

    edit(config_file, change)
    snapshot = load_config(config_file, platform="darwin")
    assert snapshot.public()["dashboards"][0]["sections"][0]["widgets"][1]["type"] == "slider"
    assert snapshot.public()["dashboards"][0]["sections"][0]["widgets"][1]["style"] == (
        style or "standard"
    )

    def change_to_button(data):
        widget = data["dashboards"][0]["sections"][0]["widgets"][-1]
        widget["type"] = "button"
        widget.pop("style", None)

    edit(config_file, change_to_button)
    with pytest.raises(ConfigurationError, match="sliders require"):
        load_config(config_file, platform="darwin")


@pytest.mark.parametrize("style", ["unknown", "", True, None, 1])
def test_slider_rejects_invalid_styles(config_file, style):
    def change(data):
        data["actions"]["volume_level"] = {"type": "volume", "target": "system", "operation": "set"}
        widget = data["dashboards"][0]["sections"][0]["widgets"][0]
        widget.update(type="slider", action="volume_level", style=style)
        widget.pop("appearance", None)

    edit(config_file, change)
    with pytest.raises(ConfigurationError, match="style"):
        load_config(config_file, platform="darwin")
