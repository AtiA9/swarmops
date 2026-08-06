# Runbook: Worker Falling Behind on the Queue

**Trigger:** the `SwarmOpsWorkerQueueBacklog` Prometheus alert (`swarmops_queue_depth
> 100` for 30s+), or alerts/history visibly lagging reality while `/api/status` still
looks current.

## Why this matters

The API never waits for the worker - that's the entire architecture. Which means a
stuck worker is invisible from the API's side: ingestion keeps returning 202 happily
while, quietly, nothing is being written to Postgres and no anomaly is being detected.
This is the failure mode that *looks* healthy until someone checks the right metric.

## Diagnose

1. **Confirm it's really the worker, not a genuine traffic spike.**
   ```promql
   rate(swarmops_worker_events_processed_total[1m])
   ```
   If this is near zero while `swarmops_queue_depth` climbs, the worker has stopped
   consuming entirely - not just running slow.

2. **Check the worker's own logs.**
   ```bash
   kubectl logs -n swarmops deploy/swarmops-worker --tail=100
   # or: docker compose logs worker --tail=100
   ```
   Look specifically for repeated `postgres connection lost, reconnecting` - the
   consume loop is written to reconnect and continue on `psycopg2.OperationalError`/
   `InterfaceError`, but if Postgres is down hard, you'll see this on a loop.

3. **Check Postgres itself.**
   ```bash
   kubectl exec -n swarmops deploy/postgres -- pg_isready -U swarmops -d swarmops
   ```
   A saturated/unreachable Postgres is the most common real cause - connection pool
   exhaustion, disk full (check the `postgres-pvc` usage), or a long-running query
   blocking writes to `telemetry_events`/`alerts`.

4. **Check for a poison-pill event.** A single malformed queue entry that raises inside
   `process_event` on every attempt would, prior to a fix, spin the loop without
   backoff. The consume loop currently catches this at the broad `except Exception`
   level and continues (so it won't fully wedge), but check logs for one `unit_id`
   erroring repeatedly - that's a signal to inspect that specific event's data.

## Remediate

- **Restart the worker pod** if it's alive-but-stuck: `kubectl rollout restart
  deployment/swarmops-worker -n swarmops`. Redis and the queue are unaffected by this -
  nothing is lost, just delayed.
- **Do not scale the worker to multiple replicas as a fix** - see `docs/SLO.md`'s
  "known gaps" section. Today's worker isn't leader-elected; a second replica would
  duplicate registry syncs and signal-lost sweeps rather than share the queue backlog
  faster (BRPOP itself *is* safe to have multiple consumers of, so this is a smaller
  fix than it sounds like - just not done yet).
- **If Postgres is the bottleneck**, that's the thing to scale/fix (bigger instance,
  check for a missing index, check `pg_stat_activity` for blocking queries) - not the
  worker.

## After queue drains

Once `swarmops_queue_depth` returns to baseline, check for a burst of alerts firing
all at once (all the anomalies that "should" have fired in real time, catching up).
This is expected and not itself an incident - but if it caused a real gap in a real
alert reaching a real operator, write up `docs/postmortem-template.md`.
