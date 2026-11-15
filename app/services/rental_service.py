from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppError, NotFoundError
from app.core.utils import parse_utc, utc_now, utc_now_iso, uuid_id
from app.models.entities import FleetParking, IncidentResolution, Rental, RentalActivity, RentalHoldContext, Trip, Vehicle, VehicleReservation
from app.schemas.rental import HoldCreate
from app.services.base import BaseService

from app.services.operational_guards import committed_task, open_vehicle_fault, pending_dispatch_vehicles
from app.services.parking_allocator import ParkingAllocator
from app.services.rental_battery import MINIMUM_RENTAL_BATTERY_PCT, battery_issue

ACTIVE_HOLDS = ("CONFIRMED", "PENDING", "RESERVED")
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
            trip.status in ("PLANNED",),
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

    def search_available_vehicles(self, criteria: dict, *, now=None) -> list[Vehicle]:
        """Search available public rental vehicles with sufficient battery at a given hub."""
        now = now or utc_now()
        for key in ("hub_id", "destination_hub_id"):
            if criteria.get(key):
                self._get_or_404(self.uow.hubs, criteria[key], "MobilityHub")
        due = {res.vehicle_id for res in self.uow.reservations.find_overdue_reservations(now.isoformat())}
        candidates = self.uow.vehicles.search({**criteria, "ownership_type": "PUBLIC"})
        committed = pending_dispatch_vehicles(self.uow)
        active_hubs = {hub.hub_id for hub in self.uow.hubs.find(status="ACTIVE")}
        return sorted(
            (vehicle for vehicle in candidates
             if vehicle.current_hub_id in active_hubs
             and vehicle.vehicle_id not in committed
             and not open_vehicle_fault(self.uow, vehicle.vehicle_id)
             and (vehicle.status == "AVAILABLE" or (vehicle.status == "RESERVED" and vehicle.vehicle_id in due))
             and battery_issue(self.uow, vehicle, now, float(criteria.get("min_battery", MINIMUM_RENTAL_BATTERY_PCT))) is None),
            key=lambda vehicle: vehicle.vehicle_id,
        )

    def reserve_vehicle(
        self, student_id: str, payload: HoldCreate
    ) -> VehicleReservation:
        """Reserve a public rental vehicle for a student."""
        student = self.uow.users.get_by_id(student_id)
        self._ensure(student is not None, f"Student {student_id} was not found", status_code=404)

        now = utc_now()
        self.expire_overdue_reservations(now)
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
        if vehicle.status != "AVAILABLE" or committed_task(self.uow, vehicle.vehicle_id) or open_vehicle_fault(self.uow, vehicle.vehicle_id):
            raise AppError("Vehicle is not available.", 409, code="VEHICLE_UNAVAILABLE")
        trip_id = getattr(payload, "trip_id", None)
        intended = payload.intended_hub_id
        if trip_id:
            trip = self._get_or_404(self.uow.trips, trip_id, "Trip")
            self._ensure(trip.student_id == student_id, "Trip does not belong to the student", status_code=403)
            departure = parse_utc(trip.planned_departure) if trip.planned_departure else now
            if (
                trip.status != "PLANNED" or trip.origin_hub_id != vehicle.current_hub_id
                or departure > now or (intended and intended != trip.destination_hub_id)
            ):
                raise AppError("Trip is incompatible with immediate pickup.", 409, code="TRIP_CONFLICT")
            intended = trip.destination_hub_id
        origin = self.uow.hubs.get_by_id(vehicle.current_hub_id) if vehicle.current_hub_id else None
        if origin is None or origin.status != "ACTIVE":
            raise AppError("Vehicle has no active origin hub.", 409, code="VEHICLE_UNAVAILABLE")
        if intended:
            self._get_or_404(self.uow.hubs, intended, "MobilityHub")
        self._require_battery(vehicle, now)
        if self.uow.reservations.find_active_by_vehicle(payload.vehicle_id):
            raise AppError("Vehicle is already reserved.", 409, code="VEHICLE_UNAVAILABLE")

        deadline = now + timedelta(minutes=DEFAULT_RESERVATION_GRACE_MINUTES)
        vehicle.status = "RESERVED"

        reservation = VehicleReservation(
            reservation_id=uuid_id(),
            trip_id=trip_id,
            student_id=student_id,
            vehicle_id=payload.vehicle_id,
            reserved_at=now.isoformat(),
            planned_start_at=now.isoformat(),
            pickup_deadline_at=deadline.isoformat(),
            status="CONFIRMED",
        )
        try:
            self.uow.reservations.add(reservation)
            self.uow.session.flush()
        except IntegrityError:
            raise AppError("Vehicle is already reserved or user already has an active reservation", status_code=409)

        self.uow.session.add(RentalHoldContext(
            reservation_id=reservation.reservation_id, origin_hub_id=origin.hub_id, intended_hub_id=intended,
        ))
        self._audit(student_id, "HOLD_CREATED", "hold", reservation.reservation_id)
        return reservation

    def cancel_reservation(self, reservation_id: str, student_id: str) -> VehicleReservation:
        """Cancel an active reservation and release the reserved vehicle back to the available pool."""
        reservation = self._reservation(reservation_id, student_id)
        self._check_expiry(reservation, utc_now())
        self._ensure(
            reservation.status in ("CONFIRMED", "PENDING", "RESERVED"),
            f"Cannot cancel reservation in '{reservation.status}' status",
            status_code=409,
        )

        reservation.status = "CANCELLED"
        vehicle = self.uow.vehicles.get_by_id(reservation.vehicle_id)
        if vehicle and vehicle.status == "RESERVED":
            vehicle.status = "AVAILABLE"

        self._audit(student_id, "HOLD_CANCELLED", "hold", reservation_id)
        self.uow.session.flush()
        return reservation

    def pick_up(self, reservation_id: str, student_id: str) -> Rental:
        """Pick up a reserved vehicle, completing reservation and initiating an active rental."""
        reservation = self._reservation(reservation_id, student_id)
        now = utc_now()
        self._check_expiry(reservation, now)
        if reservation.status not in ACTIVE_HOLDS:
            raise AppError("Hold cannot be picked up.", 409, code="INVALID_TRANSITION")
        vehicle = self._get_or_404(self.uow.vehicles, reservation.vehicle_id, "Vehicle")
        context = self.uow.session.get(RentalHoldContext, reservation_id)
        origin = self.uow.hubs.get_by_id(vehicle.current_hub_id) if vehicle.current_hub_id else None
        if (
            vehicle.status != "RESERVED" or origin is None or origin.status != "ACTIVE"
            or open_vehicle_fault(self.uow, vehicle.vehicle_id)
            or (context and context.origin_hub_id != origin.hub_id)
        ):
            raise AppError("Vehicle is no longer available at the pickup hub.", 409, code="VEHICLE_UNAVAILABLE")
        self._require_battery(vehicle, now)
        reservation.status = "COMPLETED"
        vehicle.status = "IN_USE"
        vehicle.current_hub_id = None
        allocation = self.uow.session.get(FleetParking, vehicle.vehicle_id)
        if allocation:
            self.uow.session.delete(allocation)

        rental = Rental(
            rental_id=uuid_id(),
            reservation_id=reservation.reservation_id,
            origin_hub_id=origin.hub_id,
            return_hub_id=None,
            picked_up_at=now.isoformat(),
            returned_at=None,
            return_condition=None,
            status="ACTIVE",
        )
        self.uow.rentals.add(rental)

        if reservation.trip_id:
            trip = self.uow.trips.get_by_id(reservation.trip_id)
            if trip and trip.status == "PLANNED":
                trip.status = "IN_PROGRESS"

        self._audit(student_id, "RENTAL_PICKED_UP", "rental", rental.rental_id)
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
            status_code=409,
        )

        hub = self._get_or_404(self.uow.hubs, return_hub_id, "MobilityHub")
        vehicle = self._get_or_404(self.uow.vehicles, reservation.vehicle_id, "Vehicle")
        now = utc_now()
        allocator = ParkingAllocator(self.uow)
        availability = allocator.for_arrival(hub, now, vehicle.vehicle_type)
        if not availability.candidate_spaces:
            alternatives = []
            for alternative in sorted(self.uow.hubs.find(status="ACTIVE"), key=lambda item: item.hub_id):
                if alternative.hub_id != return_hub_id:
                    free = allocator.for_arrival(alternative, now, vehicle.vehicle_type).bookable_now
                    if free:
                        alternatives.append({"hub_id": alternative.hub_id, "bookable_now": free, "as_of": now.isoformat()})
            raise AppError("No suitable space at this hub.", 409, code="HUB_FULL", details={
                "hub_id": return_hub_id, "physical_free_now": availability.physical_free_now,
                "bookable_now": 0, "as_of": now.isoformat(), "alternatives": alternatives,
            })

        self.uow.session.add(FleetParking(
            vehicle_id=vehicle.vehicle_id, parking_space_id=availability.candidate_spaces[0],
        ))

        rental.return_hub_id = return_hub_id
        rental.returned_at = now.isoformat()
        rental.return_condition = condition or "GOOD"
        rental.status = "COMPLETED"

        vehicle.current_hub_id = return_hub_id
        reported_fault = open_vehicle_fault(self.uow, vehicle.vehicle_id)
        damaged_return = condition in ("DAMAGED", "FAULTY", "NEEDS_MAINTENANCE")
        vehicle.status = "MAINTENANCE" if damaged_return or reported_fault else "AVAILABLE"
        if reported_fault and not damaged_return:
            metadata = self.uow.session.get(IncidentResolution, reported_fault)
            if metadata:
                metadata.set_vehicle_maintenance = 1

        if reservation.trip_id:
            trip = self.uow.trips.get_by_id(reservation.trip_id)
            if trip and trip.status == "IN_PROGRESS":
                trip.status = "COMPLETED"

        self._audit(student_id, "RENTAL_RETURNED", "rental", rental_id)
        self.uow.session.flush()
        return rental

    def expire_overdue_reservations(self, now=None) -> list[VehicleReservation]:
        now = now or utc_now()
        overdue = self.uow.reservations.find_overdue_reservations(now.isoformat())
        for res in overdue:
            self._expire(res)
        if overdue:
            self.uow.session.flush()
        return overdue

    @staticmethod
    def expires_at(reservation):
        if reservation.pickup_deadline_at:
            return parse_utc(reservation.pickup_deadline_at)
        return parse_utc(reservation.reserved_at) + timedelta(minutes=DEFAULT_RESERVATION_GRACE_MINUTES)

    def _check_expiry(self, reservation, now):
        if reservation.status in ACTIVE_HOLDS and now >= self.expires_at(reservation):
            self._expire(reservation)
        if reservation.status == "EXPIRED":
            raise AppError("Hold has expired.", 409, code="HOLD_EXPIRED")

    def _expire(self, reservation):
        reservation.status = "EXPIRED"
        vehicle = self.uow.vehicles.get_by_id(reservation.vehicle_id)
        if vehicle and vehicle.status == "RESERVED":
            vehicle.status = "AVAILABLE"
        self._audit(reservation.student_id, "HOLD_EXPIRED", "hold", reservation.reservation_id, "Deadline reached.")

    def _require_battery(self, vehicle, now):
        code = battery_issue(self.uow, vehicle, now)
        if code:
            raise AppError("Vehicle battery does not meet rental requirements.", 409, code=code)

    def _audit(self, user_id, action, resource_type, resource_id, reason=None):
        self.uow.session.add(RentalActivity(
            operation_id=uuid_id(), actor_id=user_id, at=utc_now_iso(),
            action=action, resource_type=resource_type, resource_id=resource_id,
            outcome="SUCCESS", reason=reason,
        ))

    def _reservation(self, reservation_id: str, student_id: str) -> VehicleReservation:
        """Verify existence and ownership of a vehicle reservation."""
        reservation = self.uow.reservations.get_by_id(reservation_id)
        if not reservation:
            raise NotFoundError("VehicleReservation", reservation_id)
        if reservation.student_id != student_id:
            raise AppError("Access denied: reservation belongs to another user", status_code=403)
        return reservation
