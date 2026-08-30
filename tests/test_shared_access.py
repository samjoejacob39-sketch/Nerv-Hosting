"""Phase 4: Shared access and collaboration permissions tests."""

from __future__ import annotations

import pytest

from app import create_app
from app.constants import MAX_GUESTS_PER_SERVER, RamTier, ServerStatus
from app.extensions import db as _db
from app.models import Server, SharedAccess, User
from tests.conftest import VALID_PASSWORD, login


@pytest.fixture
def app():
    application = create_app(
        "testing",
        {
            "PTERO_URL": "https://panel.example.test",
            "PTERO_APP_API_KEY": "ptla_test_app_key",
        },
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


def make_server(db, owner, name="collab-server"):
    server = Server(
        owner_id=owner.id,
        name=name,
        ram_tier=int(RamTier.BASIC),
        pterodactyl_server_id=5555,
        status=ServerStatus.STOPPED,
    )
    db.session.add(server)
    db.session.commit()
    return server


# ---------------------------------------------------------------------- #
# Sharing Endpoint (/share)
# ---------------------------------------------------------------------- #
def test_owner_can_share_server_with_valid_user(client, db, user_factory):
    owner = user_factory(username="shareowner")
    friend = user_factory(username="frienduser")
    server = make_server(db, owner)

    login(client, owner.username)

    res = client.post(
        f"/dashboard/server/{server.id}/share",
        json={"username": friend.username},
    )

    assert res.status_code == 201
    payload = res.get_json()
    assert payload["ok"] is True
    assert payload["data"]["grant"]["guest"]["username"] == friend.username

    assert SharedAccess.exists(server.id, friend.id) is True
    assert friend.can_access(server) is True


def test_non_owner_cannot_share_server(client, db, user_factory):
    owner = user_factory(username="realowner")
    guest = user_factory(username="firstguest")
    stranger = user_factory(username="strangeruser")
    server = make_server(db, owner)

    SharedAccess.grant(server, guest)
    db.session.commit()

    # Guest tries to invite stranger
    login(client, guest.username)
    res = client.post(
        f"/dashboard/server/{server.id}/share",
        json={"username": stranger.username},
    )
    assert res.status_code == 403
    assert res.get_json()["error"]["code"] == "forbidden"

    # Stranger tries to invite themselves or someone else
    client.post("/logout")
    login(client, stranger.username)
    res2 = client.post(
        f"/dashboard/server/{server.id}/share",
        json={"username": stranger.username},
    )
    assert res2.status_code == 403


def test_share_with_nonexistent_user_returns_404(client, db, user_factory):
    owner = user_factory(username="ghostowner")
    server = make_server(db, owner)
    login(client, owner.username)

    res = client.post(
        f"/dashboard/server/{server.id}/share",
        json={"username": "nonexistent_ghost_99"},
    )
    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "user_not_found"


def test_self_share_rejected(client, db, user_factory):
    owner = user_factory(username="selfshareowner")
    server = make_server(db, owner)
    login(client, owner.username)

    res = client.post(
        f"/dashboard/server/{server.id}/share",
        json={"username": owner.username},
    )
    assert res.status_code == 422
    assert res.get_json()["error"]["code"] == "self_share"


def test_duplicate_share_returns_409(client, db, user_factory):
    owner = user_factory(username="dupowner")
    friend = user_factory(username="dupfriend")
    server = make_server(db, owner)

    SharedAccess.grant(server, friend)
    db.session.commit()

    login(client, owner.username)
    res = client.post(
        f"/dashboard/server/{server.id}/share",
        json={"username": friend.username},
    )
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "duplicate_share"


def test_max_guests_limit_returns_409(client, db, user_factory):
    owner = user_factory(username="busyowner")
    server = make_server(db, owner)

    for i in range(MAX_GUESTS_PER_SERVER):
        guest = user_factory(username=f"guest_{i}")
        SharedAccess.grant(server, guest)
    db.session.commit()

    extra_guest = user_factory(username="extra_guest")
    login(client, owner.username)

    res = client.post(
        f"/dashboard/server/{server.id}/share",
        json={"username": extra_guest.username},
    )
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "max_guests_reached"


# ---------------------------------------------------------------------- #
# Unsharing Endpoint (/unshare)
# ---------------------------------------------------------------------- #
def test_owner_can_unshare_by_user_id(client, db, user_factory):
    owner = user_factory(username="unshareowner")
    guest = user_factory(username="unshareguest")
    server = make_server(db, owner)

    SharedAccess.grant(server, guest)
    db.session.commit()
    assert guest.can_access(server) is True

    login(client, owner.username)
    res = client.post(
        f"/dashboard/server/{server.id}/unshare",
        json={"user_id": guest.id},
    )
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    assert SharedAccess.exists(server.id, guest.id) is False
    assert guest.can_access(server) is False


def test_owner_can_unshare_by_username(client, db, user_factory):
    owner = user_factory(username="nameunshareowner")
    guest = user_factory(username="nameunshareguest")
    server = make_server(db, owner)

    SharedAccess.grant(server, guest)
    db.session.commit()

    login(client, owner.username)
    res = client.post(
        f"/dashboard/server/{server.id}/unshare",
        json={"username": guest.username},
    )
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert SharedAccess.exists(server.id, guest.id) is False


def test_unshare_nonexistent_grant_returns_404(client, db, user_factory):
    owner = user_factory(username="notsharedowner")
    other = user_factory(username="notshareduser")
    server = make_server(db, owner)

    login(client, owner.username)
    res = client.post(
        f"/dashboard/server/{server.id}/unshare",
        json={"user_id": other.id},
    )
    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "grant_not_found"


def test_non_owner_cannot_unshare(client, db, user_factory):
    owner = user_factory(username="bossowner")
    guest = user_factory(username="subguest")
    server = make_server(db, owner)

    SharedAccess.grant(server, guest)
    db.session.commit()

    login(client, guest.username)
    res = client.post(
        f"/dashboard/server/{server.id}/unshare",
        json={"user_id": guest.id},
    )
    assert res.status_code == 403


def test_revoked_guest_loses_power_control_access(client, db, user_factory):
    owner = user_factory(username="revokerowner")
    guest = user_factory(username="kickedguest")
    server = make_server(db, owner)

    SharedAccess.grant(server, guest)
    db.session.commit()

    # Owner revokes access
    login(client, owner.username)
    res = client.post(
        f"/dashboard/server/{server.id}/unshare",
        json={"user_id": guest.id},
    )
    assert res.status_code == 200

    # Kicked guest now attempts power signal
    client.post("/logout")
    login(client, guest.username)
    power_res = client.post(
        f"/dashboard/server/{server.id}/power",
        json={"signal": "start"},
    )
    assert power_res.status_code == 403


def test_guests_listing_endpoint(client, db, user_factory):
    owner = user_factory(username="listowner")
    guest = user_factory(username="listguest")
    server = make_server(db, owner)

    SharedAccess.grant(server, guest)
    db.session.commit()

    login(client, owner.username)
    res = client.get(f"/dashboard/server/{server.id}/guests")
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["ok"] is True
    assert len(payload["data"]["guests"]) == 1
    assert payload["data"]["guests"][0]["guest"]["username"] == guest.username
