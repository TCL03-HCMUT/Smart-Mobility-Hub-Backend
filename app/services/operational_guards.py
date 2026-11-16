from sqlalchemy import select

from app.models.entities import DispatchPlan, OperationalIncident, RedistributionTask

OPEN_INCIDENT = ("OPEN", "IN_PROGRESS")


def open_vehicle_fault(uow, vehicle_id):
    return uow.session.scalar(select(OperationalIncident.incident_id).where(
        OperationalIncident.vehicle_id == vehicle_id,
        OperationalIncident.status.in_(OPEN_INCIDENT),
    ).limit(1))


def pending_dispatch_vehicles(uow):
    return set(uow.session.scalars(select(RedistributionTask.vehicle_id).where(
        RedistributionTask.status == "PENDING",
    )))


def committed_task(uow, vehicle_id):
    return uow.session.scalar(select(RedistributionTask).outerjoin(DispatchPlan).where(
        RedistributionTask.vehicle_id == vehicle_id,
        (RedistributionTask.status.in_(("PENDING", "IN_TRANSIT"))) | (
            (RedistributionTask.status == "FAILED") & DispatchPlan.started_at.is_not(None)
        ),
    ).limit(1))
