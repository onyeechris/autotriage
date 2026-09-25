# AutoTriage demo app — on-call runbook

This app is a single FastAPI/Uvicorn container on one EC2 instance, started with
`docker run --restart=no`, so the process does not come back on its own if it dies.
Health check: `GET /health`. JSON logs, one line per request, with `status_code` and `path`.

Three alarms exist. Each maps to exactly one known incident type below.

## Incident: elevated 5xx rate (alarm: HTTP5xxCount > 5 in 1 min)

**Symptoms:** log lines show `"status_code": 500` on requests to normal paths (e.g. `/health`),
often with `{"error": "chaos_500_active"}` as the body. CPU and disk are normal. The container
is still running (it just returns 500 for everything).

**Root cause:** the app's `/chaos/500` test endpoint was triggered, setting an in-memory flag
that forces every request to 500 until the flag is cleared.

**Safe fix:** `restart_container` — clears all in-memory state, including the stuck flag.
This is safe: it's a stateless demo app with no persistent connections or in-flight writes.

**Risk:** low, when the only symptom is 500s with the container otherwise healthy (running,
CPU/disk normal).

## Incident: container not running (alarm: ContainerRunning < 1)

**Symptoms:** the heartbeat metric reports the container is not running. The last log lines
before the gap typically show `"event": "chaos_crash_triggered"`, then nothing.

**Root cause:** the app's `/chaos/crash` test endpoint was triggered, calling `os._exit(1)`
inside the process. Because the container runs with `--restart=no`, Docker does not restart it.

**Safe fix:** `restart_container` — Docker's `restart` command works on a stopped container
too and will bring it back up. Safe for the same reasons as above (stateless demo app).

**Risk:** low, if this is an isolated crash. If the container keeps crashing immediately after
every restart, treat as risk `medium`+ and escalate instead of auto-remediating (that pattern
suggests a real bug, not the demo chaos endpoint, and a restart loop would just mask it).

## Incident: disk usage high (alarm: disk_used_percent > 85%)

**Symptoms:** the CloudWatch agent's `disk_used_percent` metric for `/` is elevated. The
container may still be healthy and serving 200s. `df -h /` on the host shows large files under
`/tmp` (pattern: `autotriage-chaos-junk-*`).

**Root cause:** the app's `/chaos/disk` test endpoint was triggered, writing 10 MiB junk files
to `/tmp` on the host (via a bind mount) until disk usage hit ~90%.

**Safe fix:** `prune_disk` — deletes the chaos junk files and runs `docker system prune` to
reclaim any additional space, then resets the app's chaos flag as a courtesy.

**Risk:** low, when the disk filler pattern (`autotriage-chaos-junk-*` under `/tmp`) is present
and no unrelated large files are involved. If large files outside that pattern are the cause,
this is not the right fix — do not run `prune_disk`, escalate instead.

## When nothing here matches

If the logs and metrics don't clearly match one of the three patterns above — for example,
repeated crash-loops, a 5xx spike with no `chaos_500_active` marker, or disk growth from files
that aren't the chaos junk pattern — do not guess. Recommend `action: "none"` with low
confidence and let a human take a look.
