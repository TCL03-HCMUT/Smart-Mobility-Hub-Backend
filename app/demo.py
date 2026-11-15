"""Initialize and seed the local SQLite demo database."""
import argparse

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.core.utils import utc_now_iso, uuid_id
from app.core.security import hash_password
from app.database import Base, engine
from app.models.entities import (
    ChargingPoint, DemoCredential, DemoDataset, HubResource, MobilityHub, ParkingSpace,
    FleetParking, PrivateVehicle, PublicVehicle, SensorTelemetry, User, Vehicle,
)

DEMO_DATASET = "gogreen-demo-v1"
DEMO_PASSWORD = "gogreen-demo"
DEMO_ACCOUNTS = (
    ("student-anh", "anh@demo.example", "Nguyễn Minh Anh", "STUDENT"),
    ("student-bao", "bao@demo.example", "Trần Gia Bảo", "STUDENT"),
    ("operator-nam", "nam@demo.example", "Lê Hoàng Nam", "OPERATOR"),
)
DEMO_HUBS = (
    ("north", "Dormitory Hub"), ("library", "Central Library Hub"),
    ("science", "University Campus Hub"), ("metro", "Metro Interchange Hub"),
)


def seed(session: Session) -> bool:
    if session.get(DemoDataset, DEMO_DATASET):
        return False
    for table in Base.metadata.sorted_tables:
        if session.execute(select(1).select_from(table).limit(1)).first() is not None:
            raise ValueError("Use an empty database for demo seed.")
    for user_id, email, name, role in DEMO_ACCOUNTS:
        session.add(User(
            user_id=user_id, email=email, sso_subject=email, display_name=name,
            user_type=role, status="ACTIVE",
        ))
        session.flush()
        session.add(DemoCredential(user_id=user_id, password_hash=hash_password(DEMO_PASSWORD)))
    observed = utc_now_iso()
    for hub_id, label in DEMO_HUBS:
        session.add(MobilityHub(
            hub_id=hub_id, name=label, hub_type="CAMPUS", status="ACTIVE",
            max_parking_capacity=4, max_charging_capacity=1,
        ))
        session.flush()
        spaces = [
            HubResource(resource_id=f"{hub_id}-p{i}", hub_id=hub_id, code=f"P{i}", resource_type="PARKING_SPACE")
            for i in range(1, 5)
        ]
        charger_id = f"{hub_id}-c1"
        session.add_all(spaces + [HubResource(
            resource_id=charger_id, hub_id=hub_id, code="C1", resource_type="CHARGING_POINT",
        )])
        session.flush()
        session.add_all(ParkingSpace(resource_id=space.resource_id) for space in spaces)
        session.add(ChargingPoint(resource_id=charger_id, connector_type="DEMO", max_power_kw=3.6))
        for i, vehicle_type in enumerate(("e-bike", "e-motorcycle"), start=1):
            vehicle_id = f"{hub_id}-ev{i}"
            session.add(Vehicle(
                vehicle_id=vehicle_id, license_plate=f"DEMO-{hub_id.upper()}-{i}",
                current_hub_id=hub_id, vehicle_type=vehicle_type, ownership_type="PUBLIC",
                battery_pct=90 if i == 1 else 65, status="AVAILABLE",
            ))
            session.flush()
            session.add(PublicVehicle(vehicle_id=vehicle_id))
            session.add(FleetParking(vehicle_id=vehicle_id, parking_space_id=f"{hub_id}-p{i}"))
            session.add(SensorTelemetry(
                sensor_id=uuid_id(), vehicle_id=vehicle_id, metric_name="battery_pct",
                metric_value=90 if i == 1 else 65, timestamp=observed, is_simulated=1,
            ))
    for owner, *_ in DEMO_ACCOUNTS[:2]:
        vehicle_id = f"{owner}-private"
        session.add(Vehicle(
            vehicle_id=vehicle_id, license_plate=vehicle_id, vehicle_type="e-bike",
            ownership_type="PRIVATE", battery_pct=40, status="AVAILABLE",
        ))
        session.flush()
        session.add(PrivateVehicle(vehicle_id=vehicle_id, owner_id=owner))
    session.add(DemoDataset(dataset_id=DEMO_DATASET))
    return True


def run(command: str):
    if engine.dialect.name != "sqlite" or not engine.url.database or engine.url.database == ":memory:":
        raise ValueError("Demo commands require a file-backed SQLite DATABASE_URL.")
    with engine.begin() as connection:
        # Explicit BEGIN keeps SQLite DDL and seed data in one transaction.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        if command == "reset":
            if not inspect(connection).has_table(DemoDataset.__tablename__):
                raise ValueError("Reset is restricted to a database marked by demo seed.")
            with Session(connection) as session:
                if not session.get(DemoDataset, DEMO_DATASET):
                    raise ValueError("Reset is restricted to a database marked by demo seed.")
            Base.metadata.drop_all(connection)
        Base.metadata.create_all(connection)
        if command == "init":
            return "SQLite schema ready."
        with Session(connection) as session:
            created = seed(session)
            session.flush()
        return "Demo data seeded." if created else "Demo data already present."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "seed", "reset"))
    parser.add_argument("--yes", action="store_true", help="Confirm reset of the configured demo database.")
    args = parser.parse_args()
    if args.command == "reset" and not args.yes:
        parser.error("Stop the server and pass --yes to reset the demo database.")
    try:
        print(run(args.command))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
