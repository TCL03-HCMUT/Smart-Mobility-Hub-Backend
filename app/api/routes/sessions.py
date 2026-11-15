from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response, Security
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.security import (
    CSRF_COOKIE, SESSION_COOKIE, check_origin, cookie_scheme, digest, session_for_request,
    verify_password,
)
from app.core.utils import utc_now
from app.database import UowDependency
from app.models.entities import AuthSession, DemoCredential, User

router = APIRouter()


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_alias: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class SessionView(BaseModel):
    account_id: str
    display_name: str
    role: Literal["student", "operator"]
    expires_at: datetime


def session_view(user: User, session: AuthSession) -> SessionView:
    return SessionView(
        account_id=user.user_id,
        display_name=user.display_name or user.email or user.user_id,
        role=user.user_type.lower(),
        expires_at=session.expires_at,
    )


@router.post("", response_model=SessionView, status_code=201, operation_id="API-001")
def login(payload: Login, request: Request, response: Response, uow: UowDependency):
    check_origin(request)
    user = uow.users.get(email=payload.login_alias.strip().lower())
    credential = uow.session.get(DemoCredential, user.user_id) if user else None
    # Always do the password derivation, even for an unknown alias.
    stored = credential.password_hash if credential else "00" * 16 + ":" + "00" * 64
    if not verify_password(payload.password, stored) or not user or not credential or (
        user.status != "ACTIVE" or user.user_type not in {"STUDENT", "OPERATOR"}
    ):
        raise AppError("Invalid login.", 401, code="INVALID_CREDENTIALS")

    token, csrf = token_urlsafe(32), token_urlsafe(32)
    settings = get_settings()
    previous_token = request.cookies.get(SESSION_COOKIE)
    if previous_token:
        previous = uow.session.get(AuthSession, digest(previous_token))
        if previous is not None:
            uow.session.delete(previous)
    duration = timedelta(hours=settings.session_hours)
    session = AuthSession(
        token_hash=digest(token), csrf_hash=digest(csrf), user_id=user.user_id,
        expires_at=(utc_now() + duration).isoformat(),
    )
    uow.session.add(session)
    cookies = dict(max_age=int(duration.total_seconds()), secure=settings.session_cookie_secure, samesite="lax")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **cookies)
    response.set_cookie(CSRF_COOKIE, csrf, **cookies)
    response.headers["Cache-Control"] = "no-store"
    return session_view(user, session)


@router.get("/me", response_model=SessionView)
def me(request: Request, response: Response, uow: UowDependency, token: Annotated[str | None, Security(cookie_scheme)]):
    session = session_for_request(request, uow, token)
    user = uow.session.get(User, session.user_id)
    if user is None or user.status != "ACTIVE" or user.user_type not in {"STUDENT", "OPERATOR"}:
        raise AppError("Authentication required.", 401)
    response.headers["Cache-Control"] = "no-store"
    return session_view(user, session)


@router.post("/logout", status_code=204, operation_id="API-002")
def logout(request: Request, response: Response, uow: UowDependency, token: Annotated[str | None, Security(cookie_scheme)]):
    check_origin(request)
    existing = uow.session.get(AuthSession, digest(token)) if token else None
    if existing and datetime.fromisoformat(existing.expires_at) > utc_now():
        uow.session.delete(session_for_request(request, uow, token))
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
