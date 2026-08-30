"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db as _db
from app.models import User

VALID_PASSWORD = "Sw1ft-Otter!42"


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def user_factory(db):
    def _make(username="tester", email=None, password=VALID_PASSWORD, **kwargs):
        user = User(
            username=User.normalise_username(username),
            email=User.normalise_email(email or f"{username}@example.com"),
            **kwargs,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user

    return _make


@pytest.fixture
def registered_user(user_factory):
    return user_factory()


def login(client, identity, password=VALID_PASSWORD):
    return client.post(
        "/login",
        json={"identity": identity, "password": password},
    )
