from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import demo
from app.core.config import get_settings
from app.core.security import CSRF_COOKIE
from app.main import app
from app.models.entities import ParkingReservation, SensorTelemetry, Vehicle, VehicleReservation

API = "/api/v1"
HOLDS = f"{API}/rentals/reservations"
RIDES = f"{API}/rentals/rentals"
NOW = datetime(2026, 11, 16, 2, tzinfo=UTC)


@pytest.fixture
def scenario(file_database, monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_clock_utc", NOW)
    demo.run("seed")
    clients = [TestClient(app), TestClient(app)]
    for client, name in zip(clients, ["anh", "bao"]):
        response = client.post(f"{API}/sessions", json={
            "login_alias": f"{name}@demo.example", "password": demo.DEMO_PASSWORD,
        })
        assert response.status_code == 201
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE]
    yield file_database[1], clients
    for client in clients:
        client.close()


def parallel(calls):
    barrier = Barrier(len(calls))

    def execute(call):
        barrier.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(execute, calls))


def test_race_one_hold_and_queries_are_owned(scenario):
    _, (first, second) = scenario
    results = parallel([
        lambda: first.post(HOLDS, json={"vehicle_id": "north-ev1", "intended_hub_id": "metro"}),
        lambda: second.post(HOLDS, json={"vehicle_id": "north-ev1"}),
    ])
    assert sorted(response.status_code for response in results) == [201, 409]
    winner = next(index for index, response in enumerate(results) if response.status_code == 201)
    clients = [first, second]
    hold = results[winner].json()
    assert hold["origin_hub_id"] == "north"
    assert datetime.fromisoformat(hold["expires_at"]) == NOW + timedelta(minutes=15)
    path = f"{HOLDS}/{hold['reservation_id']}"
    assert clients[winner].get(path).json() == hold
    assert clients[1 - winner].get(path).status_code == 404
    assert clients[1 - winner].get(HOLDS).json() == []
    assert first.get(f"{API}/mobility/hubs/north").json()["physical_free_now"] == 2
    assert "license_plate" not in first.get(f"{API}/mobility/vehicles").json()[0]
    assert [v["vehicle_id"] for v in second.get(f"{API}/fleet/vehicles/private").json()] == ["student-bao-private"]
    assert len(clients[winner].get(f"{API}/activity").json()) == 1
    assert clients[1 - winner].get(f"{API}/activity").json() == []


def test_server_clock_expiry_and_battery_boundary(scenario, monkeypatch):
    engine, (client, _) = scenario
    with Session(engine) as session:
        session.get(Vehicle, "north-ev1").battery_pct = 30
        session.get(Vehicle, "north-ev2").battery_pct = 29
        session.commit()
    listing = client.get(f"{API}/mobility/vehicles", params={"hub_id": "north", "destination_hub_id": "metro"})
    assert [v["vehicle_id"] for v in listing.json()] == ["north-ev1"]
    assert client.post(HOLDS, json={"vehicle_id": "north-ev1", "pickup_deadline_at": NOW.isoformat()}).status_code == 422
    response = client.post(HOLDS, json={"vehicle_id": "north-ev1"})
    hold_id = response.json()["reservation_id"]
    monkeypatch.setattr(get_settings(), "demo_clock_utc", NOW + timedelta(minutes=6))
    assert client.get(f"{API}/mobility/vehicles", params={"hub_id": "north"}).json() == []
    assert client.post(f"{HOLDS}/{hold_id}/pickup").json()["error"]["code"] == "BATTERY_STALE"
    monkeypatch.setattr(get_settings(), "demo_clock_utc", NOW + timedelta(minutes=15))
    assert client.get(f"{HOLDS}/{hold_id}").json()["status"] == "EXPIRED"
    assert client.post(f"{HOLDS}/{hold_id}/pickup").json()["error"]["code"] == "HOLD_EXPIRED"
    with Session(engine) as session:
        assert session.get(VehicleReservation, hold_id).status == "EXPIRED"
        assert session.get(Vehicle, "north-ev1").status == "AVAILABLE"
    hub = f"{API}/mobility/hubs/north"
    revision = client.get(hub).headers["X-Operational-Revision"]
    assert client.post(f"{HOLDS}/{hold_id}/pickup").status_code == 409
    assert client.get(hub).headers["X-Operational-Revision"] == revision


def test_race_last_return_space_respects_booking_and_preserves_loser(scenario):
    engine, (first, second) = scenario
    rides = []
    for client, vehicle in [(first, "north-ev1"), (second, "library-ev1")]:
        hold = client.post(HOLDS, json={"vehicle_id": vehicle, "intended_hub_id": "metro"}).json()
        pickup = client.post(f"{HOLDS}/{hold['reservation_id']}/pickup")
        assert pickup.status_code == 200, pickup.text
        rides.append(pickup.json()["rental_id"])
    with Session(engine) as session:
        session.add(ParkingReservation(
            reservation_id="future", user_id="student-bao", vehicle_id="student-bao-private",
            parking_space_id="metro-p3", starts_at=(NOW + timedelta(days=1)).isoformat(),
            ends_at=(NOW + timedelta(days=1, hours=1)).isoformat(), status="RESERVED",
        ))
        session.commit()
    assert first.get(f"{API}/mobility/hubs/metro").json()["bookable_now"] == 1
    results = parallel([
        lambda: first.post(f"{RIDES}/{rides[0]}/return", json={"return_hub_id": "metro"}),
        lambda: second.post(f"{RIDES}/{rides[1]}/return", json={"return_hub_id": "metro"}),
    ])
    assert sorted(r.status_code for r in results) == [200, 409]
    losing_index = next(index for index, response in enumerate(results) if response.status_code == 409)
    error = results[losing_index].json()["error"]
    assert error["code"] == "HUB_FULL"
    assert error["details"]["physical_free_now"] == 1
    assert error["details"]["alternatives"]
    owner = [first, second][losing_index]
    other = [second, first][losing_index]
    ride_url = f"{RIDES}/{rides[losing_index]}"
    assert other.get(ride_url).status_code == 404
    assert owner.get(ride_url).json()["status"] == "ACTIVE"
    alternative = error["details"]["alternatives"][0]["hub_id"]
    assert owner.post(f"{ride_url}/return", json={"return_hub_id": alternative}).status_code == 200
    assert owner.get(RIDES).json()[0]["status"] == "COMPLETED"


def test_pickup_just_before_deadline_with_refreshed_battery(scenario, monkeypatch):
    engine, (client, _) = scenario
    hold = client.post(HOLDS, json={"vehicle_id": "north-ev1"}).json()
    soon = NOW + timedelta(minutes=15, microseconds=-1)
    monkeypatch.setattr(get_settings(), "demo_clock_utc", soon)
    with Session(engine) as session:
        session.add(SensorTelemetry(
            sensor_id="refresh", vehicle_id="north-ev1", metric_name="battery_pct",
            metric_value=90, timestamp=soon.isoformat(), is_simulated=1,
        ))
        session.commit()
    pickup = client.post(f"{HOLDS}/{hold['reservation_id']}/pickup")
    assert pickup.status_code == 200, pickup.text
    assert client.get(f"{API}/mobility/hubs/north").json()["physical_free_now"] == 3
    assert client.post(f"{HOLDS}/{hold['reservation_id']}/pickup").status_code == 409
    assert len(client.get(RIDES).json()) == 1
