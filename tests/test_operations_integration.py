from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app import demo
from app.core.config import get_settings
from app.core.security import CSRF_COOKIE
from app.core.utils import utc_now
from app.main import app

API = "/api/v1"
DASHBOARD = f"{API}/operations"
INCIDENTS = f"{API}/incidents"
TASKS = f"{API}/redistribution-tasks"
HOLDS = f"{API}/rentals/reservations"


@pytest.fixture
def demo_clients(file_database, monkeypatch):
    now = utc_now().replace(microsecond=0)
    monkeypatch.setattr(get_settings(), "demo_clock_utc", now)
    demo.run("seed")
    clients = [TestClient(app), TestClient(app)]
    for client, alias in zip(clients, ("anh", "nam")):
        response = client.post(f"{API}/sessions", json={
            "login_alias": f"{alias}@demo.example", "password": demo.DEMO_PASSWORD,
        })
        assert response.status_code == 201, response.text
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE]
    yield (*clients, now)
    for client in clients:
        client.close()


def dispatch(client):
    return client.post(TASKS, json={
        "vehicle_id": "north-ev1", "from_hub_id": "north", "to_hub_id": "metro",
    })


def incident(client, **payload):
    return client.post(INCIDENTS, json={**payload, "incident_type": "fault", "severity": "high"})


def summary(client, hub):
    return client.get(f"{API}/mobility/hubs/{hub}").json()


def test_dispatch_failure_in_transit_keeps_space_and_recovers_on_arrival(demo_clients):
    student, operator, _ = demo_clients
    assert student.get(DASHBOARD).status_code == 403
    assert student.get(TASKS).status_code == 403
    assert student.get(INCIDENTS).status_code == 403
    assert dispatch(student).status_code == 403
    response = dispatch(operator)
    assert response.status_code == 201, response.text
    task = response.json()
    task_url = f"{TASKS}/{task['task_id']}"
    assert task["status"] == "PLANNED"
    assert summary(operator, "north")["physical_free_now"] == 2
    assert summary(operator, "metro")["bookable_now"] == 1
    assert summary(operator, "metro")["physical_free_now"] == 2
    assert dispatch(operator).status_code == 409
    assert student.post(HOLDS, json={"vehicle_id": "north-ev1"}).status_code == 409
    before = operator.get(DASHBOARD)
    assert before.status_code == 200, before.text
    assert before.headers["X-Operational-Revision"] == "1"
    status = {vehicle["vehicle_id"]: vehicle["status"] for vehicle in before.json()["vehicles"]}
    assert status["north-ev1"] == "COMMITTED"

    assert operator.post(f"{task_url}/start").json()["status"] == "IN_TRANSIT"
    assert summary(operator, "north")["physical_free_now"] == 3
    report = incident(operator, hub_id="metro", vehicle_id="north-ev1")
    assert report.status_code == 201, report.text
    incident_url = f"{INCIDENTS}/{report.json()['incident_id']}"
    assert operator.get(incident_url).json()["status"] == "OPEN"
    assert operator.get(task_url).json()["status"] == "FAILED"
    assert operator.post(f"{task_url}/complete").status_code == 409
    assert summary(operator, "metro")["bookable_now"] == 1
    dashboard = operator.get(DASHBOARD).json()
    vehicle = next(item for item in dashboard["vehicles"] if item["vehicle_id"] == "north-ev1")
    assert vehicle["current_hub_id"] is None and vehicle["status"] == "IN_TRANSIT"
    assert {"hub_id": "metro", "code": "DISPATCH_INTERRUPTED"} in dashboard["alerts"]

    assert operator.post(f"{incident_url}/resolve", json={"resolution_notes": "   "}).status_code == 422
    resolved = operator.post(f"{incident_url}/resolve", json={"resolution_notes": "Vehicle inspected."})
    assert resolved.json()["status"] == "RESOLVED"
    assert resolved.json()["resolution_notes"] == "Vehicle inspected."
    assert operator.get(task_url).json()["status"] == "FAILED"
    assert operator.post(f"{task_url}/complete").json()["status"] == "COMPLETED"
    assert operator.post(f"{task_url}/complete").status_code == 409
    assert summary(operator, "north")["physical_free_now"] == 3
    assert summary(operator, "metro")["physical_free_now"] == 1
    assert operator.get(TASKS).json()[0]["completed_at"]
    audit = operator.get(f"{API}/activity").json()
    assert len(audit) == 6
    assert {item["actor_id"] for item in audit} == {"operator-nam"}
    assert {item["action"] for item in audit} == {
        "DISPATCH_PLANNED", "DISPATCH_STARTED", "DISPATCH_FAILED", "DISPATCH_COMPLETED",
        "INCIDENT_REPORTED", "INCIDENT_RESOLVED",
    }
    assert student.get(f"{API}/activity").json() == []


def test_operator_fault_reports_and_resolves_charging_through_api(demo_clients):
    student, operator, now = demo_clients
    parking = student.post(f"{API}/parking/reservations", json={
        "vehicle_id": "student-anh-private", "hub_id": "north",
        "starts_at": now.isoformat(), "ends_at": (now + timedelta(hours=1)).isoformat(),
    }).json()
    booking = f"{API}/parking/reservations/{parking['reservation_id']}"
    assert student.post(f"{booking}/check-in").status_code == 200
    request = student.post(f"{API}/charging/sessions", json={
        "vehicle_id": "student-anh-private", "parking_reservation_id": parking["reservation_id"],
    }).json()
    session = f"{API}/charging/sessions/{request['session_id']}"
    assert operator.post(f"{session}/start").json()["status"] == "ACTIVE"
    assert incident(student, hub_id="north", resource_id="north-c1").status_code == 403
    assert incident(operator, hub_id="metro", resource_id="north-c1").status_code == 404
    assert incident(operator, hub_id="north", resource_id="north-c1", vehicle_id="north-ev1").status_code == 422
    report = incident(operator, hub_id="north", resource_id="north-c1", description="Connector failed.")
    assert report.status_code == 201, report.text
    assert report.json()["description"] == "Connector failed."
    assert incident(operator, hub_id="north", resource_id="north-c1").status_code == 409
    assert student.get(session).json()["status"] == "PENDING"
    assert student.get(booking).json()["status"] == "CHECKED_IN"
    assert summary(operator, "north")["charging_points"] == [{"resource_id": "north-c1", "status": "UNAVAILABLE"}]
    assert {"hub_id": "north", "code": "CHARGER_UNAVAILABLE"} in operator.get(DASHBOARD).json()["alerts"]
    resolve_url = f"{INCIDENTS}/{report.json()['incident_id']}/resolve"
    assert operator.post(resolve_url, json={"resolution_notes": "Replaced connector."}).status_code == 200
    assert student.get(session).json()["status"] == "SCHEDULED"


def test_hold_and_dispatch_cannot_claim_same_vehicle(demo_clients):
    student, operator, _ = demo_clients
    barrier = Barrier(2)

    def race(call):
        barrier.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(max_workers=2) as executor:
        hold = executor.submit(race, lambda: student.post(HOLDS, json={"vehicle_id": "north-ev1"}))
        task = executor.submit(race, lambda: dispatch(operator))
        assert sorted([hold.result().status_code, task.result().status_code]) == [201, 409]
    assert len(student.get(HOLDS).json()) + len(operator.get(TASKS).json()) == 1


def test_failed_before_departure_releases_reserved_destination(demo_clients):
    student, operator, _ = demo_clients
    assert summary(operator, "metro")["bookable_now"] == 2
    task = dispatch(operator).json()
    url = f"{TASKS}/{task['task_id']}"
    report = incident(operator, hub_id="north", vehicle_id="north-ev1")
    assert report.status_code == 201
    assert operator.get(url).json()["status"] == "FAILED"
    assert summary(operator, "metro")["bookable_now"] == 2
    assert summary(operator, "north")["physical_free_now"] == 2
    assert operator.post(f"{url}/start").status_code == 409
    assert operator.post(f"{url}/complete").status_code == 409
    assert student.post(HOLDS, json={"vehicle_id": "north-ev1"}).status_code == 409
    assert operator.post(f"{INCIDENTS}/{report.json()['incident_id']}/resolve", json={
        "resolution_notes": "Repaired at source.",
    }).status_code == 200
    assert student.post(HOLDS, json={"vehicle_id": "north-ev1"}).status_code == 201


def test_destination_space_fault_blocks_arrival_until_an_alternate_space_is_available(demo_clients):
    _, operator, _ = demo_clients
    response = dispatch(operator)
    assert response.status_code == 201, response.text
    task_url = f"{TASKS}/{response.json()['task_id']}"
    assert operator.post(f"{task_url}/start").status_code == 200
    incidents = []
    for space in ("metro-p3", "metro-p4"):
        response = incident(operator, hub_id="metro", resource_id=space)
        assert response.status_code == 201, response.text
        incidents.append(response.json()["incident_id"])
    blocked = operator.post(f"{task_url}/complete")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "HUB_FULL"
    assert operator.get(task_url).json()["status"] == "FAILED"
    assert summary(operator, "metro")["physical_free_now"] == 2
    assert operator.post(f"{INCIDENTS}/{incidents[1]}/resolve", json={
        "resolution_notes": "Alternate space opened.",
    }).status_code == 200
    assert operator.post(f"{task_url}/complete").json()["status"] == "COMPLETED"
    assert summary(operator, "metro")["physical_free_now"] == 1
