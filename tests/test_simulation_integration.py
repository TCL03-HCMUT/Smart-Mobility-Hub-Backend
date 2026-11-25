import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import demo
from app.core.config import get_settings
from app.core.security import CSRF_COOKIE
from app.core.utils import utc_now
from app.database import Base
from app.main import app
from app.models.entities import ChargingPoint, HubResource, MobilityHub, SimulationRun
from app.services.simulation_engine import DemandSimulationEngine

API = "/api/v1"
RUNS = f"{API}/simulations"
METRO_SURGE = {
    "scenario_type": "RENTAL_DEMAND_SURGE", "hub_id": "metro",
    "horizon_minutes": 30, "demand_count": 3,
}


@pytest.fixture
def simulation_workspace(file_database, monkeypatch):
    now = utc_now().replace(microsecond=0)
    settings = get_settings()
    for field, value in {
        "demo_clock_utc": now, "simulation_step_minutes": 5,
        "simulation_baseline_rental_count": 2, "charging_duration_minutes": 20,
    }.items():
        monkeypatch.setattr(settings, field, value)
    demo.run("seed")
    clients = [TestClient(app), TestClient(app)]
    for client, alias in zip(clients, ("anh", "nam")):
        response = client.post(f"{API}/sessions", json={
            "login_alias": f"{alias}@demo.example", "password": demo.DEMO_PASSWORD,
        })
        assert response.status_code == 201, response.text
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE]
    yield file_database[1], (*clients, now)
    for client in clients:
        client.close()


def operational_rows(engine):
    with Session(engine) as session:
        return {
            table.name: {tuple(row) for row in session.execute(select(table))}
            for table in Base.metadata.tables.values() if table is not SimulationRun.__table__
        }


def kpis(run):
    return {item["name"]: (item["baseline"], item["scenario"]) for item in run["metrics"]}


def test_metro_projection_isolation_determinism_and_dispatch_advice(simulation_workspace):
    engine, (student, operator, now) = simulation_workspace
    assert student.get(RUNS).status_code == 403
    assert student.post(RUNS, json=METRO_SURGE).status_code == 403
    before = operational_rows(engine)
    first = operator.post(RUNS, json=METRO_SURGE)
    second = operator.post(RUNS, json=METRO_SURGE)
    assert first.status_code == second.status_code == 201, first.text
    run = first.json()
    assert kpis(run) == {
        "rental_requests": (2, 5), "rentals_served": (2, 2),
        "unmet_rental_demand": (0, 3), "available_vehicles_at_end": (0, 0),
    }
    assert run["snapshot_revision"] == "0"
    assert run["snapshot_at"] == now.isoformat().replace("+00:00", "Z")
    assert {key: value for key, value in run.items() if key != "run_id"} == {
        key: value for key, value in second.json().items() if key != "run_id"
    }
    assert operational_rows(engine) == before
    route = f"{RUNS}/{run['run_id']}"
    assert student.get(route).status_code == 403
    assert operator.get(route).json() == run
    assert len(operator.get(RUNS, params={"limit": 1}).json()) == 1
    with Session(engine) as session:
        stored = json.loads(session.get(SimulationRun, run["run_id"]).scenario_config)
        metro = next(hub for hub in stored["snapshot"]["hubs"] if hub["hub_id"] == "metro")
        assert len(metro["vehicles"]) == 2

    advice = run["recommendations"][0]
    assert advice["type"] == "REDISTRIBUTE_VEHICLE" and advice["reason"]
    dispatch = operator.post(f"{API}/redistribution-tasks", json={
        field: advice[field] for field in ("vehicle_id", "from_hub_id", "to_hub_id")
    })
    assert dispatch.status_code == 201, dispatch.text
    assert operator.get(route).json()["snapshot_revision"] == "0"


def test_fault_projection_uses_workload_and_horizon_without_writes(simulation_workspace):
    engine, (student, operator, now) = simulation_workspace
    response = student.post(f"{API}/parking/reservations", json={
        "vehicle_id": "student-anh-private", "hub_id": "north",
        "starts_at": now.isoformat(), "ends_at": (now + timedelta(hours=1)).isoformat(),
    })
    assert response.status_code == 201, response.text
    booking = response.json()["reservation_id"]
    assert student.post(f"{API}/parking/reservations/{booking}/check-in").status_code == 200
    response = student.post(f"{API}/charging/sessions", json={
        "parking_reservation_id": booking, "vehicle_id": "student-anh-private",
    })
    assert response.status_code == 201, response.text
    charge_route = f"{API}/charging/sessions/{response.json()['session_id']}"
    assert operator.post(f"{charge_route}/start").json()["status"] == "ACTIVE"
    before = operational_rows(engine)
    config = {"scenario_type": "CHARGER_FAULT", "hub_id": "north", "charging_point_id": "north-c1"}
    short = operator.post(RUNS, json={**config, "horizon_minutes": 19})
    assert short.status_code == 201, short.text
    full = operator.post(RUNS, json={**config, "horizon_minutes": 20})
    assert full.status_code == 201, full.text
    assert kpis(short.json())["charges_completed"] == (0, 0)
    assert kpis(full.json()) == {
        "charging_capacity": (1, 0), "charges_completed": (1, 0),
        "charges_not_completed_at_horizon": (0, 1),
    }
    assert full.json()["recommendations"][0]["type"] == "RESTORE_CHARGER"
    assert operational_rows(engine) == before
    assert student.get(charge_route).json()["status"] == "ACTIVE"


def test_snapshot_can_be_replayed_while_operations_change(simulation_workspace, monkeypatch):
    _, (student, operator, _) = simulation_workspace
    real_run = DemandSimulationEngine.run

    def run_with_interleaved_hold(engine, snapshot, scenario):
        response = student.post(f"{API}/rentals/reservations", json={"vehicle_id": "metro-ev1"})
        assert response.status_code == 201, response.text
        return real_run(engine, snapshot, scenario)

    monkeypatch.setattr(DemandSimulationEngine, "run", run_with_interleaved_hold)
    response = operator.post(RUNS, json=METRO_SURGE)
    assert response.status_code == 201, response.text
    assert response.json()["snapshot_revision"] == "0"
    assert kpis(response.json())["rentals_served"] == (2, 2)
    assert operator.get(f"{API}/operations").headers["X-Operational-Revision"] == "1"


def test_reject_invalid_or_unavailable_scenario_inputs(simulation_workspace):
    _, (_, operator, _) = simulation_workspace
    for payload in (
        {**METRO_SURGE, "scenario_type": "PARKING_SURGE"},
        {**METRO_SURGE, "horizon_minutes": 241},
        {**METRO_SURGE, "demand_count": True},
        {**METRO_SURGE, "charging_point_id": "metro-c1"},
    ):
        assert operator.post(RUNS, json=payload).status_code == 422
    for kind in ("PARKING_CAPACITY", "CHARGING_DEMAND_SURGE"):
        assert operator.post(RUNS, json={**METRO_SURGE, "scenario_type": kind, "demand_count": 0}).status_code == 422
    fleet = {"scenario_type": "FLEET_IMBALANCE", "hub_id": "north", "horizon_minutes": 30}
    assert operator.post(RUNS, json={**fleet, "direction": "SURGE", "vehicle_count": 2}).status_code == 422
    assert operator.post(RUNS, json={**fleet, "direction": "SHORTAGE", "vehicle_count": 1001}).status_code == 422
    assert operator.post(RUNS, json={**METRO_SURGE, "hub_id": "missing"}).status_code == 404
    fault = {"scenario_type": "CHARGER_FAULT", "hub_id": "north", "horizon_minutes": 30}
    assert operator.post(RUNS, json={**fault, "charging_point_id": "metro-c1"}).status_code == 404
    assert operator.post(f"{API}/incidents", json={
        "hub_id": "north", "resource_id": "north-c1", "incident_type": "FAULT", "severity": "HIGH",
    }).status_code == 201
    assert operator.post(RUNS, json={**fault, "charging_point_id": "north-c1"}).status_code == 409
    assert operator.get(RUNS).json() == []


@pytest.mark.parametrize(("payload", "metrics", "advice_type"), [
    (
        {**METRO_SURGE, "hub_id": "north"},
        {"rental_requests": (2, 5), "unmet_rental_demand": (0, 3)},
        "REDISTRIBUTE_VEHICLE",
    ),
    (
        {"scenario_type": "PARKING_CAPACITY", "demand_count": 2},
        {"parking_occupied_at_end": (2, 4), "parking_bookable_at_end": (2, 0), "parking_rejected": (0, 0)},
        "REDIRECT_PARKING",
    ),
    (
        {"scenario_type": "PARKING_CAPACITY", "demand_count": 3},
        {"parking_occupied_at_end": (2, 4), "parking_bookable_at_end": (2, 0), "parking_rejected": (0, 1)},
        "REDIRECT_PARKING",
    ),
    (
        {"scenario_type": "CHARGING_DEMAND_SURGE", "demand_count": 3, "horizon_minutes": 40},
        {"charging_capacity": (1, 1), "charging_requests": (0, 3),
         "charges_completed": (0, 2), "charges_not_completed_at_horizon": (0, 1)},
        "RESCHEDULE_CHARGING",
    ),
    (
        {"scenario_type": "FLEET_IMBALANCE", "direction": "SHORTAGE", "vehicle_count": 3},
        {"available_vehicles_at_start": (2, 0), "unmet_rental_demand": (0, 2)},
        "REDISTRIBUTE_VEHICLE",
    ),
    (
        {"scenario_type": "FLEET_IMBALANCE", "direction": "CONCENTRATION", "vehicle_count": 3},
        {"available_vehicles_at_start": (2, 5), "available_vehicles_at_end": (0, 3),
         "unmet_rental_demand": (0, 0)},
        "MONITOR_FLEET",
    ),
], ids=["other-hub-rental", "exact-parking-capacity", "parking-overflow", "charging-surge",
        "fleet-shortage", "fleet-concentration"])
def test_scenario_exposes_projected_impact_without_changing_operations(
    simulation_workspace, payload, metrics, advice_type,
):
    engine, (student, operator, _) = simulation_workspace
    config = {"hub_id": "north", "horizon_minutes": 30, **payload}
    assert student.post(RUNS, json=config).status_code == 403
    before = operational_rows(engine)
    outcomes = []
    for _ in range(3):
        response = operator.post(RUNS, json=config)
        assert response.status_code == 201, response.text
        run = response.json()
        assert run["scenario_type"] == config["scenario_type"]
        assert run["model_version"] == "demo-2"
        assert kpis(run).items() >= metrics.items()
        assert run["recommendations"][0]["type"] == advice_type
        assert operator.get(f"{RUNS}/{run['run_id']}").json() == run
        outcomes.append({key: value for key, value in run.items() if key != "run_id"})
    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert operational_rows(engine) == before


def test_partial_charging_point_failure_leaves_other_point_working(simulation_workspace):
    engine, (_, operator, _) = simulation_workspace
    with Session(engine) as session:
        session.get(MobilityHub, "north").max_charging_capacity = 2
        session.add(HubResource(
            resource_id="north-c2", hub_id="north", code="C2", resource_type="CHARGING_POINT",
        ))
        session.flush()
        session.add(ChargingPoint(resource_id="north-c2"))
        session.commit()
    before = operational_rows(engine)
    result = operator.post(RUNS, json={
        "scenario_type": "CHARGER_FAULT", "hub_id": "north",
        "charging_point_id": "north-c1", "horizon_minutes": 30,
    })
    assert result.status_code == 201, result.text
    assert kpis(result.json())["charging_capacity"] == (2, 1)
    assert operational_rows(engine) == before
