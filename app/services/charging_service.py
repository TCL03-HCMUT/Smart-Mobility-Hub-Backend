from datetime import UTC, datetime, timedelta

from sqlalchemy import literal_column, select

from app.core.config import get_settings
from app.core.exceptions import AppError, NotFoundError
from app.core.utils import parse_utc, utc_now, uuid_id
from app.models.entities import (
    ChargingPoint, ChargingRequest, ChargingSession, HubResource, OperationalIncident, SensorTelemetry,
)
from app.services.operational_activity import record_activity
from app.services.operational_guards import OPEN_INCIDENT, open_vehicle_fault
from app.services.parking_service import ParkingService

WAITING = ("PENDING", "SCHEDULED")
LIVE = (*WAITING, "ACTIVE")


class ChargingService:
    def __init__(self, uow):
        self.uow = uow
        self.db = uow.session

    def reserve(self, user_id, payload):
        parking = ParkingService(self.uow)
        booking = parking._reservation(payload.parking_reservation_id, user_id)
        vehicle = self.uow.vehicles.get_by_id(booking.vehicle_id)
        now = utc_now()
        if payload.vehicle_id != booking.vehicle_id:
            raise AppError("Vehicle must match its parking reservation.", 409, code="PARKING_CONFLICT")
        if parking.effective_status(booking, now) not in ("RESERVED", "CHECKED_IN"):
            raise AppError("An eligible parking reservation is required.", 409, code="PARKING_CONFLICT")
        if vehicle.status != "AVAILABLE" or open_vehicle_fault(self.uow, vehicle.vehicle_id):
            raise AppError("Vehicle is unavailable.", 409, code="VEHICLE_UNAVAILABLE")
        if self.db.scalar(select(ChargingRequest.session_id).where(
            ChargingRequest.vehicle_id == vehicle.vehicle_id, ChargingRequest.status.in_(LIVE),
        ).limit(1)):
            raise AppError("Vehicle already has a charging request.", 409, code="CHARGING_CONFLICT")
        settings = get_settings()
        duration = settings.charging_duration_minutes
        if max(now, parse_utc(booking.starts_at)) + timedelta(minutes=duration) > parse_utc(booking.ends_at):
            raise AppError("Charging does not fit within this parking reservation.", 409, code="CHARGING_WINDOW")
        record = ChargingRequest(
            session_id=uuid_id(), parking_reservation_id=booking.reservation_id, user_id=user_id,
            vehicle_id=vehicle.vehicle_id, hub_id=parking._hub(booking).hub_id, status="PENDING",
            requested_at=now.isoformat(), need_by=payload.need_by.astimezone(UTC).isoformat() if payload.need_by else None,
            target_battery_pct=payload.target_battery_pct or settings.charging_target_pct,
            duration_minutes=duration, reason="Waiting for check-in or a compatible charging point.",
        )
        self.db.add(record)
        self._record(record, "CHARGE_REQUESTED")
        self.db.flush()
        self.assign_priorities(now)
        return record

    def modify(self, session_id, user_id, payload):
        record = self.get(session_id, user_id)
        if record.status != "PENDING":
            raise AppError("Only a pending request can be edited.", 409, code="INVALID_TRANSITION")
        changes = payload.model_fields_set
        if "target_battery_pct" in changes:
            record.target_battery_pct = payload.target_battery_pct or get_settings().charging_target_pct
        if "need_by" in changes:
            record.need_by = payload.need_by.astimezone(UTC).isoformat() if payload.need_by else None
        self._record(record, "CHARGE_UPDATED")
        self.db.flush()
        self.assign_priorities()
        return record

    def cancel(self, session_id, user_id=None):
        record = self.get(session_id, user_id)
        if record.status not in WAITING:
            raise AppError("Only a pending or scheduled request can be canceled.", 409, code="INVALID_TRANSITION")
        self._finish(record, "CANCELLED", "User canceled the request.")
        self.assign_priorities()
        return record

    def start(self, session_id):
        record = self.get(session_id)
        now = utc_now()
        if record.status != "SCHEDULED":
            raise AppError("Charging is not scheduled.", 409, code="INVALID_TRANSITION")
        scheduled_end = parse_utc(record.scheduled_end_at)
        if not parse_utc(record.scheduled_start_at) <= now < scheduled_end:
            raise AppError("Charging start is outside the scheduled window.", 409, code="CHARGING_WINDOW")
        vehicle = self.uow.vehicles.get_by_id(record.vehicle_id)
        if not self._parked(record) or vehicle.status != "AVAILABLE":
            raise AppError("The vehicle must be checked in and available.", 409, code="VEHICLE_UNAVAILABLE")
        if record.charging_point_id not in self._points(record.hub_id):
            raise AppError("Charging point is unavailable.", 409, code="CHARGING_POINT_UNAVAILABLE")
        ends_at = now + timedelta(minutes=record.duration_minutes)
        booking = self.uow.parking_reservations.get_by_id(record.parking_reservation_id)
        if ends_at > parse_utc(booking.ends_at):
            raise AppError("Charging no longer fits in the parking window.", 409, code="CHARGING_WINDOW")
        record.scheduled_start_at = now.isoformat()
        record.scheduled_end_at = ends_at.isoformat()
        session = self.db.get(ChargingSession, record.session_id)
        session.scheduled_start_time = now.isoformat()
        session.actual_start_time = now.isoformat()
        session.status = "CHARGING"
        vehicle.status = "CHARGING"
        record.status = "ACTIVE"
        record.reason = "Vehicle checked in and charging."
        self._record(record, "CHARGE_STARTED")
        return record

    def stop(self, session_id, interrupted=False):
        record = self.get(session_id)
        if record.status != "ACTIVE":
            raise AppError("Only active charging can be stopped.", 409, code="INVALID_TRANSITION")
        if interrupted:
            self._interrupt(record, utc_now(), "Charging point interrupted.")
        else:
            self._finish(record, "STOPPED", "User stopped charging.")
        self.assign_priorities()
        return record

    def complete(self, session_id):
        record = self.get(session_id)
        if record.status == "COMPLETED":
            return record
        if record.status != "ACTIVE" or utc_now() < parse_utc(record.scheduled_end_at):
            raise AppError("The charging session has not reached its end.", 409, code="INVALID_TRANSITION")
        self.advance(utc_now())
        return record

    def advance(self, now):
        """Progress persisted schedules and failures against the server clock."""
        changed = 0
        for record in list(self.db.scalars(select(ChargingRequest).where(ChargingRequest.status.in_(LIVE)))):
            booking = self.uow.parking_reservations.get_by_id(record.parking_reservation_id)
            vehicle = self.uow.vehicles.get_by_id(record.vehicle_id)
            end = parse_utc(booking.ends_at)
            if record.status == "ACTIVE" and (
                not self._parked(record) or vehicle.status != "CHARGING"
                or open_vehicle_fault(self.uow, record.vehicle_id)
                or record.charging_point_id not in self._points(record.hub_id)
            ):
                self._interrupt(record, now, "Charging interrupted by unavailable vehicle, parking, or charging point.")
            elif record.status == "ACTIVE" and now >= parse_utc(record.scheduled_end_at):
                self._complete(record, vehicle)
            elif booking.status not in ("RESERVED", "CHECKED_IN") or now >= end:
                self._finish(record, "CANCELLED", "Parking reservation is no longer eligible.")
            elif record.status == "SCHEDULED" and (
                record.charging_point_id not in self._points(record.hub_id)
                or now >= parse_utc(record.scheduled_end_at)
            ):
                self._interrupt(record, now, "Charging point unavailable or start window elapsed.")
            else:
                continue
            changed += 1
        self.db.flush()
        return changed + len(self.assign_priorities(now))

    def assign_priorities(self, now=None):
        now = now or utc_now()
        self.db.flush()
        query = select(ChargingRequest).where(ChargingRequest.status == "PENDING")
        # The demo clock may be frozen. SQLite row order records which request arrived first.
        query = query.order_by(literal_column("CHARGING_REQUEST.rowid"))
        pending = list(self.db.scalars(query))
        infinity = datetime.max.replace(tzinfo=UTC)

        def priority(record):
            vehicle = self.uow.vehicles.get_by_id(record.vehicle_id)
            return (
                parse_utc(record.need_by) if record.need_by else infinity,
                vehicle.battery_pct if vehicle.battery_pct is not None else float("inf"),
                parse_utc(record.requested_at),
            )

        busy = set(self.db.scalars(select(ChargingRequest.charging_point_id).where(
            ChargingRequest.status.in_(("ACTIVE", "SCHEDULED")),
        )))
        # Also protect an active legacy assignment if one exists in a previously used file.
        busy.update(self.db.scalars(select(ChargingSession.charging_point_id).where(
            ChargingSession.status == "CHARGING",
        )))
        scheduled = []
        for record in sorted(pending, key=priority):
            booking = self.uow.parking_reservations.get_by_id(record.parking_reservation_id)
            vehicle = self.uow.vehicles.get_by_id(record.vehicle_id)
            end = now + timedelta(minutes=record.duration_minutes)
            if booking.status != "CHECKED_IN" or not self._parked(record) or vehicle.status != "AVAILABLE":
                continue
            if end > parse_utc(booking.ends_at):
                self._finish(record, "CANCELLED", "Remaining parking interval is too short.")
                scheduled.append(record)
                continue
            available_points = self._points(record.hub_id)
            hub = self.uow.hubs.get_by_id(record.hub_id)
            if not available_points or len(busy.intersection(available_points)) >= hub.max_charging_capacity:
                continue
            point = next((item for item in available_points if item not in busy), None)
            if point is None:
                continue
            busy.add(point)
            session = self.db.get(ChargingSession, record.session_id)
            if session is None:
                session = ChargingSession(
                    session_id=record.session_id, user_id=record.user_id, vehicle_id=record.vehicle_id,
                )
                self.db.add(session)
            session.charging_point_id = point
            session.target_battery_pct = record.target_battery_pct
            session.target_duration_minutes = record.duration_minutes
            session.scheduled_start_time = now.isoformat()
            session.actual_start_time = None
            session.status = "QUEUED"
            record.status = "SCHEDULED"
            record.charging_point_id = point
            record.scheduled_start_at = now.isoformat()
            record.scheduled_end_at = end.isoformat()
            need_by = record.need_by or "unspecified"
            soc = f"{vehicle.battery_pct:g}%" if vehicle.battery_pct is not None else "unknown"
            record.reason = f"Scheduled by need-by ({need_by}), battery ({soc}), then request time."
            self._record(record, "CHARGE_SCHEDULED")
            scheduled.append(record)
        self.db.flush()
        return scheduled

    def on_checkout(self, reservation_id):
        for record in self.db.scalars(select(ChargingRequest).where(
            ChargingRequest.parking_reservation_id == reservation_id, ChargingRequest.status.in_(LIVE),
        )):
            self._finish(record, "STOPPED" if record.status == "ACTIVE" else "CANCELLED", "Vehicle checked out.")

    def get(self, session_id, owner_id=None):
        record = self.db.get(ChargingRequest, session_id)
        if record is None or (owner_id is not None and record.user_id != owner_id):
            raise NotFoundError("Charging request", session_id)
        return record

    def list(self, owner_id, offset, limit):
        query = select(ChargingRequest)
        if owner_id is not None:
            query = query.where(ChargingRequest.user_id == owner_id)
        return list(self.db.scalars(query.order_by(
            ChargingRequest.requested_at.desc(), ChargingRequest.session_id,
        ).offset(offset).limit(limit)))

    def _parked(self, record):
        parking = self.uow.parking_sessions.get(reservation_id=record.parking_reservation_id)
        vehicle = self.uow.vehicles.get_by_id(record.vehicle_id)
        if not parking or parking.checked_out_at is not None or vehicle.current_hub_id != record.hub_id:
            return False
        return not self.db.scalar(select(OperationalIncident.incident_id).where(
            OperationalIncident.resource_id == parking.parking_space_id,
            OperationalIncident.status.in_(OPEN_INCIDENT),
        ).limit(1))

    def _points(self, hub_id):
        hub = self.uow.hubs.get_by_id(hub_id)
        if hub.status != "ACTIVE" or not hub.max_charging_capacity:
            return []
        faulty = select(OperationalIncident.resource_id).where(
            OperationalIncident.hub_id == hub_id,
            OperationalIncident.resource_id.is_not(None),
            OperationalIncident.status.in_(("OPEN", "IN_PROGRESS")),
        )
        return list(self.db.scalars(select(ChargingPoint.resource_id).join(HubResource).where(
            HubResource.hub_id == hub_id, ChargingPoint.resource_id.not_in(faulty),
        ).order_by(ChargingPoint.resource_id)))

    def _complete(self, record, vehicle):
        vehicle.battery_pct = max(vehicle.battery_pct or 0, record.target_battery_pct)
        self.db.add(SensorTelemetry(
            sensor_id=uuid_id(), vehicle_id=vehicle.vehicle_id, metric_name="battery_pct",
            metric_value=vehicle.battery_pct, timestamp=utc_now().isoformat(), is_simulated=1,
        ))
        self._finish(record, "COMPLETED", "Demo charge target reached at the end of the session.")

    def _interrupt(self, record, now, reason):
        booking = self.uow.parking_reservations.get_by_id(record.parking_reservation_id)
        self._finish(record, "INTERRUPTED", reason)
        if booking.status == "CHECKED_IN" and self._parked(record) and (
            now + timedelta(minutes=record.duration_minutes) <= parse_utc(booking.ends_at)
        ):
            record.status = "PENDING"
            record.charging_point_id = None
            record.scheduled_start_at = None
            record.scheduled_end_at = None
            record.reason = f"{reason} Returned to the queue."
            self._record(record, "CHARGE_REQUEUED")

    def _finish(self, record, state, reason):
        session = self.db.get(ChargingSession, record.session_id)
        if session is not None:
            session.status = "INTERRUPTED" if state == "STOPPED" else state
        vehicle = self.uow.vehicles.get_by_id(record.vehicle_id)
        if record.status == "ACTIVE" and vehicle.status == "CHARGING":
            vehicle.status = "AVAILABLE"
        record.status = state
        record.reason = reason
        self._record(record, f"CHARGE_{state}")

    def _record(self, record, action):
        record_activity(self.uow, record.user_id, action, "charging", record.session_id, record.reason)
