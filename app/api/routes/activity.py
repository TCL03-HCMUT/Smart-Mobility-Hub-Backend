from fastapi import APIRouter, Query
from sqlalchemy import literal_column, select

from app.core.security import Principal
from app.database import UowDependency
from app.models.entities import RentalActivity
from app.schemas.rental import ActivityOut

router = APIRouter()


@router.get("", response_model=list[ActivityOut])
def list_activity(uow: UowDependency, principal: Principal, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    query = select(RentalActivity)
    if principal.user_type == "STUDENT":
        query = query.where(RentalActivity.actor_id == principal.user_id)
    order = (literal_column("RENTAL_ACTIVITY.rowid").desc()
             if uow.session.bind.dialect.name == "sqlite" else RentalActivity.operation_id)
    query = query.order_by(RentalActivity.at.desc(), order).offset(offset).limit(limit)
    return list(uow.session.scalars(query))
