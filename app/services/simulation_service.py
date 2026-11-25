import json

from pydantic import TypeAdapter
from sqlalchemy import literal_column, select

from app.core.config import get_settings
from app.core.exceptions import AppError, NotFoundError
from app.core.utils import parse_utc, utc_now, uuid_id
from app.models.entities import ChargingPoint, ChargingRequest, SimulationRun
from app.schemas.operations import (
    ChargerFault, ChargingDemandSurge, FleetImbalance, ParkingCapacity, RentalDemandSurge, SimulationCreate,
    SimulationRunOut,
)
from app.services.charging_service import LIVE, ChargingService
from app.services.parking_allocator import ParkingAllocator
from app.services.parking_service import ParkingService
from app.services.rental_service import RentalService
from app.services.simulation_engine import (
    MODEL_VERSION, DemandSimulationEngine, OperationalSnapshot, SimCharge, SimHub,
)

RUN_PREFIX = "what-if:v1:"


class SimulationService:
    def __init__(self, uow, engine: DemandSimulationEngine | None = None):
        self.uow = uow
        self.engine = engine or DemandSimulationEngine()

    def run(self, user_id: str, payload: SimulationCreate) -> SimulationRunOut:
        self.uow.begin_operational_read()
        snapshot = self._snapshot(payload)
        # The engine has no ORM references. End the SQLite read transaction before
        # running the model and opening the independent result write transaction.
        self.uow.session.rollback()
        result = self.engine.run(snapshot, payload)
        response = SimulationRunOut(
            run_id=uuid_id(), status="COMPLETED", scenario_type=payload.scenario_type,
            hub_id=payload.hub_id, horizon_minutes=payload.horizon_minutes,
            snapshot_revision=snapshot.revision, snapshot_at=snapshot.captured_at,
            model_version=MODEL_VERSION, metrics=result.metrics, recommendations=result.recommendations,
        )
        unmet = next((metric for metric in result.metrics if metric.name == "unmet_rental_demand"), None)
        entry = SimulationRun(
            run_id=response.run_id, started_by_user_id=user_id,
            scenario_name=f"{RUN_PREFIX}{payload.scenario_type}",
            scenario_config=json.dumps({
                "input": payload.model_dump(mode="json"),
                "snapshot": TypeAdapter(OperationalSnapshot).dump_python(snapshot, mode="json"),
            }),
            baseline_unmet_demand=unmet.baseline if unmet else None,
            projected_unmet_demand=unmet.scenario if unmet else None,
            simulation_outcome=response.model_dump_json(),
            status=response.status, started_at=snapshot.captured_at.isoformat(),
        )
        self.uow.runs.add(entry)
        return response

    def get(self, run_id: str) -> SimulationRunOut:
        entry = self.uow.runs.get_by_id(run_id)
        if not entry or not (entry.scenario_name or "").startswith(RUN_PREFIX):
            raise NotFoundError("Simulation run", run_id)
        return SimulationRunOut.model_validate_json(entry.simulation_outcome)

    def list(self, offset: int, limit: int) -> list[SimulationRunOut]:
        query = (
            select(SimulationRun).where(SimulationRun.scenario_name.startswith(RUN_PREFIX))
            .order_by(SimulationRun.started_at.desc(), SimulationRun.run_id).offset(offset).limit(limit)
        )
        return [SimulationRunOut.model_validate_json(entry.simulation_outcome) for entry in self.uow.session.scalars(query)]

    def _snapshot(self, payload) -> OperationalSnapshot:
        now = utc_now()
        revision = self.uow.operational_revision()
        hub = self.uow.hubs.get_by_id(payload.hub_id)
        if not hub:
            raise NotFoundError("MobilityHub", payload.hub_id)
        if hub.status != "ACTIVE":
            raise AppError("Choose an active hub.", 409, code="HUB_UNAVAILABLE")
        settings = get_settings()
        hubs, points, requests, charging_capacity = (), (), (), 0
        if isinstance(payload, (RentalDemandSurge, FleetImbalance, ParkingCapacity)):
            hubs = self._hub_snapshot(payload, now)
        else:
            if isinstance(payload, ChargerFault):
                resource = self.uow.resources.get_by_id(payload.charging_point_id)
                if (
                    not resource or resource.hub_id != hub.hub_id
                    or not self.uow.session.get(ChargingPoint, resource.resource_id)
                ):
                    raise NotFoundError("Charging point", payload.charging_point_id)
            points, requests = self._charging_snapshot(hub.hub_id, now)
            charging_capacity = hub.max_charging_capacity or 0
            if isinstance(payload, ChargerFault) and payload.charging_point_id not in points:
                raise AppError("Choose a charging point that is working in the snapshot.",
                               409, code="CHARGING_POINT_UNAVAILABLE")
        return OperationalSnapshot(
            revision, now, hubs, points, charging_capacity, requests,
            settings.simulation_step_minutes, settings.simulation_baseline_rental_count,
            settings.charging_duration_minutes if isinstance(payload, ChargingDemandSurge) else 0,
        )

    def _hub_snapshot(self, payload, now):
        candidates = (
            [] if isinstance(payload, ParkingCapacity)
            else RentalService(self.uow).search_available_vehicles({}, now=now)
        )
        types = sorted({vehicle.vehicle_type for vehicle in candidates}, key=lambda item: item or "")
        allocator = ParkingAllocator(self.uow)
        result = []
        for hub in sorted(self.uow.hubs.find(status="ACTIVE"), key=lambda item: item.hub_id):
            parking = allocator.for_arrival(hub, now)
            accepted = tuple(
                vehicle_type for vehicle_type in types
                if allocator.for_arrival(hub, now, vehicle_type).bookable_now
            )
            result.append(SimHub(
                hub.hub_id,
                tuple((vehicle.vehicle_id, vehicle.vehicle_type) for vehicle in candidates
                      if vehicle.current_hub_id == hub.hub_id),
                accepted,
                parking.capacity, parking.physical_free_now, parking.bookable_now,
            ))
        return tuple(result)

    def _charging_snapshot(self, hub_id, now):
        charging = ChargingService(self.uow)
        points = tuple(charging._points(hub_id))
        query = select(ChargingRequest).where(
            ChargingRequest.hub_id == hub_id, ChargingRequest.status.in_(LIVE),
        ).order_by(literal_column("CHARGING_REQUEST.rowid"))
        requests = []
        for record in self.uow.session.scalars(query):
            if not charging._parked(record):
                continue
            vehicle = self.uow.vehicles.get_by_id(record.vehicle_id)
            if vehicle.status not in ("AVAILABLE", "CHARGING"):
                continue
            booking = self.uow.parking_reservations.get_by_id(record.parking_reservation_id)
            status = ParkingService.effective_status(booking, now)
            if status != "CHECKED_IN" and record.status != "ACTIVE":
                continue
            requests.append(SimCharge(
                request_id=record.session_id, duration=record.duration_minutes,
                deadline=parse_utc(booking.ends_at), need_by=parse_utc(record.need_by) if record.need_by else None,
                battery=vehicle.battery_pct, requested_at=parse_utc(record.requested_at),
                point_id=record.charging_point_id,
                starts_at=parse_utc(record.scheduled_start_at) if record.scheduled_start_at else None,
                finishes_at=parse_utc(record.scheduled_end_at) if record.scheduled_end_at else None,
                active=record.status == "ACTIVE",
            ))
        return points, tuple(requests)
