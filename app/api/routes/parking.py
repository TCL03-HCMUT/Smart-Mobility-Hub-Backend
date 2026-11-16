from fastapi import APIRouter, Query

from app.core.security import Student
from app.core.utils import utc_now
from app.database import UowDependency
from app.schemas.parking import ParkingReservationCreate, ParkingReservationOut
from app.services.charging_service import ChargingService
from app.services.operational_tick import settle_due
from app.services.parking_service import ParkingService

router = APIRouter()


@router.get("/reservations", response_model=list[ParkingReservationOut])
def list_reservations(
    uow: UowDependency, principal: Student,
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
):
    uow.begin_operational_read()
    service = ParkingService(uow)
    return [service.view(booking) for booking in service.owned_list(principal.user_id, offset, limit)]


@router.get("/reservations/{reservation_id}", response_model=ParkingReservationOut, operation_id="API-033")
def get_reservation(reservation_id: str, uow: UowDependency, principal: Student):
    uow.begin_operational_read()
    service = ParkingService(uow)
    return service.view(service._reservation(reservation_id, principal.user_id))


@router.post("/reservations", response_model=ParkingReservationOut, status_code=201, operation_id="API-010")
def reserve_parking(payload: ParkingReservationCreate, uow: UowDependency, principal: Student):
    uow.lock_operational_write()
    settle_due(uow)
    service = ParkingService(uow)
    record = service.reserve(principal.user_id, payload)
    uow.advance_operational_revision()
    return service.view(record)


@router.post("/reservations/{reservation_id}/cancel", response_model=ParkingReservationOut, operation_id="API-011")
def cancel_reservation(reservation_id: str, uow: UowDependency, principal: Student):
    uow.lock_operational_write()
    settle_due(uow)
    service = ParkingService(uow)
    record = service.cancel(reservation_id, principal.user_id)
    uow.session.flush()
    ChargingService(uow).advance(utc_now())
    uow.advance_operational_revision()
    return service.view(record)


@router.post("/reservations/{reservation_id}/check-in", response_model=ParkingReservationOut, operation_id="API-012")
def check_in(reservation_id: str, uow: UowDependency, principal: Student):
    uow.lock_operational_write()
    settle_due(uow)
    service = ParkingService(uow)
    record = service.check_in(reservation_id, principal.user_id)
    ChargingService(uow).assign_priorities()
    uow.advance_operational_revision()
    return service.view(record)


@router.post("/sessions/{session_id}/check-out", response_model=ParkingReservationOut, operation_id="API-013")
def check_out(session_id: str, uow: UowDependency, principal: Student):
    uow.lock_operational_write()
    settle_due(uow)
    service = ParkingService(uow)
    record = service.check_out(session_id, principal.user_id)
    ChargingService(uow).assign_priorities()
    uow.advance_operational_revision()
    return service.view(record)
