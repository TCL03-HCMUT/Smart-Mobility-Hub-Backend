import asyncio
import logging

from app import database
from app.core.utils import utc_now
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


def tick():
    with database.SessionLocal() as session:
        uow = UnitOfWork(session)
        uow.lock_operational_write()
        if settle_due(uow):
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
