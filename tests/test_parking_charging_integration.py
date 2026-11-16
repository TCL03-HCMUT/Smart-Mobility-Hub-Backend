from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import demo
from app.core.config import get_settings
from app.core.security import CSRF_COOKIE
from app.core.utils import utc_now
from app.main import app
from app.models.entities import OperationalIncident
from app.services.operational_tick import tick

API = "/api/v1"
PARK = f"{API}/parking/reservations"
CHARGE = f"{API}/charging/sessions"


@pytest.fixture
def workspace(file_database, monkeypatch):
    now = utc_now().replace(microsecond=0)
    monkeypatch.setattr(get_settings(), "demo_clock_utc", now)
    monkeypatch.setattr(get_settings(), "parking_no_show_minutes", 10)
    monkeypatch.setattr(get_settings(), "charging_duration_minutes", 20)
    demo.run("seed")
    clients = [TestClient(app) for _ in range(3)]
    for client, alias in zip(clients, ("anh", "bao", "nam")):
        response = client.post(f"{API}/sessions", json={
            "login_alias": f"{alias}@demo.example", "password": demo.DEMO_PASSWORD,
        })
        assert response.status_code == 201, response.text
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE]

    def advance(minutes):
        monkeypatch.setattr(get_settings(), "demo_clock_utc", now + timedelta(minutes=minutes))
        tick()

    yield file_database[1], clients, now, advance
    for client in clients:
        client.close()


def book(client, vehicle, start, end):
    return client.post(PARK, json={
        "hub_id": "north", "vehicle_id": f"student-{vehicle}-private",
        "starts_at": start.isoformat(), "ends_at": end.isoformat(),
    })


def charge(client, vehicle, reservation, need_by=None):
    return client.post(CHARGE, json={
        "parking_reservation_id": reservation, "vehicle_id": f"student-{vehicle}-private",
        "need_by": need_by.isoformat() if need_by else None,
    })


def checkout(client, booking):
    return client.post(f"{API}/parking/sessions/{booking['session_id']}/check-out")


def fault(engine, resolve=False):
    with Session(engine) as session:
        if resolve:
            session.get(OperationalIncident, "point-fault").status = "RESOLVED"
        else:
            session.add(OperationalIncident(
                incident_id="point-fault", hub_id="north", resource_id="north-c1",
                reported_by_user_id="operator-nam", incident_type="CHARGER_FAULT",
                status="OPEN", opened_at=utc_now().isoformat(),
            ))
        session.commit()
    tick()


def test_parking_boundaries_no_show_and_overstay(workspace):
    _, (owner, other, _), now, advance = workspace
    first = book(owner, "anh", now, now + timedelta(minutes=10)).json()
    adjacent = book(owner, "anh", now + timedelta(minutes=10), now + timedelta(minutes=30))
    assert adjacent.status_code == 201, adjacent.text
    assert adjacent.json()["parking_space_id"] == first["parking_space_id"]
    conflict = book(owner, "anh", now + timedelta(minutes=9), now + timedelta(minutes=12))
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "PARKING_CONFLICT"
    assert other.get(f"{PARK}/{first['reservation_id']}").status_code == 404
    assert other.get(PARK).json() == []
    checked_in = owner.post(f"{PARK}/{first['reservation_id']}/check-in")
    assert checked_in.status_code == 200, checked_in.text
    occupied = checked_in.json()
    assert occupied["status"] == "CHECKED_IN"
    assert owner.get(f"{API}/mobility/hubs/north").json()["physical_free_now"] == 1
    advance(10)
    assert owner.get(f"{PARK}/{first['reservation_id']}").json()["status"] == "OVERSTAYING"
    assert owner.get(f"{API}/mobility/hubs/north").json()["physical_free_now"] == 1
    assert owner.post(f"{PARK}/{adjacent.json()['reservation_id']}/check-in").status_code == 409
    assert checkout(owner, occupied).json()["status"] == "COMPLETED"
    advance(20)
    expired = f"{PARK}/{adjacent.json()['reservation_id']}"
    assert owner.get(expired).json()["status"] == "EXPIRED"
    assert owner.post(f"{expired}/check-in").status_code == 409
    assert owner.get(f"{API}/mobility/hubs/north").json()["physical_free_now"] == 2


def test_race_for_last_space_and_cancel_releases_it(workspace):
    _, (first, second, _), now, _ = workspace
    end = now + timedelta(hours=1)
    # One of the two free spaces is committed during the target interval.
    commitment = book(first, "anh", now, end).json()
    additional_vehicle = first.post(f"{API}/fleet/vehicles/private", json={"license_plate": "ANH-SECOND"}).json()
    start = now + timedelta(minutes=1)
    payload = {
        "hub_id": "north", "vehicle_id": additional_vehicle["vehicle_id"],
        "starts_at": start.isoformat(), "ends_at": end.isoformat(),
    }
    barrier = Barrier(2)

    def take_first():
        barrier.wait(timeout=5)
        return first.post(PARK, json=payload)

    def take_second():
        barrier.wait(timeout=5)
        return book(second, "bao", start, end)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(take_first)
        second_future = pool.submit(take_second)
        responses = [first_future.result(), second_future.result()]
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert first.post(f"{PARK}/{commitment['reservation_id']}/cancel").json()["status"] == "CANCELLED"
    retry = first.post(PARK, json=payload) if responses[0].status_code == 409 else book(second, "bao", start, end)
    assert retry.status_code == 201


def test_charging_priority_fault_requeue_complete_and_stop(workspace):
    engine, (first, second, operator), now, advance = workspace
    end = now + timedelta(hours=2)
    first_id = book(first, "anh", now, end).json()["reservation_id"]
    second_id = book(second, "bao", now, end).json()["reservation_id"]
    low_priority = charge(first, "anh", first_id, now + timedelta(hours=1)).json()
    high_priority = charge(second, "bao", second_id, now + timedelta(minutes=30)).json()
    low = f"{CHARGE}/{low_priority['session_id']}"
    high = f"{CHARGE}/{high_priority['session_id']}"
    assert low_priority["status"] == high_priority["status"] == "PENDING"
    assert low_priority["charging_point_id"] is None
    assert operator.post(f"{low}/start").status_code == 409
    assert first.get(high).status_code == 404
    assert len(operator.get(CHARGE).json()) == 2
    fault(engine)
    assert first.post(f"{PARK}/{first_id}/check-in").status_code == 200
    parked = second.post(f"{PARK}/{second_id}/check-in").json()
    fault(engine, resolve=True)
    assert first.get(low).json()["status"] == "PENDING"
    assert second.get(high).json()["status"] == "SCHEDULED"
    assert "need-by" in second.get(high).json()["reason"]
    assert second.post(f"{high}/start").status_code == 403
    assert operator.post(f"{high}/start").json()["status"] == "ACTIVE"
    for client, expected_actors in (
        (operator, ["operator-nam"]), (second, ["operator-nam"]), (first, []),
    ):
        activity = client.get(f"{API}/activity")
        assert activity.status_code == 200, activity.text
        actors = [
            event["actor_id"] for event in activity.json()
            if event["action"] == "CHARGE_STARTED"
            and event["resource_type"] == "charging"
            and event["resource_id"] == high_priority["session_id"]
        ]
        assert actors == expected_actors
    assert operator.post(f"{high}/complete").status_code == 409
    assert operator.post(f"{low}/start").status_code == 409
    # A fault after start releases the c1 assignment, preserves parking and returns the request to the queue.
    with Session(engine) as session:
        session.get(OperationalIncident, "point-fault").status = "OPEN"
        session.commit()
    tick()
    requeued = second.get(high).json()
    assert requeued["status"] == "PENDING" and requeued["charging_point_id"] is None
    assert "Returned to the queue" in requeued["reason"]
    fault(engine, resolve=True)
    assert operator.post(f"{high}/start").json()["status"] == "ACTIVE"
    advance(20)
    assert second.get(high).json()["status"] == "COMPLETED"
    assert second.get(f"{API}/fleet/vehicles/private").json()[0]["battery_pct"] == 80
    assert second.get(f"{PARK}/{second_id}").json()["status"] == "CHECKED_IN"
    assert second.get(f"{API}/mobility/hubs/north").json()["physical_free_now"] == 0
    assert operator.post(f"{low}/start").json()["status"] == "ACTIVE"
    assert first.post(f"{low}/stop").json()["status"] == "STOPPED"
    assert checkout(second, parked).json()["status"] == "COMPLETED"
    assert second.get(f"{API}/mobility/hubs/north").json()["physical_free_now"] == 1


@pytest.mark.parametrize("second_has_lower_battery", [False, True])
def test_priority_uses_low_battery_then_arrival_order(workspace, second_has_lower_battery):
    engine, (first, second, _), now, _ = workspace
    if second_has_lower_battery:
        from app.models.entities import Vehicle

        with Session(engine) as session:
            session.get(Vehicle, "student-bao-private").battery_pct = 20
            session.commit()
    fault(engine)
    paths = []
    for client, alias in [(first, "anh"), (second, "bao")]:
        booking = book(client, alias, now, now + timedelta(hours=1)).json()
        booking_id = booking["reservation_id"]
        response = client.post(f"{PARK}/{booking_id}/check-in")
        assert response.status_code == 200, response.text
        request = charge(client, alias, booking_id).json()
        assert request["status"] == "PENDING"
        paths.append(f"{CHARGE}/{request['session_id']}")
    fault(engine, resolve=True)
    expected = ["PENDING", "SCHEDULED"] if second_has_lower_battery else ["SCHEDULED", "PENDING"]
    assert [client.get(path).json()["status"] for client, path in zip((first, second), paths)] == expected
