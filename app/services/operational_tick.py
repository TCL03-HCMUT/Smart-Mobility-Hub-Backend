import asyncio
import logging
from datetime import timedelta

from app import database
from app.core.config import get_settings
from app.core.utils import parse_utc, utc_now
from app.demo import DEMO_DATASET, DEMO_HUBS
from app.models.entities import DemoDataset
from app.schemas.fleet import TelemetryCreate
from app.services.fleet_service import FleetService
from app.repositories.unit_of_work import UnitOfWork
from app.services.charging_service import ChargingService
from app.services.parking_service import ParkingService
from app.services.rental_service import RentalService

logger = logging.getLogger(__name__)


def settle_due(uow):
    """Process overdue records within the caller's SQLite write transaction."""
    now = utc_now()
    return (
        ParkingService(uow).update_due(now)
        + ChargingService(uow).advance(now)
        + len(RentalService(uow).expire_overdue_reservations(now))
    )


def refresh_demo_telemetry(uow):
    """Publish simulated readings only for seeded demo vehicles in wall-clock mode."""
    settings = get_settings()
    seconds = settings.demo_telemetry_seconds
    if not seconds or settings.demo_clock_utc is not None:
        return 0
    if not uow.session.get(DemoDataset, DEMO_DATASET):
        return 0
    now = utc_now()
    service = FleetService(uow)
    changed = 0
    for hub_id, _ in DEMO_HUBS:
        for index in (1, 2):
            vehicle_id = f"{hub_id}-ev{index}"
            vehicle = uow.vehicles.get_by_id(vehicle_id)
            sample = uow.telemetry.get_latest_for_vehicle(vehicle_id, "battery_pct")
            if (vehicle is None or vehicle.battery_pct is None or sample is None
                    or sample.metric_value is None or sample.is_simulated != 1):
                continue
            if now - parse_utc(sample.timestamp) < timedelta(seconds=seconds):
                continue
            service.record_telemetry(TelemetryCreate(
                vehicle_id=vehicle_id, metric_name="battery_pct",
                metric_value=vehicle.battery_pct, is_simulated=True,
            ))
            changed += 1
    return changed


def tick():
    with database.SessionLocal() as session:
        uow = UnitOfWork(session)
        uow.lock_operational_write()
        changed = settle_due(uow)
        changed += refresh_demo_telemetry(uow)
        if changed:
            uow.advance_operational_revision()
        session.commit()


async def run_operational_clock(stop: asyncio.Event, seconds: int):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            try:
                await asyncio.to_thread(tick)
            except Exception:
                logger.exception("Failed to process operational deadlines")
