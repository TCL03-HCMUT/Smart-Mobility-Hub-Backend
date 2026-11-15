from datetime import datetime

from pydantic import AwareDatetime, ConfigDict

from app.schemas.common import APIModel


class ParkingReservationCreate(APIModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str
    hub_id: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class ParkingReservationOut(APIModel):
    reservation_id: str
    session_id: str | None = None
    vehicle_id: str
    hub_id: str
    parking_space_id: str
    starts_at: datetime
    ends_at: datetime
    status: str
