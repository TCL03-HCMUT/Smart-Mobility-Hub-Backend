import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base, get_uow
from app.main import app
from app.repositories.unit_of_work import UnitOfWork
from app.models.entities import MobilityHub, User


@pytest.fixture
def db_session():
    """Create a fresh in-memory SQLite database session for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def uow(db_session):
    """Provide a UnitOfWork bound to the test database session."""
    return UnitOfWork(db_session)


@pytest.fixture
def client(db_session):
    """Provide a FastAPI TestClient bound to the test database."""
    def override_get_uow():
        test_uow = UnitOfWork(db_session)
        try:
            yield test_uow
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_uow] = override_get_uow
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def seed_data(uow):
    """Seed sample users and mobility hubs for tests."""
    student = User(
        user_id="student-1",
        sso_subject="student1@vnu.edu.vn",
        email="student1@vnu.edu.vn",
        display_name="Nguyen Van A",
        user_type="STUDENT",
        status="ACTIVE",
    )
    student2 = User(
        user_id="student-2",
        sso_subject="student2@vnu.edu.vn",
        email="student2@vnu.edu.vn",
        display_name="Tran Thi B",
        user_type="STUDENT",
        status="ACTIVE",
    )
    hub_metro = MobilityHub(
        hub_id="hub-metro",
        name="Metro Central Hub",
        hub_type="CENTRAL",
        latitude="10.880",
        longitude="106.805",
        max_parking_capacity=50,
        max_charging_capacity=20,
        status="ACTIVE",
    )
    hub_dorm = MobilityHub(
        hub_id="hub-dorm",
        name="Dormitory Zone A Hub",
        hub_type="CAMPUS",
        latitude="10.875",
        longitude="106.800",
        max_parking_capacity=30,
        max_charging_capacity=10,
        status="ACTIVE",
    )
    uow.users.add(student)
    uow.users.add(student2)
    uow.hubs.add(hub_metro)
    uow.hubs.add(hub_dorm)
    uow.commit()
    return {
        "student": student,
        "student2": student2,
        "hub_metro": hub_metro,
        "hub_dorm": hub_dorm,
    }

