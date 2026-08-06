"""SwarmOps worker — the slow path.

Consumes telemetry events the API already wrote to Redis (via a Redis list acting as
a lightweight queue), persists full history to Postgres, and runs the three anomaly
rules. This is deliberately decoupled from the API: nothing here ever blocks or is
waited on by a telemetry POST.

Also keeps the API's unit-registry cache (Redis hash `unit_registry`) in sync with
Postgres, which is the registry's source of truth — this is the only reason this
service talks to both Redis and Postgres.
"""
import json
import logging
import math
import os
import threading
import time

import psycopg2
import psycopg2.extras
import redis
from prometheus_client import Counter, Gauge, start_http_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s worker: %(message)s")
log = logging.getLogger("swarmops.worker")

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
QUEUE_KEY = os.environ.get("QUEUE_KEY", "telemetry:queue")
REGISTRY_KEY = os.environ.get("REGISTRY_KEY", "unit_registry")
LAST_SEEN_PREFIX = "unit:"

PG_HOST = os.environ.get("POSTGRES_HOST", "postgres")
PG_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
PG_DB = os.environ.get("POSTGRES_DB", "swarmops")
PG_USER = os.environ.get("POSTGRES_USER", "swarmops")
PG_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "swarmops")

BATTERY_LOW_THRESHOLD = float(os.environ.get("BATTERY_LOW_THRESHOLD", "15"))
SIGNAL_LOST_SECONDS = float(os.environ.get("SIGNAL_LOST_SECONDS", "30"))
BASE_LAT = float(os.environ.get("BASE_LAT", "31.9730"))
BASE_LON = float(os.environ.get("BASE_LON", "34.7925"))
ALERT_COOLDOWN_SECONDS = int(os.environ.get("ALERT_COOLDOWN_SECONDS", "60"))
REGISTRY_REFRESH_SECONDS = int(os.environ.get("REGISTRY_REFRESH_SECONDS", "30"))
SIGNAL_SWEEP_INTERVAL_SECONDS = int(os.environ.get("SIGNAL_SWEEP_INTERVAL_SECONDS", "5"))
METRICS_PORT = int(os.environ.get("WORKER_METRICS_PORT", "9200"))

EVENTS_PROCESSED = Counter(
    "swarmops_worker_events_processed_total", "Telemetry events written to Postgres history"
)
ALERTS_FIRED = Counter(
    "swarmops_alerts_fired_total", "Anomaly alerts fired, by type", ["alert_type"]
)
QUEUE_DEPTH = Gauge(
    "swarmops_queue_depth", "Pending events in the telemetry queue (sampled)"
)
UNIT_SECONDS_SINCE_UPDATE = Gauge(
    "swarmops_unit_seconds_since_update", "Seconds since last telemetry from this unit", ["unit_id"]
)

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def pg_connect():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD
    )


def haversine_km(lat1, lon1, lat2, lon2):
    r_earth = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r_earth * math.asin(math.sqrt(a))


def fire_alert(conn, unit_id, alert_type, details):
    cooldown_key = f"alert:cooldown:{unit_id}:{alert_type}"
    if r.get(cooldown_key):
        return  # already alerted recently, don't spam
    r.set(cooldown_key, "1", ex=ALERT_COOLDOWN_SECONDS)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO alerts (unit_id, alert_type, details) VALUES (%s, %s, %s)",
            (unit_id, alert_type, details),
        )
    conn.commit()
    ALERTS_FIRED.labels(alert_type=alert_type).inc()
    log.warning("ALERT [%s] unit=%s %s", alert_type, unit_id, details)


def sync_registry_loop():
    """Keep Redis `unit_registry` in sync with Postgres `units` — the API reads only
    from Redis, so this loop is what makes the registry visible to it."""
    while True:
        try:
            conn = pg_connect()
            with conn.cursor() as cur:
                cur.execute("SELECT id, token, geofence_radius_km FROM units")
                rows = cur.fetchall()
            conn.close()
            mapping = {
                row[0]: json.dumps({"token": row[1], "geofence_radius_km": row[2]})
                for row in rows
            }
            if mapping:
                r.delete(REGISTRY_KEY)
                r.hset(REGISTRY_KEY, mapping=mapping)
                log.info("registry synced: %d units", len(mapping))
        except Exception:
            log.exception("registry sync failed, will retry")
        time.sleep(REGISTRY_REFRESH_SECONDS)


def signal_lost_sweep_loop():
    """Periodically checks for units that have gone quiet - this is the one anomaly
    rule that can't be detected on message arrival, since it's the absence of a
    message that matters."""
    while True:
        try:
            conn = pg_connect()
            now = time.time()
            for key in r.keys(f"{LAST_SEEN_PREFIX}*"):
                raw = r.get(key)
                if not raw:
                    continue
                state = json.loads(raw)
                age = now - state["updated_at"]
                UNIT_SECONDS_SINCE_UPDATE.labels(unit_id=state["unit_id"]).set(age)
                if age > SIGNAL_LOST_SECONDS:
                    fire_alert(
                        conn, state["unit_id"], "signal_lost",
                        f"no update in {age:.0f}s (threshold {SIGNAL_LOST_SECONDS:.0f}s)",
                    )
            conn.close()
        except Exception:
            log.exception("signal-lost sweep failed, will retry")
        time.sleep(SIGNAL_SWEEP_INTERVAL_SECONDS)


def get_registered_unit(unit_id):
    raw = r.hget(REGISTRY_KEY, unit_id)
    return json.loads(raw) if raw else None


def process_event(conn, event):
    unit_id = event["unit_id"]
    lat, lon = event["lat"], event["lon"]
    battery_pct, signal_pct = event["battery_pct"], event["signal_pct"]

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO telemetry_events (unit_id, lat, lon, battery_pct, signal_pct, received_at)
               VALUES (%s, %s, %s, %s, %s, to_timestamp(%s))""",
            (unit_id, lat, lon, battery_pct, signal_pct, event["updated_at"]),
        )
    conn.commit()
    EVENTS_PROCESSED.inc()

    if battery_pct < BATTERY_LOW_THRESHOLD:
        fire_alert(conn, unit_id, "low_battery", f"battery at {battery_pct:.1f}%")

    unit = get_registered_unit(unit_id)
    if unit:
        dist_km = haversine_km(BASE_LAT, BASE_LON, lat, lon)
        if dist_km > unit["geofence_radius_km"]:
            fire_alert(
                conn, unit_id, "geofence_breach",
                f"{dist_km:.2f}km from base (radius {unit['geofence_radius_km']}km)",
            )


def consume_loop():
    conn = pg_connect()
    conn.autocommit = False
    log.info("worker started, consuming from %s", QUEUE_KEY)
    while True:
        try:
            item = r.brpop(QUEUE_KEY, timeout=5)
            QUEUE_DEPTH.set(r.llen(QUEUE_KEY))
            if item is None:
                continue
            _, raw = item
            event = json.loads(raw)
            process_event(conn, event)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            log.exception("postgres connection lost, reconnecting")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(2)
            conn = pg_connect()
            conn.autocommit = False
        except Exception:
            log.exception("failed to process event, continuing")


def main():
    start_http_server(METRICS_PORT)
    log.info("metrics exposed on :%d/metrics", METRICS_PORT)

    threading.Thread(target=sync_registry_loop, daemon=True).start()
    threading.Thread(target=signal_lost_sweep_loop, daemon=True).start()

    # Give the registry sync a head start so the sweep/consume loops have data to work with.
    time.sleep(2)
    consume_loop()


if __name__ == "__main__":
    main()
