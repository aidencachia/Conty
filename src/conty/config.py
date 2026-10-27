"""Compile owner-local YAML into immutable execution and public snapshots."""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent, ScalarEvent
from ruamel.yaml.nodes import MappingNode, SequenceNode

from conty.adapters.macos.keyboard import SUPPORTED_KEYS
from conty.network import detect_host_ip

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 10_000
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")]
Label = Annotated[str, Field(min_length=1, max_length=256)]
Timeout = Annotated[float, Field(gt=0, le=300, allow_inf_nan=False)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Auth(Model):
    mode: Literal["pairing_code"] = "pairing_code"


class Server(Model):
    bind: str = Field(default_factory=lambda: detect_host_ip(), validate_default=True)
    port: Annotated[int, Field(ge=1, le=65535)] = 8765
    allowed_hosts: list[str] = Field(default_factory=list)
    auth: Auth = Field(default_factory=Auth)

    @field_validator("bind")
    @classmethod
    def normalized_bind(cls, value):
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("bind must be a numeric IPv4 or IPv6 address") from exc
        if address.is_multicast or "%" in value:
            raise ValueError("multicast and scoped bind addresses are unsupported")
        return address.compressed

    @field_validator("allowed_hosts")
    @classmethod
    def normalized_hosts(cls, values):
        result = []
        for value in values:
            if not valid_host(value):
                raise ValueError("allowed_hosts must contain usable IP addresses or DNS names")
            try:
                result.append(ipaddress.ip_address(value).compressed)
            except ValueError:
                result.append(value.lower())
        return result

    @model_validator(mode="after")
    def addresses(self):
        try:
            address = ipaddress.ip_address(self.bind)
        except ValueError as exc:
            raise ValueError("bind must be a numeric IPv4 or IPv6 address") from exc
        if address.is_unspecified and not self.allowed_hosts:
            raise ValueError("wildcard binding requires explicit allowed_hosts")
        return self


def valid_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
        return not address.is_unspecified and not address.is_multicast and "%" not in host
    except ValueError:
        return bool(
            len(host) <= 253
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
            and all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                for part in host.split(".")
            )
        )


class Defaults(Model):
    action_timeout_seconds: Timeout = 5.0


class Target(Model):
    app_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$")] | None = None
    focus: Literal["activate", "require_frontmost"] | None = None

    @model_validator(mode="after")
    def focus_target(self):
        if self.focus is not None and self.app_id is None:
            raise ValueError("focus requires an explicit app_id")
        return self


class Action(Model):
    timeout_seconds: Timeout | None = None


class Keyboard(Action):
    type: Literal["keyboard"]
    key: str
    modifiers: list[Literal["shift", "control", "option", "command"]] = Field(default_factory=list)
    target: Target | None = None

    @field_validator("key")
    @classmethod
    def physical_key(cls, value):
        if value not in SUPPORTED_KEYS:
            raise ValueError("unsupported physical key; see SUPPORTED_KEYS")
        return value

    @field_validator("modifiers")
    @classmethod
    def unique_modifiers(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("modifiers must be unique")
        return value


class PathArgument(Model):
    path: Annotated[str, Field(min_length=1)]


class Process(Action):
    type: Literal["process"]
    argv: Annotated[list[str | PathArgument], Field(min_length=1)]
    cwd: str | None = None
    env_from: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def executable(self):
        if not isinstance(self.argv[0], str) or not Path(self.argv[0]).is_absolute():
            raise ValueError("argv[0] must be an absolute executable path")
        for value in self.argv:
            text = value if isinstance(value, str) else value.path
            if "\0" in text:
                raise ValueError("arguments must not contain NUL")
        if self.cwd is not None and (not self.cwd or "\0" in self.cwd):
            raise ValueError("cwd must be a nonempty path without NUL")
        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            for pair in self.env_from.items()
            for name in pair
        ):
            raise ValueError("env_from requires environment variable names")
        return self


class MusicTarget(Model):
    app_id: Literal["com.apple.Music"]


class Media(Action):
    type: Literal["media"]
    target: Literal["system"] | MusicTarget = "system"
    command: Literal["previous_track", "play_pause", "next_track"]


class Volume(Action):
    type: Literal["volume"]
    target: Literal["system"]
    operation: Literal["adjust", "toggle_mute", "set"]
    delta: Annotated[int, Field(ge=-100, le=100)] | None = None

    @model_validator(mode="after")
    def operation_delta(self):
        if self.operation == "adjust" and self.delta is None:
            raise ValueError("adjust requires delta")
        if self.operation != "adjust" and "delta" in self.model_fields_set:
            raise ValueError(f"{self.operation} does not accept delta")
        return self


ActionDefinition = Annotated[Keyboard | Process | Media | Volume, Field(discriminator="type")]


class Layout(Model):
    rows: Annotated[int, Field(gt=0, le=1000)]
    columns: Annotated[int, Field(gt=0, le=1000)]


class Position(Model):
    row: Annotated[int, Field(gt=0)]
    column: Annotated[int, Field(gt=0)]
    row_span: Annotated[int, Field(gt=0)] = 1
    column_span: Annotated[int, Field(gt=0)] = 1


class Size(Model):
    width: Annotated[int, Field(gt=0)] = 1
    height: Annotated[int, Field(gt=0)] = 1


class Tile(Model):
    id: Identifier
    label: Label
    symbol: Annotated[str, Field(min_length=1, max_length=16)] = "•"
    position: Position
    size: Size | None = None

    @model_validator(mode="after")
    def cell_size(self):
        if self.size is not None:
            for span, dimension in (("row_span", "height"), ("column_span", "width")):
                if span in self.position.model_fields_set and getattr(
                    self.position, span
                ) != getattr(self.size, dimension):
                    raise ValueError("size conflicts with position spans")
            object.__setattr__(
                self,
                "position",
                self.position.model_copy(
                    update={
                        "row_span": self.size.height,
                        "column_span": self.size.width,
                    }
                ),
            )
        return self


class Button(Tile):
    type: Literal["button"]
    action: Identifier
    appearance: Literal["default", "primary", "danger"] = "default"


class Slider(Tile):
    type: Literal["slider"]
    action: Identifier
    style: Literal["standard", "ios"] = "standard"


Widget = Annotated[Button | Slider, Field(discriminator="type")]


class Section(Model):
    id: Identifier
    title: Label
    layout: Layout
    widgets: list[Widget]


class Dashboard(Model):
    id: Identifier
    title: Label
    sections: list[Section] = Field(default_factory=list)
    layout: Layout | None = None
    widgets: list[Widget] | None = None

    @model_validator(mode="after")
    def grid_form(self):
        direct = self.layout is not None or self.widgets is not None
        if direct and (
            self.layout is None or self.widgets is None or "sections" in self.model_fields_set
        ):
            raise ValueError("use either layout/widgets or sections")
        if not direct and "sections" not in self.model_fields_set:
            raise ValueError("dashboard requires layout/widgets or sections")
        return self

    def grids(self):
        if self.layout is not None:
            return [Section(id="main", title=self.title, layout=self.layout, widgets=self.widgets)]
        return self.sections


class Configuration(Model):
    version: Literal[1]
    server: Server = Field(default_factory=Server)
    defaults: Defaults = Field(default_factory=Defaults)
    actions: dict[Identifier, ActionDefinition]
    dashboards: Annotated[list[Dashboard], Field(min_length=1)]

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("version must be the integer 1")
        return value


@dataclass(frozen=True)
class Diagnostic:
    path: str
    message: str
    line: int | None = None
    column: int | None = None

    def __str__(self):
        location = f" ({self.line}:{self.column})" if self.line else ""
        return f"{self.path}{location}: {self.message}"


class ConfigurationError(Exception):
    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = diagnostics
        super().__init__("\n".join(map(str, diagnostics)))


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


def thaw(value):
    if isinstance(value, MappingProxyType):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class Snapshot:
    revision: str
    source: Path
    server: Any
    actions: Any
    _public: Any

    def public(self) -> dict:
        return thaw(self._public)


def config_path(parts) -> str:
    result = ""
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else ("." if result else "") + str(part)
    return result or "$"


def load_config(path: str | Path, *, platform: str | None = None) -> Snapshot:
    source = Path(path).resolve()
    try:
        with source.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("configuration exceeds 1 MiB")
        text = raw.decode("utf-8")
        yaml = YAML(typ="safe", pure=True)
        yaml.version = (1, 2)
        yaml.allow_duplicate_keys = False
        depth = count = 0
        for event in yaml.parse(text):

            def reject(message):
                raise ConfigurationError(
                    [
                        Diagnostic(
                            "$", message, event.start_mark.line + 1, event.start_mark.column + 1
                        )
                    ]
                )

            if isinstance(event, AliasEvent):
                reject("YAML aliases are not supported")
            if getattr(event, "tag", None):
                reject("explicit YAML tags are not supported")
            if isinstance(event, (CollectionStartEvent, ScalarEvent)):
                count += 1
            if isinstance(event, CollectionStartEvent):
                depth += 1
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            if depth > MAX_DEPTH or count > MAX_NODES:
                reject("configuration exceeds nesting or node limits")
        node = yaml.compose(text)
        locations = {}

        def locate(current, parts=()):
            if current is None:
                return
            locations[parts] = (current.start_mark.line + 1, current.start_mark.column + 1)
            if isinstance(current, MappingNode):
                for key, value in current.value:
                    if key.tag != "tag:yaml.org,2002:str" or key.value == "<<":
                        raise ValueError("mapping keys must be strings; merge keys are unsupported")
                    locate(value, (*parts, key.value))
            elif isinstance(current, SequenceNode):
                for index, value in enumerate(current.value):
                    locate(value, (*parts, index))

        locate(node)
        data = yaml.constructor.construct_document(node) if node is not None else None
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigurationError([Diagnostic("$", str(exc))]) from exc
    except Exception as exc:
        mark = getattr(exc, "problem_mark", None)
        raise ConfigurationError(
            [
                Diagnostic(
                    "$",
                    getattr(exc, "problem", "Invalid YAML"),
                    mark.line + 1 if mark else None,
                    mark.column + 1 if mark else None,
                )
            ]
        ) from exc

    def diagnostic(parts, message):
        point = parts
        while point not in locations and point:
            point = point[:-1]
        line, column = locations.get(point, (None, None))
        return Diagnostic(config_path(parts), message, line, column)

    try:
        config = Configuration.model_validate(data)
    except ValidationError as exc:
        # Pydantic discriminator labels are not source path components.
        diagnostics = []
        for error in exc.errors(include_input=False):
            parts = tuple(error["loc"])
            if (
                len(parts) > 2
                and parts[0] == "actions"
                and parts[2] in ("keyboard", "process", "media", "volume")
            ):
                parts = (*parts[:2], *parts[3:])
            diagnostics.append(diagnostic(parts, error["msg"]))
        raise ConfigurationError(diagnostics) from exc

    errors = []
    if (platform or sys.platform) != "darwin":
        for name, action in config.actions.items():
            if action.type != "process":
                errors.append(diagnostic(("actions", name), "action type requires macOS"))

    def unique(items, parts):
        seen = set()
        for index, item in enumerate(items):
            if item.id in seen:
                errors.append(diagnostic((*parts, index, "id"), f"duplicate ID {item.id!r}"))
            seen.add(item.id)

    unique(config.dashboards, ("dashboards",))
    for di, dashboard in enumerate(config.dashboards):
        unique(dashboard.grids(), ("dashboards", di, "sections"))
        for si, section in enumerate(dashboard.grids()):
            parts = (
                ("dashboards", di, "widgets")
                if dashboard.layout
                else ("dashboards", di, "sections", si, "widgets")
            )
            unique(section.widgets, parts)
            rectangles = []
            for wi, widget in enumerate(section.widgets):
                if widget.action not in config.actions:
                    errors.append(diagnostic((*parts, wi, "action"), "unknown action reference"))
                else:
                    action = config.actions[widget.action]
                    dynamic = isinstance(action, Volume) and action.operation == "set"
                    if isinstance(widget, Slider) != dynamic:
                        errors.append(
                            diagnostic(
                                (*parts, wi, "action"),
                                "sliders require a volume set action; buttons require a fixed action",
                            )
                        )
                p = widget.position
                bottom, right = p.row + p.row_span - 1, p.column + p.column_span - 1
                if bottom > section.layout.rows or right > section.layout.columns:
                    errors.append(diagnostic((*parts, wi, "position"), "span exceeds section grid"))
                for row, column, end_row, end_column, other in rectangles:
                    if (
                        p.row <= end_row
                        and bottom >= row
                        and p.column <= end_column
                        and right >= column
                    ):
                        errors.append(
                            diagnostic((*parts, wi, "position"), f"overlaps widget {other!r}")
                        )
                rectangles.append((p.row, p.column, bottom, right, widget.id))
    if errors:
        raise ConfigurationError(errors)

    actions = {}
    for name, action in config.actions.items():
        definition = action.model_dump(exclude_none=True)
        definition["timeout_seconds"] = (
            action.timeout_seconds or config.defaults.action_timeout_seconds
        )
        if isinstance(action, Process):
            definition["argv"] = [
                str((source.parent / value.path).resolve())
                if isinstance(value, PathArgument)
                else value
                for value in action.argv
            ]
            definition["cwd"] = str((source.parent / (action.cwd or ".")).resolve())
        actions[name] = definition
    dashboards = [
        {
            "id": d.id,
            "title": d.title,
            "sections": [s.model_dump(exclude_none=True) for s in d.grids()],
        }
        for d in config.dashboards
    ]
    for dashboard in dashboards:
        for section in dashboard["sections"]:
            section["widgets"].sort(key=lambda w: (w["position"]["row"], w["position"]["column"]))
    revision = secrets.token_urlsafe(24)
    # The dashboard models contain presentation fields only, never action definitions.
    public = {"version": 1, "revision": revision, "dashboards": dashboards}
    json.dumps(public)  # Ensure the projection remains JSON-compatible.
    return Snapshot(
        revision, source, freeze(config.server.model_dump()), freeze(actions), freeze(public)
    )
