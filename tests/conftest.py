from dataclasses import replace
from pathlib import Path

import pytest

from conty.adapters.base import ActionResult
from conty.config import freeze, load_config, thaw

CONFIG = """version: 1
server:
  bind: 127.0.0.1
actions:
  hello:
    type: process
    argv: [/bin/echo, PRIVATE_ARGUMENT]
    env_from: {CHILD_SECRET: HOST_SECRET}
dashboards:
  - id: tools
    title: Tools
    sections:
      - id: controls
        title: Controls
        layout: {rows: 2, columns: 3}
        widgets:
          - id: second
            type: button
            label: Second
            action: hello
            position: {row: 2, column: 1, column_span: 2}
          - id: first
            type: button
            label: First
            action: hello
            appearance: primary
            position: {row: 1, column: 2}
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "conty.yaml"
    path.write_text(CONFIG)
    return path


@pytest.fixture
def snapshot(config_file):
    return load_config(config_file)


@pytest.fixture
def volume_snapshot(snapshot):
    actions = thaw(snapshot.actions)
    actions["volume_level"] = {
        "type": "volume",
        "target": "system",
        "operation": "set",
        "timeout_seconds": 5,
    }
    return replace(snapshot, actions=freeze(actions))


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeExecutor:
    available = True

    def __init__(self):
        self.calls = []
        self.error = None
        self.closed = False
        self.volume = 37
        self.volume_error = None
        self.volume_reads = 0

    async def execute(self, definition):
        self.calls.append(definition)
        if self.error:
            raise self.error
        if definition["type"] == "volume" and definition["operation"] == "set":
            self.volume = definition["value"]
        return ActionResult("SECRET MESSAGE", {"stdout": "PRIVATE_OUTPUT", "pid": 123})

    async def start(self):
        pass

    async def check(self):
        return {"process": {"available": True}}

    async def read_volume(self):
        self.volume_reads += 1
        if self.volume_error:
            raise self.volume_error
        return {"value": self.volume, "muted": False, "device_id": "PRIVATE_DEVICE"}

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_executor():
    return FakeExecutor()


@pytest.fixture
def project_root():
    return Path(__file__).resolve().parent.parent
