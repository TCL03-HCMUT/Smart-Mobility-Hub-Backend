from datetime import datetime
from typing import Annotated, Literal, Union

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


class SimulationInput(APIModel):
    model_config = ConfigDict(extra="forbid")

    hub_id: str = Field(min_length=1)
    horizon_minutes: int = Field(ge=1, le=240, strict=True)


class RentalDemandSurge(SimulationInput):
    scenario_type: Literal["RENTAL_DEMAND_SURGE"]
    demand_count: int = Field(gt=0, le=1000, strict=True)


class ChargerFault(SimulationInput):
    scenario_type: Literal["CHARGER_FAULT"]
    charging_point_id: str = Field(min_length=1)


SimulationCreate = Annotated[Union[RentalDemandSurge, ChargerFault], Field(discriminator="scenario_type")]


class SimulationMetric(APIModel):
    name: str
    unit: str
    baseline: int
    scenario: int


class SimulationAdvice(APIModel):
    type: Literal["REDISTRIBUTE_VEHICLE", "MONITOR_DEMAND", "RESTORE_CHARGER"]
    reason: str
    vehicle_id: str | None = None
    from_hub_id: str | None = None
    to_hub_id: str | None = None


class SimulationRunOut(APIModel):
    run_id: str
    status: Literal["COMPLETED"]
    scenario_type: Literal["RENTAL_DEMAND_SURGE", "CHARGER_FAULT"]
    hub_id: str
    horizon_minutes: int
    snapshot_revision: str
    snapshot_at: datetime
    model_version: str
    metrics: list[SimulationMetric]
    recommendations: list[SimulationAdvice]
    error_code: str | None = None
