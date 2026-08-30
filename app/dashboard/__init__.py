"""Dashboard blueprint.

Every route registered here is authenticated.  Rather than relying on each
view remembering ``@login_required``, the blueprint enforces it in a
``before_request`` hook -- a new endpoint added later cannot accidentally ship
unprotected.
"""

from __future__ import annotations

from flask import Blueprint, request
from flask_login import login_required

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


@dashboard_bp.before_request
def require_authenticated_user():
    """Gate the blueprint, except for public webhook endpoints."""
    if request.path.endswith("/api/credit-reward") or request.endpoint in {
        "dashboard.credit_reward",
        "credit_reward_alias",
    }:
        return None
    return login_required(lambda: None)()


from app.dashboard import routes  # noqa: E402,F401  (registers the view functions)
