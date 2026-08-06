import json
import time

import fakeredis
import pytest

import app as api_app


@pytest.fixture
def client():
    api_app.r = fakeredis.FakeRedis(decode_responses=True)
    api_app.r.hset(
        api_app.REGISTRY_KEY,
        mapping={
            "unit-001": json.dumps({"token": "good-token", "geofence_radius_km": 5}),
        },
    )
    api_app.app.config["TESTING"] = True
    with api_app.app.test_client() as c:
        yield c


def telemetry(client, **overrides):
    body = {
        "unit_id": "unit-001",
        "token": "good-token",
        "lat": 31.973,
        "lon": 34.7925,
        "battery_pct": 80,
        "signal_pct": 95,
    }
    body.update(overrides)
    return client.post("/telemetry", data=json.dumps(body), content_type="application/json")


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_metrics_exposed(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200


def test_missing_credentials_rejected(client):
    resp = client.post("/telemetry", data=json.dumps({"lat": 1, "lon": 1}), content_type="application/json")
    assert resp.status_code == 400


def test_unregistered_unit_rejected(client):
    resp = telemetry(client, unit_id="ghost-unit")
    assert resp.status_code == 401


def test_wrong_token_rejected(client):
    resp = telemetry(client, token="totally-wrong")
    assert resp.status_code == 403


def test_bad_coordinates_rejected(client):
    resp = telemetry(client, lat=999, lon=999)
    assert resp.status_code == 422


def test_bad_battery_rejected(client):
    resp = telemetry(client, battery_pct=150)
    assert resp.status_code == 422


def test_valid_telemetry_accepted_and_readable(client):
    resp = telemetry(client)
    assert resp.status_code == 202

    status_resp = client.get("/status/unit-001")
    assert status_resp.status_code == 200
    data = status_resp.get_json()
    assert data["battery_pct"] == 80
    assert data["unit_id"] == "unit-001"


def test_implausible_speed_rejected(client):
    # First a legitimate reading near base...
    first = telemetry(client, lat=31.973, lon=34.7925)
    assert first.status_code == 202

    # ...then a "teleport" 500km away a second later - physically impossible.
    second = telemetry(client, lat=35.0, lon=38.0)
    assert second.status_code == 422


def test_status_all_lists_units(client):
    telemetry(client)
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 1
    assert data["units"][0]["unit_id"] == "unit-001"
