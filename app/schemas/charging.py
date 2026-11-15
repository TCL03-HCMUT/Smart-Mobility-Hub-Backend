from datetime import datetime

from pydantic import AwareDatetime, ConfigDict, Field

from app.schemas.common import APIModel


class ChargingSessionCreate(APIModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str
    parking_reservation_id: str
    target_battery_pct: float | None = Field(None, gt=0, le=100)
    need_by: AwareDatetime | None = None


class ChargingSessionOut(APIModel):
    session_id: str
    vehicle_id: str
    parking_reservation_id: str
    hub_id: str
    status: str
    charging_point_id: str | None
    scheduled_start_at: datetime | None
    scheduled_end_at: datetime | None
    target_battery_pct: float
    reason: str | None


class ChargingSessionModify(APIModel):
    model_config = ConfigDict(extra="forbid")

    target_battery_pct: float | None = Field(None, gt=0, le=100)
    need_by: AwareDatetime | None = None
