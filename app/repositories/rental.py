from sqlalchemy import desc, select

from app.models.entities import Rental, Trip, VehicleReservation
from app.repositories.base import BaseRepository


class RentalRepository(BaseRepository[Rental]):
    model = Rental

    def find_active_by_student(self, student_id: str) -> Rental | None:
        """Find an ongoing active rental for a student through their vehicle reservation."""
        statement = (
            select(self.model)
            .join(VehicleReservation, self.model.reservation_id == VehicleReservation.reservation_id)
            .where(
                VehicleReservation.student_id == student_id,
                self.model.status == "ACTIVE",
            )
        )
        return self.db.scalar(statement)

    def find_active_by_vehicle(self, vehicle_id: str) -> Rental | None:
        """Find an active rental currently using the specified vehicle."""
        statement = (
            select(self.model)
            .join(VehicleReservation, self.model.reservation_id == VehicleReservation.reservation_id)
            .where(
                VehicleReservation.vehicle_id == vehicle_id,
                self.model.status == "ACTIVE",
            )
        )
        return self.db.scalar(statement)

    def find_by_reservation(self, reservation_id: str) -> Rental | None:
        """Find the rental record created for a given reservation."""
        return self.get(reservation_id=reservation_id)


class VehicleReservationRepository(BaseRepository[VehicleReservation]):
    model = VehicleReservation

    ACTIVE_STATUSES = ("CONFIRMED", "PENDING", "RESERVED")

    def find_active_by_student(self, student_id: str) -> VehicleReservation | None:
        """Find any ongoing active reservation for a student to prevent duplicate reservations."""
        statement = select(self.model).where(
            self.model.student_id == student_id,
            self.model.status.in_(self.ACTIVE_STATUSES),
        )
        return self.db.scalar(statement)

    def find_active_by_vehicle(self, vehicle_id: str) -> VehicleReservation | None:
        """Find any ongoing active reservation for a vehicle to prevent double-booking."""
        statement = select(self.model).where(
            self.model.vehicle_id == vehicle_id,
            self.model.status.in_(self.ACTIVE_STATUSES),
        )
        return self.db.scalar(statement)

    def find_by_student(self, student_id: str) -> list[VehicleReservation]:
        """List all reservations made by a student, newest first."""
        statement = (
            select(self.model)
            .where(self.model.student_id == student_id)
            .order_by(desc(self.model.reserved_at))
        )
        return list(self.db.scalars(statement).all())

    def find_overdue_reservations(self, current_time_iso: str) -> list[VehicleReservation]:
        """Find active reservations that have passed their pickup deadline (FR-09)."""
        statement = select(self.model).where(
            self.model.status.in_(self.ACTIVE_STATUSES),
            self.model.pickup_deadline_at.isnot(None),
            self.model.pickup_deadline_at < current_time_iso,
        )
        return list(self.db.scalars(statement).all())


class TripRepository(BaseRepository[Trip]):
    model = Trip

    def find_by_student(self, student_id: str) -> list[Trip]:
        """List all trips planned by a student."""
        statement = (
            select(self.model)
            .where(self.model.student_id == student_id)
            .order_by(desc(self.model.planned_departure))
        )
        return list(self.db.scalars(statement).all())

    def find_active_by_student(self, student_id: str) -> Trip | None:
        """Find an active or planned trip for a student."""
        statement = select(self.model).where(
            self.model.student_id == student_id,
            self.model.status.in_(("PLANNED", "IN_PROGRESS")),
        )
        return self.db.scalar(statement)
