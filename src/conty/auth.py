"""Bounded in-memory pairing, sessions and monotonic token buckets."""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class Limiter:
    def __init__(self, burst: int, rate: float, *, clock=time.monotonic, capacity=4096):
        self.burst, self.rate, self.clock, self.capacity = burst, rate, clock, capacity
        self.entries: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        if key not in self.entries and len(self.entries) >= self.capacity:
            self.entries = {
                k: v for k, v in self.entries.items() if now - v[1] < self.burst / self.rate
            }
            if len(self.entries) >= self.capacity:
                return False
        tokens, updated = self.entries.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - updated) * self.rate)
        allowed = tokens >= 1
        self.entries[key] = (tokens - 1 if allowed else tokens, now)
        return allowed


@dataclass(frozen=True)
class Session:
    id: str
    csrf: str
    expires: float


class Sessions:
    def __init__(self, *, clock=time.monotonic, output: Callable[[str], None] = print):
        self.clock, self.output = clock, output
        self.records: dict[str, Session] = {}
        self.code = ""
        self.code_expires = 0.0
        self.pair_source = Limiter(5, 5 / 60, clock=clock)
        self.pair_host = Limiter(20, 20 / 60, clock=clock)

    def rotate(self):
        previous = self.code
        self.code = f"{secrets.randbelow(1_000_000):06d}"
        while self.code == previous:
            self.code = f"{secrets.randbelow(1_000_000):06d}"
        self.code_expires = self.clock() + 120
        self.output(f"Pairing code: {self.code} (valid for 2 minutes; single use)")

    def maintain(self):
        now = self.clock()
        self.records = {key: value for key, value in self.records.items() if value.expires > now}
        if now >= self.code_expires:
            self.rotate()

    def pair(self, code: str, source: str) -> tuple[str, Session]:
        if not self.pair_host.allow("host") or not self.pair_source.allow(source):
            raise APIError(429, "pairing_rate_limited", "Too many pairing attempts; try later")
        now = self.clock()
        if now >= self.code_expires or not secrets.compare_digest(code, self.code):
            raise APIError(403, "invalid_pairing_code", "Pairing code is invalid or expired")
        self.records = {key: value for key, value in self.records.items() if value.expires > now}
        if len(self.records) >= 32:
            raise APIError(503, "session_capacity", "Session capacity reached; restart locally")
        credential = secrets.token_urlsafe(32)
        session = Session(secrets.token_urlsafe(24), secrets.token_urlsafe(32), now + 8 * 3600)
        self.records[self.digest(credential)] = session
        logger.info("session_created session=%s", session.id)
        self.rotate()
        return credential, session

    @staticmethod
    def digest(credential: str):
        return hashlib.sha256(credential.encode()).hexdigest()

    def authenticate(self, credential: str | None) -> Session:
        session = (
            self.records.get(self.digest(credential))
            if credential and len(credential) <= 128
            else None
        )
        if session is None or session.expires <= self.clock():
            raise APIError(401, "pairing_required", "Pair this browser with the host")
        return session

    def active(self, session_id: str) -> bool:
        return any(s.id == session_id and s.expires > self.clock() for s in self.records.values())
