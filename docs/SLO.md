# SwarmOps — Service Level Objectives

## Why these two SLOs

SwarmOps has exactly one job that matters more than any other: **tell an operator
something is wrong with a unit before it stops mattering that they know.** Everything
else (dashboards, history, GitOps) is in service of that. So the SLOs center on the
two things that actually break that promise: telemetry stops being ingested, or an
anomaly happens and nobody gets told in time.

## SLIs and SLOs

| SLI | Definition | SLO target | Measured via |
|---|---|---|---|
| Ingest availability | % of `POST /telemetry` requests from *valid* units that receive a 202 | **99.9%** over a rolling 30 days (≈ 43 min/month error budget) | `swarmops_telemetry_ingest_total` vs. 5xx rate on `/api/telemetry` |
| Anomaly → alert latency (event-driven: battery, geofence) | Time from a telemetry event being queued to the corresponding row landing in `alerts` | **p95 < 10 seconds** | worker consumes via a blocking `BRPOP` (no polling delay) — observed in manual testing at 1-3s typical |
| Anomaly → alert latency (signal-lost) | Time from a unit's true last update to the `signal_lost` alert firing | **≤ 35 seconds** (30s threshold + ≤ 1 sweep cycle of 5s) | this SLO is *structurally* bounded by `SIGNAL_LOST_SECONDS` + `SIGNAL_SWEEP_INTERVAL_SECONDS`, not just observed |
| Ingest latency | `POST /telemetry` handler time (Redis write + queue push only — no Postgres, no anomaly checks) | **p95 < 200ms** | `swarmops_telemetry_ingest_latency_seconds` histogram, Grafana panel |

## What was actually verified vs. what's a target

The event-driven latency (battery/geofence) was observed directly during manual testing
(docker compose stack, forced anomalies) at 1-3 seconds — well inside the 10s target.
The signal-lost bound is a property of the code, not a measurement: it cannot fire faster
than the threshold itself allows, by construction. Ingest availability and the 200ms
ingest-latency target are *targets*, not yet backed by 30 days of production traffic —
honestly, there's no 30 days of traffic yet. This doc will get an "actuals" section once
there's real history to report.

## Security-gate policy note (relevant to "time to alert" thinking)

The CI Trivy gate uses `ignore-unfixed: true` — it blocks the pipeline on any
CRITICAL/HIGH CVE **that has a fix available**, but not on freshly-disclosed CVEs in
base-image packages with no patch yet. The alternative (block on every CRITICAL,
period) sounds stricter but isn't actually safer: it would make the pipeline
permanently red the moment any upstream package gets a new CVE, through no fault of
this code, and teams under a permanently-red pipeline stop trusting red pipelines -
which is a worse security posture than a gate that stays meaningful. This mirrors the
SLO philosophy above: a target only does its job if hitting it is actually possible.

## Known gaps (documented honestly, not hidden)

- **Worker has no HA today.** It runs as a single replica by design (the registry-sync
  and signal-lost-sweep loops aren't leader-elected, so a naive second replica would
  duplicate work rather than share it). A worker crash means a gap in alerting until
  Kubernetes restarts the pod. Mitigation: `livenessProbe`-driven restart is fast, but
  a proper fix (leader election, e.g. via a Kubernetes Lease) is a real follow-up, not
  done here.
- **Terraform/Ansible are written and validated (`terraform validate`, `ansible-playbook
  --syntax-check`) but not applied against real AWS** in this project's development —
  no AWS account was provisioned for it. The Kubernetes + ArgoCD path, by contrast, was
  proven end-to-end against a local `kind` cluster synced from this repo's real GitHub
  history.
- **30-day SLO actuals don't exist yet** — see above.
- ~~GitHub Actions has not run yet on this repo~~ **Resolved.** Actions was blocked
  for roughly the first 15 minutes after repo creation (GitHub's new-account
  verification gate, zero runs with no error surfaced via the API) - once that
  cleared, every queued run from earlier pushes executed automatically. The pipeline
  is confirmed green end-to-end; see the root README's status table for the run link.
