from fastapi import APIRouter, Query

from app.core.exceptions import AppError, NotFoundError, error_response
from app.core.security import Student
from app.core.utils import parse_utc, utc_now
from app.database import UowDependency
from app.models.entities import Rental, RentalHoldContext, Trip, VehicleReservation
from app.schemas.rental import HoldCreate, RentalOut, RentalReturn, VehicleReservationOut
from app.services.rental_service import ACTIVE_HOLDS, RentalService

router = APIRouter()


def hold_view(uow, hold, now):
    context = uow.session.get(RentalHoldContext, hold.reservation_id)
    trip = uow.session.get(Trip, hold.trip_id) if hold.trip_id else None
    rental = uow.rentals.find_by_reservation(hold.reservation_id) if not context and not trip else None
    vehicle = uow.vehicles.get_by_id(hold.vehicle_id)
    expires_at = RentalService.expires_at(hold)
    status = "EXPIRED" if hold.status in ACTIVE_HOLDS and now >= expires_at else hold.status
    return VehicleReservationOut(
        reservation_id=hold.reservation_id, vehicle_id=hold.vehicle_id,
        origin_hub_id=(context or trip or rental).origin_hub_id if context or trip or rental else vehicle.current_hub_id,
        intended_hub_id=context.intended_hub_id if context else trip.destination_hub_id if trip else None,
        reserved_at=parse_utc(hold.reserved_at), expires_at=expires_at, status=status,
    )


def rental_view(rental, hold):
    return RentalOut(
        rental_id=rental.rental_id, reservation_id=hold.reservation_id, vehicle_id=hold.vehicle_id,
        origin_hub_id=rental.origin_hub_id, return_hub_id=rental.return_hub_id,
        picked_up_at=parse_utc(rental.picked_up_at),
        returned_at=parse_utc(rental.returned_at) if rental.returned_at else None, status=rental.status,
    )


def owned_hold(uow, reservation_id, student_id):
    record = uow.reservations.get_by_id(reservation_id)
    if record is None or record.student_id != student_id:
        raise NotFoundError("VehicleReservation", reservation_id)
    return record


def owned_rental(uow, rental_id, student_id):
    rental = uow.rentals.get_by_id(rental_id)
    hold = uow.reservations.get_by_id(rental.reservation_id) if rental else None
    if hold is None or hold.student_id != student_id:
        raise NotFoundError("Rental", rental_id)
    return rental, hold


@router.get("/reservations", response_model=list[VehicleReservationOut])
def list_reservations(uow: UowDependency, principal: Student, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    now = utc_now()
    return [hold_view(uow, hold, now) for hold in uow.reservations.find_by_student(principal.user_id)[offset:offset + limit]]


@router.get("/reservations/{reservation_id}", response_model=VehicleReservationOut, operation_id="API-031")
def get_reservation(reservation_id: str, uow: UowDependency, principal: Student):
    return hold_view(uow, owned_hold(uow, reservation_id, principal.user_id), utc_now())


@router.get("/rentals", response_model=list[RentalOut])
def list_rentals(uow: UowDependency, principal: Student, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    rentals = uow.rentals.find_by_student(principal.user_id)[offset:offset + limit]
    return [rental_view(rental, uow.reservations.get_by_id(rental.reservation_id)) for rental in rentals]


@router.get("/rentals/{rental_id}", response_model=RentalOut, operation_id="API-032")
def get_rental(rental_id: str, uow: UowDependency, principal: Student):
    return rental_view(*owned_rental(uow, rental_id, principal.user_id))


@router.post("/reservations", response_model=VehicleReservationOut, status_code=201, operation_id="API-005")
def create_reservation(payload: HoldCreate, uow: UowDependency, principal: Student):
    uow.lock_operational_write()
    hold = RentalService(uow).reserve_vehicle(principal.user_id, payload)
    uow.session.flush()
    uow.advance_operational_revision()
    return hold_view(uow, hold, utc_now())


def change_hold(uow, reservation_id, student_id, command):
    uow.lock_operational_write()
    hold = owned_hold(uow, reservation_id, student_id)
    was_active = hold.status in ACTIVE_HOLDS
    try:
        record = command(reservation_id, student_id)
    except AppError as exc:
        if exc.code != "HOLD_EXPIRED":
            raise
        if was_active:
            uow.advance_operational_revision()
        # Expiry is an operational change even though pickup/cancel was refused.
        # The dependency commits before the 409 is sent.
        return error_response(exc.status_code, exc.message, code=exc.code)
    uow.advance_operational_revision()
    return record


@router.post("/reservations/{reservation_id}/cancel", response_model=VehicleReservationOut, operation_id="API-006")
def cancel_reservation(reservation_id: str, uow: UowDependency, principal: Student):
    result = change_hold(uow, reservation_id, principal.user_id, RentalService(uow).cancel_reservation)
    return hold_view(uow, result, utc_now()) if isinstance(result, VehicleReservation) else result


@router.post("/reservations/{reservation_id}/pickup", response_model=RentalOut, operation_id="API-007")
def pick_up_vehicle(reservation_id: str, uow: UowDependency, principal: Student):
    result = change_hold(uow, reservation_id, principal.user_id, RentalService(uow).pick_up)
    return rental_view(result, uow.reservations.get_by_id(reservation_id)) if isinstance(result, Rental) else result


@router.post("/rentals/{rental_id}/return", response_model=RentalOut, operation_id="API-008")
def return_vehicle(rental_id: str, payload: RentalReturn, uow: UowDependency, principal: Student):
    uow.lock_operational_write()
    _, hold = owned_rental(uow, rental_id, principal.user_id)
    rental = RentalService(uow).return_vehicle(rental_id, principal.user_id, payload.return_hub_id, payload.return_condition)
    uow.advance_operational_revision()
    return rental_view(rental, hold)
