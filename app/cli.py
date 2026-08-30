"""Custom ``flask`` CLI commands for local development and operations."""

from __future__ import annotations

from decimal import Decimal

import click
from flask import Flask, current_app
from flask.cli import with_appcontext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Server, SharedAccess, User


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    """Create any missing tables. Safe to re-run; never drops data.

    Use this for a quick start; use ``flask db upgrade`` (Flask-Migrate) once
    the schema starts changing.
    """
    db.create_all()
    tables = sorted(inspect(db.engine).get_table_names())
    click.secho(f"Schema ready at {current_app.config['SQLALCHEMY_DATABASE_URI']}", fg="green")
    click.echo(f"Tables: {', '.join(tables) or '(none)'}")


@click.command("reset-db")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@with_appcontext
def reset_db_command(yes: bool) -> None:
    """DESTRUCTIVE: drop every table, then recreate the schema."""
    if current_app.config["CONFIG_NAME"] == "production":
        raise click.ClickException("reset-db is disabled when FLASK_CONFIG=production.")

    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    if not yes:
        click.confirm(
            f"This permanently deletes ALL data in {uri}. Continue?", abort=True
        )
    db.drop_all()
    db.create_all()
    click.secho("Database reset.", fg="yellow")


@click.command("create-user")
@click.option("--username", prompt=True)
@click.option("--email", prompt=True)
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@click.option("--credits", "credits_", default="0", help="Starting credit balance.")
@click.option("--admin", is_flag=True, help="Grant administrator rights.")
@with_appcontext
def create_user_command(
    username: str, email: str, password: str, credits_: str, admin: bool
) -> None:
    """Create an account without going through the HTTP endpoint."""
    min_length = current_app.config["MIN_PASSWORD_LENGTH"]
    if len(password) < min_length:
        raise click.ClickException(f"Password must be at least {min_length} characters.")

    user = User(
        username=User.normalise_username(username),
        email=User.normalise_email(email),
        is_admin=admin,
    )
    user.set_password(password)
    try:
        starting = Decimal(credits_)
    except Exception:
        raise click.ClickException(f"{credits_!r} is not a valid credit amount.") from None
    if starting > 0:
        user.add_credits(starting)

    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise click.ClickException("That username or email is already registered.") from None

    click.secho(
        f"Created {'admin' if admin else 'user'} {user.username} (id={user.id}) "
        f"with {user.credits} credits.",
        fg="green",
    )


@click.command("list-users")
@with_appcontext
def list_users_command() -> None:
    """Print a summary of every account."""
    users = db.session.scalars(db.select(User).order_by(User.id)).all()
    if not users:
        click.echo("No users yet.")
        return
    click.echo(f"{'ID':>4}  {'USERNAME':<20} {'CREDITS':>12}  {'SERVERS':>7}  FLAGS")
    for user in users:
        flags = ",".join(
            f for f, on in (("admin", user.is_admin), ("suspended", not user.is_active)) if on
        )
        click.echo(
            f"{user.id:>4}  {user.username:<20} {str(user.credits):>12}  "
            f"{len(user.owned_servers):>7}  {flags or '-'}"
        )


def register_cli(app: Flask) -> None:
    for command in (
        init_db_command,
        reset_db_command,
        create_user_command,
        list_users_command,
    ):
        app.cli.add_command(command)

    @app.shell_context_processor
    def shell_context() -> dict:
        """Preload the ORM in ``flask shell``."""
        return {
            "db": db,
            "User": User,
            "Server": Server,
            "SharedAccess": SharedAccess,
            "select": db.select,
        }
