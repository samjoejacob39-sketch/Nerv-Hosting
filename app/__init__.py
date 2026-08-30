"""Application factory.

Nothing is created at import time: ``create_app()`` builds and wires a fresh
instance, so tests, the CLI and a WSGI server can each construct one with the
configuration they need.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

__all__ = ["create_app"]


def create_app(config_name: str | None = None, overrides: Mapping[str, object] | None = None) -> Flask:
    """Build a configured application.

    ``overrides`` is applied immediately after the config class and before any
    extension is initialised, which matters: Flask-SQLAlchemy and Flask-Limiter
    read their settings during ``init_app``, so mutating ``app.config``
    afterwards silently has no effect.
    """
    # Imported here, not at module scope: ``config`` reads its Pterodactyl
    # defaults from ``app.constants``, so a top-level import either way round
    # would make ``import config`` and ``import app`` order-dependent.
    from config import INSTANCE_DIR, BaseConfig, get_config

    config_class: type[BaseConfig] = get_config(config_name)

    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__, instance_path=str(INSTANCE_DIR), instance_relative_config=True)
    app.config.from_object(config_class)
    if overrides:
        app.config.update(overrides)

    _configure_logging(app)
    config_class.init_app(app)
    _configure_proxy(app)
    _init_extensions(app)
    _register_blueprints(app)
    _register_security_headers(app)

    from app.cli import register_cli
    from app.errors import register_error_handlers
    from app.templating import register_template_helpers

    register_template_helpers(app)
    register_error_handlers(app)
    register_cli(app)

    app.logger.info(
        "VpsHosting started | config=%s | db=%s",
        app.config["CONFIG_NAME"],
        _redact_dsn(app.config["SQLALCHEMY_DATABASE_URI"]),
    )
    return app


# ---------------------------------------------------------------------- #
# Extensions
# ---------------------------------------------------------------------- #
def _init_extensions(app: Flask) -> None:
    from app.extensions import csrf, db, limiter, login_manager, migrate

    db.init_app(app)
    # ``render_as_batch`` rewrites ALTER TABLE as copy-and-swap, which is the
    # only way Alembic can alter columns on SQLite. Harmless on PostgreSQL.
    migrate.init_app(app, db, render_as_batch=True)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "warning"
    login_manager.refresh_view = "auth.login"
    # "strong" invalidates the session when the client fingerprint changes,
    # which limits the damage of a stolen session cookie.
    login_manager.session_protection = "strong"

    from app.errors import register_login_handlers
    from app.models import User

    register_login_handlers(login_manager)

    @login_manager.user_loader
    def load_user(user_id: str):
        """Resolve the session's user id.

        The id arrives as a string from the cookie and is attacker-controlled,
        so a non-numeric value must yield an anonymous session rather than a 500.
        """
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

    # SQLite ignores REFERENCES clauses unless foreign keys are switched on per
    # connection; the listener that does so lives in ``app.extensions`` and is
    # registered once at import time.


def _register_blueprints(app: Flask) -> None:
    from app.admin import admin_bp
    from app.auth import auth_bp
    from app.core import core_bp
    from app.dashboard import dashboard_bp
    from app.dashboard import routes as dashboard_routes

    app.register_blueprint(core_bp)
    app.register_blueprint(auth_bp)  # root-level: /register, /login, /logout
    app.register_blueprint(dashboard_bp)  # /dashboard/*, login required
    app.register_blueprint(admin_bp)  # /admin/*, admin required

    # Root alias mappings for server endpoints
    app.add_url_rule(
        "/server/<int:server_id>/power",
        endpoint="server_power_alias",
        view_func=dashboard_routes.power,
        methods=["POST"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/status",
        endpoint="server_status_alias",
        view_func=dashboard_routes.status,
        methods=["GET"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/share",
        endpoint="server_share_alias",
        view_func=dashboard_routes.share,
        methods=["POST"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/unshare",
        endpoint="server_unshare_alias",
        view_func=dashboard_routes.unshare,
        methods=["POST"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/guests",
        endpoint="server_guests_alias",
        view_func=dashboard_routes.guests,
        methods=["GET"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/console-token",
        endpoint="server_console_token_alias",
        view_func=dashboard_routes.console_token,
        methods=["GET"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/files",
        endpoint="server_files_alias",
        view_func=dashboard_routes.files,
        methods=["GET"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/files/content",
        endpoint="server_files_content_alias",
        view_func=dashboard_routes.file_content,
        methods=["GET"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/files/save",
        endpoint="server_files_save_alias",
        view_func=dashboard_routes.file_save,
        methods=["POST"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/backups",
        endpoint="server_backups_alias",
        view_func=dashboard_routes.backups,
        methods=["POST"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/subdomain",
        endpoint="server_subdomain_claim_alias",
        view_func=dashboard_routes.subdomain_claim,
        methods=["POST"],
    )
    app.add_url_rule(
        "/server/<int:server_id>/subdomain",
        endpoint="server_subdomain_release_alias",
        view_func=dashboard_routes.subdomain_release,
        methods=["DELETE"],
    )
    app.add_url_rule(
        "/api/credit-reward",
        endpoint="credit_reward_alias",
        view_func=dashboard_routes.credit_reward,
        methods=["POST"],
    )

    from app.scheduler import init_scheduler

    init_scheduler(app)


# ---------------------------------------------------------------------- #
# Hardening
# ---------------------------------------------------------------------- #
def _configure_proxy(app: Flask) -> None:
    """Trust ``X-Forwarded-*`` only when explicitly told how many proxies sit
    in front.  Trusting them blindly lets a client spoof its own IP and defeat
    rate limiting."""
    hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "0") or 0)
    if hops > 0:
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops, x_prefix=hops
        )


def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def apply_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


# ---------------------------------------------------------------------- #
# Logging
# ---------------------------------------------------------------------- #
def _configure_logging(app: Flask) -> None:
    if app.config.get("TESTING"):
        app.logger.setLevel(logging.CRITICAL)
        return

    level = logging.DEBUG if app.config.get("DEBUG") else logging.INFO
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)

    app.logger.handlers.clear()
    app.logger.addHandler(stream)
    app.logger.setLevel(level)
    app.logger.propagate = False

    log_path = os.environ.get("LOG_FILE")
    if log_path:
        file_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        app.logger.addHandler(file_handler)


def _redact_dsn(uri: str) -> str:
    """Strip credentials before a DSN reaches the logs."""
    if "@" not in uri or "://" not in uri:
        return uri
    scheme, rest = uri.split("://", 1)
    return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
