"""Centralised error handling.

Every failure path -- HTTP aborts, CSRF rejections, database faults and
unhandled exceptions -- funnels through here, so an API client always receives
the same envelope shape, a browser always receives the same error page, and the
session is never left in a dirty state.
"""

from __future__ import annotations

from typing import Any, Mapping

from flask import Flask, current_app, redirect, render_template, url_for
from flask_wtf.csrf import CSRFError
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException

from app.extensions import db
from app.responses import error_response, wants_json


def _fail(
    status: int,
    message: str,
    *,
    code: str | None = None,
    errors: Mapping[str, list[str]] | None = None,
    **extra: Any,
):
    """Report a failure in whichever format the caller asked for.

    Browsers get ``errors/error.html``; anything that looks like an API client
    gets the JSON envelope.  The template render is guarded: an error page that
    itself raises would re-enter the handler chain, so a failure there degrades
    to JSON rather than turning a 404 into an infinite loop.
    """
    if not wants_json():
        try:
            page = render_template(
                "errors/error.html", status=status, message=message, code=code
            )
            return page, status
        except Exception:  # pragma: no cover - defensive
            current_app.logger.exception("Failed to render the error page for status %s", status)
    return error_response(status, message, code=code, errors=errors, **extra)


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(CSRFError)
    def handle_csrf_error(error: CSRFError):
        # Usually an expired session rather than an attack, so say something
        # actionable instead of a bare 400.
        return _fail(
            400,
            "Your session token is missing or expired. Reload the page and try again.",
            code="csrf_error",
            detail=error.description,
        )

    @app.errorhandler(404)
    def handle_not_found(error):
        return _fail(404, "The requested resource does not exist.")

    @app.errorhandler(405)
    def handle_method_not_allowed(error):
        allowed = sorted(getattr(error, "valid_methods", None) or [])
        return _fail(
            405,
            "That HTTP method is not allowed on this endpoint.",
            allowed_methods=allowed,
        )

    @app.errorhandler(413)
    def handle_payload_too_large(error):
        limit = current_app.config.get("MAX_CONTENT_LENGTH")
        return _fail(413, "Request body is too large.", limit_bytes=limit)

    @app.errorhandler(429)
    def handle_rate_limited(error):
        return _fail(
            429,
            getattr(error, "description", None) or "Too many requests. Slow down.",
            retry_after=getattr(error, "retry_after", None),
        )

    @app.errorhandler(HTTPException)
    def handle_http_exception(error: HTTPException):
        """Catch-all for aborts without a dedicated handler (400/401/403/...)."""
        return _fail(
            error.code or 500,
            error.description or error.name,
            code=error.name.lower().replace(" ", "_"),
        )

    @app.errorhandler(SQLAlchemyError)
    def handle_database_error(error: SQLAlchemyError):
        # Roll back before responding; otherwise the failed transaction is
        # still open and the next query on this connection also fails.
        db.session.rollback()
        current_app.logger.exception("Unhandled database error")
        return _fail(503, "A database error occurred. Please try again shortly.")

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        db.session.rollback()
        current_app.logger.exception("Unhandled application error")
        if current_app.debug:
            # Let the interactive debugger surface the traceback locally.
            raise error
        return _fail(500, "An unexpected error occurred.")


def register_login_handlers(login_manager) -> None:
    """Teach Flask-Login how to refuse a request.

    API clients need a 401 with a JSON body; browsers need a redirect to the
    login page carrying ``?next=``.
    """

    @login_manager.unauthorized_handler
    def unauthorized():
        if wants_json():
            return error_response(401, "Authentication is required.", code="unauthenticated")
        from flask import request

        return redirect(url_for("auth.login", next=request.full_path))

    @login_manager.needs_refresh_handler
    def needs_refresh():
        if wants_json():
            return error_response(
                401, "Please sign in again to continue.", code="session_stale"
            )
        return redirect(url_for("auth.login"))
