from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import BASE_DIR, get_settings


settings = get_settings()


def _sqlite_path_from_url(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    raw = database_url.replace("sqlite:///", "", 1)
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


sqlite_path = _sqlite_path_from_url(settings.database_url)
if sqlite_path:
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{sqlite_path.as_posix()}"
else:
    database_url = settings.database_url

connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}

engine = create_engine(
    database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    future=True,
)


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
    if database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)

Base = declarative_base()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()


def _run_lightweight_migrations() -> None:
    if not database_url.startswith("sqlite"):
        return
    with engine.begin() as connection:
        rows = connection.exec_driver_sql("PRAGMA table_info(admin_users)").fetchall()
        columns = {row[1] for row in rows}
        migrations = {
            "role": "ALTER TABLE admin_users ADD COLUMN role VARCHAR(20) DEFAULT 'user' NOT NULL",
            "youtube_refresh_token": "ALTER TABLE admin_users ADD COLUMN youtube_refresh_token TEXT",
            "youtube_channel_id": "ALTER TABLE admin_users ADD COLUMN youtube_channel_id VARCHAR(255)",
            "youtube_channel": "ALTER TABLE admin_users ADD COLUMN youtube_channel JSON DEFAULT '{}' NOT NULL",
        }
        for column, sql in migrations.items():
            if column not in columns:
                connection.exec_driver_sql(sql)
        connection.exec_driver_sql(
            "UPDATE admin_users SET role = 'admin' WHERE role IS NULL OR role = ''"
        )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def new_session() -> Session:
    return SessionLocal()
