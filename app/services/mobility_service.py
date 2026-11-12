from sqlalchemy import func, select

from app.core.exceptions import AppError
from app.core.utils import uuid_id
from app.models.entities import (
    ChargingPoint,
    ChargingSession,
    HubResource,
    MobilityHub,
    ParkingReservation,
    ParkingSpace,
    Vehicle,
)
from app.services.base import BaseService


class MobilityService(BaseService):
    def __init__(self, uow):
        super().__init__(uow)

    def search_hubs(self, criteria: dict) -> list[MobilityHub]:
        statement = select(MobilityHub)
        if not criteria:
            return list(self.uow.session.scalars(statement).all())

        for field, value in criteria.items():
            if value is None:
                continue
            if field == "name":
                statement = statement.where(MobilityHub.name.ilike(f"%{str(value)}%"))
            elif field in {"hub_id", "hub_type", "status"}:
                statement = statement.where(getattr(MobilityHub, field) == value)
            elif field in {"max_parking_capacity", "max_charging_capacity"}:
                statement = statement.where(getattr(MobilityHub, field) >= int(value))
        return list(self.uow.session.scalars(statement).all())

    def get_hub(self, hub_id: str) -> MobilityHub:
        return self._get_or_404(self.uow.hubs, hub_id, "MobilityHub")

    def create_hub(self, hub_data: dict) -> MobilityHub:
        if not hub_data:
            raise AppError("hub_data is required", status_code=400)

        name = (hub_data.get("name") or "").strip()
        self._ensure(bool(name), "name is required", status_code=400)

        hub_id = hub_data.get("hub_id") or uuid_id()
        hub = MobilityHub(
            hub_id=hub_id,
            name=name,
            hub_type=hub_data.get("hub_type"),
            latitude=hub_data.get("latitude"),
            longitude=hub_data.get("longitude"),
            max_parking_capacity=int(hub_data.get("max_parking_capacity") or 0),
            max_charging_capacity=int(hub_data.get("max_charging_capacity") or 0),
            status=hub_data.get("status") or "ACTIVE",
        )
        self.uow.hubs.add(hub)
        self.uow.session.flush()
        return hub

    def update_hub(self, hub_id: str, changes: dict) -> MobilityHub:
        hub = self._get_or_404(self.uow.hubs, hub_id, "MobilityHub")
        if not changes:
            return hub

        for field, value in changes.items():
            if value is None:
                continue
            if field in {"max_parking_capacity", "max_charging_capacity"}:
                setattr(hub, field, int(value))
            else:
                setattr(hub, field, value)
        self.uow.session.flush()
        return hub

    def search_resources(self, hub_id: str | None, resource_type: str | None) -> list[HubResource]:
        statement = select(HubResource)
        if hub_id:
            statement = statement.where(HubResource.hub_id == hub_id)
        if resource_type:
            statement = statement.where(HubResource.resource_type == resource_type)
        return list(self.uow.session.scalars(statement).all())

    def create_resource(self, hub_id: str, resource_data: dict) -> HubResource:
        self._get_or_404(self.uow.hubs, hub_id, "MobilityHub")
        if not resource_data:
            raise AppError("resource_data is required", status_code=400)

        resource_type = (resource_data.get("resource_type") or "").upper()
        self._ensure(resource_type in {"PARKING_SPACE", "CHARGING_POINT"}, "resource_type must be 'PARKING_SPACE' or 'CHARGING_POINT'", status_code=400)

        resource_id = resource_data.get("resource_id") or uuid_id()
        code = resource_data.get("code") or f"{hub_id}-{resource_type.lower()}-{resource_id[:8]}"

        resource = HubResource(
            resource_id=resource_id,
            hub_id=hub_id,
            code=code,
            resource_type=resource_type,
        )
        self.uow.resources.add(resource)
        self.uow.session.flush()

        if resource_type == "PARKING_SPACE":
            parking_space = ParkingSpace(
                resource_id=resource_id,
                supported_vehicle_type=resource_data.get("supported_vehicle_type"),
            )
            self.uow.parking_spaces.add(parking_space)
        elif resource_type == "CHARGING_POINT":
            charging_point = ChargingPoint(
                resource_id=resource_id,
                connector_type=resource_data.get("connector_type"),
                max_power_kw=resource_data.get("max_power_kw"),
            )
            self.uow.charging_points.add(charging_point)

        self.uow.session.flush()
        return resource

    def deactivate_resource(self, resource_id: str) -> HubResource:
        resource = self._get_or_404(self.uow.resources, resource_id, "HubResource")

        if resource.resource_type == "PARKING_SPACE":
            parking_space = self.uow.parking_spaces.get_by_id(resource_id)
            if parking_space is not None:
                self.uow.parking_spaces.delete(parking_space)
        elif resource.resource_type == "CHARGING_POINT":
            charging_point = self.uow.charging_points.get_by_id(resource_id)
            if charging_point is not None:
                self.uow.charging_points.delete(charging_point)

        self.uow.resources.delete(resource)
        self.uow.session.flush()
        return resource

    def search_available_vehicles(
        self,
        hub_id: str | None = None,
        vehicle_type: str | None = None,
        battery_min: float | None = None,
    ) -> list[Vehicle]:
        if battery_min is not None:
            battery_min = float(battery_min)
        return self.uow.vehicles.find_available(
            hub_id=hub_id,
            vehicle_type=vehicle_type,
            min_battery=battery_min if battery_min is not None else 0.0,
        )

    def calculate_hub_capacity(self, hub_id: str) -> dict:
        hub = self._get_or_404(self.uow.hubs, hub_id, "MobilityHub")

        total_parking = self.uow.session.scalar(
            select(func.count()).select_from(HubResource).where(
                HubResource.hub_id == hub_id,
                HubResource.resource_type == "PARKING_SPACE",
            )
        ) or 0

        active_parking = self.uow.session.scalar(
            select(func.count())
            .select_from(ParkingReservation)
            .where(
                ParkingReservation.parking_space_id.in_(
                    select(HubResource.resource_id).where(
                        HubResource.hub_id == hub_id,
                        HubResource.resource_type == "PARKING_SPACE",
                    )
                ),
                ParkingReservation.status.in_(("RESERVED", "CHECKED_IN")),
            )
        ) or 0

        total_charging = self.uow.session.scalar(
            select(func.count()).select_from(HubResource).where(
                HubResource.hub_id == hub_id,
                HubResource.resource_type == "CHARGING_POINT",
            )
        ) or 0

        active_charging = self.uow.session.scalar(
            select(func.count())
            .select_from(ChargingSession)
            .where(
                ChargingSession.charging_point_id.in_(
                    select(HubResource.resource_id).where(
                        HubResource.hub_id == hub_id,
                        HubResource.resource_type == "CHARGING_POINT",
                    )
                ),
                ChargingSession.status.in_(("SUBMITTED", "QUEUED", "CHARGING")),
            )
        ) or 0

        available_parking = max(int(total_parking) - int(active_parking), 0)
        available_charging = max(int(total_charging) - int(active_charging), 0)

        parking_util = round((active_parking / total_parking) * 100, 2) if total_parking else 0.0
        charging_util = round((active_charging / total_charging) * 100, 2) if total_charging else 0.0

        return {
            "hub_id": hub.hub_id,
            "name": hub.name,
            "max_parking_capacity": hub.max_parking_capacity,
            "max_charging_capacity": hub.max_charging_capacity,
            "total_parking_spaces": total_parking,
            "available_parking_spaces": available_parking,
            "total_charging_points": total_charging,
            "available_charging_points": available_charging,
            "parking_utilization_pct": parking_util,
            "charging_utilization_pct": charging_util,
            "status": hub.status,
        }
