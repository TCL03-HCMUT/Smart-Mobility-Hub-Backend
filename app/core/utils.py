from datetime import UTC, datetime
from uuid import uuid4

from app.core.config import get_settings


def uuid_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    fixed = get_settings().demo_clock_utc
    return fixed.astimezone(UTC) if fixed else datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="microseconds")


def parse_utc(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    return instant.astimezone(UTC) if instant.tzinfo else instant.replace(tzinfo=UTC)
