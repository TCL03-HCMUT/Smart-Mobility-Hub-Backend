from sqlalchemy import select

from app.core.exceptions import AppError, NotFoundError
from app.core.utils import utc_now, utc_now_iso, uuid_id
from app.models.entities import (
    ChargingSession, DispatchPlan, FleetParking, IncidentResolution, OperationalIncident,
    RedistributionTask, RentalHoldContext,
)
from app.schemas.operations import DispatchOut, IncidentOut
from app.services.charging_service import ChargingService
from app.services.operational_activity import record_activity
from app.services.operational_guards import OPEN_INCIDENT, committed_task, open_vehicle_fault
from app.services.parking_allocator import ParkingAllocator


class OperationService:
    def __init__(self, uow):
        self.uow = uow
        self.db = uow.session

    def report_incident(self, reporter_id, payload):
        hub = self._hub(payload.hub_id)
        vehicle = None
        task = None
        if payload.vehicle_id:
            vehicle = self.uow.vehicles.get_by_id(payload.vehicle_id)
            if vehicle is None:
                raise NotFoundError("Vehicle", payload.vehicle_id)
            task = committed_task(self.uow, vehicle.vehicle_id)
            incident_hubs = {vehicle.current_hub_id}
            if task and task.status in ("IN_TRANSIT", "FAILED"):
                incident_hubs.update((task.from_hub_id, task.to_hub_id))
            if vehicle.status == "IN_USE":
                trip = self.uow.rentals.find_active_by_vehicle(vehicle.vehicle_id)
                if trip:
                    incident_hubs.add(trip.origin_hub_id)
                    context = self.db.get(RentalHoldContext, trip.reservation_id)
                    if context and context.intended_hub_id:
                        incident_hubs.add(context.intended_hub_id)
            if hub.hub_id not in incident_hubs:
                raise AppError("Vehicle is not at this hub or on its dispatch route.", 409, code="INCIDENT_TARGET_MISMATCH")
            target = OperationalIncident.vehicle_id == vehicle.vehicle_id
        else:
            resource = self.uow.resources.get_by_id(payload.resource_id)
            if resource is None or resource.hub_id != hub.hub_id:
                raise NotFoundError("Hub resource", payload.resource_id)
            target = OperationalIncident.resource_id == resource.resource_id
        existing = self.db.scalar(select(OperationalIncident.incident_id).where(
            target, OperationalIncident.status.in_(OPEN_INCIDENT),
        ).limit(1))
        if existing:
            raise AppError("An incident is already open for this resource.", 409, code="INCIDENT_ALREADY_OPEN")

        record = OperationalIncident(
            incident_id=uuid_id(), hub_id=hub.hub_id, vehicle_id=payload.vehicle_id,
            resource_id=payload.resource_id, reported_by_user_id=reporter_id,
            incident_type=payload.incident_type.upper(), severity=payload.severity.upper(),
            description=payload.description, status="OPEN", opened_at=utc_now_iso(),
        )
        # Preserve active rental/hold/transit states. Their commands also inspect incidents.
        changed_vehicle_state = bool(vehicle and vehicle.status in ("AVAILABLE", "CHARGING"))
        if changed_vehicle_state:
            vehicle.status = "MAINTENANCE"
        self.db.add(record)
        self.db.flush()
        self.db.add(IncidentResolution(
            incident_id=record.incident_id, set_vehicle_maintenance=int(changed_vehicle_state),
        ))
        if task:
            self._interrupt_task(task, reporter_id, f"Vehicle incident {record.incident_id}.")
        if payload.resource_id:
            affected = self.db.scalars(select(RedistributionTask).join(DispatchPlan).where(
                DispatchPlan.target_space_id == payload.resource_id,
                RedistributionTask.status.in_(("PENDING", "IN_TRANSIT")),
            )).all()
            for dispatch in affected:
                self._interrupt_task(dispatch, reporter_id, f"Destination space incident {record.incident_id}.")
        self.db.flush()
        ChargingService(self.uow).advance(utc_now())
        record_activity(self.uow, reporter_id, "INCIDENT_REPORTED", "incident", record.incident_id, record.incident_type)
        return record

    def resolve_incident(self, incident_id, resolver_id, notes):
        incident = self.get_incident(incident_id)
        if incident.status not in OPEN_INCIDENT:
            raise AppError("Incident is already closed.", 409, code="INVALID_TRANSITION")
        metadata = self.db.get(IncidentResolution, incident_id)
        if metadata is None:
            metadata = IncidentResolution(incident_id=incident_id, set_vehicle_maintenance=0)
            self.db.add(metadata)
        incident.status = "RESOLVED"
        incident.closed_at = utc_now_iso()
        incident.resolved_by_user_id = resolver_id
        metadata.resolution_notes = notes
        if incident.vehicle_id and metadata.set_vehicle_maintenance:
            vehicle = self.uow.vehicles.get_by_id(incident.vehicle_id)
            self.db.flush()
            if vehicle.status == "MAINTENANCE" and not open_vehicle_fault(self.uow, vehicle.vehicle_id):
                vehicle.status = "AVAILABLE"
        self.db.flush()
        ChargingService(self.uow).advance(utc_now())
        record_activity(self.uow, resolver_id, "INCIDENT_RESOLVED", "incident", incident_id, notes)
        return incident

    def create_distribution_task(self, creator_id, payload):
        source, target = self._hub(payload.from_hub_id), self._hub(payload.to_hub_id)
        if source.hub_id == target.hub_id:
            raise AppError("Choose two different hubs.", 422)
        vehicle = self.uow.vehicles.get_by_id(payload.vehicle_id)
        if vehicle is None:
            raise NotFoundError("Vehicle", payload.vehicle_id)
        self._require_dispatchable(vehicle, source)
        if committed_task(self.uow, vehicle.vehicle_id):
            raise AppError("Vehicle already has a dispatch.", 409, code="DISPATCH_CONFLICT")
        slot = self._target_space(target, vehicle)
        task = RedistributionTask(
            task_id=uuid_id(), vehicle_id=vehicle.vehicle_id, from_hub_id=source.hub_id,
            to_hub_id=target.hub_id, created_by_user_id=creator_id, priority=0, status="PENDING",
        )
        self.db.add(task)
        self.db.flush()
        self.db.add(DispatchPlan(task_id=task.task_id, target_space_id=slot, created_at=utc_now_iso()))
        record_activity(self.uow, creator_id, "DISPATCH_PLANNED", "dispatch", task.task_id)
        self.db.flush()
        return task

    def start_task(self, task_id, actor_id):
        task = self.get_task(task_id)
        if task.status != "PENDING":
            raise AppError("Only planned dispatches can start.", 409, code="INVALID_TRANSITION")
        plan = self._plan(task)
        vehicle = self.uow.vehicles.get_by_id(task.vehicle_id)
        self._require_dispatchable(vehicle, self._hub(task.from_hub_id))
        plan.target_space_id = self._target_space(self._hub(task.to_hub_id), vehicle, task, plan)
        plan.started_at = utc_now_iso()
        task.status = "IN_TRANSIT"
        vehicle.status = "IN_TRANSIT"
        vehicle.current_hub_id = None
        occupancy = self.db.get(FleetParking, vehicle.vehicle_id)
        if occupancy:
            self.db.delete(occupancy)
        record_activity(self.uow, actor_id, "DISPATCH_STARTED", "dispatch", task_id)
        return task

    def complete_task(self, task_id, actor_id):
        task = self.get_task(task_id)
        plan = self._plan(task)
        if task.status not in ("IN_TRANSIT", "FAILED") or not plan.started_at:
            raise AppError("Only a departed dispatch can arrive.", 409, code="INVALID_TRANSITION")
        vehicle = self.uow.vehicles.get_by_id(task.vehicle_id)
        if vehicle.status != "IN_TRANSIT" or vehicle.current_hub_id or open_vehicle_fault(self.uow, vehicle.vehicle_id):
            raise AppError("Resolve the vehicle incident before confirming arrival.", 409, code="VEHICLE_UNAVAILABLE")
        target = self._hub(task.to_hub_id)
        try:
            slot = self._target_space(target, vehicle, task, plan)
        except AppError as exc:
            if exc.code != "HUB_FULL":
                raise
            self._interrupt_task(task, actor_id, "Arrival blocked. Vehicle remains in transit.")
            return task
        recovered = task.status == "FAILED"
        self.db.add(FleetParking(vehicle_id=vehicle.vehicle_id, parking_space_id=slot))
        vehicle.current_hub_id = target.hub_id
        vehicle.status = "AVAILABLE"
        plan.target_space_id = None
        plan.completed_at = utc_now_iso()
        plan.reason = "Recovered and arrived at the destination." if recovered else None
        task.status = "COMPLETED"
        record_activity(self.uow, actor_id, "DISPATCH_COMPLETED", "dispatch", task_id, plan.reason)
        return task

    def _interrupt_task(self, task, actor_id, reason):
        if task.status == "FAILED":
            return
        plan = self._plan(task)
        if not plan.started_at:
            plan.target_space_id = None
        task.status = "FAILED"
        plan.reason = reason
        record_activity(self.uow, actor_id, "DISPATCH_FAILED", "dispatch", task.task_id, reason)

    def get_task(self, task_id):
        task = self.uow.tasks.get_by_id(task_id)
        if task is None:
            raise NotFoundError("Dispatch", task_id)
        return task

    def get_incident(self, incident_id):
        incident = self.uow.incidents.get_by_id(incident_id)
        if incident is None:
            raise NotFoundError("Incident", incident_id)
        return incident

    def incident_view(self, incident):
        metadata = self.db.get(IncidentResolution, incident.incident_id)
        return IncidentOut(
            incident_id=incident.incident_id, hub_id=incident.hub_id, vehicle_id=incident.vehicle_id,
            resource_id=incident.resource_id, incident_type=incident.incident_type, severity=incident.severity,
            status=incident.status, description=incident.description, opened_at=incident.opened_at, resolved_at=incident.closed_at,
            resolution_notes=metadata.resolution_notes if metadata else None,
        )

    def dispatch_view(self, task):
        plan = self.db.get(DispatchPlan, task.task_id)
        return DispatchOut(
            task_id=task.task_id, from_hub_id=task.from_hub_id, to_hub_id=task.to_hub_id,
            vehicle_id=task.vehicle_id, status="PLANNED" if task.status == "PENDING" else task.status,
            started_at=plan.started_at if plan else None, completed_at=plan.completed_at if plan else None,
            reason=plan.reason if plan else None,
        )

    def list_tasks(self, offset, limit):
        query = (select(RedistributionTask).outerjoin(DispatchPlan)
                 .order_by(DispatchPlan.created_at.desc(), RedistributionTask.task_id).offset(offset).limit(limit))
        return [self.dispatch_view(task) for task in self.db.scalars(query)]

    def list_incidents(self, offset, limit):
        query = (select(OperationalIncident)
                 .order_by(OperationalIncident.opened_at.desc(), OperationalIncident.incident_id).offset(offset).limit(limit))
        return [self.incident_view(incident) for incident in self.db.scalars(query)]

    def _hub(self, hub_id):
        hub = self.uow.hubs.get_by_id(hub_id)
        if hub is None:
            raise NotFoundError("MobilityHub", hub_id)
        return hub

    def _plan(self, task):
        plan = self.db.get(DispatchPlan, task.task_id)
        if plan is None:
            raise AppError("Legacy dispatch has no recorded allocation.", 409, code="DISPATCH_CONFLICT")
        return plan

    def _require_dispatchable(self, vehicle, source):
        if (
            source.status != "ACTIVE" or vehicle.ownership_type != "PUBLIC"
            or vehicle.status != "AVAILABLE" or vehicle.current_hub_id != source.hub_id
            or self.uow.reservations.find_active_by_vehicle(vehicle.vehicle_id)
            or self.uow.rentals.find_active_by_vehicle(vehicle.vehicle_id)
            or open_vehicle_fault(self.uow, vehicle.vehicle_id)
            or self.db.scalar(select(ChargingSession.session_id).where(
                ChargingSession.vehicle_id == vehicle.vehicle_id,
                ChargingSession.status.in_(("SUBMITTED", "QUEUED", "CHARGING")),
            ).limit(1))
        ):
            raise AppError("Vehicle is unavailable for dispatch.", 409, code="VEHICLE_UNAVAILABLE")

    def _target_space(self, target, vehicle, task=None, plan=None):
        spaces = ParkingAllocator(self.uow).for_arrival(
            target, utc_now(), vehicle.vehicle_type, ignore_task_id=task.task_id if task else None,
        ).candidate_spaces
        if not spaces:
            raise AppError("Destination has no suitable space.", 409, code="HUB_FULL",
                           details={"hub_id": target.hub_id})
        return plan.target_space_id if plan and plan.target_space_id in spaces else spaces[0]
