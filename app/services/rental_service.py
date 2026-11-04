from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppError, NotFoundError
from app.core.utils import utc_now_iso, uuid_id
from app.models.entities import Rental, Trip, Vehicle, VehicleReservation
from app.schemas.rental import VehicleReservationCreate
from app.services.base import BaseService

MINIMUM_RENTAL_BATTERY_PCT = 20.0
DEFAULT_RESERVATION_GRACE_MINUTES = 15


class RentalService(BaseService):
    """Business logic for rental workflows, trip planning, vehicle reservation, checkout, and return."""

    def plan_trip(self, student_id: str, trip_data: dict) -> Trip:
        """Plan a journey between two mobility hubs."""
        student = self.uow.users.get_by_id(student_id)
        self._ensure(student is not None, f"Student {student_id} was not found", status_code=404)

        origin_hub_id = trip_data.get("origin_hub_id")
        destination_hub_id = trip_data.get("destination_hub_id")
        self._ensure(bool(origin_hub_id), "origin_hub_id is required", status_code=400)
        self._ensure(bool(destination_hub_id), "destination_hub_id is required", status_code=400)

        self._get_or_404(self.uow.hubs, origin_hub_id, "MobilityHub")
        self._get_or_404(self.uow.hubs, destination_hub_id, "MobilityHub")

        trip = Trip(
            trip_id=uuid_id(),
            student_id=student_id,
            origin_hub_id=origin_hub_id,
            destination_hub_id=destination_hub_id,
            planned_departure=trip_data.get("planned_departure") or utc_now_iso(),
            status="PLANNED",
        )
        self.uow.trips.add(trip)
        self.uow.session.flush()
        return trip

    def get_trip(self, trip_id: str) -> Trip:
        """Retrieve a trip by ID."""
        return self._get_or_404(self.uow.trips, trip_id, "Trip")

    def cancel_trip(self, trip_id: str, student_id: str) -> Trip:
        """Cancel a planned trip and release any associated vehicle reservations."""
        trip = self._get_or_404(self.uow.trips, trip_id, "Trip")
        self._ensure(
            trip.student_id == student_id,
            "Access denied: trip belongs to another student",
            status_code=403,
        )
        self._ensure(
            trip.status in ("PLANNED", "IN_PROGRESS"),
            f"Trip cannot be cancelled in '{trip.status}' status",
            status_code=400,
        )

        trip.status = "CANCELLED"
        reservation = self.uow.reservations.get(trip_id=trip_id)
        if reservation and reservation.status in ("CONFIRMED", "PENDING", "RESERVED"):
            reservation.status = "CANCELLED"
            vehicle = self.uow.vehicles.get_by_id(reservation.vehicle_id)
            if vehicle and vehicle.status == "RESERVED":
                vehicle.status = "AVAILABLE"

        self.uow.session.flush()
        return trip

    def search_available_vehicles(self, criteria: dict) -> list[Vehicle]:
        """Search available public rental vehicles with sufficient battery at a given hub."""
        hub_id = criteria.get("hub_id")
        vehicle_type = criteria.get("vehicle_type")
        min_battery = float(criteria.get("min_battery", MINIMUM_RENTAL_BATTERY_PCT))
        return self.uow.vehicles.find_available(
            hub_id=hub_id,
            vehicle_type=vehicle_type,
            min_battery=min_battery,
        )

    def reserve_vehicle(
        self, student_id: str, payload: VehicleReservationCreate
    ) -> VehicleReservation:
        """Reserve a public rental vehicle for a student."""
        student = self.uow.users.get_by_id(student_id)
        self._ensure(student is not None, f"Student {student_id} was not found", status_code=404)

        active_res = self.uow.reservations.find_active_by_student(student_id)
        if active_res:
            raise AppError("User already has an active vehicle reservation", status_code=409)

        active_rental = self.uow.rentals.find_active_by_student(student_id)
        if active_rental:
            raise AppError("User currently has an active vehicle rental", status_code=409)

        vehicle = self._get_or_404(self.uow.vehicles, payload.vehicle_id, "Vehicle")
        self._ensure(
            vehicle.ownership_type == "PUBLIC",
            "Only public fleet vehicles can be reserved for rental",
            status_code=400,
        )
        self._ensure(
            vehicle.status == "AVAILABLE",
            f"Vehicle '{payload.vehicle_id}' is not available (current status: '{vehicle.status}')",
            status_code=409,
        )
        self._ensure(
            vehicle.battery_pct is None or vehicle.battery_pct >= MINIMUM_RENTAL_BATTERY_PCT,
            f"Vehicle battery level ({vehicle.battery_pct}%) is below the minimum threshold ({MINIMUM_RENTAL_BATTERY_PCT}%)",
            status_code=400,
        )

        existing_res = self.uow.reservations.find_active_by_vehicle(payload.vehicle_id)
        if existing_res:
            raise AppError(f"Vehicle '{payload.vehicle_id}' is already reserved", status_code=409)

        if payload.trip_id:
            trip = self._get_or_404(self.uow.trips, payload.trip_id, "Trip")
            self._ensure(
                trip.student_id == student_id,
                "Trip does not belong to the student",
                status_code=403,
            )

        now = datetime.now(UTC)
        start_dt = payload.planned_start_at or now
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)

        deadline_dt = payload.pickup_deadline_at or (
            start_dt + timedelta(minutes=DEFAULT_RESERVATION_GRACE_MINUTES)
        )
        if deadline_dt.tzinfo is None:
            deadline_dt = deadline_dt.replace(tzinfo=UTC)

        vehicle.status = "RESERVED"

        reservation = VehicleReservation(
            reservation_id=uuid_id(),
            trip_id=payload.trip_id,
            student_id=student_id,
            vehicle_id=payload.vehicle_id,
            reserved_at=now.isoformat(),
            planned_start_at=start_dt.isoformat(),
            pickup_deadline_at=deadline_dt.isoformat(),
            status="CONFIRMED",
        )
        try:
            self.uow.reservations.add(reservation)
            self.uow.session.flush()
        except IntegrityError:
            raise AppError("Vehicle is already reserved or user already has an active reservation", status_code=409)

        return reservation

    def cancel_reservation(self, reservation_id: str, student_id: str) -> VehicleReservation:
        """Cancel an active reservation and release the reserved vehicle back to the available pool."""
        reservation = self._reservation(reservation_id, student_id)
        self._ensure(
            reservation.status in ("CONFIRMED", "PENDING", "RESERVED"),
            f"Cannot cancel reservation in '{reservation.status}' status",
            status_code=400,
        )

        reservation.status = "CANCELLED"
        vehicle = self.uow.vehicles.get_by_id(reservation.vehicle_id)
        if vehicle and vehicle.status == "RESERVED":
            vehicle.status = "AVAILABLE"

        self.uow.session.flush()
        return reservation

    def pick_up(self, reservation_id: str, student_id: str) -> Rental:
        """Pick up a reserved vehicle, completing reservation and initiating an active rental."""
        reservation = self._reservation(reservation_id, student_id)
        self._ensure(
            reservation.status in ("CONFIRMED", "PENDING", "RESERVED"),
            f"Reservation cannot be picked up in '{reservation.status}' status",
            status_code=400,
        )

        now = datetime.now(UTC)
        if reservation.pickup_deadline_at:
            try:
                deadline = datetime.fromisoformat(reservation.pickup_deadline_at.replace("Z", "+00:00"))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=UTC)
                if now > deadline:
                    reservation.status = "EXPIRED"
                    vehicle = self.uow.vehicles.get_by_id(reservation.vehicle_id)
                    if vehicle and vehicle.status == "RESERVED":
                        vehicle.status = "AVAILABLE"
                    self.uow.session.flush()
                    raise AppError("Reservation has expired past pickup deadline", status_code=400)
            except ValueError:
                pass

        vehicle = self._get_or_404(self.uow.vehicles, reservation.vehicle_id, "Vehicle")
        self._ensure(
            vehicle.status == "RESERVED",
            f"Vehicle is in status '{vehicle.status}', expected 'RESERVED'",
            status_code=400,
        )

        reservation.status = "COMPLETED"
        vehicle.status = "IN_USE"

        rental = Rental(
            rental_id=uuid_id(),
            reservation_id=reservation.reservation_id,
            origin_hub_id=vehicle.current_hub_id or "",
            return_hub_id=None,
            picked_up_at=utc_now_iso(),
            returned_at=None,
            return_condition=None,
            status="ACTIVE",
        )
        self.uow.rentals.add(rental)

        if reservation.trip_id:
            trip = self.uow.trips.get_by_id(reservation.trip_id)
            if trip and trip.status == "PLANNED":
                trip.status = "IN_PROGRESS"

        self.uow.session.flush()
        return rental

    def return_vehicle(
        self,
        rental_id: str,
        student_id: str,
        return_hub_id: str,
        condition: str | None = None,
    ) -> Rental:
        """Return a rented vehicle at a hub, completing rental and updating vehicle status and hub location."""
        rental = self._get_or_404(self.uow.rentals, rental_id, "Rental")
        reservation = self._get_or_404(
            self.uow.reservations, rental.reservation_id, "VehicleReservation"
        )
        self._ensure(
            reservation.student_id == student_id,
            "Access denied: rental belongs to another student",
            status_code=403,
        )
        self._ensure(
            rental.status == "ACTIVE",
            f"Rental is in '{rental.status}' status, expected 'ACTIVE'",
            status_code=400,
        )

        self._get_or_404(self.uow.hubs, return_hub_id, "MobilityHub")

        rental.return_hub_id = return_hub_id
        rental.returned_at = utc_now_iso()
        rental.return_condition = condition or "GOOD"
        rental.status = "COMPLETED"

        vehicle = self._get_or_404(self.uow.vehicles, reservation.vehicle_id, "Vehicle")
        vehicle.current_hub_id = return_hub_id
        if condition in ("DAMAGED", "FAULTY", "NEEDS_MAINTENANCE"):
            vehicle.status = "MAINTENANCE"
        else:
            vehicle.status = "AVAILABLE"

        if reservation.trip_id:
            trip = self.uow.trips.get_by_id(reservation.trip_id)
            if trip and trip.status == "IN_PROGRESS":
                trip.status = "COMPLETED"

        self.uow.session.flush()
        return rental

    def expire_overdue_reservations(self) -> list[VehicleReservation]:
        """Sweep and expire any active reservations that have passed their pickup deadline (FR-09)."""
        now_iso = utc_now_iso()
        overdue = self.uow.reservations.find_overdue_reservations(now_iso)
        for res in overdue:
            res.status = "EXPIRED"
            vehicle = self.uow.vehicles.get_by_id(res.vehicle_id)
            if vehicle and vehicle.status == "RESERVED":
                vehicle.status = "AVAILABLE"
        if overdue:
            self.uow.session.flush()
        return overdue

    def _reservation(self, reservation_id: str, student_id: str) -> VehicleReservation:
        """Verify existence and ownership of a vehicle reservation."""
        reservation = self.uow.reservations.get_by_id(reservation_id)
        if not reservation:
            raise NotFoundError("VehicleReservation", reservation_id)
        if reservation.student_id != student_id:
            raise AppError("Access denied: reservation belongs to another user", status_code=403)
        return reservation
