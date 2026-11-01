# Lightweight Docker worker readiness probe

## Scope and evidence

Production observations supplied on 2026-11-03 associate repeated 40–89% single-core bursts
with 30-second healthchecks. The supplied per-command measurements were 0.311 CPU seconds
for Python startup, 0.984 for importing urllib.request, and 1.071 for Python plus `/ready`;
direct readiness traffic used about 0.03–0.05 worker CPU seconds. This identifies one
repeatable contributor, not the cause of all host CPU usage.

The image and base Compose worker now run exec-form curl as UID 10001, with proxy and curlrc
processing disabled. HTTP 503, timeout and refused connection fail; 200 and recovery pass.
The base policy remains interval 30s, timeout 5s, start period 20s, retries 3, and the client
deadline remains 3s. Bot, Local Bot API and Redis probes are unchanged.

The server still checks Redis, SQLite, writable storage, Telegram identity, ffmpeg, engines
and applicable Local Bot API state. It reports logger health without letting an auxiliary
logger outage block readiness. The routed engine's yt-dlp version is already in memory.
The existing locked gallery-dl version cache (300s success, 30s failure) deduplicates refreshes;
it still probes after expiry and recovers from a negative entry without restart. A cold or
expired gallery-dl check can still spawn a subprocess. Do not cache aggregate readiness,
Redis, storage or SQLite status: that would delay detection/recovery and is not justified
by the client-versus-endpoint measurements.

## Reproduce the benchmark and smoke

```bash
image=telegram-media-downloader-bot:worker-healthcheck-bench
docker build -t "$image" .
probe="$(docker image inspect --format '{{json .Config.Healthcheck.Test}}' "$image")"
docker run --rm -i --no-healthcheck --network none --read-only --cpus 1.5 --cpuset-cpus 0,1 \
  -e no_proxy=127.0.0.1 "$image" python - "$probe" --benchmark 30 \
  < scripts/tests/worker_healthcheck_probe.py
```

This is a disposable local container with the actual worker HTTP server and a deterministic
readiness callback, not a production worker or a live Redis/Telegram load test. Use two available
CPU IDs for the cpuset. It alternates the former 12s Python command and the actual image probe
against the same endpoint. Both run 30 times after warm-up. CPU time is the container-wide
`cpu.stat` `usage_usec` delta including server and sampler. Utilization is delta CPU time divided
by elapsed wall time in approximately 20ms windows, normalized to one CPU core. The final
window may include idle time after a short command exits. Only the disposable benchmark disables
automatic scheduling, so background Docker probes cannot enter the samples. These are local,
warm-cache numbers; they are not exact production peak or throughput measurements.

Measured on 2026-11-03, Docker Desktop Linux/amd64, Engine 29.7.2, cgroup v2, two allowed
CPUs and a 1.5-vCPU quota:

| Command | Runs | Mean CPU seconds/check | Max CPU seconds/check | Windows | p95 % of one core | Max % of one core |
|---|---:|---:|---:|---:|---:|---:|
| Legacy Python HTTP probe | 30 | 0.218671 | 0.351839 | 334 | 118.86 | 120.05 |
| Exec-form curl | 30 | 0.006297 | 0.007595 | 30 | 34.60 | 35.18 |

The mean CPU cost fell by 97.1% locally; even the most expensive measured curl execution
was below 0.1 CPU seconds. Values above 100% are possible because this measures the whole
two-CPU cgroup, not an individual process or quota percentage. Short curl runs complete
within one sample window, so the reported peak is a 20ms-scale average, not an
instantaneous peak.

## Rollout and rollback (only after approval)

1. Prepare a verified tagged candidate image and matching release archive using the normal release
   pipeline. Keep the current image/tag available and record service state. Use the existing
   transactional updater and consistent backup procedure; drain active downloads and follow
   companion stop requirements before the maintenance window.
2. Privately back up the operator's `docker-compose.override.yml` if present. In a staged copy,
   remove only `services.worker.healthcheck.test` (or remove the worker healthcheck mapping if
   that was its sole key). Keep custom timing, CPU, volumes and unrelated services unchanged.
   The repository does not ship that operator file. [Compose merge rules](https://docs.docker.com/reference/compose-file/merge/#shell-commands)
   replace the healthcheck command rather than merging its arguments. A stale override will
   still run Python. For the measured 12s installation, use the replacement in step 3.
3. Before starting the candidate worker, render the **candidate base plus corrected override**
   with the installation's normal Compose file order and env settings. Inspect only the
   worker probe; do not publish a full production config:

   ```bash
   docker compose config --format json | python3 -c \
     'import json,sys; h=json.load(sys.stdin)["services"]["worker"]["healthcheck"]; print(h); assert h["test"][:2]==["CMD","curl"] and h["test"][-1].endswith("/ready") and not h.get("disable")'
   ```

   A custom Docker timeout must allow the client deadline. For the measured installation
   with an existing 12s client budget, preserve that budget at first rollout: instead of
   dropping the override key, replace just its `test` with the following. Retain its existing
   Docker timeout if longer than 12s; otherwise set it to 15s. Tune the budget separately
   after measuring cold and expired-cache readiness.

   ```yaml
   test: ["CMD", "curl", "--disable", "--noproxy", "*", "--fail", "--silent", "--show-error", "--max-time", "12", "--output", "/dev/null", "http://127.0.0.1:8080/ready"]
   ```
4. With approval, install the edited override and apply the verified release through the updater.
   Confirm the actual container's command with
   `docker inspect --format '{{json .Config.Healthcheck.Test}}' "$(docker compose ps -q worker)"`.
   Check healthy status, verify a known-safe canary download/upload and compare idle cgroup
   CPU samples and VPN latency over multiple probe intervals. Do not change CPU/concurrency
   settings in the same rollout.
5. If candidate readiness or canary fails, use the updater's transaction rollback. For a
   post-activation rollback, restore the backed-up override alongside the matching previous
   tagged release via the normal rollback procedure. Verify the rendered and actual probe,
   exact service state, worker health and queue progress. Do not replace live SQLite/Redis
   data with an older snapshot merely to revert a healthcheck.

## CPU and concurrency

The existing `TMB_WORKER_CPUS` setting supports fractional quotas. It is independent of this
fix. On a shared two-vCPU host, evaluate 0.75 first, then 0.5 only if it meets throughput and
latency targets. Compare against the current 1.5 quota using representative download,
upload and FFmpeg jobs, queue latency and VPN p95 latency. Watch `nr_throttled` and
`throttled_usec` as well as job timeouts/cancellations. If needed, separately test
`queue.max_jobs: 1` and the existing transcode concurrency gate to limit contention.
No quota or concurrency change is validated for production by this client benchmark.
Local Compose renders both 0.5 and 0.75 correctly without altering the default.

## Validation

- Tested the actual image probe as UID 10001 with a read-only root and no external network:
  200, 503, recovery, 3s timeout and connection refusal. A broken proxy environment did
  not divert the loopback probe.
- In a separate disposable container, Docker itself transitioned healthy → unhealthy →
  healthy when the real HTTP server fixture toggled readiness (accelerated 1s test
  intervals; production timings unchanged).
- Verified exec argv and base timings in image metadata and effective Compose JSON.
  The Compose regression includes an operator timing/CPU override; its settings remain
  intact while it inherits curl.
- Docker build and offline CLI, gallery-dl, native selection/UI and inspection-workspace
  smokes passed. The workspace check used only the example configuration and scratch storage.
- Frozen lock/sync, Ruff, mypy, architecture/context/text checks, manifest, secret scan and
  pip consistency passed. The Linux-only probe also passed standalone mypy for Linux.
  Focused Compose/CI/cache suite: 68 passed, seven platform skips.
- Full non-contract suite: WSL 1,896 passed, one failed, five skipped, 83.29% coverage;
  Windows 1,889 passed, one failed, 12 skipped, 83.21% coverage. Both selected the same
  pre-existing release-policy snapshot failure; no test was weakened or excluded to pass.
- The unchanged base commit `242d18d` already has two independent CI failures in
  [scheduled run 41609946686](https://github.com/HamedSanaei/telegram-media-downloader-bot/actions/runs/41609946686):
  embedded release-policy snapshot drift in `install.sh` and a `msgpack` audit finding
  (`CVE-2026-114613`). Neither is changed by this narrowly scoped probe patch.

## Graphify usage

- Version `0.9.26`; `graphify check-update .` completed without a stale report, no update or fallback.
- `graphify query "Trace Docker worker healthcheck readiness health_server gallery-dl cache Compose override CPU limits tests CI publish workflow" --budget 1200 --graph graphify-out/graph.json`
- Located `HealthServer`, worker settings, `GalleryDlEngine`, Compose management, and
  `_effective_cpu_capacity`; verified source and cache tests on current `origin/main`.
