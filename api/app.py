"""SwarmOps API — the fast path.

This service does exactly two things on telemetry ingest: write the unit's current
state to Redis, and push the same event onto a queue for the worker. It never talks
to Postgres and never runs anomaly detection — that separation is the entire point
of the architecture (see README.md). Auth/plausibility checks happen here because
they're needed to decide whether to accept the write at all, not because they're
"anomaly logic" — a spoofed or malformed request is rejected before it ever reaches
Redis or the queue.

The unit registry (id -> token, geofence radius) lives in Postgres as the source of
truth, but the API reads it from a Redis hash (`unit_registry`) that the worker keeps
in sync — the API itself never opens a Postgres connection.
"""
import json
import logging
import math
import os
import time

import redis
from flask import Flask, jsonify, request
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s api: %(message)s")
log = logging.getLogger("swarmops.api")

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
QUEUE_KEY = os.environ.get("QUEUE_KEY", "telemetry:queue")
REGISTRY_KEY = os.environ.get("REGISTRY_KEY", "unit_registry")
LAST_SEEN_PREFIX = "unit:"
MAX_SPEED_KMH = float(os.environ.get("MAX_SPEED_KMH", "300"))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

app = Flask(__name__)

INGEST_COUNTER = Counter(
    "swarmops_telemetry_ingest_total", "Telemetry events accepted", []
)
REJECTED_COUNTER = Counter(
    "swarmops_telemetry_rejected_total", "Telemetry events rejected", ["reason"]
)
INGEST_LATENCY = Histogram(
    "swarmops_telemetry_ingest_latency_seconds", "POST /telemetry handler latency"
)


def haversine_km(lat1, lon1, lat2, lon2):
    r_earth = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r_earth * math.asin(math.sqrt(a))


def get_registered_unit(unit_id):
    raw = r.hget(REGISTRY_KEY, unit_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


@app.get("/health")
def health():
    try:
        r.ping()
    except redis.RedisError as exc:
        return jsonify(status="error", redis=str(exc)), 503
    return jsonify(status="ok"), 200


@app.get("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.post("/telemetry")
@INGEST_LATENCY.time()
def telemetry():
    body = request.get_json(silent=True) or {}
    unit_id = body.get("unit_id")
    token = body.get("token")

    if not unit_id or not token:
        REJECTED_COUNTER.labels(reason="missing_credentials").inc()
        return jsonify(error="unit_id and token are required"), 400

    registered = get_registered_unit(unit_id)
    if registered is None:
        REJECTED_COUNTER.labels(reason="unregistered_unit").inc()
        log.warning("rejected telemetry from unregistered unit_id=%s", unit_id)
        return jsonify(error="unit not registered"), 401

    if not _tokens_equal(token, registered.get("token", "")):
        REJECTED_COUNTER.labels(reason="bad_token").inc()
        log.warning("rejected telemetry: bad token for unit_id=%s", unit_id)
        return jsonify(error="invalid token"), 403

    try:
        lat = float(body["lat"])
        lon = float(body["lon"])
        battery_pct = float(body["battery_pct"])
        signal_pct = float(body["signal_pct"])
    except (KeyError, TypeError, ValueError):
        REJECTED_COUNTER.labels(reason="malformed_payload").inc()
        return jsonify(error="lat, lon, battery_pct, signal_pct are required numbers"), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        REJECTED_COUNTER.labels(reason="invalid_coordinates").inc()
        return jsonify(error="coordinates out of range"), 422
    if not (0 <= battery_pct <= 100) or not (0 <= signal_pct <= 100):
        REJECTED_COUNTER.labels(reason="invalid_range").inc()
        return jsonify(error="battery_pct/signal_pct must be 0-100"), 422

    now = time.time()
    previous_raw = r.get(f"{LAST_SEEN_PREFIX}{unit_id}")
    if previous_raw:
        try:
            previous = json.loads(previous_raw)
            dt_seconds = max(now - previous["updated_at"], 1e-6)
            dist_km = haversine_km(previous["lat"], previous["lon"], lat, lon)
            implied_speed_kmh = (dist_km / dt_seconds) * 3600
            if implied_speed_kmh > MAX_SPEED_KMH:
                REJECTED_COUNTER.labels(reason="implausible_speed").inc()
                log.warning(
                    "rejected telemetry: unit_id=%s implied_speed_kmh=%.1f",
                    unit_id, implied_speed_kmh,
                )
                return jsonify(error="implausible movement speed"), 422
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            pass  # corrupt previous record shouldn't block a legit new one

    state = {
        "unit_id": unit_id,
        "lat": lat,
        "lon": lon,
        "battery_pct": battery_pct,
        "signal_pct": signal_pct,
        "updated_at": now,
    }
    payload = json.dumps(state)

    # The only two things this endpoint does: fast-path write, then queue for the worker.
    r.set(f"{LAST_SEEN_PREFIX}{unit_id}", payload)
    r.lpush(QUEUE_KEY, payload)

    INGEST_COUNTER.inc()
    return jsonify(status="accepted"), 202


def _tokens_equal(a, b):
    # Constant-time-ish comparison to avoid trivial timing side-channels on token checks.
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


@app.get("/status/<unit_id>")
def status_one(unit_id):
    raw = r.get(f"{LAST_SEEN_PREFIX}{unit_id}")
    if not raw:
        return jsonify(error="no data for unit"), 404
    return jsonify(json.loads(raw)), 200


@app.get("/status")
def status_all():
    keys = r.keys(f"{LAST_SEEN_PREFIX}*")
    units = []
    for key in keys:
        raw = r.get(key)
        if raw:
            units.append(json.loads(raw))
    units.sort(key=lambda u: u["unit_id"])
    return jsonify(units=units, count=len(units)), 200


if __name__ == "__main__":
    # Binding 0.0.0.0 is required here, not a mistake: this container is only
    # reachable by other containers on the internal swarmops-net network (it has
    # no published port in docker-compose.yml) - nginx is the actual internet-facing
    # boundary. Production runs via gunicorn (see Dockerfile.api); this path is
    # local-dev-only.
    app.run(host="0.0.0.0", port=5000)  # nosec B104
