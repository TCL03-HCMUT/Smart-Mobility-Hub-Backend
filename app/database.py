from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    if engine.dialect.name == "sqlite":
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            if settings.sqlite_foreign_keys:
                cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_uow():
    from app.repositories.unit_of_work import UnitOfWork

    db = SessionLocal()
    try:
        yield UnitOfWork(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# Finalize the transaction before FastAPI sends the response.
UowDependency = Annotated[object, Depends(get_uow, scope="function")]
