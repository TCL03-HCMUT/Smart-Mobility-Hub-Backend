from datetime import datetime

from pydantic import ConfigDict, Field, model_validator

from app.schemas.charging import ChargingSessionOut
from app.schemas.common import APIModel
from app.schemas.mobility import HubOut, VehicleOut


class IncidentCreate(APIModel):
    model_config = ConfigDict(extra="forbid")

    hub_id: str
    vehicle_id: str | None = None
    resource_id: str | None = None
    incident_type: str = Field(min_length=1, max_length=64)
    severity: str = Field(min_length=1, max_length=32)
    description: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def require_one_target(self):
        if bool(self.vehicle_id) == bool(self.resource_id):
            raise ValueError("Choose exactly one vehicle or resource.")
        return self


class IncidentResolve(APIModel):
    model_config = ConfigDict(extra="forbid")

    resolution_notes: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class IncidentOut(APIModel):
    incident_id: str
    hub_id: str
    vehicle_id: str | None
    resource_id: str | None
    incident_type: str
    severity: str
    status: str
    description: str | None = None
    opened_at: datetime
    resolved_at: datetime | None
    resolution_notes: str | None


class RedistributionTaskCreate(APIModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str
    from_hub_id: str
    to_hub_id: str


class DispatchOut(APIModel):
    task_id: str
    vehicle_id: str
    from_hub_id: str
    to_hub_id: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    reason: str | None


class OperationsAlert(APIModel):
    hub_id: str
    code: str


class OperationsOut(APIModel):
    as_of: datetime
    hubs: list[HubOut]
    vehicles: list[VehicleOut]
    incidents: list[IncidentOut]
    dispatches: list[DispatchOut]
    charging: list[ChargingSessionOut]
    alerts: list[OperationsAlert]


class SimulationCreate(APIModel):
    scenario_name: str
    scenario_config: dict = {}
