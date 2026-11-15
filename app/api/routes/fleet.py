from fastapi import APIRouter, Query, status
from sqlalchemy import select
from app.core.exceptions import AppError
from app.core.security import Operator, Student
from app.database import UowDependency
from app.models.entities import PrivateVehicle, Vehicle
from app.schemas.mobility import OwnedVehicleOut
from app.schemas.fleet import PrivateVehicleCreate, TelemetryCreate, VehicleStatusUpdate
from app.services.fleet_service import FleetService
from app.services.operational_guards import committed_task, open_vehicle_fault

router = APIRouter()


@router.post("/vehicles/private", response_model=OwnedVehicleOut, status_code=status.HTTP_201_CREATED, operation_id="API-009")
def register_private_vehicle(
    payload: PrivateVehicleCreate,
    uow: UowDependency,
    principal: Student,
):
    uow.lock_operational_write()
    vehicle = FleetService(uow).register_private_vehicle(principal.user_id, payload)
    uow.advance_operational_revision()
    return vehicle


@router.patch("/vehicles/{vehicle_id}/status")
def update_vehicle_status(
    vehicle_id: str,
    payload: VehicleStatusUpdate,
    uow: UowDependency,
    principal: Operator,
):
    uow.lock_operational_write()
    service = FleetService(uow)
    vehicle = service.get_vehicle(vehicle_id)
    if committed_task(uow, vehicle_id) or open_vehicle_fault(uow, vehicle_id):
        raise AppError("Use dispatch or incident operations for this vehicle.", 409, code="VEHICLE_UNAVAILABLE")
    if vehicle.ownership_type == "PUBLIC" and (
        payload.status in ("RESERVED", "IN_USE", "IN_TRANSIT")
        or vehicle.status in ("RESERVED", "IN_USE", "IN_TRANSIT")
        or (payload.current_hub_id is not None and payload.current_hub_id != vehicle.current_hub_id)
    ):
        raise AppError("Use the fleet rental or dispatch workflow for this transition.", 409, code="VEHICLE_UNAVAILABLE")
    before = (vehicle.status, vehicle.current_hub_id, vehicle.battery_pct)
    vehicle = service.update_status(vehicle_id, payload)
    if before != (vehicle.status, vehicle.current_hub_id, vehicle.battery_pct) or payload.battery_pct is not None:
        uow.advance_operational_revision()
    return vehicle


@router.post("/telemetry", status_code=status.HTTP_201_CREATED)
def record_telemetry(payload: TelemetryCreate, uow: UowDependency, principal: Operator):
    uow.lock_operational_write()
    result = FleetService(uow).record_telemetry(payload)
    uow.advance_operational_revision()
    return result


@router.get("/vehicles/private", response_model=list[OwnedVehicleOut])
def list_owned_vehicles(uow: UowDependency, principal: Student, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    query = (select(Vehicle).join(PrivateVehicle)
             .where(PrivateVehicle.owner_id == principal.user_id)
             .order_by(Vehicle.vehicle_id).offset(offset).limit(limit))
    return list(uow.session.scalars(query))
