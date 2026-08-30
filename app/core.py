"""Public, unauthenticated endpoints: landing stub and health probe."""

from __future__ import annotations

from flask import Blueprint, current_app, redirect, url_for
from flask_login import current_user
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.responses import json_response, wants_json

core_bp = Blueprint("core", __name__)


@core_bp.get("/")
def index():
    """API index for clients; for browsers, the front door.

    There is no marketing page yet, so a browser is sent where it actually
    wants to go -- the dashboard when signed in, otherwise sign-in.
    """
    if not wants_json():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.index"))
        return redirect(url_for("auth.login"))

    return json_response(
        message="VpsHosting API",
        data={
            "environment": current_app.config["CONFIG_NAME"],
            "endpoints": {
                "register": "POST /register",
                "login": "POST /login",
                "logout": "POST /logout",
                "session": "GET /me",
                "dashboard": "GET /dashboard/ (requires login)",
            },
        },
    )


@core_bp.get("/healthz")
def healthz():
    """Liveness plus a real database round-trip.

    Returns 503 when the database is unreachable so an orchestrator pulls the
    instance out of rotation instead of serving 500s.
    """
    try:
        db.session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        current_app.logger.exception("Health check failed: database unreachable")
        return json_response(503, message="degraded", data={"database": "unreachable"})
    return json_response(data={"status": "ok", "database": "ok"})
