from sqlalchemy import desc, select

from app.models.entities import PrivateVehicle, PublicVehicle, SensorTelemetry, Vehicle
from app.repositories.base import BaseRepository


class VehicleRepository(BaseRepository[Vehicle]):
    model = Vehicle

    def find_by_license_plate(self, license_plate: str) -> Vehicle | None:
        """Find a vehicle by its unique license plate."""
        return self.get(license_plate=license_plate)

    def find_available(
        self,
        hub_id: str | None = None,
        vehicle_type: str | None = None,
        min_battery: float = 20.0,
    ) -> list[Vehicle]:
        """Find publicly rentable vehicles that are currently available and above the minimum battery threshold."""
        statement = select(self.model).where(
            self.model.ownership_type == "PUBLIC",
            self.model.status == "AVAILABLE",
            self.model.battery_pct >= min_battery,
        )
        if hub_id:
            statement = statement.where(self.model.current_hub_id == hub_id)
        if vehicle_type:
            statement = statement.where(self.model.vehicle_type == vehicle_type)
        return list(self.db.scalars(statement).all())

    def search(self, criteria: dict) -> list[Vehicle]:
        """Search vehicles by dynamic criteria (hub_id, vehicle_type, ownership_type, status, min_battery)."""
        statement = select(self.model)
        if "hub_id" in criteria and criteria["hub_id"]:
            statement = statement.where(self.model.current_hub_id == criteria["hub_id"])
        if "vehicle_type" in criteria and criteria["vehicle_type"]:
            statement = statement.where(self.model.vehicle_type == criteria["vehicle_type"])
        if "ownership_type" in criteria and criteria["ownership_type"]:
            statement = statement.where(self.model.ownership_type == criteria["ownership_type"])
        if "status" in criteria and criteria["status"]:
            statement = statement.where(self.model.status == criteria["status"])
        if "min_battery" in criteria and criteria["min_battery"] is not None:
            statement = statement.where(self.model.battery_pct >= float(criteria["min_battery"]))
        return list(self.db.scalars(statement).all())


class PrivateVehicleRepository(BaseRepository[PrivateVehicle]):
    model = PrivateVehicle

    def find_by_owner(self, owner_id: str) -> list[PrivateVehicle]:
        """Find all private vehicles registered to a specific owner."""
        return self.find(owner_id=owner_id)


class PublicVehicleRepository(BaseRepository[PublicVehicle]):
    model = PublicVehicle

    def find_with_schedule(self) -> list[PublicVehicle]:
        """Find public vehicles that have a scheduled maintenance date."""
        statement = select(self.model).where(self.model.maintenance_schedule.isnot(None))
        return list(self.db.scalars(statement).all())


class SensorTelemetryRepository(BaseRepository[SensorTelemetry]):
    model = SensorTelemetry

    def find_by_vehicle(self, vehicle_id: str, limit: int = 50) -> list[SensorTelemetry]:
        """Find telemetry entries for a vehicle, newest first."""
        statement = (
            select(self.model)
            .where(self.model.vehicle_id == vehicle_id)
            .order_by(desc(self.model.timestamp))
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def find_by_resource(self, resource_id: str, limit: int = 50) -> list[SensorTelemetry]:
        """Find telemetry entries for a hub resource, newest first."""
        statement = (
            select(self.model)
            .where(self.model.resource_id == resource_id)
            .order_by(desc(self.model.timestamp))
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def get_latest_for_vehicle(
        self, vehicle_id: str, metric_name: str | None = None
    ) -> SensorTelemetry | None:
        """Get the most recent telemetry entry for a vehicle."""
        statement = select(self.model).where(self.model.vehicle_id == vehicle_id)
        if metric_name:
            statement = statement.where(self.model.metric_name == metric_name)
        statement = statement.order_by(desc(self.model.timestamp)).limit(1)
        return self.db.scalar(statement)

