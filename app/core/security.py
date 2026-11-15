from datetime import datetime
from hashlib import sha256, scrypt
from hmac import compare_digest
from secrets import token_hex
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.utils import utc_now
from app.database import UowDependency
from app.models.entities import AuthSession, User

SESSION_COOKIE = "mobility_session"
CSRF_COOKIE = "mobility_csrf"
cookie_scheme = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or token_hex(16)
    hashed = scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"{salt}:{hashed.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, _, _ = stored.partition(":")
    return compare_digest(hash_password(password, salt), stored)


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        if origin not in get_settings().cors_origins:
            raise AppError("Origin is not allowed.", 403)


def session_for_request(request: Request, uow, token: str | None):
    session = uow.session.get(AuthSession, digest(token)) if token else None
    if session is None or datetime.fromisoformat(session.expires_at) <= utc_now():
        raise AppError("Authentication required.", 401)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        csrf = request.headers.get("x-csrf-token")
        if not csrf or not compare_digest(digest(csrf), session.csrf_hash):
            raise AppError("Invalid CSRF token.", 403, code="INVALID_CSRF")
    return session


def get_current_principal(
    request: Request, uow: UowDependency,
    token: Annotated[str | None, Security(cookie_scheme)],
) -> User:
    session = session_for_request(request, uow, token)
    user = uow.session.get(User, session.user_id)
    if user is None or user.status != "ACTIVE" or user.user_type not in {"STUDENT", "OPERATOR"}:
        raise AppError("Authentication required.", 401)
    return user


def require_student(user: Annotated[User, Depends(get_current_principal)]) -> User:
    if user.user_type != "STUDENT":
        raise AppError("Student role required.", 403)
    return user


def require_operator(user: Annotated[User, Depends(get_current_principal)]) -> User:
    if user.user_type != "OPERATOR":
        raise AppError("Operator role required.", 403)
    return user


Principal = Annotated[User, Depends(get_current_principal)]
Student = Annotated[User, Depends(require_student)]
Operator = Annotated[User, Depends(require_operator)]
