"""Flask extension singletons.

Instantiated here, unbound, so that models and blueprints can import them
without creating a circular dependency on the application factory.
"""

from __future__ import annotations

from flask_apscheduler import APScheduler
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import MetaData, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

# Explicit, deterministic constraint names.  Without these, Alembic cannot
# generate ``ALTER``/``DROP CONSTRAINT`` statements for unnamed constraints,
# which breaks the SQLite -> PostgreSQL migration path.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all models (SQLAlchemy 2.0 typed style)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


db = SQLAlchemy(model_class=Base)
migrate = Migrate()
csrf = CSRFProtect()
login_manager = LoginManager()
scheduler = APScheduler()


def rate_limit_key() -> str:
    """Bucket by account when authenticated, otherwise by client address.

    Keying on the user id stops one abusive account from burning a shared
    NAT/proxy address's budget, and vice versa.
    """
    if current_user.is_authenticated:
        return f"user:{current_user.get_id()}"
    return f"ip:{get_remote_address()}"


limiter = Limiter(key_func=rate_limit_key, default_limits=[])


# Registered once, at import time, against every engine this process creates.
@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """Turn on SQLite foreign-key enforcement for each new connection.

    SQLite parses ``REFERENCES`` but ignores it unless this pragma is set, so
    without this hook ``ON DELETE CASCADE`` silently does nothing: deleting a
    user would leave orphaned ``servers`` and ``shared_access`` rows behind, and
    the bug would only surface after migrating to PostgreSQL.
    """
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
