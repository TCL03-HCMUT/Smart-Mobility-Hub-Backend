from fastapi import APIRouter, Query, Response

from app.api.routes.mobility import hub_summary, public_vehicle_view
from app.core.exceptions import error_response
from app.core.security import Operator
from app.core.utils import utc_now
from app.database import UowDependency
from app.schemas.operations import (
    DispatchOut, IncidentCreate, IncidentOut, IncidentResolve, OperationsAlert, OperationsOut,
    RedistributionTaskCreate, SimulationCreate,
)
from app.services.charging_service import ChargingService
from app.services.operation_service import OperationService
from app.services.operational_guards import pending_dispatch_vehicles
from app.services.operational_tick import settle_due
from app.services.simulation_service import SimulationService

router = APIRouter()


@router.get("/operations", response_model=OperationsOut, operation_id="API-019")
def dashboard(response: Response, uow: UowDependency, principal: Operator):
    uow.begin_operational_read()
    response.headers["X-Operational-Revision"] = uow.operational_revision()
    now = utc_now()
    hubs = [hub_summary(uow, hub, now) for hub in sorted(uow.hubs.find(), key=lambda hub: hub.hub_id)]
    committed = pending_dispatch_vehicles(uow)
    vehicles = [
        public_vehicle_view(uow, vehicle, committed=vehicle.vehicle_id in committed)
        for vehicle in sorted(uow.vehicles.find(ownership_type="PUBLIC"), key=lambda vehicle: vehicle.vehicle_id)
    ]
    service = OperationService(uow)
    incidents, dispatches = service.list_incidents(0, 200), service.list_tasks(0, 200)
    alerts = []
    for hub in hubs:
        if hub.bookable_now == 0:
            alerts.append(OperationsAlert(hub_id=hub.hub_id, code="HUB_FULL"))
        if any(point.status == "UNAVAILABLE" for point in hub.charging_points):
            alerts.append(OperationsAlert(hub_id=hub.hub_id, code="CHARGER_UNAVAILABLE"))
    for hub_id in sorted({task.to_hub_id for task in dispatches if task.status == "FAILED" and task.started_at}):
        alerts.append(OperationsAlert(hub_id=hub_id, code="DISPATCH_INTERRUPTED"))
    return OperationsOut(
        as_of=now, hubs=hubs, vehicles=vehicles, incidents=incidents, dispatches=dispatches,
        charging=ChargingService(uow).list(None, 0, 200), alerts=alerts,
    )


@router.get("/incidents", response_model=list[IncidentOut])
def list_incidents(
    uow: UowDependency, principal: Operator,
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
):
    uow.begin_operational_read()
    return OperationService(uow).list_incidents(offset, limit)


@router.get("/incidents/{incident_id}", response_model=IncidentOut, operation_id="API-036")
def get_incident(incident_id: str, uow: UowDependency, principal: Operator):
    uow.begin_operational_read()
    service = OperationService(uow)
    return service.incident_view(service.get_incident(incident_id))


@router.post("/incidents", response_model=IncidentOut, status_code=201, operation_id="API-020")
def report_incident(payload: IncidentCreate, uow: UowDependency, principal: Operator):
    uow.lock_operational_write()
    service = OperationService(uow)
    incident = service.report_incident(principal.user_id, payload)
    uow.advance_operational_revision()
    return service.incident_view(incident)


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentOut, operation_id="API-021")
def resolve_incident(incident_id: str, payload: IncidentResolve, uow: UowDependency, principal: Operator):
    uow.lock_operational_write()
    service = OperationService(uow)
    incident = service.resolve_incident(incident_id, principal.user_id, payload.resolution_notes)
    uow.advance_operational_revision()
    return service.incident_view(incident)


@router.get("/redistribution-tasks", response_model=list[DispatchOut])
def list_redistribution_tasks(
    uow: UowDependency, principal: Operator,
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
):
    uow.begin_operational_read()
    return OperationService(uow).list_tasks(offset, limit)


@router.get("/redistribution-tasks/{task_id}", response_model=DispatchOut, operation_id="API-035")
def get_redistribution_task(task_id: str, uow: UowDependency, principal: Operator):
    uow.begin_operational_read()
    service = OperationService(uow)
    return service.dispatch_view(service.get_task(task_id))


@router.post("/redistribution-tasks", response_model=DispatchOut, status_code=201, operation_id="API-022")
def create_redistribution_task(payload: RedistributionTaskCreate, uow: UowDependency, principal: Operator):
    uow.lock_operational_write()
    settle_due(uow)
    service = OperationService(uow)
    task = service.create_distribution_task(principal.user_id, payload)
    uow.advance_operational_revision()
    return service.dispatch_view(task)


@router.post("/redistribution-tasks/{task_id}/start", response_model=DispatchOut, operation_id="API-023")
def start_redistribution_task(task_id: str, uow: UowDependency, principal: Operator):
    uow.lock_operational_write()
    settle_due(uow)
    service = OperationService(uow)
    task = service.start_task(task_id, principal.user_id)
    uow.advance_operational_revision()
    return service.dispatch_view(task)


@router.post("/redistribution-tasks/{task_id}/complete", response_model=DispatchOut, operation_id="API-024")
def complete_redistribution_task(task_id: str, uow: UowDependency, principal: Operator):
    uow.lock_operational_write()
    changed_due = settle_due(uow)
    service = OperationService(uow)
    previous_status = service.get_task(task_id).status
    task = service.complete_task(task_id, principal.user_id)
    if changed_due or previous_status != task.status:
        uow.advance_operational_revision()
    if task.status == "FAILED":
        return error_response(409, "Arrival blocked. Vehicle remains in transit.", code="HUB_FULL",
                              details={"hub_id": task.to_hub_id, "task_id": task.task_id, "status": task.status})
    return service.dispatch_view(task)


@router.post("/simulations", status_code=201)
def run_simulation(payload: SimulationCreate, uow: UowDependency, principal: Operator):
    return SimulationService(uow).run(principal.user_id, payload)
