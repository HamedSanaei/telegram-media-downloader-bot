#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare baseline and canary failure rates")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("canary", type=Path)
    parser.add_argument("--minimum-canary-jobs", type=int, default=20)
    parser.add_argument("--max-regression-points", type=float, default=2.0)
    args = parser.parse_args()
    baseline_rate, _ = _failure_rate(_load(args.baseline))
    canary_rate, canary_jobs = _failure_rate(_load(args.canary))
    regression = (canary_rate - baseline_rate) * 100
    print(f"Baseline failure rate: {baseline_rate:.2%}")
    print(f"Canary failure rate: {canary_rate:.2%} ({canary_jobs} jobs)")
    print(f"Regression: {regression:+.2f} percentage points")
    if canary_jobs < args.minimum_canary_jobs:
        print("PROMOTION BLOCKED: insufficient canary sample")
        return 2
    if regression > args.max_regression_points:
        print("PROMOTION BLOCKED: failure-rate regression is above threshold")
        return 1
    print("PROMOTION GATE PASSED")
    return 0


def _load(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Canary snapshot root must be an object")
    return raw


def _failure_rate(snapshot: dict[str, Any]) -> tuple[float, int]:
    jobs = int(snapshot.get("jobs_total", 0))
    failures = int(snapshot.get("failures_total", 0))
    if jobs <= 0 or failures < 0 or failures > jobs:
        raise ValueError("Snapshot must have valid jobs_total and failures_total")
    return failures / jobs, jobs


if __name__ == "__main__":
    raise SystemExit(main())
