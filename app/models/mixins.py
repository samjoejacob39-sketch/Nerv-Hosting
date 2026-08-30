"""Shared declarative mixins and time helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware "now". Never use ``datetime.utcnow()``: it returns a
    naive value that silently compares wrong against aware timestamps."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Force a stored timestamp to be timezone-aware.

    SQLite has no native timestamp type, so SQLAlchemy hands back *naive*
    datetimes even for ``DateTime(timezone=True)`` columns.  PostgreSQL returns
    aware values.  This normalises both to UTC-aware so application code and
    serialisation behave identically on either backend.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    aware = as_utc(value)
    return aware.isoformat() if aware else None


class TimestampMixin:
    """Adds a server-defaulted, indexed ``created_at`` column."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
        index=True,
    )

    @property
    def created_at_utc(self) -> datetime | None:
        return as_utc(self.created_at)
