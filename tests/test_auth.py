"""End-to-end coverage for /register, /login, /logout and the auth wall."""

from __future__ import annotations

from tests.conftest import VALID_PASSWORD, login

GOOD_SIGNUP = {
    "username": "newcomer",
    "email": "Newcomer@Example.COM",
    "password": VALID_PASSWORD,
    "confirm_password": VALID_PASSWORD,
}


# --------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------- #
def test_register_creates_account_and_logs_in(client):
    response = client.post("/register", json=GOOD_SIGNUP)
    assert response.status_code == 201, response.get_json()

    user = response.get_json()["data"]["user"]
    assert user["username"] == "newcomer"
    assert user["email"] == "newcomer@example.com"  # normalised
    assert user["credit_balance"] == "0.0000"

    # The signup response already carries an authenticated session.
    assert client.get("/me").status_code == 200


def test_register_rejects_password_mismatch(client):
    response = client.post("/register", json={**GOOD_SIGNUP, "confirm_password": "Different!99"})
    assert response.status_code == 422
    assert "confirm_password" in response.get_json()["error"]["fields"]


def test_register_rejects_weak_password(client):
    response = client.post(
        "/register", json={**GOOD_SIGNUP, "password": "password123", "confirm_password": "password123"}
    )
    assert response.status_code == 422
    assert "password" in response.get_json()["error"]["fields"]


def test_register_rejects_duplicate_username_case_insensitively(client, registered_user):
    response = client.post(
        "/register",
        json={**GOOD_SIGNUP, "username": registered_user.username.upper()},
    )
    assert response.status_code == 422
    assert "username" in response.get_json()["error"]["fields"]


def test_register_rejects_duplicate_email(client, registered_user):
    response = client.post("/register", json={**GOOD_SIGNUP, "email": registered_user.email})
    assert response.status_code == 422
    assert "email" in response.get_json()["error"]["fields"]


def test_register_rejects_invalid_email(client):
    response = client.post("/register", json={**GOOD_SIGNUP, "email": "not-an-email"})
    assert response.status_code == 422


def test_password_is_hashed_not_stored(client, app):
    client.post("/register", json=GOOD_SIGNUP)
    from app.models import User

    user = User.by_username("newcomer")
    assert user is not None
    assert VALID_PASSWORD not in user.password_hash
    assert user.password_hash.startswith("pbkdf2:")  # testing config's method
    assert user.check_password(VALID_PASSWORD)
    assert not user.check_password(VALID_PASSWORD + "x")


# --------------------------------------------------------------------- #
# Login / logout
# --------------------------------------------------------------------- #
def test_login_with_username(client, registered_user):
    response = login(client, registered_user.username)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["user"]["id"] == registered_user.id


def test_login_with_email(client, registered_user):
    assert login(client, registered_user.email).status_code == 200


def test_login_wrong_password_is_generic(client, registered_user):
    response = login(client, registered_user.username, "Wr0ng-Password!")
    assert response.status_code == 401
    body = response.get_json()
    assert body["error"]["code"] == "invalid_credentials"
    # Must not reveal whether the account exists.
    assert body["error"]["message"] == "Incorrect credentials."


def test_login_unknown_user_matches_wrong_password_response(client):
    response = login(client, "ghost")
    assert response.status_code == 401
    assert response.get_json()["error"]["message"] == "Incorrect credentials."


def test_login_rejects_suspended_account(client, user_factory):
    user = user_factory(username="banned", is_active=False)
    response = login(client, user.username)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "account_suspended"


def test_logout_ends_session_and_requires_post(client, registered_user):
    login(client, registered_user.username)
    assert client.get("/me").status_code == 200

    assert client.get("/logout").status_code == 405  # GET must not log out
    assert client.post("/logout").status_code == 200
    assert client.get("/me").status_code == 401


def test_logout_requires_authentication(client):
    assert client.post("/logout").status_code == 401


# --------------------------------------------------------------------- #
# Route protection
# --------------------------------------------------------------------- #
def test_dashboard_blocked_for_anonymous_users(client):
    for path in ("/dashboard/", "/dashboard/credits", "/dashboard/tiers"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.get_json()["error"]["code"] == "unauthenticated"


def test_dashboard_accessible_after_login(client, registered_user):
    login(client, registered_user.username)
    body = client.get("/dashboard/").get_json()["data"]
    assert body["user"]["username"] == registered_user.username
    assert body["owned_servers"] == []
    assert body["limits"]["slots_remaining"] == body["limits"]["max_servers"]


def test_anonymous_browser_navigation_redirects_to_login(client):
    response = client.get("/dashboard/", headers={"Accept": "text/html"})
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_public_endpoints_stay_open(client):
    assert client.get("/").status_code == 200
    assert client.get("/healthz").get_json()["data"]["status"] == "ok"
