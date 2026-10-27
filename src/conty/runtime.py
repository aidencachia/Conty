"""Single-owner FIFO admission, bounded request records and sanitized outcomes."""

from __future__ import annotations

import asyncio
import heapq
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from conty.adapters.base import AdapterError
from conty.auth import APIError, Limiter
from conty.config import Snapshot, thaw

logger = logging.getLogger(__name__)
TERMINAL = frozenset({"succeeded", "failed", "timed_out", "cancelled"})
SAFE_MESSAGES = {
    "permission_denied": "Host permission is missing; check the local setup",
    "target_unavailable": "The configured target is not available",
    "target_not_frontmost": "The target application is not frontmost",
    "capability_unavailable": "The host cannot currently perform this operation",
    "execution_failed": "The configured action failed",
    "invalid_action": "The configured action could not be executed",
    "timed_out": "The action timed out; effects may already have occurred",
    "executor_unavailable": "Execution is unavailable; check the host and restart locally",
    "session_expired": "The session expired before execution",
    "shutdown": "The host stopped before execution",
}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Invocation:
    id: str
    session_id: str
    request_id: str
    action_id: str
    revision: str
    definition: dict | None
    value: int | None = None
    state: str = "queued"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    finished_mono: float | None = None
    created_mono: float = 0.0
    started_mono: float | None = None
    result: dict | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def notify(self):
        event, self.changed = self.changed, asyncio.Event()
        event.set()

    def public(self):
        return {
            "id": self.id,
            "request_id": self.request_id,
            "action_id": self.action_id,
            "revision": self.revision,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": dict(self.result) if self.result else None,
        }


class Coordinator:
    def __init__(
        self,
        snapshot: Snapshot,
        executor,
        sessions,
        *,
        clock=time.monotonic,
        queue_limit=64,
        record_limit=10_000,
        retention=900,
    ):
        self.snapshot, self.executor, self.sessions, self.clock = (
            snapshot,
            executor,
            sessions,
            clock,
        )
        self.queue_limit, self.record_limit, self.retention = queue_limit, record_limit, retention
        self.queue: deque[Invocation] = deque()
        self.records: dict[str, Invocation] = {}
        self.requests: dict[tuple[str, str], str] = {}
        self.expirations: list[tuple[float, str]] = []
        self.history: deque[dict] = deque(maxlen=1000)
        self.session_limit = Limiter(20, 10, clock=clock)
        self.slider_limit = Limiter(30, 20, clock=clock)
        self.host_limit = Limiter(100, 50, clock=clock)
        self.available = True
        self.accepting = True
        self.readiness: dict = {}
        self.wakeup = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.probe_task: asyncio.Task | None = None

    def prune(self):
        now = self.clock()
        while self.expirations and self.expirations[0][0] <= now:
            _, identity = heapq.heappop(self.expirations)
            record = self.records.pop(identity, None)
            if record is not None:
                self.requests.pop((record.session_id, record.request_id), None)

    def submit(self, session_id: str, request_id: str, action_id: str, revision: str, value=None):
        # No awaits: event-loop ownership makes admission/dequeue atomic.
        self.prune()
        existing_id = self.requests.get((session_id, request_id))
        if existing_id:
            record = self.records[existing_id]
            if (record.action_id, record.revision, record.value) != (action_id, revision, value):
                raise APIError(
                    409, "request_conflict", "Request ID was already used for another payload"
                )
            return record.public(), False
        if not self.accepting or not self.available:
            raise APIError(503, "executor_unavailable", SAFE_MESSAGES["executor_unavailable"])
        if revision != self.snapshot.revision:
            raise APIError(
                409, "stale_revision", "Dashboard revision changed; refresh the dashboard"
            )
        if action_id not in self.snapshot.actions:
            raise APIError(404, "unknown_action", "Action is not configured")
        definition = thaw(self.snapshot.actions[action_id])
        dynamic = definition["type"] == "volume" and definition["operation"] == "set"
        if dynamic:
            if type(value) is not int or not 0 <= value <= 100:
                raise APIError(422, "invalid_value", "Volume requires an integer from 0 to 100")
            definition["value"] = value
        elif value is not None:
            raise APIError(422, "invalid_value", "This action does not accept a value")
        if len(self.queue) >= self.queue_limit or len(self.records) >= self.record_limit:
            raise APIError(
                503, "capacity_exceeded", "Host queue or retained-record capacity is full"
            )
        session_limit = self.slider_limit if dynamic else self.session_limit
        if not self.host_limit.allow("host") or not session_limit.allow(session_id):
            raise APIError(429, "submission_rate_limited", "Too many action submissions")
        record = Invocation(
            secrets.token_urlsafe(24),
            session_id,
            request_id,
            action_id,
            revision,
            definition,
            value=value,
            created_at=timestamp(),
            created_mono=self.clock(),
        )
        self.records[record.id] = record
        self.requests[(session_id, request_id)] = record.id
        self.queue.append(record)
        self.wakeup.set()
        logger.info(
            "action_admitted invocation=%s action=%s revision=%s", record.id, action_id, revision
        )
        return record.public(), True

    def lookup(self, identity: str, session_id: str):
        self.prune()
        record = self.records.get(identity)
        if record is None or record.session_id != session_id:
            raise APIError(404, "invocation_not_found", "Invocation is not available")
        return record.public()

    async def wait_for_completion(self, identity: str, session_id: str, *, wait: float):
        # Waiting observes admission; cancelling/disconnecting never cancels an
        # accepted action. Use the event loop's clock, not an injected test clock.
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            result = self.lookup(identity, session_id)
            remaining = deadline - asyncio.get_running_loop().time()
            if result["state"] in TERMINAL or remaining <= 0:
                return result
            event = self.records[identity].changed
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                return self.lookup(identity, session_id)

    async def lookup_many(self, identities, session_id, *, wait=0):
        self.prune()

        def owned():
            return [
                self.records[identity]
                for identity in dict.fromkeys(identities)
                if identity in self.records and self.records[identity].session_id == session_id
            ]

        records = owned()
        if (
            records
            and wait
            and len(records) == len(set(identities))
            and all(r.state not in TERMINAL for r in records)
        ):
            listeners = [asyncio.create_task(record.changed.wait()) for record in records]
            try:
                await asyncio.wait(listeners, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for listener in listeners:
                    listener.cancel()
                await asyncio.gather(*listeners, return_exceptions=True)
        self.prune()
        records = owned()
        found = {r.id for r in records}
        return {
            "invocations": [r.public() for r in records],
            "missing_ids": [
                identity for identity in dict.fromkeys(identities) if identity not in found
            ],
        }

    def finish(self, record, state, *, code=None, effects=False, details=None):
        kind = record.definition.get("type") if record.definition else None
        record.state, record.finished_at, record.finished_mono = state, timestamp(), self.clock()
        record.definition = None
        record.result = {
            "code": code,
            "message": SAFE_MESSAGES.get(
                code,
                {
                    "process": "Configured process exited successfully",
                    "keyboard": "Input dispatched",
                    "media": "Media command dispatched",
                    "volume": "Output change confirmed",
                }.get(kind, "Action completed"),
            ),
            "effects_possible": effects,
        }
        heapq.heappush(self.expirations, (record.finished_mono + self.retention, record.id))
        record.notify()
        # Adapter output is bounded and kept only in this local memory store.
        # Routes must always use Invocation.public(), never this diagnostic history.
        self.history.append({**record.public(), "local_details": details or {}})
        queue_wait = (
            record.started_mono if record.started_mono is not None else record.finished_mono
        ) - record.created_mono
        duration = (
            record.finished_mono - record.started_mono if record.started_mono is not None else 0
        )
        logger.info(
            "action_finished invocation=%s action=%s state=%s code=%s effects_possible=%s queue_wait=%.3f duration=%.3f",
            record.id,
            record.action_id,
            state,
            code,
            effects,
            queue_wait,
            duration,
        )

    def cancel_queue(self, code):
        while self.queue:
            self.finish(self.queue.popleft(), "cancelled", code=code)

    def start(self):
        self.task = asyncio.create_task(self.work(), name="conty-action-worker")
        self.probe_task = asyncio.create_task(self.probe_readiness(), name="conty-readiness-worker")

    async def probe_readiness(self):
        while self.accepting:
            try:
                self.readiness = await self.executor.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.readiness = {"native": {"code": "capability_unavailable", "available": False}}
            await asyncio.sleep(30)

    async def work(self):
        try:
            await self.executor.start()
        except AdapterError:
            self.available = False
            self.cancel_queue("executor_unavailable")
        while True:
            if self.queue and self.available:
                record = self.queue.popleft()
                if not self.sessions.active(record.session_id):
                    self.finish(record, "cancelled", code="session_expired")
                    continue
                record.state, record.started_at = "running", timestamp()
                record.started_mono = self.clock()
                record.notify()
                try:
                    outcome = await self.executor.execute(record.definition)
                    self.finish(record, "succeeded", details=outcome.details)
                except asyncio.CancelledError:
                    self.finish(record, "timed_out", code="shutdown", effects=True)
                    raise
                except AdapterError as exc:
                    code = exc.code if exc.code in SAFE_MESSAGES else "execution_failed"
                    self.finish(
                        record,
                        "timed_out" if code == "timed_out" else "failed",
                        code=code,
                        effects=exc.effects_possible,
                        details=exc.details,
                    )
                    if code == "executor_unavailable":
                        self.available = False
                        self.cancel_queue(code)
                except Exception:
                    # Unexpected executor failures cannot establish cleanup or ordering.
                    self.finish(record, "failed", code="executor_unavailable", effects=True)
                    self.available = False
                    self.cancel_queue("executor_unavailable")
                continue
            if not self.accepting:
                return
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), 1)
            except TimeoutError:
                self.prune()

    async def stop(self):
        self.accepting = False
        self.cancel_queue("shutdown")
        self.wakeup.set()
        if self.probe_task:
            self.probe_task.cancel()
            await asyncio.gather(self.probe_task, return_exceptions=True)
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        await self.executor.close()
