from fastapi import APIRouter, Query
from sqlalchemy import and_, literal_column, or_, select

from app.core.security import Principal
from app.database import UowDependency
from app.models.entities import ChargingRequest, RentalActivity
from app.schemas.rental import ActivityOut

router = APIRouter()


@router.get("", response_model=list[ActivityOut])
def list_activity(uow: UowDependency, principal: Principal, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    query = select(RentalActivity)
    if principal.user_type == "STUDENT":
        owns_charging_request = select(ChargingRequest.session_id).where(
            ChargingRequest.session_id == RentalActivity.resource_id,
            ChargingRequest.user_id == principal.user_id,
        ).exists()
        query = query.where(or_(
            RentalActivity.actor_id == principal.user_id,
            and_(RentalActivity.resource_type == "charging", owns_charging_request),
        ))
    order = (literal_column("RENTAL_ACTIVITY.rowid").desc()
             if uow.session.bind.dialect.name == "sqlite" else RentalActivity.operation_id)
    query = query.order_by(RentalActivity.at.desc(), order).offset(offset).limit(limit)
    return list(uow.session.scalars(query))
