"""Exercise the image's real healthcheck against an isolated worker HTTP server.

Run inside the runtime image. The optional benchmark needs cgroup v2 cpu.stat.
"""

import argparse
import asyncio
import json
import os
from math import ceil
from pathlib import Path
from statistics import mean
from time import monotonic

from telegram_media_bot.domain.models import ComponentHealth, HealthReport
from telegram_media_bot.infrastructure.observability.health_server import HealthServer
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry

_CPU_STAT = Path("/sys/fs/cgroup/cpu.stat")
_WINDOW_SECONDS = 0.020
_LEGACY = [
    "python",
    "-c",
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=12)",
]


async def _expect(command: list[str], code: int) -> None:
    process = await asyncio.create_subprocess_exec(*command, stderr=asyncio.subprocess.PIPE)
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    if process.returncode != code:
        raise AssertionError(f"expected exit {code}, got {process.returncode}: {stderr!r}")


def _cpu_seconds() -> float:
    for line in _CPU_STAT.read_text(encoding="utf-8").splitlines():
        name, value = line.split()
        if name == "usage_usec":
            return int(value) / 1_000_000
    raise RuntimeError("cgroup v2 usage_usec is missing")


async def _measure(command: list[str]) -> tuple[float, list[float]]:
    start_cpu = last_cpu = _cpu_seconds()
    last_time = monotonic()
    process = await asyncio.create_subprocess_exec(*command)
    windows: list[float] = []
    while True:
        await asyncio.sleep(_WINDOW_SECONDS)
        now_cpu, now_time = _cpu_seconds(), monotonic()
        windows.append(100 * (now_cpu - last_cpu) / (now_time - last_time))
        last_cpu, last_time = now_cpu, now_time
        if process.returncode is not None:
            break
    if process.returncode != 0:
        raise AssertionError(f"benchmark command failed with exit {process.returncode}")
    return last_cpu - start_cpu, windows


async def _benchmark(command: list[str], runs: int) -> None:
    commands = {"python": _LEGACY, "curl": command}
    results: dict[str, list[tuple[float, list[float]]]] = {name: [] for name in commands}
    for probe in commands.values():
        await _expect(probe, 0)  # warm both command paths equally
    for iteration in range(runs):
        order = tuple(commands) if iteration % 2 == 0 else tuple(reversed(commands))
        for name in order:
            results[name].append(await _measure(commands[name]))
    for name, samples in results.items():
        windows = sorted(value for _, values in samples for value in values)
        cpu = [value for value, _ in samples]
        print(
            json.dumps(
                {
                    "command": name,
                    "runs": len(samples),
                    "mean_cpu_seconds": round(mean(cpu), 6),
                    "max_cpu_seconds": round(max(cpu), 6),
                    "sample_windows": len(windows),
                    "target_window_ms": 1000 * _WINDOW_SECONDS,
                    "p95_percent_of_one_core": round(windows[ceil(0.95 * len(windows)) - 1], 2),
                    "max_percent_of_one_core": round(max(windows), 2),
                }
            )
        )


async def _run(command: list[str], runs: int) -> None:
    assert os.getuid() == 10001, "run the probe under the default unprivileged runtime UID"
    healthy = True
    slow = False

    async def readiness() -> HealthReport:
        if slow:
            await asyncio.sleep(4)
        return HealthReport((ComponentHealth("redis", healthy),))

    async def queue_depth() -> int:
        return 0

    server = HealthServer(
        host="127.0.0.1",
        port=8080,
        probe=readiness,
        metrics=MetricsRegistry(),
        queue_depth=queue_depth,
    )
    await server.start()
    try:
        await _expect(command, 0)
        healthy = False
        await _expect(command, 22)  # HTTP 503, even though /health still returns 200
        healthy = True
        await _expect(command, 0)
        if runs:
            await _benchmark(command, runs)
        slow = True
        await _expect(command, 28)
    finally:
        await server.close()
    await _expect(command, 7)
    print("Worker healthcheck: 200, 503, recovery, timeout and connection refusal passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("healthcheck_json")
    parser.add_argument("--benchmark", type=int, default=0, metavar="RUNS")
    args = parser.parse_args()
    test = json.loads(args.healthcheck_json)
    assert test[:2] == ["CMD", "curl"]
    assert test[-1] == "http://127.0.0.1:8080/ready"
    asyncio.run(_run(test[1:], args.benchmark))
