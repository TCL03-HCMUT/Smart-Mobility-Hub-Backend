from datetime import datetime, timedelta

from app.core.config import get_settings
from app.core.utils import parse_utc

MINIMUM_RENTAL_BATTERY_PCT = 30.0


def observed_at(uow, vehicle) -> datetime | None:
    sample = uow.telemetry.get_latest_for_vehicle(vehicle.vehicle_id, "battery_pct")
    if sample is None or sample.metric_value is None:
        return None
    return parse_utc(sample.timestamp)


def battery_issue(uow, vehicle, now: datetime, minimum: float = MINIMUM_RENTAL_BATTERY_PCT) -> str | None:
    measured = observed_at(uow, vehicle)
    if vehicle.battery_pct is None or measured is None:
        return "BATTERY_UNKNOWN"
    freshness = timedelta(minutes=get_settings().battery_fresh_minutes)
    if not now - freshness <= measured <= now:
        return "BATTERY_STALE"
    if vehicle.battery_pct < max(minimum, MINIMUM_RENTAL_BATTERY_PCT):
        return "BATTERY_LOW"
    return None
