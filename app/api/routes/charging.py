from fastapi import APIRouter, Query

from app.core.security import Operator, Principal, Student
from app.database import UowDependency
from app.schemas.charging import ChargingSessionCreate, ChargingSessionModify, ChargingSessionOut
from app.services.charging_service import ChargingService
from app.services.operational_tick import settle_due

router = APIRouter()


def owner_scope(principal):
    return None if principal.user_type == "OPERATOR" else principal.user_id


@router.get("/sessions", response_model=list[ChargingSessionOut])
def list_sessions(
    uow: UowDependency, principal: Principal,
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
):
    uow.begin_operational_read()
    return ChargingService(uow).list(owner_scope(principal), offset, limit)


@router.get("/sessions/{session_id}", response_model=ChargingSessionOut, operation_id="API-034")
def get_session(session_id: str, uow: UowDependency, principal: Principal):
    return ChargingService(uow).get(session_id, owner_scope(principal))


@router.post("/sessions", response_model=ChargingSessionOut, status_code=201, operation_id="API-016")
def reserve_charging(payload: ChargingSessionCreate, uow: UowDependency, principal: Student):
    uow.lock_operational_write()
    settle_due(uow)
    record = ChargingService(uow).reserve(principal.user_id, payload)
    uow.advance_operational_revision()
    return record


@router.patch("/sessions/{session_id}", response_model=ChargingSessionOut)
def modify_session(session_id: str, payload: ChargingSessionModify, uow: UowDependency, principal: Student):
    uow.lock_operational_write()
    settle_due(uow)
    record = ChargingService(uow).modify(session_id, principal.user_id, payload)
    uow.advance_operational_revision()
    return record


@router.post("/sessions/{session_id}/cancel", response_model=ChargingSessionOut, operation_id="API-017")
def cancel_session(session_id: str, uow: UowDependency, principal: Principal):
    uow.lock_operational_write()
    settle_due(uow)
    record = ChargingService(uow).cancel(session_id, owner_scope(principal))
    uow.advance_operational_revision()
    return record


@router.post("/sessions/{session_id}/start", response_model=ChargingSessionOut)
def start_session(session_id: str, uow: UowDependency, principal: Operator):
    uow.lock_operational_write()
    settle_due(uow)
    record = ChargingService(uow).start(session_id, actor_id=principal.user_id)
    uow.advance_operational_revision()
    return record


@router.post("/sessions/{session_id}/stop", response_model=ChargingSessionOut, operation_id="API-018")
def stop_session(session_id: str, uow: UowDependency, principal: Principal):
    uow.lock_operational_write()
    settle_due(uow)
    service = ChargingService(uow)
    service.get(session_id, owner_scope(principal))
    record = service.stop(session_id)
    uow.advance_operational_revision()
    return record


@router.post("/sessions/{session_id}/complete", response_model=ChargingSessionOut)
def complete_session(session_id: str, uow: UowDependency, principal: Operator):
    uow.lock_operational_write()
    changed = settle_due(uow)
    record = ChargingService(uow).complete(session_id)
    if changed:
        uow.advance_operational_revision()
    return record


@router.post("/priorities/recalculate", response_model=list[ChargingSessionOut])
def recalculate_priorities(uow: UowDependency, principal: Operator):
    uow.lock_operational_write()
    if settle_due(uow):
        uow.advance_operational_revision()
    return ChargingService(uow).list(None, 0, 200)
