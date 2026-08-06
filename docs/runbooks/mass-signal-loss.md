# Runbook: Mass Signal Loss

**Trigger:** the `SwarmOpsMassSignalLoss` Prometheus alert (more than 5 units silent
for >30s at the same time), or the Grafana fleet dashboard showing a sudden drop in
"Units Active."

## Before you do anything else

Don't assume it's a real field-wide outage. The whole point of separating the fast
path (Redis, written directly by the API) from the slow path (worker-computed alerts)
is that you can tell these apart quickly:

1. **Is the API itself healthy?**
   ```bash
   curl -s http://<host>/api/health
   ```
   If this is failing, the problem is the ingestion path itself (nginx/api/redis), not
   the field units - skip to "Ingestion path is down" below.

2. **Are *some* units still reporting?**
   ```bash
   curl -s http://<host>/api/status | jq '.count'
   ```
   If this number is healthy (close to your fleet size) while the alert says several
   specific units are silent, those units are genuinely offline (or being rejected -
   see step 4) - this is closer to a real field-side problem, or at least isolated to
   those units, not a platform-wide issue.

3. **Is the worker actually processing, or stuck?**
   Check `swarmops_queue_depth` in Prometheus/Grafana. A climbing queue depth with
   units still posting successfully to the API means: **Redis has the truth, the
   worker just hasn't caught up to compute alerts from it yet.** This is the exact
   scenario in the curriculum's own incident walkthrough - it looks like "the whole
   fleet vanished" but it's actually "the worker fell behind." See
   `worker-falling-behind.md` for that path.

4. **Check the rejection counter**, `swarmops_telemetry_rejected_total` broken down by
   `reason`. A spike in `unregistered_unit` or `bad_token` after a deploy usually means
   the worker's registry-sync (`unit_registry` in Redis) didn't populate correctly -
   units are sending valid telemetry that's being rejected before it ever reaches
   Redis, which looks identical to "signal lost" from the dashboard's point of view.

## Ingestion path is down

If `/api/health` itself is failing:
- `kubectl get pods -n swarmops` / `docker compose ps` - is `api` actually running?
- Check Redis: `redis-cli -h <redis-host> ping`. If Redis is down, every replica of
  `api` will fail its readiness probe and stop receiving traffic - this is the real
  root cause, not the units.
- Roll back the most recent deploy if this started right after one (`kubectl rollout
  undo deployment/swarmops-api` or `argocd app sync swarmops` after reverting the
  offending commit).

## It's confirmed a real field-side event

If the API is healthy, the worker's queue isn't backed up, and specific units really
have stopped sending telemetry (confirmed via `/api/status/<unit_id>` showing an old
`updated_at`):
- This is now an operational/field problem, not a platform one - escalate to fleet
  operators with the specific unit IDs and last-known positions (from
  `telemetry_events` in Postgres, which has the full history Redis doesn't keep).
- Do **not** restart Redis "to clear the state" - Redis holds the last genuinely-known
  position of every unit, including the silent ones. Wiping it destroys the one thing
  you need for the escalation.

## Postmortem

If this took more than a few minutes to diagnose, or caused a real gap in alerting,
write it up using `docs/postmortem-template.md`.
