"""Measure native protocol overhead without accessing or controlling the desktop.

Run after installing Conty: .venv/bin/python tools/benchmark_latency.py
The request has an expired deadline and no key: no native action can execute.
"""

import argparse
import asyncio
import json
import math
import statistics
import time

from conty.adapters.base import AdapterError
from conty.executor import Executor


async def sample(executor):
    start = time.perf_counter()
    try:
        await executor.helper({"action": {"type": "keyboard", "deadline_monotonic": -1}}, 2)
    except AdapterError as exc:
        if exc.code != "timed_out" or exc.effects_possible:
            raise
    else:
        raise RuntimeError("An expired benchmark request must be rejected")
    return (time.perf_counter() - start) * 1000


async def benchmark(samples):
    results = {}
    for persistent in (False, True):
        executor = Executor(persistent=persistent)
        try:
            startup = await sample(executor)
            times = [await sample(executor) for _ in range(samples)]
            results["persistent" if persistent else "one_shot"] = {
                "startup_ms": round(startup, 3),
                "median_ms": round(statistics.median(times), 3),
                "p95_ms": round(sorted(times)[math.ceil(0.95 * samples) - 1], 3),
                "samples": samples,
            }
        finally:
            await executor.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples", type=int, default=50, choices=range(5, 1001), metavar="5..1000"
    )
    print(json.dumps(asyncio.run(benchmark(parser.parse_args().samples)), indent=2))
