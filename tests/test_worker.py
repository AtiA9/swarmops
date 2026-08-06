import json
from unittest.mock import MagicMock

import fakeredis

import worker as worker_mod


def test_haversine_zero_distance():
    assert worker_mod.haversine_km(31.97, 34.79, 31.97, 34.79) == 0


def test_haversine_known_distance():
    # Rishon LeZion to Tel Aviv center is roughly 10-13km.
    dist = worker_mod.haversine_km(31.9730, 34.7925, 32.0853, 34.7818)
    assert 9 < dist < 15


def test_process_event_writes_history_and_fires_low_battery_alert(monkeypatch):
    fake_r = fakeredis.FakeRedis(decode_responses=True)
    fake_r.hset(
        worker_mod.REGISTRY_KEY,
        mapping={"unit-001": json.dumps({"token": "t", "geofence_radius_km": 5})},
    )
    monkeypatch.setattr(worker_mod, "r", fake_r)

    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value

    event = {
        "unit_id": "unit-001",
        "lat": 31.9730,
        "lon": 34.7925,
        "battery_pct": 5.0,  # below threshold (15) -> should alert
        "signal_pct": 90,
        "updated_at": 1000.0,
    }
    worker_mod.process_event(conn, event)

    inserted_tables = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("INSERT INTO telemetry_events" in q for q in inserted_tables)
    assert any("INSERT INTO alerts" in q for q in inserted_tables)


def test_process_event_no_alert_for_healthy_reading(monkeypatch):
    fake_r = fakeredis.FakeRedis(decode_responses=True)
    fake_r.hset(
        worker_mod.REGISTRY_KEY,
        mapping={"unit-001": json.dumps({"token": "t", "geofence_radius_km": 50})},
    )
    monkeypatch.setattr(worker_mod, "r", fake_r)

    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value

    event = {
        "unit_id": "unit-001",
        "lat": 31.9730,
        "lon": 34.7925,
        "battery_pct": 80.0,
        "signal_pct": 90,
        "updated_at": 1000.0,
    }
    worker_mod.process_event(conn, event)

    inserted_tables = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("INSERT INTO telemetry_events" in q for q in inserted_tables)
    assert not any("INSERT INTO alerts" in q for q in inserted_tables)


def test_geofence_breach_fires_alert(monkeypatch):
    fake_r = fakeredis.FakeRedis(decode_responses=True)
    # tiny radius so any reading away from base breaches it
    fake_r.hset(
        worker_mod.REGISTRY_KEY,
        mapping={"unit-001": json.dumps({"token": "t", "geofence_radius_km": 0.01})},
    )
    monkeypatch.setattr(worker_mod, "r", fake_r)

    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value

    event = {
        "unit_id": "unit-001",
        "lat": 31.9830,  # ~1km north of base - well outside the 0.01km test radius
        "lon": 34.7925,
        "battery_pct": 80.0,
        "signal_pct": 90,
        "updated_at": 1000.0,
    }
    worker_mod.process_event(conn, event)

    inserted = [call.args for call in cursor.execute.call_args_list]
    alert_calls = [args for args in inserted if "INSERT INTO alerts" in args[0]]
    assert any(a[1][1] == "geofence_breach" for a in alert_calls)


def test_alert_cooldown_prevents_duplicate_firing(monkeypatch):
    fake_r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(worker_mod, "r", fake_r)
    conn = MagicMock()

    worker_mod.fire_alert(conn, "unit-001", "low_battery", "battery at 5%")
    worker_mod.fire_alert(conn, "unit-001", "low_battery", "battery at 4%")

    cursor = conn.cursor.return_value.__enter__.return_value
    assert cursor.execute.call_count == 1  # second call suppressed by cooldown
