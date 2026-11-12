from sqlalchemy import select

from app.core.exceptions import AppError, NotFoundError
from app.core.utils import uuid_id
from app.models.entities import Role, StudentProfile, User, UserRole
from app.services.base import BaseService


class IdentityService(BaseService):
    def __init__(self, uow):
        super().__init__(uow)

    def authenticate_sso(self, sso_subject: str) -> User:
        user = self.uow.users.get(sso_subject=sso_subject)
        if user is None:
            raise NotFoundError("User", sso_subject)
        return user

    def provision_user(self, identity_data: dict) -> User:
        if not identity_data:
            raise AppError("identity_data is required", status_code=400)

        sso_subject = identity_data.get("sso_subject") or identity_data.get("ssoSubject")
        self._ensure(bool(sso_subject), "sso_subject is required", status_code=400)

        if self.uow.users.get(sso_subject=sso_subject):
            raise AppError(f"User with SSO subject '{sso_subject}' already exists", status_code=409)

        email = identity_data.get("email")
        if email and self.uow.users.get(email=email):
            raise AppError(f"User with email '{email}' already exists", status_code=409)

        user_type = (identity_data.get("user_type") or "STUDENT").upper()
        if user_type not in {"STUDENT", "OPERATOR"}:
            raise AppError("user_type must be either 'STUDENT' or 'OPERATOR'", status_code=400)

        user = User(
            user_id=uuid_id(),
            sso_subject=sso_subject,
            email=email,
            display_name=identity_data.get("display_name") or identity_data.get("name"),
            user_type=user_type,
            status=identity_data.get("status") or "ACTIVE",
        )
        self.uow.users.add(user)
        self.uow.session.flush()
        return user

    def get_user(self, user_id: str) -> User:
        return self._get_or_404(self.uow.users, user_id, "User")

    def update_user(self, user_id: str, changes: dict) -> User:
        user = self._get_or_404(self.uow.users, user_id, "User")
        if not changes:
            return user

        for field, value in changes.items():
            if value is None:
                continue
            if field == "sso_subject":
                existing = self.uow.users.get(sso_subject=value)
                if existing and existing.user_id != user_id:
                    raise AppError(f"User with SSO subject '{value}' already exists", status_code=409)
                user.sso_subject = value
            elif field == "email":
                existing = self.uow.users.get(email=value)
                if existing and existing.user_id != user_id:
                    raise AppError(f"User with email '{value}' already exists", status_code=409)
                user.email = value
            elif field == "user_type":
                user_type = str(value).upper()
                if user_type not in {"STUDENT", "OPERATOR"}:
                    raise AppError("user_type must be either 'STUDENT' or 'OPERATOR'", status_code=400)
                user.user_type = user_type
            elif field in {"display_name", "status"}:
                setattr(user, field, value)

        self.uow.session.flush()
        return user

    def assign_role(self, user_id: str, role_id: str) -> UserRole:
        self._get_or_404(self.uow.users, user_id, "User")
        self._get_or_404(self.uow.roles, role_id, "Role")

        existing = self.uow.user_roles.get(user_id=user_id, role_id=role_id)
        if existing:
            return existing

        assignment = UserRole(user_id=user_id, role_id=role_id)
        self.uow.user_roles.add(assignment)
        self.uow.session.flush()
        return assignment

    def revoke_role(self, user_id: str, role_id: str) -> None:
        self._get_or_404(self.uow.users, user_id, "User")
        self._get_or_404(self.uow.roles, role_id, "Role")

        assignment = self.uow.user_roles.get(user_id=user_id, role_id=role_id)
        if assignment is not None:
            self.uow.user_roles.delete(assignment)
            self.uow.session.flush()

    def authorize(self, user_id: str, permission: str) -> bool:
        user = self._get_or_404(self.uow.users, user_id, "User")
        if user.status != "ACTIVE":
            return False

        permission_value = (permission or "").strip()
        if not permission_value:
            return False

        normalized = permission_value.upper()
        if user.user_type and user.user_type.upper() == normalized:
            return True

        statement = (
            select(Role.name, Role.role_id)
            .join(UserRole, UserRole.role_id == Role.role_id)
            .where(UserRole.user_id == user_id)
        )
        rows = self.uow.session.execute(statement).all()
        for role_name, role_id in rows:
            if (role_name or "").upper() == normalized or (role_id or "").upper() == normalized:
                return True
        return False

    def create_student_profile(self, user_id: str, profile_data: dict) -> StudentProfile:
        user = self._get_or_404(self.uow.users, user_id, "User")
        if user.user_type != "STUDENT":
            raise AppError("Only student users can have a student profile", status_code=400)

        if not profile_data:
            raise AppError("profile_data is required", status_code=400)

        student_code = profile_data.get("student_code")
        self._ensure(bool(student_code), "student_code is required", status_code=400)

        existing = self.uow.student_profiles.get(user_id=user_id)
        if existing:
            if existing.student_code != student_code and self.uow.student_profiles.get(student_code=student_code):
                raise AppError(f"Student code '{student_code}' already exists", status_code=409)
            existing.student_code = student_code
            existing.university_name = profile_data.get("university_name", existing.university_name)
            existing.faculty = profile_data.get("faculty", existing.faculty)
            self.uow.session.flush()
            return existing

        if self.uow.student_profiles.get(student_code=student_code):
            raise AppError(f"Student code '{student_code}' already exists", status_code=409)

        profile = StudentProfile(
            user_id=user_id,
            student_code=student_code,
            university_name=profile_data.get("university_name"),
            faculty=profile_data.get("faculty"),
        )
        self.uow.student_profiles.add(profile)
        self.uow.session.flush()
        return profile

    def suspend_user(self, user_id: str) -> User:
        user = self._get_or_404(self.uow.users, user_id, "User")
        user.status = "SUSPENDED"
        self.uow.session.flush()
        return user
