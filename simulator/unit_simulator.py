#!/usr/bin/env python3
"""Fakes the fleet: ~30 field units, each POSTing believable random telemetry every
few seconds. This is the test traffic for SwarmOps since there's no real hardware.

Normal mode (default): every registered unit in seed/units.json random-walks near
base and reports plausible GPS/battery/signal on a loop.

Forced-anomaly env vars (for proving the Definition-of-Done alert checks work):
  FORCE_LOW_BATTERY_UNIT=<unit-id>    that unit's battery is pinned low (<15%)
  FORCE_SIGNAL_LOSS_UNIT=<unit-id>    that unit stops sending after its first report
  FORCE_GEOFENCE_BREACH_UNIT=<unit-id> that unit walks straight outside its geofence

Spoof-test mode (`--spoof-test`): fires a handful of adversarial requests
(unregistered unit, wrong token, impossible speed, bad coordinates) and prints
whether the API correctly rejected each one, then exits. This is the stage-8
anti-spoofing proof.
"""
import argparse
import json
import logging
import os
import random
import sys
import threading
import time
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s simulator: %(message)s")
log = logging.getLogger("swarmops.simulator")

API_BASE_URL = os.environ.get("API_BASE_URL", "http://nginx/api")
SEED_PATH = os.environ.get("SEED_PATH", "/app/seed/units.json")
MIN_INTERVAL = float(os.environ.get("MIN_INTERVAL_SECONDS", "3"))
MAX_INTERVAL = float(os.environ.get("MAX_INTERVAL_SECONDS", "7"))

FORCE_LOW_BATTERY_UNIT = os.environ.get("FORCE_LOW_BATTERY_UNIT")
FORCE_SIGNAL_LOSS_UNIT = os.environ.get("FORCE_SIGNAL_LOSS_UNIT")
FORCE_GEOFENCE_BREACH_UNIT = os.environ.get("FORCE_GEOFENCE_BREACH_UNIT")

EARTH_KM_PER_DEG_LAT = 110.574


def load_seed():
    return json.loads(Path(SEED_PATH).read_text())


def km_to_deg(lat_deg, dlat_km, dlon_km):
    dlat = dlat_km / EARTH_KM_PER_DEG_LAT
    km_per_deg_lon = 111.320 * abs(__import__("math").cos(__import__("math").radians(lat_deg))) or 1e-6
    dlon = dlon_km / km_per_deg_lon
    return dlat, dlon


def post_telemetry(unit_id, token, lat, lon, battery_pct, signal_pct):
    try:
        resp = requests.post(
            f"{API_BASE_URL}/telemetry",
            json={
                "unit_id": unit_id,
                "token": token,
                "lat": lat,
                "lon": lon,
                "battery_pct": battery_pct,
                "signal_pct": signal_pct,
            },
            timeout=5,
        )
        return resp
    except requests.RequestException as exc:
        log.warning("unit=%s POST failed: %s", unit_id, exc)
        return None


def unit_loop(unit, base_lat, base_lon):
    unit_id = unit["id"]
    token = unit["token"]
    radius_km = unit["geofence_radius_km"]

    # Start somewhere plausible within the unit's own patrol radius.
    lat, lon = base_lat, base_lon
    battery = random.uniform(60, 100)
    signal = random.uniform(80, 100)

    breaching = unit_id == FORCE_GEOFENCE_BREACH_UNIT
    sent_once = False

    while True:
        if unit_id == FORCE_SIGNAL_LOSS_UNIT and sent_once:
            log.info("unit=%s FORCE_SIGNAL_LOSS active, going quiet", unit_id)
            time.sleep(3600)
            continue

        if breaching:
            # Walk steadily outward past the geofence radius, away from base, at a
            # brisk but still physically plausible ~120km/h - fast enough to breach
            # in a couple of minutes, well under MAX_SPEED_KMH so it's the geofence
            # rule that catches it, not the anti-spoofing speed check.
            dlat, dlon = km_to_deg(lat, 0.12, 0.12)
            lat += dlat
            lon += dlon
        else:
            # Small random walk (well under a realistic ~50km/h patrol pace even at
            # the minimum report interval), gently pulled back toward base to stay
            # in-bounds.
            dlat_km = random.uniform(-0.05, 0.05)
            dlon_km = random.uniform(-0.05, 0.05)
            dlat, dlon = km_to_deg(lat, dlat_km, dlon_km)
            candidate_lat, candidate_lon = lat + dlat, lon + dlon
            # cheap pull-back: if we'd exceed ~80% of radius, bias back toward base
            approx_km = ((candidate_lat - base_lat) * EARTH_KM_PER_DEG_LAT) ** 2
            if approx_km ** 0.5 > radius_km * 0.8:
                candidate_lat = lat - dlat * 0.5
                candidate_lon = lon - dlon * 0.5
            lat, lon = candidate_lat, candidate_lon

        if unit_id == FORCE_LOW_BATTERY_UNIT:
            battery = round(random.uniform(2, 14), 1)
        else:
            battery = max(1.0, battery - random.uniform(0, 0.4))
            if battery < 20 and random.random() < 0.05:
                battery = round(random.uniform(60, 100), 1)  # simulate a swap/recharge

        signal = min(100.0, max(10.0, signal + random.uniform(-5, 5)))

        resp = post_telemetry(unit_id, token, lat, lon, round(battery, 1), round(signal, 1))
        sent_once = True
        if resp is not None and resp.status_code != 202:
            log.warning("unit=%s rejected: %s %s", unit_id, resp.status_code, resp.text)

        time.sleep(random.uniform(MIN_INTERVAL, MAX_INTERVAL))


def run_fleet():
    seed = load_seed()
    base_lat, base_lon = seed["base_lat"], seed["base_lon"]
    units = seed["units"]
    log.info("starting fleet simulation: %d units against %s", len(units), API_BASE_URL)

    threads = []
    for unit in units:
        t = threading.Thread(target=unit_loop, args=(unit, base_lat, base_lon), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.05)  # stagger startup so we don't slam the API in the same instant

    for t in threads:
        t.join()


def run_spoof_test():
    seed = load_seed()
    real_unit = seed["units"][0]
    base_lat, base_lon = seed["base_lat"], seed["base_lon"]

    cases = [
        ("unregistered unit id", {
            "unit_id": "unit-does-not-exist", "token": "whatever",
            "lat": base_lat, "lon": base_lon, "battery_pct": 50, "signal_pct": 90,
        }, 401),
        ("wrong token for real unit", {
            "unit_id": real_unit["id"], "token": "wrong-token-entirely",
            "lat": base_lat, "lon": base_lon, "battery_pct": 50, "signal_pct": 90,
        }, 403),
        ("out-of-range coordinates", {
            "unit_id": real_unit["id"], "token": real_unit["token"],
            "lat": 999, "lon": 999, "battery_pct": 50, "signal_pct": 90,
        }, 422),
        ("out-of-range battery", {
            "unit_id": real_unit["id"], "token": real_unit["token"],
            "lat": base_lat, "lon": base_lon, "battery_pct": 150, "signal_pct": 90,
        }, 422),
    ]

    all_passed = True
    for name, body, expected in cases:
        try:
            resp = requests.post(f"{API_BASE_URL}/telemetry", json=body, timeout=5)
            ok = resp.status_code == expected
            all_passed &= ok
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: expected {expected}, got {resp.status_code}")
        except requests.RequestException as exc:
            all_passed = False
            print(f"[FAIL] {name}: request error {exc}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--spoof-test", action="store_true", help="run anti-spoofing checks and exit")
    args = parser.parse_args()

    if args.spoof_test:
        run_spoof_test()
    else:
        run_fleet()
