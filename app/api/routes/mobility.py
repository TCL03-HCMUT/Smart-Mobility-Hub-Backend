from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from app.core.exceptions import NotFoundError
from app.core.security import Principal
from app.core.utils import utc_now
from app.database import UowDependency
from app.models.entities import ChargingPoint, ChargingRequest, ChargingSession, HubResource, OperationalIncident
from app.schemas.mobility import ChargingPointOut, HubOut, VehicleOut
from app.services.operational_guards import open_vehicle_fault, pending_dispatch_vehicles
from app.services.parking_allocator import ParkingAllocator
from app.services.rental_battery import observed_at
from app.services.rental_service import RentalService

router = APIRouter()


def hub_summary(uow, hub, now):
    capacity = ParkingAllocator(uow).for_arrival(hub, now)
    points = uow.session.scalars(
        select(ChargingPoint.resource_id).join(HubResource)
        .where(HubResource.hub_id == hub.hub_id).order_by(ChargingPoint.resource_id)
    ).all()
    failed = set(uow.session.scalars(select(OperationalIncident.resource_id).where(
        OperationalIncident.resource_id.in_(points),
        OperationalIncident.status.in_(("OPEN", "IN_PROGRESS")),
    )))
    busy = set(uow.session.scalars(select(ChargingSession.charging_point_id).where(
        ChargingSession.charging_point_id.in_(points), ChargingSession.status == "CHARGING",
    )))
    reserved = set(uow.session.scalars(select(ChargingRequest.charging_point_id).where(
        ChargingRequest.charging_point_id.in_(points), ChargingRequest.status == "SCHEDULED",
    )))
    return HubOut(
        hub_id=hub.hub_id, name=hub.name, hub_type=hub.hub_type,
        latitude=hub.latitude, longitude=hub.longitude, status=hub.status,
        parking_capacity=capacity.capacity, physical_free_now=capacity.physical_free_now,
        bookable_now=capacity.bookable_now, as_of=now,
        charging_points=[ChargingPointOut(
            resource_id=point,
            status="UNAVAILABLE" if hub.status != "ACTIVE" or point in failed else "IN_USE" if point in busy or point in reserved else "AVAILABLE",
        ) for point in points],
    )


@router.get("/hubs", response_model=list[HubOut], operation_id="API-003")
def list_hubs(
    response: Response, uow: UowDependency, principal: Principal,
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
):
    uow.begin_operational_read()
    response.headers["X-Operational-Revision"] = uow.operational_revision()
    now = utc_now()
    hubs = sorted(uow.hubs.find(), key=lambda hub: hub.hub_id)[offset:offset + limit]
    return [hub_summary(uow, hub, now) for hub in hubs]


@router.get("/hubs/{hub_id}", response_model=HubOut)
def get_hub(hub_id: str, response: Response, uow: UowDependency, principal: Principal):
    uow.begin_operational_read()
    hub = uow.hubs.get_by_id(hub_id)
    if not hub:
        raise NotFoundError("MobilityHub", hub_id)
    response.headers["X-Operational-Revision"] = uow.operational_revision()
    return hub_summary(uow, hub, utc_now())


@router.get("/vehicles", response_model=list[VehicleOut], operation_id="API-004")
def list_vehicles(
    uow: UowDependency, principal: Principal, hub_id: str | None = None,
    vehicle_type: str | None = None, destination_hub_id: str | None = None,
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
):
    uow.begin_operational_read()
    criteria = {"hub_id": hub_id, "vehicle_type": vehicle_type, "destination_hub_id": destination_hub_id}
    if principal.user_type == "STUDENT":
        items = RentalService(uow).search_available_vehicles(criteria)
    else:
        items = sorted(uow.vehicles.search({**criteria, "ownership_type": "PUBLIC"}), key=lambda item: item.vehicle_id)
    committed = pending_dispatch_vehicles(uow) if principal.user_type == "OPERATOR" else set()
    return [public_vehicle_view(
        uow, vehicle, available=principal.user_type == "STUDENT", committed=vehicle.vehicle_id in committed,
    ) for vehicle in items[offset:offset + limit]]


def public_vehicle_view(uow, vehicle, *, available=False, committed=False):
    status = vehicle.status
    if available:
        status = "AVAILABLE"
    elif committed:
        status = "COMMITTED"
    elif status == "AVAILABLE" and open_vehicle_fault(uow, vehicle.vehicle_id):
        status = "MAINTENANCE"
    return VehicleOut.model_validate(vehicle).model_copy(update={
        "battery_observed_at": observed_at(uow, vehicle),
        "status": status,
    })
