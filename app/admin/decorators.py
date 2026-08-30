"""Admin access control decorators."""

from __future__ import annotations

from functools import wraps

from flask import abort, redirect, request, url_for
from flask_login import current_user

from app.responses import error_response, wants_json


def admin_required(fn):
    """Ensure the user is authenticated and possesses administrator privileges."""
    @wraps(fn)
    def decorated_view(*args, **kwargs):
        if not current_user.is_authenticated:
            if wants_json():
                return error_response(401, "Authentication required.", code="unauthenticated")
            return redirect(url_for("auth.login", next=request.url))
        if not getattr(current_user, "is_admin", False):
            if wants_json():
                return error_response(403, "Forbidden: Admin privileges required.", code="forbidden")
            abort(403)
        return fn(*args, **kwargs)
    return decorated_view
