from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import demo
from app.core.config import get_settings
from app.main import app
from app.models.entities import DemoDataset, SensorTelemetry, Vehicle
from app.services.operational_tick import tick


NOW = datetime(2026, 11, 22, 2, tzinfo=UTC)
API = "/api/v1"


def test_wall_clock_demo_refresh_restores_student_eligibility_without_bypassing_rules(file_database, monkeypatch):
    _, engine = file_database
    settings = get_settings()
    monkeypatch.setattr(settings, "demo_clock_utc", None)
    monkeypatch.setattr(settings, "demo_telemetry_seconds", 60)
    monkeypatch.setattr("app.core.utils.utc_now", lambda: NOW)
    monkeypatch.setattr("app.services.rental_service.utc_now", lambda: NOW)
    monkeypatch.setattr("app.services.operational_tick.utc_now", lambda: NOW)
    demo.run("seed")
    with Session(engine) as session:
        for sample in session.scalars(select(SensorTelemetry)):
            sample.timestamp = (NOW - timedelta(days=4)).isoformat()
        session.get(Vehicle, "north-ev2").battery_pct = 20
        session.get(Vehicle, "library-ev1").status = "MAINTENANCE"
        session.commit()
    student, operator = TestClient(app), TestClient(app)
    try:
        for client, alias in [(student, "anh"), (operator, "nam")]:
            assert client.post(f"{API}/sessions", json={
                "login_alias": f"{alias}@demo.example", "password": demo.DEMO_PASSWORD,
            }).status_code == 201
        assert len(operator.get(f"{API}/mobility/vehicles").json()) == 8
        assert student.get(f"{API}/mobility/vehicles").json() == []
        tick()
        visible = {row["vehicle_id"] for row in student.get(f"{API}/mobility/vehicles").json()}
        assert "north-ev1" in visible
        assert "north-ev2" not in visible  # Low battery remains ineligible.
        assert "library-ev1" not in visible  # Maintenance remains ineligible.
        assert len(visible) == 6
        with Session(engine) as session:
            count = len(session.scalars(select(SensorTelemetry)).all())
        tick()
        with Session(engine) as session:
            assert len(session.scalars(select(SensorTelemetry)).all()) == count
    finally:
        student.close()
        operator.close()


def test_demo_refresh_respects_disable_fixed_clock_real_readings_and_dataset_marker(file_database, monkeypatch):
    _, engine = file_database
    from app.repositories.unit_of_work import UnitOfWork
    from app.services.operational_tick import refresh_demo_telemetry
    settings = get_settings()
    monkeypatch.setattr(settings, "demo_clock_utc", NOW - timedelta(days=1))
    demo.run("seed")
    monkeypatch.setattr("app.services.operational_tick.utc_now", lambda: NOW)
    with Session(engine) as session:
        uow = UnitOfWork(session)
        monkeypatch.setattr(settings, "demo_telemetry_seconds", 0)
        assert refresh_demo_telemetry(uow) == 0
        monkeypatch.setattr(settings, "demo_telemetry_seconds", 60)
        assert refresh_demo_telemetry(uow) == 0  # Fixed clock is controlled by test/demo.
        monkeypatch.setattr(settings, "demo_clock_utc", None)
        for sample in session.scalars(select(SensorTelemetry)):
            sample.is_simulated = 0
        session.flush()
        assert refresh_demo_telemetry(uow) == 0  # Never replace a real sensor source.
        session.delete(session.get(DemoDataset, demo.DEMO_DATASET))
        session.flush()
        assert refresh_demo_telemetry(uow) == 0
