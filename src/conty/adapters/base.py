"""Platform-independent adapter outcomes; no HTTP or configuration dependencies."""

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionResult:
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class AdapterError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        effects_possible: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.effects_possible = effects_possible
        self.details = details or {}


def validate_timeout(timeout: float) -> None:
    """Reject invalid deadlines, including bools (which are numeric in Python)."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise AdapterError("invalid_action", "timeout must be a finite positive number")
