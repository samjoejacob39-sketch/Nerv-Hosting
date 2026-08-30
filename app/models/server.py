"""The ``Server`` model: one hosted container, mirrored from the panel."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import RAM_TIER_VALUES, RamTier, ServerStatus, ServerType
from app.extensions import db
from app.models.mixins import TimestampMixin, isoformat

if TYPE_CHECKING:  # pragma: no cover
    from app.models.shared_access import SharedAccess
    from app.models.user import User

SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,46}[A-Za-z0-9]$")
SERVER_NAME_MIN_LENGTH = 3
SERVER_NAME_MAX_LENGTH = 48

_ALLOWED_STATUSES = ", ".join(f"'{s}'" for s in ServerStatus.ALL)
_ALLOWED_TIERS = ", ".join(str(v) for v in RAM_TIER_VALUES)
_ALLOWED_TYPES = ", ".join(f"'{t}'" for t in ServerType.ALL)


class Server(TimestampMixin, db.Model):
    __tablename__ = "servers"
    __table_args__ = (
        # One account cannot own two servers with the same name; ownership
        # transfer and re-creation stay unambiguous.
        UniqueConstraint("owner_id", "name", name="uq_servers_owner_id_name"),
        CheckConstraint(f"ram_tier IN ({_ALLOWED_TIERS})", name="ram_tier_known"),
        CheckConstraint(f"status IN ({_ALLOWED_STATUSES})", name="status_known"),
        CheckConstraint(f"server_type IN ({_ALLOWED_TYPES})", name="server_type_known"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(SERVER_NAME_MAX_LENGTH), nullable=False)
    ram_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    #: What software the container runs; decides which nest/egg provisions it.
    server_type: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=ServerType.MINECRAFT,
        server_default=ServerType.MINECRAFT,
        index=True,
    )
    #: Identifier assigned by the Pterodactyl panel. NULL until provisioning
    #: succeeds, which is what distinguishes a queued row from a live container.
    pterodactyl_server_id: Mapped[int | None] = mapped_column(
        Integer, unique=True, nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=ServerStatus.PENDING,
        server_default=ServerStatus.PENDING,
        index=True,
    )
    #: Custom claimed subdomain (e.g. sammjoe.yourdomain.com)
    subdomain: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    #: Cloudflare DNS record ID for automated record updates/deletion
    cloudflare_record_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    owner: Mapped["User"] = relationship(back_populates="owned_servers", lazy="joined")
    shared_accesses: Mapped[list["SharedAccess"]] = relationship(
        back_populates="server",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ------------------------------------------------------------------ #
    # Tier / status helpers
    # ------------------------------------------------------------------ #
    @property
    def tier(self) -> RamTier:
        return RamTier(self.ram_tier)

    @property
    def ram_mb(self) -> int:
        return self.tier.ram_mb

    @property
    def hourly_credits(self):
        return self.tier.hourly_credits

    @property
    def is_provisioned(self) -> bool:
        return self.pterodactyl_server_id is not None

    @property
    def is_billable(self) -> bool:
        return self.status in ServerStatus.BILLABLE

    @property
    def can_start(self) -> bool:
        return self.is_provisioned and self.status in ServerStatus.STARTABLE

    def set_status(self, status: str) -> None:
        """Assign a lifecycle state, rejecting anything not in the vocabulary
        before the database's CHECK constraint has to."""
        if status not in ServerStatus.ALL:
            raise ValueError(
                f"Unknown server status {status!r}. Valid: {', '.join(ServerStatus.ALL)}."
            )
        self.status = status

    # ------------------------------------------------------------------ #
    # Sharing helpers
    # ------------------------------------------------------------------ #
    @property
    def guests(self) -> list["User"]:
        return [grant.guest for grant in self.shared_accesses if grant.guest is not None]

    def is_shared_with(self, user: "User") -> bool:
        return any(grant.guest_user_id == user.id for grant in self.shared_accesses)

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    @classmethod
    def for_owner(cls, owner_id: int) -> list["Server"]:
        return list(
            db.session.scalars(
                select(cls).where(cls.owner_id == owner_id).order_by(cls.created_at.desc())
            )
        )

    @classmethod
    def by_panel_id(cls, panel_id: int) -> "Server | None":
        return db.session.scalar(select(cls).where(cls.pterodactyl_server_id == panel_id))

    @classmethod
    def name_taken(cls, owner_id: int, name: str) -> bool:
        """True when ``owner_id`` already owns a server called ``name``.

        Checked before provisioning so a clash surfaces as a form error rather
        than an IntegrityError raised after the account has been charged.
        """
        clash = db.session.scalar(
            select(cls.id).where(cls.owner_id == owner_id, cls.name == name).limit(1)
        )
        return clash is not None

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self, *, include_owner: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "server_type": self.server_type,
            "ram_tier": self.ram_tier,
            "ram_tier_label": self.tier.label,
            "ram_mb": self.ram_mb,
            "hourly_credits": str(self.hourly_credits),
            "pterodactyl_server_id": self.pterodactyl_server_id,
            "is_provisioned": self.is_provisioned,
            "subdomain": self.subdomain,
            "cloudflare_record_id": self.cloudflare_record_id,
            "created_at": isoformat(self.created_at),
        }
        if include_owner:
            payload["owner"] = {"id": self.owner_id, "username": self.owner.username}
        return payload

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Server id={self.id} name={self.name!r} status={self.status}>"
