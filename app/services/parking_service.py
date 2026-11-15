from datetime import UTC

from sqlalchemy import select

from app.core.exceptions import AppError, NotFoundError
from app.core.utils import parse_utc, utc_now, uuid_id
from app.models.entities import ParkingReservation, ParkingSession
from app.schemas.parking import ParkingReservationCreate, ParkingReservationOut
from app.services.operational_activity import record_activity
from app.services.parking_allocator import ParkingAllocator, booking_deadline

ACTIVE_BOOKINGS = ("RESERVED", "CHECKED_IN", "OVERDUE")


class ParkingService:
    def __init__(self, uow):
        self.uow = uow
        self.db = uow.session

    def reserve(self, user_id: str, payload: ParkingReservationCreate):
        now = utc_now()
        starts, ends = payload.starts_at.astimezone(UTC), payload.ends_at.astimezone(UTC)
        if starts < now or ends <= starts:
            raise AppError("Choose a future interval with start before end.", 422)
        private = self.uow.private_vehicles.get_by_id(payload.vehicle_id)
        if private is None or private.owner_id != user_id:
            raise NotFoundError("Vehicle", payload.vehicle_id)
        vehicle = self.uow.vehicles.get_by_id(payload.vehicle_id)
        hub = self.uow.hubs.get_by_id(payload.hub_id)
        if not hub:
            raise NotFoundError("MobilityHub", payload.hub_id)
        if vehicle.status in ("IN_USE", "IN_TRANSIT", "MAINTENANCE"):
            raise AppError("Vehicle is unavailable.", 409, code="VEHICLE_UNAVAILABLE")
        for item in self.db.scalars(select(ParkingReservation).where(
            ParkingReservation.vehicle_id == vehicle.vehicle_id,
            ParkingReservation.status.in_(ACTIVE_BOOKINGS),
        )):
            if item.status != "RESERVED" or (
                now < booking_deadline(item) and parse_utc(item.starts_at) < ends and starts < parse_utc(item.ends_at)
            ):
                raise AppError("Vehicle has a conflicting booking.", 409, code="PARKING_CONFLICT")
        result = ParkingAllocator(self.uow).for_booking(hub, now, starts, ends, vehicle.vehicle_type)
        if not result.candidate_spaces:
            raise AppError("No compatible space for this interval.", 409, code="PARKING_CONFLICT")
        booking = ParkingReservation(
            reservation_id=uuid_id(), user_id=user_id, vehicle_id=vehicle.vehicle_id,
            parking_space_id=result.candidate_spaces[0], starts_at=starts.isoformat(),
            ends_at=ends.isoformat(), status="RESERVED",
        )
        self.db.add(booking)
        self._record(booking, "PARKING_RESERVED")
        self.db.flush()
        return booking

    def cancel(self, reservation_id, user_id):
        booking = self._reservation(reservation_id, user_id)
        if self.effective_status(booking, utc_now()) != "RESERVED":
            raise AppError("Only an unexpired booking before check-in can be canceled.", 409, code="INVALID_TRANSITION")
        booking.status = "CANCELLED"
        self._record(booking, "PARKING_CANCELLED")
        return booking

    def check_in(self, reservation_id, user_id):
        booking = self._reservation(reservation_id, user_id)
        now = utc_now()
        if booking.status != "RESERVED":
            raise AppError("Booking cannot be checked in.", 409, code="INVALID_TRANSITION")
        if now < parse_utc(booking.starts_at) or now >= booking_deadline(booking):
            raise AppError("Check-in is outside the reserved window.", 409, code="CHECK_IN_WINDOW")
        vehicle = self.uow.vehicles.get_by_id(booking.vehicle_id)
        if vehicle.current_hub_id or vehicle.status != "AVAILABLE":
            raise AppError("Vehicle is unavailable for check-in.", 409, code="VEHICLE_UNAVAILABLE")
        hub = self._hub(booking)
        available = ParkingAllocator(self.uow).for_booking(
            hub, now, now, parse_utc(booking.ends_at), vehicle.vehicle_type, booking,
        )
        if booking.parking_space_id not in available.candidate_spaces:
            raise AppError("Reserved space is currently blocked.", 409, code="PARKING_CONFLICT")
        booking.status = "CHECKED_IN"
        vehicle.current_hub_id = hub.hub_id
        self.db.add(ParkingSession(
            session_id=uuid_id(), reservation_id=reservation_id, user_id=user_id,
            vehicle_id=vehicle.vehicle_id, parking_space_id=booking.parking_space_id,
            checked_in_at=now.isoformat(),
        ))
        self._record(booking, "PARKING_CHECKED_IN")
        self.db.flush()
        return booking

    def check_out(self, session_id, user_id):
        session = self.uow.parking_sessions.get_by_id(session_id)
        if not session or session.user_id != user_id:
            raise NotFoundError("Parking session", session_id)
        booking = self._reservation(session.reservation_id, user_id)
        if session.checked_out_at or booking.status not in ("CHECKED_IN", "OVERDUE"):
            raise AppError("Parking session is not active.", 409, code="INVALID_TRANSITION")
        from app.services.charging_service import ChargingService

        charging = ChargingService(self.uow)
        charging.on_checkout(booking.reservation_id)
        session.checked_out_at = utc_now().isoformat()
        booking.status = "COMPLETED"
        self.uow.vehicles.get_by_id(session.vehicle_id).current_hub_id = None
        self._record(booking, "PARKING_CHECKED_OUT")
        self.db.flush()
        return booking

    def update_due(self, now):
        changed = 0
        for booking in self.db.scalars(select(ParkingReservation).where(
            ParkingReservation.status.in_(("RESERVED", "CHECKED_IN")),
        )):
            status = self.effective_status(booking, now)
            if status != booking.status:
                booking.status = status
                self._record(booking, "PARKING_EXPIRED" if status == "NOSHOW" else "PARKING_OVERSTAYING")
                changed += 1
        if changed:
            self.db.flush()
        return changed

    @staticmethod
    def effective_status(booking, now):
        if booking.status == "RESERVED" and now >= booking_deadline(booking):
            return "NOSHOW"
        if booking.status == "CHECKED_IN" and now >= parse_utc(booking.ends_at):
            return "OVERDUE"
        return booking.status

    def view(self, booking, now=None):
        session = self.uow.parking_sessions.get(reservation_id=booking.reservation_id)
        state = self.effective_status(booking, now or utc_now())
        return ParkingReservationOut(
            reservation_id=booking.reservation_id, session_id=session.session_id if session else None,
            vehicle_id=booking.vehicle_id, hub_id=self._hub(booking).hub_id, parking_space_id=booking.parking_space_id,
            starts_at=parse_utc(booking.starts_at), ends_at=parse_utc(booking.ends_at),
            status={"NOSHOW": "EXPIRED", "OVERDUE": "OVERSTAYING"}.get(state, state),
        )

    def owned_list(self, user_id, offset, limit):
        query = (select(ParkingReservation).where(ParkingReservation.user_id == user_id)
                 .order_by(ParkingReservation.starts_at.desc(), ParkingReservation.reservation_id).offset(offset).limit(limit))
        return list(self.db.scalars(query))

    def _reservation(self, reservation_id, user_id):
        booking = self.uow.parking_reservations.get_by_id(reservation_id)
        if not booking or booking.user_id != user_id:
            raise NotFoundError("Parking reservation", reservation_id)
        return booking

    def _hub(self, booking):
        resource = self.uow.resources.get_by_id(booking.parking_space_id)
        return self.uow.hubs.get_by_id(resource.hub_id)

    def _record(self, booking, action):
        record_activity(self.uow, booking.user_id, action, "parking", booking.reservation_id)
