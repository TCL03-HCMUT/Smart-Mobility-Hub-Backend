from datetime import datetime

from pydantic import ConfigDict

from app.schemas.common import APIModel


class HoldCreate(APIModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str
    intended_hub_id: str | None = None


class VehicleReservationCreate(HoldCreate):
    """Internal compatibility for callers with an existing Trip."""

    trip_id: str | None = None


class VehicleReservationOut(APIModel):
    reservation_id: str
    vehicle_id: str
    origin_hub_id: str | None
    intended_hub_id: str | None
    reserved_at: datetime
    expires_at: datetime
    status: str


class RentalReturn(APIModel):
    model_config = ConfigDict(extra="forbid")

    return_hub_id: str
    return_condition: str | None = None


class RentalOut(APIModel):
    rental_id: str
    reservation_id: str
    vehicle_id: str
    origin_hub_id: str
    return_hub_id: str | None
    picked_up_at: datetime
    returned_at: datetime | None
    status: str


class ActivityOut(APIModel):
    operation_id: str
    at: datetime
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    reason: str | None = None
