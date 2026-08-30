"""Security-control coverage that the main suite disables for convenience:
CSRF enforcement, token rotation, open-redirect handling and rate limiting.
"""

from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db, limiter

PWD = "Sw1ft-Otter!42"
SIGNUP = {"username": "secuser", "email": "sec@example.com",
          "password": PWD, "confirm_password": PWD}


@pytest.fixture
def csrf_client():
    """A client with CSRF enforcement on.

    No app context is held open: ``g`` -- and the CSRF token Flask-WTF caches on
    it -- must not leak between requests, or the checks below pass vacuously.
    """
    app = create_app("testing", {"WTF_CSRF_ENABLED": True})
    with app.app_context():
        db.create_all()
    yield app.test_client()
    with app.app_context():
        db.session.remove()
        db.drop_all()


def _token(client, path="/register"):
    return client.get(path).get_json()["data"]["csrf_token"]


def test_post_without_csrf_token_is_rejected(csrf_client):
    response = csrf_client.post("/register", json=SIGNUP)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "csrf_error"


def test_post_with_csrf_token_succeeds(csrf_client):
    token = _token(csrf_client)
    response = csrf_client.post("/register", json=SIGNUP, headers={"X-CSRFToken": token})
    assert response.status_code == 201


def test_login_rotates_the_csrf_token_and_the_new_one_works(csrf_client):
    """Registering/logging in clears the session, so the client's old token is
    dead. The response must carry a usable replacement."""
    stale = _token(csrf_client)
    response = csrf_client.post("/register", json=SIGNUP, headers={"X-CSRFToken": stale})
    rotated = response.get_json()["data"]["csrf_token"]
    assert rotated and rotated != stale

    # The pre-login token must no longer be accepted...
    assert csrf_client.post("/logout", headers={"X-CSRFToken": stale}).status_code == 400
    # ...and the rotated one must be.
    assert csrf_client.post("/logout", headers={"X-CSRFToken": rotated}).status_code == 200


def test_logout_then_login_gets_a_fresh_working_token(csrf_client):
    token = _token(csrf_client)
    rotated = csrf_client.post(
        "/register", json=SIGNUP, headers={"X-CSRFToken": token}
    ).get_json()["data"]["csrf_token"]
    csrf_client.post("/logout", headers={"X-CSRFToken": rotated})

    token = _token(csrf_client, "/login")
    response = csrf_client.post(
        "/login", json={"identity": "SEC@example.com", "password": PWD},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 200, response.get_json()


def test_offsite_next_target_is_ignored(csrf_client):
    token = _token(csrf_client)
    csrf_client.post("/register", json=SIGNUP, headers={"X-CSRFToken": token})
    rotated = csrf_client.get("/csrf-token").get_json()["data"]["csrf_token"]
    csrf_client.post("/logout", headers={"X-CSRFToken": rotated})

    token = _token(csrf_client, "/login")
    response = csrf_client.post(
        "/login?next=https://evil.example.com/steal",
        json={"identity": "secuser", "password": PWD},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 200
    assert response.get_json()["next"] == "/dashboard/"


def test_login_attempts_are_rate_limited():
    """Brute-force protection: the 4th attempt in the window is refused."""
    app = create_app(
        "testing",
        {
            "RATELIMIT_ENABLED": True,
            "RATELIMIT_STORAGE_URI": "memory://",
            "AUTH_RATELIMIT_LOGIN": "3 per minute",
        },
    )
    try:
        with app.app_context():
            db.create_all()
        client = app.test_client()
        payload = {"identity": "nobody", "password": "Wr0ng-Password!"}
        codes = [client.post("/login", json=payload).status_code for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429], codes
        assert client.post("/login", json=payload).get_json()["error"]["code"] == "rate_limited"
    finally:
        # The Limiter instance is a module-level singleton; leaving it armed
        # would bleed 429s into unrelated tests.
        limiter.enabled = False
        with app.app_context():
            db.session.remove()
            db.drop_all()
