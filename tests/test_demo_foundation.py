from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app import database, demo
from app.core.config import get_settings
from app.core.security import CSRF_COOKIE, SESSION_COOKIE
from app.main import app
from app.models.entities import AuthSession, User


def login(client, alias="anh@demo.example", password=demo.DEMO_PASSWORD):
    return client.post("/api/v1/sessions", json={"login_alias": alias, "password": password})


def test_seed_reset_and_session_persistence(file_database):
    url, _ = file_database
    assert demo.run("init") == "SQLite schema ready."
    with pytest.raises(ValueError, match="restricted"):
        demo.run("reset")
    assert demo.run("seed") == "Demo data seeded."
    assert demo.run("seed") == "Demo data already present."
    client = TestClient(app)
    try:
        response = login(client)
        assert response.status_code == 201
        assert response.json()["role"] == "student"
        assert any(SESSION_COOKIE in cookie and "HttpOnly" in cookie for cookie in response.headers.get_list("set-cookie"))
        token = client.cookies[SESSION_COOKIE]
        new_engine = create_engine(url)
        try:
            with Session(new_engine) as session:
                assert len(session.scalars(select(User)).all()) == 3
                persisted = session.scalars(select(AuthSession)).one()
                assert persisted.token_hash != token
        finally:
            new_engine.dispose()
        assert demo.run("reset") == "Demo data seeded."
        assert client.get("/api/v1/sessions/me").status_code == 401
    finally:
        client.close()


def test_role_csrf_logout_expiry_and_error_shape(file_database, monkeypatch):
    demo.run("seed")
    client = TestClient(app)
    fixed = datetime(2026, 11, 16, 2, tzinfo=UTC)
    monkeypatch.setattr(get_settings(), "demo_clock_utc", fixed)
    preflight = client.options("/api/v1/sessions", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-csrf-token",
    })
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert preflight.headers["access-control-allow-credentials"] == "true"
    invalid = client.post("/api/v1/sessions", json={"login_alias": "x", "password": ""})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_INPUT"
    assert login(client, password="wrong").json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert login(client).status_code == 201

    protected = "/api/v1/fleet/vehicles/private"
    attempt = client.post(protected, json={"license_plate": "without-csrf"})
    assert attempt.status_code == 403
    assert attempt.json()["error"]["code"] == "INVALID_CSRF"
    assert client.post(
        "/api/v1/redistribution-tasks/first/start",
        headers={"X-CSRF-Token": client.cookies[CSRF_COOKIE]},
    ).status_code == 403
    assert all(row["ownership_type"] == "PUBLIC" for row in client.get("/api/v1/mobility/vehicles").json())

    logout_headers = {"X-CSRF-Token": client.cookies[CSRF_COOKIE]}
    assert client.post("/api/v1/sessions/logout", headers=logout_headers).status_code == 204
    assert client.get("/api/v1/sessions/me").status_code == 401
    assert login(client).status_code == 201
    monkeypatch.setattr(get_settings(), "demo_clock_utc", fixed + timedelta(hours=get_settings().session_hours))
    assert client.get("/api/v1/sessions/me").status_code == 401
    client.close()


def test_commit_failure_rolls_back_before_success_response(file_database):
    _, engine = file_database
    demo.run("seed")

    def reject_commit(session):
        raise RuntimeError("injected commit failure")

    event.listen(database.SessionLocal, "before_commit", reject_commit)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = login(client)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert not response.headers.get_list("set-cookie")
        client.close()
    finally:
        event.remove(database.SessionLocal, "before_commit", reject_commit)
    with Session(engine) as session:
        assert session.scalars(select(AuthSession)).first() is None
