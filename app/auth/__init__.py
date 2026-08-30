"""Authentication blueprint.

Mounted at the application root so the endpoints are ``/register``, ``/login``
and ``/logout`` as specified.
"""

from __future__ import annotations

from flask import Blueprint

auth_bp = Blueprint("auth", __name__)

from app.auth import routes  # noqa: E402,F401  (registers the view functions)
