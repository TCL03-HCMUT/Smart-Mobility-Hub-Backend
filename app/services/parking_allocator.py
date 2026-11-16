from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models.entities import (
    DispatchPlan, FleetParking, HubResource, OperationalIncident, ParkingReservation, ParkingSession,
    ParkingSpace, RedistributionTask, Vehicle,
)
from app.core.config import get_settings
from app.core.utils import parse_utc


@dataclass(frozen=True)
class ParkingAvailability:
    capacity: int
    physical_free_now: int
    candidate_spaces: tuple[str, ...]

    @property
    def bookable_now(self) -> int:
        return len(self.candidate_spaces)


def booking_deadline(booking):
    return min(
        parse_utc(booking.starts_at) + timedelta(minutes=get_settings().parking_no_show_minutes),
        parse_utc(booking.ends_at),
    )


class ParkingAllocator:
    """Allocate compatible physical spaces for booking and open-ended arrivals."""

    def __init__(self, uow):
        self.db = uow.session

    def for_arrival(self, hub, as_of: datetime, vehicle_type: str | None = None, *, ignore_task_id=None) -> ParkingAvailability:
        return self._availability(hub, as_of, as_of, None, vehicle_type, ignore_task_id=ignore_task_id)

    def for_booking(self, hub, now, start, end, vehicle_type, booking=None):
        return self._availability(hub, now, start, end, vehicle_type, booking)

    def _availability(self, hub, as_of, start, end, vehicle_type, booking=None, ignore_task_id=None):
        spaces = self.db.execute(
            select(ParkingSpace.resource_id, ParkingSpace.supported_vehicle_type)
            .join(HubResource)
            .where(HubResource.hub_id == hub.hub_id)
            .order_by(ParkingSpace.resource_id)
        ).all()
        ids = {space.resource_id for space in spaces}
        capacity = min(len(ids), hub.max_parking_capacity) if hub.max_parking_capacity is not None else len(ids)
        capacity = max(capacity, 0)
        if not ids:
            return ParkingAvailability(capacity, 0, ())

        sessions = self.db.execute(
            select(ParkingSession.parking_space_id, ParkingSession.vehicle_id, ParkingReservation.ends_at)
            .outerjoin(ParkingReservation, ParkingReservation.reservation_id == ParkingSession.reservation_id)
            .where(ParkingSession.parking_space_id.in_(ids), ParkingSession.checked_out_at.is_(None))
        ).all()
        occupied = {space_id: vehicle_id for space_id, vehicle_id, _ in sessions}
        future_release = {
            space_id for space_id, _, departure in sessions
            if departure and as_of < parse_utc(departure) <= start
        }
        for space_id, vehicle_id in self.db.execute(
            select(FleetParking.parking_space_id, FleetParking.vehicle_id)
            .where(FleetParking.parking_space_id.in_(ids))
        ):
            occupied.setdefault(space_id, vehicle_id)
            future_release.discard(space_id)
        present = set(self.db.scalars(select(Vehicle.vehicle_id).where(
            Vehicle.current_hub_id == hub.hub_id,
            Vehicle.status.not_in(("IN_USE", "IN_TRANSIT")),
        )))
        unassigned = len(present - set(occupied.values()))
        physical = max(capacity - len(occupied) - unassigned, 0)
        blocked = set(self.db.scalars(select(OperationalIncident.resource_id).where(
            OperationalIncident.hub_id == hub.hub_id,
            OperationalIncident.resource_id.in_(ids),
            OperationalIncident.status.in_(("OPEN", "IN_PROGRESS")),
        )))
        reservations = self.db.scalars(select(ParkingReservation).where(
            ParkingReservation.parking_space_id.in_(ids),
            ParkingReservation.status.in_(("RESERVED", "CHECKED_IN", "OVERDUE")),
        ))
        for commitment in reservations:
            if booking is not None and commitment.reservation_id == booking.reservation_id:
                continue
            if commitment.status != "RESERVED" or not (commitment.starts_at and commitment.ends_at):
                if commitment.status != "CHECKED_IN" or commitment.parking_space_id not in future_release:
                    blocked.add(commitment.parking_space_id)
                continue
            if as_of < booking_deadline(commitment) and parse_utc(commitment.ends_at) > start and (
                end is None or parse_utc(commitment.starts_at) < end
            ):
                blocked.add(commitment.parking_space_id)
        inbound = self.db.execute(select(RedistributionTask.task_id, DispatchPlan.target_space_id, RedistributionTask.status)
                                  .outerjoin(DispatchPlan).where(RedistributionTask.to_hub_id == hub.hub_id,
                                      RedistributionTask.status.in_(("PENDING", "IN_TRANSIT", "FAILED")))).all()
        unassigned_inbound = 0
        for task_id, slot, status in inbound:
            if task_id == ignore_task_id:
                continue
            if slot:
                blocked.add(slot)
            elif status != "FAILED":
                unassigned_inbound += 1
        occupied_for_interval = set(occupied) - future_release
        interval_capacity = max(capacity - len(occupied_for_interval) - unassigned, 0)
        spare = [space for space in spaces if space.resource_id not in blocked and space.resource_id not in occupied_for_interval]
        # Legacy/seed fleet without assigned spaces and inbound tasks consume capacity too.
        count = max(0, min(interval_capacity, len(spare) - unassigned) - unassigned_inbound)
        if booking is not None:
            spare.sort(key=lambda space: space.resource_id != booking.parking_space_id)
        candidates = tuple(space.resource_id for space in spare if (
            vehicle_type is None or space.supported_vehicle_type in (None, vehicle_type)
        ))[:count]
        return ParkingAvailability(capacity, physical, candidates if hub.status == "ACTIVE" else ())
