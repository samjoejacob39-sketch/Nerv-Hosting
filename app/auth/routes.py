"""Registration, login and logout views.

Each view answers in whichever format the caller asked for: ``wants_json()``
routes API clients to the JSON envelopes from ``app.responses`` and browsers to
the Jinja templates added in Phase 2.  The decision is purely about
presentation -- validation, hashing and session handling are shared.
"""

from __future__ import annotations

import secrets
from decimal import Decimal

from flask import current_app, flash, g, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf.csrf import generate_csrf
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from app.auth import auth_bp
from app.auth.forms import LoginForm, RegistrationForm
from app.extensions import db, limiter
from app.models.user import User
from app.responses import (
    error_response,
    form_errors,
    json_response,
    safe_redirect_target,
    wants_json,
)

#: Cache of throwaway hashes, keyed by hashing method.
_dummy_hashes: dict[str, str] = {}


def _dummy_hash() -> str:
    """A real hash of a random string, using the configured method.

    Verifying a submitted password against this when the identity does not
    exist makes "no such account" cost the same as "wrong password", so login
    response times cannot be used to enumerate registered accounts.  Built on
    first use (not hard-coded) so it always matches the live hash parameters.
    """
    method = current_app.config["PASSWORD_HASH_METHOD"]
    if method not in _dummy_hashes:
        _dummy_hashes[method] = generate_password_hash(secrets.token_urlsafe(32), method=method)
    return _dummy_hashes[method]


def _describe(form) -> dict:
    """Introspect a form into a small JSON schema.

    Useful while there is no UI: a client can discover the expected payload by
    issuing a GET, and the description cannot drift from the validators.
    """
    return {
        "fields": [
            {
                "name": field.name,
                "label": str(field.label.text),
                "type": field.type,
                "required": bool(field.flags.required),
            }
            for field in form
            if field.name != "csrf_token"
        ],
        "csrf_token": _current_csrf(),
    }


def _current_csrf() -> str | None:
    return generate_csrf() if current_app.config["WTF_CSRF_ENABLED"] else None


def _start_session(user: User, *, remember: bool) -> None:
    """Log ``user`` in on a brand-new session.

    ``session.clear()`` discards any pre-authentication session contents, which
    defeats session fixation: a session an attacker planted in the victim's
    browser is not the one that ends up authenticated.

    Clearing also drops the CSRF seed.  ``FlaskForm`` has already cached a token
    for this request in ``g``, derived from the *old* seed, so that cache is
    dropped too and the new session re-seeded -- otherwise the token handed back
    in the response would fail validation on the client's next POST.
    """
    session.clear()
    g.pop("csrf_token", None)
    login_user(user, remember=remember)
    session.permanent = True
    generate_csrf()


@auth_bp.get("/csrf-token")
def csrf_token():
    """Hand a CSRF token to non-browser clients.

    Send it back in the ``X-CSRFToken`` header (or a ``csrf_token`` field) on
    every subsequent POST.
    """
    return json_response(data={"csrf_token": generate_csrf()})


def _authenticated_payload(user: User) -> dict:
    """Body returned after a successful register/login.

    The fresh CSRF token is included because ``_start_session`` rotated the
    session: a JSON client's previously held token is now stale, and this saves
    it a round-trip to ``/csrf-token``.
    """
    return {"user": user.to_dict(include_email=True), "csrf_token": _current_csrf()}


def _reject(template: str, form, status: int, message: str, *, code: str | None = None):
    """Refuse a submission in the caller's preferred format.

    API clients get the error envelope with per-field messages; browsers get the
    form re-rendered with the same messages attached to their inputs, plus a
    flash for whatever could not be pinned to a single field.  Re-rendering
    (rather than redirecting) is what preserves the user's typed input.
    """
    if wants_json():
        return error_response(status, message, errors=form_errors(form), code=code)
    flash(message, "error")
    return render_template(template, form=form), status


# ---------------------------------------------------------------------- #
# Registration
# ---------------------------------------------------------------------- #
@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit(
    lambda: current_app.config["AUTH_RATELIMIT_REGISTER"],
    methods=["POST"],
    error_message="Too many sign-up attempts. Please try again later.",
)
def register():
    if current_user.is_authenticated:
        if wants_json():
            return error_response(409, "You are already signed in.", code="already_authenticated")
        return redirect(url_for("dashboard.index"))

    form = RegistrationForm()

    if request.method == "GET":
        if wants_json():
            return json_response(
                message="Submit these fields to POST /register.", data=_describe(form)
            )
        return render_template("auth/register.html", form=form)

    if not form.validate_on_submit():
        return _reject("auth/register.html", form, 422, "Registration details are invalid.")

    user = User(username=form.username.data, email=form.email.data)
    user.set_password(form.password.data)

    bonus = Decimal(str(current_app.config.get("SIGNUP_BONUS_CREDITS", "0") or "0"))
    if bonus > 0:
        user.add_credits(bonus)

    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        # Lost a race against a concurrent signup with the same username or
        # email; the form's uniqueness checks cannot close that window, the
        # UNIQUE constraints can.
        db.session.rollback()
        return _reject(
            "auth/register.html",
            form,
            409,
            "That username or email address was just taken. Please try another.",
            code="duplicate_account",
        )
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Failed to persist new account")
        return _reject(
            "auth/register.html",
            form,
            503,
            "Could not create your account. Please try again.",
        )

    _start_session(user, remember=False)
    current_app.logger.info("Registered user id=%s username=%s", user.id, user.username)

    if wants_json():
        return json_response(
            201,
            message="Account created.",
            data=_authenticated_payload(user),
        )
    flash(f"Welcome aboard, {user.username}.", "success")
    return redirect(url_for("dashboard.index"))


# ---------------------------------------------------------------------- #
# Login
# ---------------------------------------------------------------------- #
@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit(
    lambda: current_app.config["AUTH_RATELIMIT_LOGIN"],
    methods=["POST"],
    error_message="Too many login attempts. Please try again later.",
)
def login():
    if current_user.is_authenticated:
        if wants_json():
            return json_response(
                message="Already signed in.", data={"user": current_user.to_dict()}
            )
        return redirect(safe_redirect_target())

    form = LoginForm()

    if request.method == "GET":
        if wants_json():
            return json_response(
                message="Submit these fields to POST /login.", data=_describe(form)
            )
        return render_template("auth/login.html", form=form)

    if not form.validate_on_submit():
        return _reject("auth/login.html", form, 422, "Login details are incomplete.")

    user = User.by_identity(form.identity.data)

    # One generic message for "no such account", "wrong password" and (below)
    # "suspended": anything more specific lets an attacker confirm which
    # usernames and emails are registered.
    if user is None:
        check_password_hash(_dummy_hash(), form.password.data or "")
        current_app.logger.info(
            "Login failed: unknown identity %r from %s", form.identity.data, request.remote_addr
        )
        return _reject(
            "auth/login.html", form, 401, "Incorrect credentials.", code="invalid_credentials"
        )

    if not user.check_password(form.password.data):
        current_app.logger.info(
            "Login failed: bad password for user id=%s from %s", user.id, request.remote_addr
        )
        return _reject(
            "auth/login.html", form, 401, "Incorrect credentials.", code="invalid_credentials"
        )

    if not user.is_active:
        current_app.logger.warning("Login blocked: user id=%s is suspended", user.id)
        return _reject(
            "auth/login.html",
            form,
            403,
            "This account is suspended. Contact support.",
            code="account_suspended",
        )

    _start_session(user, remember=bool(form.remember_me.data))
    current_app.logger.info("Login succeeded for user id=%s", user.id)

    if wants_json():
        return json_response(
            message="Signed in.",
            data=_authenticated_payload(user),
            next=safe_redirect_target(),
        )
    flash(f"Welcome back, {user.username}.", "success")
    return redirect(safe_redirect_target())


# ---------------------------------------------------------------------- #
# Logout
# ---------------------------------------------------------------------- #
@auth_bp.post("/logout")
@login_required
def logout():
    """POST-only and CSRF-protected, so a third-party page cannot sign the user
    out by embedding a link or image."""
    user_id = current_user.get_id()
    logout_user()
    session.clear()
    current_app.logger.info("Logout for user id=%s", user_id)

    if wants_json():
        return json_response(message="Signed out.")
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------- #
# Session introspection
# ---------------------------------------------------------------------- #
@auth_bp.get("/me")
@login_required
def me():
    """Current session's account. Handy for verifying auth end to end."""
    return json_response(data={"user": current_user.to_dict(include_email=True)})
