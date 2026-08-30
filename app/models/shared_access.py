"""The ``SharedAccess`` model: co-management grants (the Aternos "friends"
feature).  One row means ``guest_user_id`` may operate ``server_id`` without
owning it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import MAX_GUESTS_PER_SERVER
from app.extensions import db
from app.models.mixins import TimestampMixin, isoformat

if TYPE_CHECKING:  # pragma: no cover
    from app.models.server import Server
    from app.models.user import User


class SharedAccessError(ValueError):
    """Raised when a share would be invalid (duplicate, self-share, full)."""


class SharedAccess(TimestampMixin, db.Model):
    __tablename__ = "shared_access"
    __table_args__ = (
        # The database is the final authority on "one grant per guest per
        # server", so a duplicate cannot slip through two concurrent requests.
        UniqueConstraint("server_id", "guest_user_id", name="uq_shared_access_server_guest"),
        Index("ix_shared_access_guest_user_id", "guest_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    guest_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    server: Mapped["Server"] = relationship(back_populates="shared_accesses", lazy="joined")
    guest: Mapped["User"] = relationship(
        back_populates="guest_grants", foreign_keys=[guest_user_id], lazy="joined"
    )

    # ------------------------------------------------------------------ #
    # Factory
    # ------------------------------------------------------------------ #
    @classmethod
    def grant(cls, server: "Server", guest: "User") -> "SharedAccess":
        """Create a grant after checking the invariants a CHECK constraint
        cannot express (they span two tables).  Caller commits."""
        if server is None or guest is None:
            raise SharedAccessError("Both a server and a guest user are required.")
        if server.owner_id == guest.id:
            raise SharedAccessError("The owner already has full access to this server.")
        if cls.exists(server.id, guest.id):
            raise SharedAccessError(f"{guest.username} already has access to this server.")
        if cls.count_for_server(server.id) >= MAX_GUESTS_PER_SERVER:
            raise SharedAccessError(
                f"A server may be shared with at most {MAX_GUESTS_PER_SERVER} guests."
            )

        grant = cls(server_id=server.id, guest_user_id=guest.id)
        db.session.add(grant)
        return grant

    @classmethod
    def revoke(cls, server_id: int, guest_user_id: int) -> bool:
        """Delete a grant. Returns False when there was nothing to revoke."""
        deleted = db.session.execute(
            db.delete(cls).where(cls.server_id == server_id, cls.guest_user_id == guest_user_id)
        ).rowcount
        return bool(deleted)

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    @classmethod
    def exists(cls, server_id: int, guest_user_id: int) -> bool:
        return (
            db.session.scalar(
                select(func.count())
                .select_from(cls)
                .where(cls.server_id == server_id, cls.guest_user_id == guest_user_id)
            )
            or 0
        ) > 0

    @classmethod
    def count_for_server(cls, server_id: int) -> int:
        return (
            db.session.scalar(
                select(func.count()).select_from(cls).where(cls.server_id == server_id)
            )
            or 0
        )

    @classmethod
    def for_guest(cls, guest_user_id: int) -> list["SharedAccess"]:
        return list(
            db.session.scalars(
                select(cls)
                .where(cls.guest_user_id == guest_user_id)
                .order_by(cls.created_at.desc())
            )
        )

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "server_id": self.server_id,
            "guest": {"id": self.guest_user_id, "username": self.guest.username},
            "created_at": isoformat(self.created_at),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SharedAccess server_id={self.server_id} guest_user_id={self.guest_user_id}>"
