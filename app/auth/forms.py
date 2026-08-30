"""WTForms definitions for the auth endpoints.

Flask-WTF reads ``request.form`` for browser posts and falls back to the JSON
body, so a single form class validates both transports.  Keeping validation in
forms (rather than inline in the view) means Phase 2's templates can render
field-level errors with no changes to the routes.
"""

from __future__ import annotations

import re

from flask import current_app
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    Length,
    Regexp,
    ValidationError,
)

from app.models.user import (
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    USERNAME_PATTERN,
    User,
)

#: Substrings too weak to allow regardless of length.
_BANNED_PASSWORD_FRAGMENTS = (
    "password",
    "qwerty",
    "123456",
    "letmein",
    "iloveyou",
    "vpshosting",
)


def strong_password(form: FlaskForm, field) -> None:
    """Enforce the configured length floor plus basic composition rules.

    Reads limits from ``current_app.config`` at validation time so operators can
    tighten the policy through ``.env`` without a code change.
    """
    password = field.data or ""
    min_length = current_app.config.get("MIN_PASSWORD_LENGTH", 10)
    max_length = current_app.config.get("MAX_PASSWORD_LENGTH", 128)

    if len(password) < min_length:
        raise ValidationError(f"Password must be at least {min_length} characters long.")
    if len(password) > max_length:
        # Long inputs are a cheap CPU-exhaustion vector against scrypt.
        raise ValidationError(f"Password must be at most {max_length} characters long.")

    classes = sum(
        bool(pattern.search(password))
        for pattern in (
            re.compile(r"[a-z]"),
            re.compile(r"[A-Z]"),
            re.compile(r"\d"),
            re.compile(r"[^A-Za-z0-9]"),
        )
    )
    if classes < 3:
        raise ValidationError(
            "Password must combine at least three of: lowercase, uppercase, "
            "digits, symbols."
        )

    lowered = password.lower()
    if any(fragment in lowered for fragment in _BANNED_PASSWORD_FRAGMENTS):
        raise ValidationError("Password contains a well-known, easily guessed sequence.")

    username = getattr(getattr(form, "username", None), "data", None)
    if username and len(username) >= 3 and username.lower() in lowered:
        raise ValidationError("Password must not contain your username.")


class RegistrationForm(FlaskForm):
    username = StringField(
        "Username",
        filters=[User.normalise_username],
        validators=[
            DataRequired(message="A username is required."),
            Length(
                min=USERNAME_MIN_LENGTH,
                max=USERNAME_MAX_LENGTH,
                message=(
                    f"Username must be between {USERNAME_MIN_LENGTH} and "
                    f"{USERNAME_MAX_LENGTH} characters."
                ),
            ),
            Regexp(
                USERNAME_PATTERN,
                message=(
                    "Username may contain letters, digits, dots, dashes and "
                    "underscores, and must start and end with a letter or digit."
                ),
            ),
        ],
    )
    email = StringField(
        "Email",
        filters=[User.normalise_email],
        validators=[
            DataRequired(message="An email address is required."),
            Length(max=255, message="Email address is too long."),
            Email(message="Enter a valid email address."),
        ],
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(message="A password is required."), strong_password],
    )
    confirm_password = PasswordField(
        "Confirm password",
        validators=[
            DataRequired(message="Please confirm your password."),
            EqualTo("password", message="The two passwords do not match."),
        ],
    )
    accept_terms = BooleanField("I accept the terms of service", default=False)

    # -- Uniqueness --------------------------------------------------------
    # WTForms calls ``validate_<field>`` automatically after that field's own
    # validator chain passes.  These are advisory: the database's UNIQUE
    # constraints remain the authority, since another request can insert the
    # same value between this SELECT and our INSERT.
    def validate_username(self, field) -> None:
        if User.by_username(field.data):
            raise ValidationError("That username is already taken.")

    def validate_email(self, field) -> None:
        if User.by_email(field.data):
            raise ValidationError("An account with that email address already exists.")


class LoginForm(FlaskForm):
    identity = StringField(
        "Username or email",
        filters=[lambda value: (value or "").strip()],
        validators=[
            DataRequired(message="Enter your username or email address."),
            Length(max=255, message="Value is too long."),
        ],
    )
    password = PasswordField(
        "Password", validators=[DataRequired(message="Enter your password.")]
    )
    remember_me = BooleanField("Remember me", default=False)
