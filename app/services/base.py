from typing import Any, TypeVar

from app.core.exceptions import AppError, NotFoundError
from app.repositories.base import BaseRepository
from app.repositories.unit_of_work import UnitOfWork

T = TypeVar("T")


class BaseService:
    """Base class for domain service classes in the application.

    Maintains separation between business logic workflows and underlying
    data-access CRUD operations through the UnitOfWork. Provides standardized
    helpers for entity lookups, invariant assertions, and domain exceptions.
    """

    def __init__(self, uow: UnitOfWork):
        self.uow = uow

    def _get_or_404(
        self,
        repo: BaseRepository[T],
        entity_id: Any,
        entity_name: str,
    ) -> T:
        """Fetch an entity by id or raise a NotFoundError if it does not exist."""
        entity = repo.get_by_id(entity_id)
        if not entity:
            raise NotFoundError(entity_name, str(entity_id))
        return entity

    def _ensure(
        self,
        condition: bool,
        message: str,
        status_code: int = 400,
    ) -> None:
        """Assert a domain invariant or precondition, raising AppError if violated."""
        if not condition:
            raise AppError(message, status_code=status_code)

