"""Configuration objects for the VpsHosting backend.

Every tunable value is read from the environment (optionally populated by a
local ``.env`` file), so the exact same code runs in development, testing and
production without edits.

The default database is a SQLite file under ``instance/``.  Setting
``DATABASE_URL`` to a PostgreSQL DSN is the only change required to move to
production -- pooling options and driver normalisation are handled here.
"""

from __future__ import annotations

import os
import secrets
import warnings
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

from app.constants import (
    DEFAULT_PTERO_NODE_ID,
    DEFAULT_PTERO_OWNER_USER_ID,
    MINECRAFT_DOCKER_IMAGE,
    MINECRAFT_EGG_ID,
    MINECRAFT_NEST_ID,
    PTERO_TIMEOUT_SECONDS,
)

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"

# Loaded once, at import time, before any config class body is evaluated.
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        warnings.warn(f"{name}={raw!r} is not an integer; falling back to {default}", stacklevel=2)
        return default


def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def normalise_database_url(url: str) -> str:
    """Map ambiguous/legacy schemes onto explicit SQLAlchemy 2.x drivers.

    Managed hosts (Heroku, Railway, Render, ...) hand out ``postgres://``,
    which SQLAlchemy 2 no longer accepts, and bare ``postgresql://`` resolves
    to psycopg2.  We standardise on psycopg 3.
    """
    for legacy in ("postgres://", "postgresql://"):
        if url.startswith(legacy):
            return "postgresql+psycopg://" + url[len(legacy) :]
    return url


def _engine_options(url: str) -> dict:
    """Driver-appropriate engine options.

    SQLite gets a busy timeout so concurrent writers wait instead of raising
    ``database is locked``; networked databases get connection pooling with
    liveness checks.
    """
    if url.startswith("sqlite"):
        return {"connect_args": {"timeout": 15}}
    return {
        "pool_pre_ping": True,
        "pool_recycle": _env_int("DB_POOL_RECYCLE", 1800),
        "pool_size": _env_int("DB_POOL_SIZE", 10),
        "max_overflow": _env_int("DB_MAX_OVERFLOW", 20),
    }


_DEFAULT_SQLITE_URL = f"sqlite:///{(INSTANCE_DIR / 'vpshosting.db').as_posix()}"
_DATABASE_URL = normalise_database_url(_env_str("DATABASE_URL") or _DEFAULT_SQLITE_URL)


class BaseConfig:
    """Settings shared by every environment."""

    CONFIG_NAME = "base"

    # --- Core ------------------------------------------------------------
    SECRET_KEY = _env_str("SECRET_KEY")
    MAX_CONTENT_LENGTH = _env_int("MAX_CONTENT_LENGTH", 1 * 1024 * 1024)  # 1 MiB
    JSON_SORT_KEYS = False

    # --- SQLAlchemy ------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = _DATABASE_URL
    SQLALCHEMY_ENGINE_OPTIONS = _engine_options(_DATABASE_URL)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ECHO = _env_bool("SQLALCHEMY_ECHO", False)

    # --- Session cookies -------------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = timedelta(days=_env_int("SESSION_LIFETIME_DAYS", 7))

    # --- "Remember me" cookie (Flask-Login) ------------------------------
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_DURATION = timedelta(days=_env_int("REMEMBER_COOKIE_DAYS", 30))

    # --- CSRF (Flask-WTF) ------------------------------------------------
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None  # tie token lifetime to the session instead

    # --- Password policy -------------------------------------------------
    PASSWORD_HASH_METHOD = _env_str("PASSWORD_HASH_METHOD", "scrypt:32768:8:1")
    MIN_PASSWORD_LENGTH = _env_int("MIN_PASSWORD_LENGTH", 10)
    MAX_PASSWORD_LENGTH = _env_int("MAX_PASSWORD_LENGTH", 128)

    # --- Rate limiting ---------------------------------------------------
    RATELIMIT_ENABLED = _env_bool("RATELIMIT_ENABLED", True)
    RATELIMIT_STORAGE_URI = _env_str("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    AUTH_RATELIMIT_LOGIN = _env_str("AUTH_RATELIMIT_LOGIN", "10 per 5 minutes")
    AUTH_RATELIMIT_REGISTER = _env_str("AUTH_RATELIMIT_REGISTER", "5 per hour")

    # --- Domain defaults -------------------------------------------------
    SIGNUP_BONUS_CREDITS = _env_str("SIGNUP_BONUS_CREDITS", "0")

    # --- Pterodactyl -----------------------------------------------------
    # ``PTERO_URL`` is the panel root (no trailing ``/api``);
    # ``PTERO_APP_API_KEY`` is an *Application* key from the admin area, which
    # is what /api/application/* requires -- a client key will 403.  Both are
    # secrets-shaped: never log their values.
    PTERO_URL = _env_str("PTERO_URL") or _env_str("PTERODACTYL_URL")
    PTERO_APP_API_KEY = _env_str("PTERO_APP_API_KEY") or _env_str("PTERODACTYL_API_KEY")
    PTERO_TIMEOUT = _env_int("PTERO_TIMEOUT", PTERO_TIMEOUT_SECONDS)
    #: Verify the panel's TLS certificate.  Only ever turn this off against a
    #: self-signed lab panel, and never in production.
    PTERO_VERIFY_TLS = _env_bool("PTERO_VERIFY_TLS", True)

    # Panel topology.  Installation-specific, so the constants are only
    # fallbacks -- see app/constants.py.
    PTERO_NODE_ID = _env_int("PTERO_NODE_ID", DEFAULT_PTERO_NODE_ID)
    #: Panel-side owner of every container we provision; see app/constants.py.
    PTERO_OWNER_USER_ID = _env_int("PTERO_OWNER_USER_ID", DEFAULT_PTERO_OWNER_USER_ID)
    PTERO_MINECRAFT_NEST_ID = _env_int("PTERO_MINECRAFT_NEST_ID", MINECRAFT_NEST_ID)
    PTERO_MINECRAFT_EGG_ID = _env_int("PTERO_MINECRAFT_EGG_ID", MINECRAFT_EGG_ID)
    PTERO_MINECRAFT_IMAGE = _env_str("PTERO_MINECRAFT_IMAGE", MINECRAFT_DOCKER_IMAGE)

    #: Provisioning is expensive and irreversible-ish; keep it well below the
    #: general request rate.
    DEPLOY_RATELIMIT = _env_str("DEPLOY_RATELIMIT", "6 per hour")

    # --- Ad Rewards Webhook & Scheduling ---------------------------------
    AD_WEBHOOK_SECRET = _env_str("AD_WEBHOOK_SECRET", "ad-reward-super-secret-key")
    SCHEDULER_API_ENABLED = False
    SCHEDULER_TIMEZONE = "UTC"
    SCHEDULER_ENABLED = _env_bool("SCHEDULER_ENABLED", True)
    HOURLY_BILLING_ENABLED = _env_bool("HOURLY_BILLING_ENABLED", True)

    # --- Cloudflare DNS Integration ---------------------------------------
    CLOUDFLARE_API_TOKEN = _env_str("CLOUDFLARE_API_TOKEN", "")
    CLOUDFLARE_ZONE_ID = _env_str("CLOUDFLARE_ZONE_ID", "")
    CLOUDFLARE_DOMAIN = _env_str("CLOUDFLARE_DOMAIN", "yourdomain.com")
    DEFAULT_NODE_IP = _env_str("DEFAULT_NODE_IP", "127.0.0.1")

    @classmethod
    def init_app(cls, app) -> None:  # pragma: no cover - overridden per env
        """Hook for environment-specific wiring, called by the app factory."""


class DevelopmentConfig(BaseConfig):
    CONFIG_NAME = "development"
    DEBUG = True

    @classmethod
    def init_app(cls, app) -> None:
        if not app.config["SECRET_KEY"]:
            # Ephemeral key: sessions are invalidated on restart, which is fine
            # for local work but must never happen in production.
            app.config["SECRET_KEY"] = secrets.token_hex(32)
            app.logger.warning(
                "SECRET_KEY is unset; generated a throwaway key for this process. "
                "Add SECRET_KEY to .env to keep sessions across restarts."
            )


class TestingConfig(BaseConfig):
    CONFIG_NAME = "testing"
    TESTING = True
    DEBUG = False

    SECRET_KEY = "testing-only-not-a-secret"
    SQLALCHEMY_DATABASE_URI = "sqlite://"  # in-memory, per-process
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    SQLALCHEMY_ECHO = False

    WTF_CSRF_ENABLED = False  # tests post payloads directly
    RATELIMIT_ENABLED = False
    # Keep hashing cheap so the suite stays fast; production uses scrypt.
    PASSWORD_HASH_METHOD = "pbkdf2:sha256:10000"
    MIN_PASSWORD_LENGTH = 10
    AD_WEBHOOK_SECRET = "test-ad-webhook-secret"
    SCHEDULER_ENABLED = False
    CLOUDFLARE_API_TOKEN = "test-cf-token"
    CLOUDFLARE_ZONE_ID = "test-cf-zone-id"
    CLOUDFLARE_DOMAIN = "testdomain.test"
    DEFAULT_NODE_IP = "192.0.2.1"


class ProductionConfig(BaseConfig):
    CONFIG_NAME = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)
    REMEMBER_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)
    PREFERRED_URL_SCHEME = "https"

    @classmethod
    def init_app(cls, app) -> None:
        # Fail fast rather than silently running with a weak/absent key.
        if not app.config["SECRET_KEY"]:
            raise RuntimeError("SECRET_KEY must be set when FLASK_CONFIG=production.")
        if len(app.config["SECRET_KEY"]) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters long.")
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
            app.logger.warning(
                "Running production against SQLite. Set DATABASE_URL to a "
                "PostgreSQL DSN before taking real traffic."
            )
        if app.config["RATELIMIT_ENABLED"] and app.config["RATELIMIT_STORAGE_URI"].startswith("memory"):
            app.logger.warning(
                "Rate limits use in-process memory storage; counters are not "
                "shared between workers. Set RATELIMIT_STORAGE_URI to Redis."
            )


CONFIG_MAP: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "dev": DevelopmentConfig,
    "testing": TestingConfig,
    "test": TestingConfig,
    "production": ProductionConfig,
    "prod": ProductionConfig,
}

DEFAULT_CONFIG_NAME = "development"


def get_config(name: str | None = None) -> type[BaseConfig]:
    """Resolve a config class from an explicit name, then FLASK_CONFIG."""
    key = (name or _env_str("FLASK_CONFIG") or DEFAULT_CONFIG_NAME).lower()
    try:
        return CONFIG_MAP[key]
    except KeyError:
        raise RuntimeError(
            f"Unknown configuration {key!r}. Valid options: {sorted(CONFIG_MAP)}"
        ) from None
