from typing import Any, Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Generic repository providing standardized CRUD operations for entity models.

    Simplifies data access across modules while keeping class diagrams clean.
    """

    def __init__(self, db: Session, model: type[ModelT] | None = None):
        self.db = db
        self.model = model or getattr(type(self), "model")

    def get_by_id(self, entity_id: Any) -> ModelT | None:
        """Fetch a single entity by its primary key."""
        return self.db.get(self.model, entity_id)

    def get(self, **criteria) -> ModelT | None:
        """Fetch the first entity matching the given equality criteria."""
        statement = select(self.model)
        for field, value in criteria.items():
            statement = statement.where(getattr(self.model, field) == value)
        return self.db.scalar(statement)

    def find(self, **criteria) -> list[ModelT]:
        """Fetch all entities matching the given equality criteria."""
        statement = select(self.model)
        for field, value in criteria.items():
            statement = statement.where(getattr(self.model, field) == value)
        return list(self.db.scalars(statement).all())

    def list(self, offset: int = 0, limit: int = 50) -> tuple[list[ModelT], int]:
        """Fetch a paginated list of entities along with the total count."""
        items = self.db.scalars(select(self.model).offset(offset).limit(limit)).all()
        total = self.db.scalar(select(func.count()).select_from(self.model)) or 0
        return list(items), total

    def add(self, entity: ModelT) -> ModelT:
        """Add a new entity to the persistence session."""
        self.db.add(entity)
        return entity

    def delete(self, entity: ModelT) -> None:
        """Delete an existing entity from the persistence session."""
        self.db.delete(entity)
