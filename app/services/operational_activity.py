from app.core.utils import utc_now_iso, uuid_id
from app.models.entities import RentalActivity


def record_activity(uow, actor_id, action, resource_type, resource_id, reason=None):
    uow.session.add(RentalActivity(
        operation_id=uuid_id(), at=utc_now_iso(), actor_id=actor_id,
        action=action, resource_type=resource_type, resource_id=resource_id,
        outcome="SUCCESS", reason=reason,
    ))
