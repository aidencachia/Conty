import asyncio
from types import SimpleNamespace

import pytest
from conftest import Clock

from conty.adapters.base import AdapterError
from conty.auth import APIError
from conty.runtime import Coordinator


def coordinator(snapshot, executor, **kwargs):
    return Coordinator(snapshot, executor, SimpleNamespace(active=lambda _: True), **kwargs)


async def settled(runtime, ids):
    for _ in range(100):
        if all(runtime.records[identity].finished_at for identity in ids):
            return
        await asyncio.sleep(0.005)
    pytest.fail("Worker did not settle")


def test_fifo_duplicate_conflict_and_cross_session_ownership(snapshot, fake_executor):
    async def scenario():
        runtime = coordinator(snapshot, fake_executor)
        one, created = runtime.submit("a", "request", "hello", snapshot.revision)
        duplicate, again = runtime.submit("a", "request", "hello", snapshot.revision)
        two, _ = runtime.submit("b", "request", "hello", snapshot.revision)
        assert created and not again and one == duplicate
        assert two["id"] != one["id"]
        with pytest.raises(APIError) as caught:
            runtime.submit("a", "request", "hello", "changed")
        assert caught.value.code == "request_conflict"
        with pytest.raises(APIError) as caught:
            runtime.lookup(one["id"], "b")
        assert caught.value.status == 404
        runtime.start()
        await settled(runtime, [one["id"], two["id"]])
        assert [row["id"] for row in runtime.history] == [one["id"], two["id"]]
        assert len(fake_executor.calls) == 2
        assert runtime.records[one["id"]].definition is None
        await runtime.stop()

    asyncio.run(scenario())


def test_concurrent_duplicates_are_admitted_once(snapshot, fake_executor):
    async def scenario():
        runtime = coordinator(snapshot, fake_executor)

        async def submit():
            return runtime.submit("a", "same", "hello", snapshot.revision)

        results = await asyncio.gather(*(submit() for _ in range(20)))
        assert sum(created for _, created in results) == 1
        assert len(runtime.queue) == len(runtime.records) == 1
        runtime.start()
        await settled(runtime, [results[0][0]["id"]])
        assert len(fake_executor.calls) == 1
        await runtime.stop()

    asyncio.run(scenario())


def test_capacity_never_evicts_unexpired_deduplication(snapshot, fake_executor):
    async def scenario():
        clock = Clock()
        runtime = coordinator(snapshot, fake_executor, clock=clock, record_limit=1)
        one, _ = runtime.submit("a", "one", "hello", snapshot.revision)
        runtime.start()
        await settled(runtime, [one["id"]])
        with pytest.raises(APIError) as caught:
            runtime.submit("a", "two", "hello", snapshot.revision)
        assert caught.value.code == "capacity_exceeded"
        original, created = runtime.submit("a", "one", "hello", snapshot.revision)
        assert not created and original["state"] == "succeeded"
        clock.now += 899
        runtime.prune()
        assert one["id"] in runtime.records
        clock.now += 1
        runtime.prune()
        assert not runtime.records and not runtime.requests
        runtime.submit("a", "two", "hello", snapshot.revision)
        await runtime.stop()

    asyncio.run(scenario())


def test_rejection_allocates_nothing_and_queue_is_bounded(snapshot, fake_executor):
    runtime = coordinator(snapshot, fake_executor, queue_limit=1)
    for action, revision, code in [
        ("hello", "old", "stale_revision"),
        ("missing", snapshot.revision, "unknown_action"),
    ]:
        with pytest.raises(APIError) as caught:
            runtime.submit("a", "reject", action, revision)
        assert caught.value.code == code
        assert not runtime.records
    runtime.submit("a", "one", "hello", snapshot.revision)
    with pytest.raises(APIError) as caught:
        runtime.submit("a", "two", "hello", snapshot.revision)
    assert caught.value.code == "capacity_exceeded"
    assert len(runtime.records) == 1


@pytest.mark.parametrize(
    "error,state,available",
    [
        (AdapterError("timed_out", "PRIVATE", effects_possible=True), "timed_out", True),
        (AdapterError("permission_denied", "PRIVATE"), "failed", True),
        (AdapterError("executor_unavailable", "PRIVATE", effects_possible=True), "failed", False),
        (RuntimeError("PRIVATE"), "failed", False),
    ],
)
def test_failure_projection_and_executor_latching(snapshot, fake_executor, error, state, available):
    async def scenario():
        fake_executor.error = error
        runtime = coordinator(snapshot, fake_executor)
        one, _ = runtime.submit("a", "one", "hello", snapshot.revision)
        two, _ = runtime.submit("a", "two", "hello", snapshot.revision)
        runtime.start()
        await settled(runtime, [one["id"], two["id"]])
        assert runtime.records[one["id"]].state == state
        assert runtime.available == available
        assert "PRIVATE" not in str(runtime.lookup(one["id"], "a"))
        if not available:
            assert len(fake_executor.calls) == 1
            assert runtime.records[two["id"]].state == "cancelled"
            assert not runtime.records[two["id"]].result["effects_possible"]
        await runtime.stop()

    asyncio.run(scenario())


def test_expired_session_and_shutdown_cancel_unstarted_work(snapshot, fake_executor):
    async def scenario():
        runtime = Coordinator(snapshot, fake_executor, SimpleNamespace(active=lambda _: False))
        record, _ = runtime.submit("a", "one", "hello", snapshot.revision)
        runtime.start()
        await settled(runtime, [record["id"]])
        assert runtime.records[record["id"]].result["code"] == "session_expired"
        assert not fake_executor.calls
        await runtime.stop()
        assert fake_executor.closed
        with pytest.raises(APIError):
            runtime.submit("a", "two", "hello", snapshot.revision)
        runtime2 = coordinator(snapshot, fake_executor)
        queued, _ = runtime2.submit("a", "one", "hello", snapshot.revision)
        await runtime2.stop()
        assert runtime2.records[queued["id"]].state == "cancelled"

    asyncio.run(scenario())


def test_rate_limit_rejects_new_records_but_returns_duplicates(snapshot, fake_executor):
    runtime = coordinator(snapshot, fake_executor)
    for index in range(20):
        runtime.submit("a", str(index), "hello", snapshot.revision)
    with pytest.raises(APIError) as caught:
        runtime.submit("a", "new", "hello", snapshot.revision)
    assert caught.value.status == 429
    assert len(runtime.records) == 20
    assert not runtime.submit("a", "0", "hello", snapshot.revision)[1]


def test_live_slider_has_a_bounded_independent_session_budget(volume_snapshot, fake_executor):
    clock = Clock()
    runtime = coordinator(volume_snapshot, fake_executor, clock=clock)
    for index in range(30):
        runtime.submit("a", f"v{index}", "volume_level", volume_snapshot.revision, value=index)
    with pytest.raises(APIError) as caught:
        runtime.submit("a", "limited", "volume_level", volume_snapshot.revision, value=50)
    assert caught.value.status == 429
    assert not runtime.submit("a", "v0", "volume_level", volume_snapshot.revision, value=0)[1]
    # A drag does not consume the normal button budget.
    runtime.submit("a", "button", "hello", volume_snapshot.revision)
    clock.now += 0.051
    runtime.submit("a", "next", "volume_level", volume_snapshot.revision, value=50)
    with pytest.raises(APIError) as caught:
        runtime.submit("a", "limited_again", "volume_level", volume_snapshot.revision, value=51)
    assert caught.value.status == 429


def test_completion_wait_timeout_and_cancellation_do_not_cancel_admitted_work(
    snapshot, fake_executor
):
    async def scenario():
        runtime = coordinator(snapshot, fake_executor)
        record, _ = runtime.submit("a", "request", "hello", snapshot.revision)
        observed = await runtime.wait_for_completion(record["id"], "a", wait=0.01)
        assert observed["state"] == "queued"
        waiting = asyncio.create_task(runtime.wait_for_completion(record["id"], "a", wait=1))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert len(runtime.queue) == 1
        with pytest.raises(APIError) as caught:
            await runtime.wait_for_completion(record["id"], "b", wait=0.1)
        assert caught.value.status == 404
        runtime.start()
        observed = await runtime.wait_for_completion(record["id"], "a", wait=0.25)
        assert observed["state"] == "succeeded"
        assert len(fake_executor.calls) == 1
        await runtime.stop()

    asyncio.run(scenario())


def test_slow_readiness_probe_does_not_delay_action_dispatch(snapshot, fake_executor):
    async def scenario():
        probe_started, action_started = asyncio.Event(), asyncio.Event()

        async def check():
            probe_started.set()
            await asyncio.Event().wait()

        original = fake_executor.execute

        async def execute(definition):
            action_started.set()
            return await original(definition)

        fake_executor.check, fake_executor.execute = check, execute
        runtime = coordinator(snapshot, fake_executor)
        runtime.start()
        await asyncio.wait_for(probe_started.wait(), 1)
        runtime.submit("a", "one", "hello", snapshot.revision)
        await asyncio.wait_for(action_started.wait(), 0.25)
        await runtime.stop()

    asyncio.run(scenario())


def test_batched_read_wakes_on_completion_and_hides_other_sessions(snapshot, fake_executor):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        original = fake_executor.execute

        async def execute(definition):
            started.set()
            await release.wait()
            return await original(definition)

        fake_executor.execute = execute
        runtime = coordinator(snapshot, fake_executor)
        one, _ = runtime.submit("a", "one", "hello", snapshot.revision)
        runtime.start()
        await started.wait()
        waiting = asyncio.create_task(runtime.lookup_many([one["id"]], "a", wait=2))
        await asyncio.sleep(0)
        assert not waiting.done()
        release.set()
        result = await asyncio.wait_for(waiting, 0.25)
        assert result["invocations"][0]["state"] == "succeeded"
        assert result["missing_ids"] == []
        assert "PRIVATE" not in str(result)
        foreign = await asyncio.wait_for(
            runtime.lookup_many([one["id"], "missing"], "b", wait=2), 0.25
        )
        assert foreign == {"invocations": [], "missing_ids": [one["id"], "missing"]}
        await runtime.stop()

    asyncio.run(scenario())


def test_batched_read_timeout_and_cancellation_do_not_change_actions(snapshot, fake_executor):
    async def scenario():
        runtime = coordinator(snapshot, fake_executor)
        one, _ = runtime.submit("a", "one", "hello", snapshot.revision)
        result = await runtime.lookup_many([one["id"]], "a", wait=0.01)
        assert result["invocations"][0]["state"] == "queued"
        waiting = asyncio.create_task(runtime.lookup_many([one["id"]], "a", wait=2))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert runtime.records[one["id"]].state == "queued"
        assert not fake_executor.calls
        await runtime.stop()

    asyncio.run(scenario())
