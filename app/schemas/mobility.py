from datetime import datetime

from app.schemas.common import APIModel


class ChargingPointOut(APIModel):
    resource_id: str
    status: str


class HubOut(APIModel):
    hub_id: str
    name: str
    parking_capacity: int
    physical_free_now: int
    bookable_now: int
    charging_points: list[ChargingPointOut]
    as_of: datetime
    hub_type: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    status: str | None = None


class VehicleOut(APIModel):
    vehicle_id: str
    current_hub_id: str | None = None
    vehicle_type: str | None = None
    ownership_type: str | None = None
    battery_pct: float | None = None
    battery_observed_at: datetime | None = None
    status: str | None = None


class OwnedVehicleOut(VehicleOut):
    license_plate: str
