"""WTForms definitions for the dashboard's write endpoints.

Same trick as ``app/auth/forms.py``: Flask-WTF validates ``request.form`` for a
browser post and falls back to the JSON body, so one class covers both
transports and the view never inspects ``request`` directly.
"""

from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import SelectField, StringField
from wtforms.validators import DataRequired, Length, Regexp, ValidationError

from app.constants import MINECRAFT_RAM_TIERS, RamTier
from app.models.server import (
    SERVER_NAME_MAX_LENGTH,
    SERVER_NAME_MIN_LENGTH,
    SERVER_NAME_PATTERN,
    Server,
)


def _tier_choices() -> list[tuple[str, str]]:
    """The deployable sizes, as ``(value, label)`` pairs for a ``<select>``.

    Values are strings because that is what an HTML form submits; ``coerce=str``
    on the field keeps a JSON body's integer comparable with them.
    """
    return [
        (str(gb), f"{RamTier(gb).label} - {gb} GB RAM")
        for gb in MINECRAFT_RAM_TIERS
    ]


class DeployServerForm(FlaskForm):
    """Inputs for ``POST /dashboard/deploy``.

    ``ram_tier`` is validated against :data:`MINECRAFT_RAM_TIERS` rather than the
    whole enum: ``FREE`` (1 GB) exists for the lighter container types and cannot
    hold a Paper server.
    """

    server_name = StringField(
        "Server name",
        filters=[lambda value: (value or "").strip()],
        validators=[
            DataRequired(message="A server name is required."),
            Length(
                min=SERVER_NAME_MIN_LENGTH,
                max=SERVER_NAME_MAX_LENGTH,
                message=(
                    f"Server name must be between {SERVER_NAME_MIN_LENGTH} and "
                    f"{SERVER_NAME_MAX_LENGTH} characters."
                ),
            ),
            Regexp(
                SERVER_NAME_PATTERN,
                message=(
                    "Server name may contain letters, digits, spaces, dots, "
                    "dashes and underscores, and must start and end with a "
                    "letter or digit."
                ),
            ),
        ],
    )
    ram_tier = SelectField(
        "Plan",
        coerce=str,
        validators=[DataRequired(message="Choose a RAM tier.")],
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Populated per instance, not at class definition time, so the allowlist
        # stays a single source of truth in ``app.constants``.
        self.ram_tier.choices = _tier_choices()

    @property
    def tier(self) -> RamTier:
        """The chosen tier. Only meaningful after ``validate_on_submit()``."""
        return RamTier(int(self.ram_tier.data))

    def validate_ram_tier(self, field) -> None:
        """Reject anything outside the Minecraft allowlist.

        ``SelectField`` already refuses values absent from ``choices``, but its
        message ("Not a valid choice") is useless to a user, and this keeps the
        rule readable next to the constant it enforces.
        """
        try:
            gb = int(field.data)
        except (TypeError, ValueError):
            raise ValidationError("Choose a RAM tier.") from None
        if gb not in MINECRAFT_RAM_TIERS:
            allowed = ", ".join(f"{g}GB" for g in MINECRAFT_RAM_TIERS)
            raise ValidationError(f"A Minecraft server needs one of: {allowed}.")

    def validate_server_name(self, field) -> None:
        """Advisory duplicate check.

        The ``uq_servers_owner_id_name`` UNIQUE constraint remains the authority;
        catching the clash here means we refuse *before* charging for it.
        """
        from flask_login import current_user

        if current_user.is_authenticated and Server.name_taken(current_user.id, field.data):
            raise ValidationError("You already have a server with that name.")
