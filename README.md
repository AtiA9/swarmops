# SwarmOps

A backend platform that ingests live telemetry (GPS, battery, signal strength) from a
fleet of field units, keeps an instantly-queryable "current status" for each one, and
fires alerts the moment something goes wrong — without letting alerting slow down the
live data feed.

This is the capstone project of a 24-week DevOps self-study curriculum: it's the one
place all nine prior stages (Linux/Python, Git, Docker, CI/CD, Terraform/Ansible,
Kubernetes/GitOps, Observability, DevSecOps, SRE) meet in a single working system,
rather than nine isolated exercises.

## Architecture

```
                    ┌──────────────┐
 field units ──────▶│    nginx     │  reverse proxy, single public entrypoint
 (telemetry)        └──────┬───────┘
                           ▼
                 ┌──────────────────┐
                 │  api (2+ replicas)│  validates + rejects spoofed/implausible
                 └───┬──────────┬───┘   telemetry, then ONLY: writes Redis, queues event
                     ▼          ▼
             ┌──────────┐  ┌──────────────┐
             │  redis   │  │  postgres    │  full history + per-unit geofence config
             │ (current │  └──────┬───────┘  + the unit registry (id → token)
             │  state)  │         ▲
             └──────────┘  ┌──────┴───────┐
                           │   worker      │  consumes the queue, writes history,
                           │               │  runs the 3 anomaly rules, fires alerts,
                           │               │  keeps Redis's registry cache in sync
                           └──────────────┘
                 ┌─────────────┐      ┌──────────────┐
                 │ prometheus   │─────▶│   grafana    │
                 └─────────────┘      └──────────────┘
```

**The one rule that matters most:** `POST /telemetry` in [api/app.py](api/app.py) does
exactly two things after validating the request — write to Redis, push to the queue.
It never touches Postgres and never runs anomaly detection. That separation (fast path
vs. slow path) is the entire point of the project; see [docs/SLO.md](docs/SLO.md) for
why, and the curriculum's own incident scenario in
[docs/runbooks/mass-signal-loss.md](docs/runbooks/mass-signal-loss.md) for what it buys
you when something breaks.

### Why Redis holds current state and Postgres holds history

"What is unit #47 doing right now" is answered from Redis (in-memory, always the
latest write) in single-digit milliseconds. Postgres holds every telemetry event ever
received, for playback/analysis, and the per-unit geofence radius + auth token
registry (the source of truth the worker keeps synced into a Redis hash the API reads
from — see the comment at the top of [worker/worker.py](worker/worker.py)).

### The three anomaly rules ([worker/worker.py](worker/worker.py))

| Rule | Threshold | Detected |
|---|---|---|
| Low battery | `battery_pct < 15` | on event arrival (event-driven) |
| Signal lost | no update in `> 30s` | periodic sweep, every 5s |
| Geofence breach | distance from base `> ` per-unit radius | on event arrival (event-driven) |

### Anti-spoofing (stage 8)

Every unit is registered in Postgres with a unique token (seeded via
[scripts/generate_seed.py](scripts/generate_seed.py) → `seed/units.json` +
`db/init.sql`). `POST /telemetry` rejects: unregistered unit IDs (401), wrong tokens
(403), out-of-range coordinates/battery/signal (422), and physically-implausible
movement speed computed against the unit's last known position (422). Proven live via
`python simulator/unit_simulator.py --spoof-test`.

## Running it locally

```bash
cp .env.example .env   # fill in real values
docker compose up -d
curl http://localhost:8080/api/health
curl http://localhost:8080/api/status
```

- API/telemetry: `http://localhost:8080/api/`
- Grafana: `http://localhost:3000` (anonymous viewer access enabled for convenience,
  or log in with `admin` / your `GRAFANA_ADMIN_PASSWORD`)
- Prometheus: `http://localhost:9090`

Force a specific anomaly for a live demo by uncommenting the relevant
`FORCE_*_UNIT` line under the `simulator` service in `docker-compose.yml` and
`docker compose up -d simulator`.

Run the test suite:
```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

## Kubernetes + GitOps — proven live, not just written

This was actually stood up and exercised, against this repo's real, pushed commit
history:

1. `kind create cluster --name swarmops`, ArgoCD installed via its standard manifests.
2. `kubectl apply -f k8s/argocd-application.yaml` — the `Application` pulled `k8s/`
   straight from `https://github.com/AtiA9/swarmops.git`, `main`, and synced
   automatically. Result: `sync=Synced health=Healthy`, all 8 services running,
   `swarmops-api` at real `replicas: 2`.
3. **GitOps drift/self-heal, proven live:** `kubectl scale deployment
   swarmops-worker --replicas=3` (git says `1`) → ArgoCD's `selfHeal` reverted it back
   to 1 within seconds, no human ran `kubectl apply` to fix it — git stayed the source
   of truth the whole time.
4. Confirmed the app itself works inside the cluster too: `kubectl port-forward
   svc/nginx`, `curl /api/health` → `200`, and a bad-token telemetry POST → `403`
   (the anti-spoofing check works identically in Kubernetes as in Docker Compose).

## What's actually been verified end-to-end (and what hasn't)

In the spirit of the curriculum's own rule about honest documentation over a fake
100%, here's the real status, not the aspirational one:

| Area | Status |
|---|---|
| `docker compose up` brings up all 8 services healthy | ✅ verified |
| Simulator → Redis current-state, sub-second | ✅ verified (30/30 units reporting) |
| Worker writes history to Postgres without blocking ingest | ✅ verified |
| Forced low-battery / signal-lost / geofence-breach → real alert in Postgres + firing Prometheus alert | ✅ verified, all three |
| Unregistered/spoofed unit rejected | ✅ verified (`--spoof-test`, 4/4 cases pass) |
| CI: test → build (matrix) → Trivy+Bandit gate → push to GHCR | ✅ **verified green end-to-end**, live, on `main` ([run #31130454754](https://github.com/AtiA9/swarmops/actions/runs/31130454754)): 16/16 tests pass, both images build, Trivy (CRITICAL+HIGH, `ignore-unfixed`) and Bandit both clean on both images, both pushed to GHCR tagged by commit SHA. (Actions was blocked with zero runs for the first ~15 minutes after this repo's creation — matched GitHub's new-account verification gate; once resolved, the backlog of queued runs from every earlier push executed automatically, no code changes needed except one real bug the first live run caught: a pinned `trivy-action` version tag that had been pruned upstream, fixed in a follow-up PR.) GHCR package visibility may still default to private — set it to public under the repo's Packages settings if you want anonymous `docker pull`. |
| Terraform (VPC/EC2/SG) + Ansible (Docker install + deploy) | ✅ written, `terraform validate`/`fmt` and `ansible-playbook --syntax-check` clean — **not applied to real AWS** (no cloud account provisioned for this build; see `docs/SLO.md`) |
| Kubernetes manifests + ArgoCD, `api` at `replicas: 2` | ✅ written **and proven live** against a local `kind` cluster, with the ArgoCD `Application` synced from this repo's real, pushed git history (see below). Because CI hasn't been able to push images to GHCR yet (see above), the images used in that live demo were built locally and loaded directly into the kind node (`kind load docker-image`) with `imagePullPolicy: IfNotPresent` rather than pulled from a registry — a deliberate, documented substitution for the local proof, not a hidden shortcut. Once Actions is unblocked, `git push` alone will make the real pipeline build/scan/push real SHA-tagged images with no manual steps. |
| Grafana dashboard + real firing alert rule | ✅ verified (`SwarmOpsAnyUnitSignalLost` observed firing during the forced-anomaly test) |
| SLO doc, 2 runbooks, postmortem template | ✅ this repo, `docs/` |

## Repo layout

- `api/`, `worker/`, `simulator/` — the three custom Python services (see above)
- `nginx/`, `db/`, `observability/` — config mounted into the compose stack
- `terraform/`, `ansible/` — infra-as-code (see status table)
- `k8s/` — Kubernetes manifests + the ArgoCD `Application` (GitOps, no manual
  `kubectl apply` — see `k8s/argocd-application.yaml`)
- `docs/` — `SLO.md`, `runbooks/`, `postmortem-template.md`
- `.github/workflows/ci-cd.yml` — the pipeline
- `scripts/generate_seed.py` — regenerates the dev/demo fleet registry

## Deploying for real

- **VM path:** `terraform apply` in `terraform/` (after bootstrapping the S3+DynamoDB
  backend — see `terraform/backend.tf`), then `ansible-playbook -i inventory.ini
  playbook.yml` in `ansible/`.
- **Kubernetes path:** `kubectl apply -f k8s/argocd-application.yaml -n argocd` against
  a cluster with ArgoCD installed, after creating the real `swarmops-secrets` Secret
  out-of-band (see `k8s/secret.example.yaml` for the exact command — secrets are
  deliberately not synced through git).
# CI test 1786056174
