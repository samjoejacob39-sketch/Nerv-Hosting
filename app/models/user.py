"""The ``User`` model: identity, credentials and the credit wallet."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from flask import current_app
from flask_login import UserMixin
from sqlalchemy import Boolean, CheckConstraint, Numeric, String, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from app.constants import CREDIT_PRECISION, CREDIT_QUANTUM, CREDIT_SCALE
from app.extensions import db
from app.models.mixins import TimestampMixin, isoformat

if TYPE_CHECKING:  # pragma: no cover
    from app.models.server import Server
    from app.models.shared_access import SharedAccess

#: Usernames are lowercased on write, so this pattern only needs lowercase.
USERNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{1,30}[a-z0-9])$")
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
ZERO = Decimal("0")


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("credit_balance >= 0", name="credit_balance_non_negative"),
        CheckConstraint(
            f"length(username) >= {USERNAME_MIN_LENGTH}", name="username_min_length"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(
        String(USERNAME_MAX_LENGTH), unique=True, nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    credit_balance: Mapped[Decimal] = mapped_column(
        Numeric(CREDIT_PRECISION, CREDIT_SCALE),
        nullable=False,
        default=ZERO,
        server_default="0",
    )
    # Overrides ``UserMixin.is_active``; Flask-Login refuses to log in a user
    # whose ``is_active`` is False, which gives us free account suspension.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    owned_servers: Mapped[list["Server"]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    guest_grants: Mapped[list["SharedAccess"]] = relationship(
        back_populates="guest",
        foreign_keys="SharedAccess.guest_user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ------------------------------------------------------------------ #
    # Normalisation
    # ------------------------------------------------------------------ #
    @staticmethod
    def normalise_username(raw: str) -> str:
        return (raw or "").strip().lower()

    @staticmethod
    def normalise_email(raw: str) -> str:
        """Lowercase and trim. The local part is technically case-sensitive per
        RFC 5321, but no real provider treats it that way, and folding case is
        what stops ``Bob@x.com`` from registering twice."""
        return (raw or "").strip().lower()

    # ------------------------------------------------------------------ #
    # Credentials
    # ------------------------------------------------------------------ #
    def set_password(self, password: str) -> None:
        """Hash and store ``password``.

        The plaintext is never assigned to an attribute, so it cannot leak via
        ``repr``, logs or an ORM flush.
        """
        if not password:
            raise ValueError("Password must not be empty.")
        method = current_app.config.get("PASSWORD_HASH_METHOD", "scrypt:32768:8:1")
        self.password_hash = generate_password_hash(password, method=method)

    def check_password(self, password: str) -> bool:
        """Constant-time-ish verification via Werkzeug. Returns False (rather
        than raising) for empty input or an unreadable stored hash."""
        if not password or not self.password_hash:
            return False
        try:
            return check_password_hash(self.password_hash, password)
        except (ValueError, TypeError):
            # Corrupt or unsupported hash format: treat as a failed attempt
            # instead of a 500, and leave a breadcrumb for operators.
            current_app.logger.error("Unreadable password hash for user id=%s", self.id)
            return False

    # ------------------------------------------------------------------ #
    # Credit wallet
    # ------------------------------------------------------------------ #
    @staticmethod
    def quantise_credits(amount: Decimal | int | float | str) -> Decimal:
        """Coerce any numeric input to the column's exact 4-decimal scale."""
        try:
            return Decimal(str(amount)).quantize(CREDIT_QUANTUM)
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError(f"{amount!r} is not a valid credit amount.") from None

    @property
    def credits(self) -> Decimal:
        """Balance as an exact ``Decimal``, never ``None``."""
        return self.quantise_credits(self.credit_balance or ZERO)

    def add_credits(self, amount: Decimal | int | float | str) -> Decimal:
        """Top the wallet up.

        Issued as ``SET credit_balance = credit_balance + :amount`` in SQL
        rather than read-modify-write in Python, so two concurrent top-ups
        cannot overwrite each other.  Caller commits.
        """
        delta = self.quantise_credits(amount)
        if delta <= ZERO:
            raise ValueError("Credit top-up must be a positive amount.")

        if self.id is None:
            # Row does not exist yet (e.g. the signup bonus): a plain assignment
            # is correct and there is nothing to race against.
            self.credit_balance = self.credits + delta
            return delta

        db.session.execute(
            db.update(User)
            .where(User.id == self.id)
            .values(credit_balance=User.credit_balance + delta)
        )
        db.session.refresh(self, attribute_names=["credit_balance"])
        return delta

    def deduct_credits(self, amount: Decimal | int | float | str) -> bool:
        """Spend credits atomically.

        Returns ``False`` without touching the row when the balance is
        insufficient.  The ``WHERE credit_balance >= :amount`` guard means the
        check and the decrement happen in one statement, so a user cannot spend
        the same credits twice from two parallel requests.
        """
        delta = self.quantise_credits(amount)
        if delta <= ZERO:
            raise ValueError("Credit charge must be a positive amount.")

        updated = db.session.execute(
            db.update(User)
            .where(User.id == self.id, User.credit_balance >= delta)
            .values(credit_balance=User.credit_balance - delta)
        ).rowcount
        if not updated:
            return False
        db.session.refresh(self, attribute_names=["credit_balance"])
        return True

    def can_afford(self, amount: Decimal | int | float | str) -> bool:
        return self.credits >= self.quantise_credits(amount)

    # ------------------------------------------------------------------ #
    # Access helpers
    # ------------------------------------------------------------------ #
    @property
    def shared_servers(self) -> list["Server"]:
        """Servers owned by somebody else that this account may manage."""
        return [grant.server for grant in self.guest_grants if grant.server is not None]

    @property
    def accessible_servers(self) -> list["Server"]:
        return [*self.owned_servers, *self.shared_servers]

    def owns(self, server: "Server") -> bool:
        return server is not None and server.owner_id == self.id

    def can_access(self, server: "Server") -> bool:
        """True when this account owns ``server`` or holds a share for it."""
        if server is None:
            return False
        if self.owns(server) or self.is_admin:
            return True
        from app.models.shared_access import SharedAccess

        return db.session.scalar(
            select(func.count())
            .select_from(SharedAccess)
            .where(
                SharedAccess.server_id == server.id,
                SharedAccess.guest_user_id == self.id,
            )
        ) > 0

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    @classmethod
    def by_username(cls, username: str) -> "User | None":
        return db.session.scalar(select(cls).where(cls.username == cls.normalise_username(username)))

    @classmethod
    def by_email(cls, email: str) -> "User | None":
        return db.session.scalar(select(cls).where(cls.email == cls.normalise_email(email)))

    @classmethod
    def by_identity(cls, identity: str) -> "User | None":
        """Resolve a login field that may hold either a username or an email."""
        identity = (identity or "").strip()
        if not identity:
            return None
        return cls.by_email(identity) if "@" in identity else cls.by_username(identity)

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self, *, include_email: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "username": self.username,
            "credit_balance": str(self.credits),
            "is_admin": self.is_admin,
            "created_at": isoformat(self.created_at),
        }
        if include_email:
            payload["email"] = self.email
        return payload

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} username={self.username!r}>"
