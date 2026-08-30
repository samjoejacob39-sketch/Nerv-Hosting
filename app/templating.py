"""Jinja filters and globals for the Phase 2 templates.

Presentation-only helpers live here so templates never have to reach into
``Decimal`` arithmetic or hard-code a status colour in two places.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Flask

from app.constants import MAX_SERVERS_PER_USER, RamTier, ServerStatus

#: Chip styling per lifecycle state.  Keyed by the raw column value, with a
#: fallback so an unknown state renders neutrally instead of unstyled.
STATUS_STYLES: dict[str, dict[str, str]] = {
    ServerStatus.PENDING: {
        "label": "Queued",
        "chip": "bg-slate-500/15 text-slate-300 ring-slate-400/25",
        "dot": "bg-slate-400",
    },
    ServerStatus.INSTALLING: {
        "label": "Installing",
        "chip": "bg-sky-500/15 text-sky-300 ring-sky-400/25",
        "dot": "bg-sky-400 animate-pulse",
    },
    ServerStatus.STARTING: {
        "label": "Starting",
        "chip": "bg-amber-500/15 text-amber-300 ring-amber-400/25",
        "dot": "bg-amber-400 animate-pulse",
    },
    ServerStatus.RUNNING: {
        "label": "Online",
        "chip": "bg-brand-500/15 text-brand-300 ring-brand-400/30",
        "dot": "bg-brand-400",
    },
    ServerStatus.STOPPING: {
        "label": "Stopping",
        "chip": "bg-amber-500/15 text-amber-300 ring-amber-400/25",
        "dot": "bg-amber-400 animate-pulse",
    },
    ServerStatus.STOPPED: {
        "label": "Offline",
        "chip": "bg-slate-500/15 text-slate-300 ring-slate-400/25",
        "dot": "bg-slate-500",
    },
    ServerStatus.SUSPENDED: {
        "label": "Suspended",
        "chip": "bg-amber-500/15 text-amber-300 ring-amber-400/25",
        "dot": "bg-amber-400",
    },
    ServerStatus.ERROR: {
        "label": "Error",
        "chip": "bg-rose-500/15 text-rose-300 ring-rose-400/25",
        "dot": "bg-rose-400",
    },
    ServerStatus.DELETING: {
        "label": "Deleting",
        "chip": "bg-rose-500/15 text-rose-300 ring-rose-400/25",
        "dot": "bg-rose-400 animate-pulse",
    },
}

_UNKNOWN_STATUS: dict[str, str] = {
    "label": "Unknown",
    "chip": "bg-slate-500/15 text-slate-300 ring-slate-400/25",
    "dot": "bg-slate-500",
}

#: Flash category -> chip styling.  ``category`` is whatever the view passed to
#: ``flash()``, so unrecognised values fall back to "info".
FLASH_STYLES: dict[str, dict[str, str]] = {
    "success": {"chip": "border-brand-500/40 bg-brand-500/10 text-brand-200", "icon": "check"},
    "error": {"chip": "border-rose-500/40 bg-rose-500/10 text-rose-200", "icon": "alert"},
    "warning": {"chip": "border-amber-500/40 bg-amber-500/10 text-amber-200", "icon": "alert"},
    "info": {"chip": "border-ink-600 bg-ink-800/70 text-slate-300", "icon": "info"},
}


def format_credits(value: object, places: int = 2) -> str:
    """Render a credit balance for display.

    ``credit_balance`` is a ``Decimal`` off a ``NUMERIC(12, 4)`` column, so it
    arrives with four decimal places; the UI shows two.  Anything unparseable
    degrades to ``0.00`` rather than raising mid-render.
    """
    try:
        amount = Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, ValueError, TypeError):
        amount = Decimal("0")
    return f"{amount:,.{places}f}"


def format_datetime(value: datetime | None, pattern: str = "%d %b %Y") -> str:
    """Format a timestamp in UTC.  SQLite hands back naive datetimes even for
    ``DateTime(timezone=True)``, so assume UTC when no tzinfo is attached."""
    if value is None:
        return "--"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(pattern)


def status_style(status: str | None) -> dict[str, str]:
    return STATUS_STYLES.get(status or "", _UNKNOWN_STATUS)


def flash_style(category: str | None) -> dict[str, str]:
    return FLASH_STYLES.get(category or "info", FLASH_STYLES["info"])


def register_template_helpers(app: Flask) -> None:
    """Wire the filters and globals every template depends on."""
    from app import navigation

    app.jinja_env.filters["credits"] = format_credits
    app.jinja_env.filters["datetime"] = format_datetime
    app.jinja_env.filters["status_style"] = status_style
    app.jinja_env.filters["flash_style"] = flash_style

    # Trim template whitespace: the sidebar is deeply nested and the rendered
    # HTML is otherwise mostly blank lines.
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    @app.context_processor
    def inject_globals() -> dict[str, object]:
        return {
            "sidebar": navigation.SIDEBAR,
            "submenu_row_threshold": navigation.SUBMENU_ROW_THRESHOLD,
            "submenu_rows_per_column": navigation.SUBMENU_ROWS_PER_COLUMN,
            "submenu_max_columns": navigation.SUBMENU_MAX_COLUMNS,
            "ram_tiers": tuple(RamTier),
            "max_servers_per_user": MAX_SERVERS_PER_USER,
            "current_year": datetime.now(timezone.utc).year,
        }
