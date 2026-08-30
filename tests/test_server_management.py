"""Phase 4: Server management, power controls and status monitoring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from app import create_app
from app.constants import PowerSignal, RamTier, ServerStatus
from app.extensions import db as _db
from app.models import Server, SharedAccess, User
from app.ptero_client import (
    PterodactylAPIError,
    PterodactylClient,
    get_client,
)
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


def make_provisioned_server(db, owner, name="mc-server", panel_id=1001, status=ServerStatus.STOPPED):
    server = Server(
        owner_id=owner.id,
        name=name,
        ram_tier=int(RamTier.BASIC),
        pterodactyl_server_id=panel_id,
        status=status,
    )
    db.session.add(server)
    db.session.commit()
    return server


# ---------------------------------------------------------------------- #
# Client API Unit Tests
# ---------------------------------------------------------------------- #
def test_send_power_signal_calls_client_api_endpoint():
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 204
    response.content = b""
    session.request.return_value = response

    client = PterodactylClient(
        base_url="https://panel.example.test",
        api_key="secret",
        session=session,
    )

    client.send_power_signal(42, "start")

    session.request.assert_called_once_with(
        "POST",
        "https://panel.example.test/api/client/servers/42/power",
        json={"signal": "start"},
        params=None,
        headers={
            "Authorization": "Bearer secret",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=10.0,
        verify=True,
    )


def test_send_power_signal_rejects_invalid_signal():
    client = PterodactylClient(
        base_url="https://panel.example.test",
        api_key="secret",
        session=MagicMock(),
    )
    with pytest.raises(ValueError, match="Invalid power signal"):
        client.send_power_signal(42, "explode")


def test_get_server_status_calls_client_resources_endpoint():
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.content = b'{"object":"stats","attributes":{"current_state":"running","is_suspended":false}}'
    response.json.return_value = {
        "object": "stats",
        "attributes": {"current_state": "running", "is_suspended": False},
    }
    session.request.return_value = response

    client = PterodactylClient(
        base_url="https://panel.example.test",
        api_key="secret",
        session=session,
    )

    status = client.get_server_status(42)
    assert status["current_state"] == "running"
    assert status["is_suspended"] is False

    session.request.assert_called_once_with(
        "GET",
        "https://panel.example.test/api/client/servers/42/resources",
        json=None,
        params=None,
        headers={
            "Authorization": "Bearer secret",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=10.0,
        verify=True,
    )


# ---------------------------------------------------------------------- #
# Power Control Routes
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("signal", "expected_resulting_status"),
    [
        ("start", ServerStatus.STARTING),
        ("restart", ServerStatus.STARTING),
        ("stop", ServerStatus.STOPPING),
        ("kill", ServerStatus.STOPPED),
    ],
)
def test_owner_can_send_power_signals(client, db, user_factory, signal, expected_resulting_status):
    owner = user_factory(username="owner1")
    login(client, owner.username)
    server = make_provisioned_server(db, owner, name=f"server-{signal}")

    with patch("app.dashboard.routes.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_get_client.return_value.__enter__.return_value = mock_panel

        res = client.post(
            f"/dashboard/server/{server.id}/power",
            json={"signal": signal},
        )

        assert res.status_code == 200
        payload = res.get_json()
        assert payload["ok"] is True
        assert payload["data"]["signal"] == signal
        assert payload["data"]["status"] == expected_resulting_status
        mock_panel.send_power_signal.assert_called_once_with(server.pterodactyl_server_id, signal)

    db.session.refresh(server)
    assert server.status == expected_resulting_status


def test_shared_guest_can_send_power_signals(client, db, user_factory):
    owner = user_factory(username="srvowner")
    guest = user_factory(username="guestfriend")
    server = make_provisioned_server(db, owner, name="co-op-srv", status=ServerStatus.STOPPED)

    SharedAccess.grant(server, guest)
    db.session.commit()

    login(client, guest.username)

    with patch("app.dashboard.routes.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_get_client.return_value.__enter__.return_value = mock_panel

        res = client.post(
            f"/dashboard/server/{server.id}/power",
            json={"signal": "start"},
        )
        assert res.status_code == 200
        payload = res.get_json()
        assert payload["ok"] is True
        assert payload["data"]["status"] == ServerStatus.STARTING
        mock_panel.send_power_signal.assert_called_once_with(server.pterodactyl_server_id, "start")

    db.session.refresh(server)
    assert server.status == ServerStatus.STARTING


def test_unauthorized_user_gets_403(client, db, user_factory):
    owner = user_factory(username="legitowner")
    stranger = user_factory(username="stranger")
    server = make_provisioned_server(db, owner, name="private-srv")

    login(client, stranger.username)

    res = client.post(
        f"/dashboard/server/{server.id}/power",
        json={"signal": "start"},
    )
    assert res.status_code == 403
    payload = res.get_json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "forbidden"


def test_root_alias_route_works(client, db, user_factory):
    owner = user_factory(username="aliasuser")
    login(client, owner.username)
    server = make_provisioned_server(db, owner, name="alias-srv")

    with patch("app.dashboard.routes.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_get_client.return_value.__enter__.return_value = mock_panel

        res = client.post(
            f"/server/{server.id}/power",
            json={"signal": "start"},
        )
        assert res.status_code == 200
        assert res.get_json()["ok"] is True


def test_nonexistent_server_returns_404(client, user_factory):
    user = user_factory(username="lonelyuser")
    login(client, user.username)

    res = client.post(
        "/dashboard/server/99999/power",
        json={"signal": "start"},
    )
    assert res.status_code == 404
    assert res.get_json()["ok"] is False


def test_unprovisioned_server_returns_400(client, db, user_factory):
    owner = user_factory(username="unprovowner")
    login(client, owner.username)

    server = Server(
        owner_id=owner.id,
        name="not-ready",
        ram_tier=int(RamTier.BASIC),
        pterodactyl_server_id=None,
        status=ServerStatus.PENDING,
    )
    db.session.add(server)
    db.session.commit()

    res = client.post(
        f"/dashboard/server/{server.id}/power",
        json={"signal": "start"},
    )
    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "server_not_provisioned"


def test_invalid_power_signal_returns_422(client, db, user_factory):
    owner = user_factory(username="badsignaluser")
    login(client, owner.username)
    server = make_provisioned_server(db, owner)

    res = client.post(
        f"/dashboard/server/{server.id}/power",
        json={"signal": "destroy_everything"},
    )
    assert res.status_code == 422
    assert res.get_json()["error"]["code"] == "invalid_signal"


def test_panel_error_bubbles_appropriate_status(client, db, user_factory):
    owner = user_factory(username="panelerruser")
    login(client, owner.username)
    server = make_provisioned_server(db, owner)

    with patch("app.dashboard.routes.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_panel.send_power_signal.side_effect = PterodactylAPIError(
            "Node is offline", status_code=502
        )
        mock_get_client.return_value.__enter__.return_value = mock_panel

        res = client.post(
            f"/dashboard/server/{server.id}/power",
            json={"signal": "start"},
        )
        assert res.status_code == 502
        assert res.get_json()["ok"] is False


def test_get_server_status_polling_reconciles_database_state(client, db, user_factory):
    owner = user_factory(username="polluser")
    login(client, owner.username)
    server = make_provisioned_server(db, owner, status=ServerStatus.STARTING)

    with patch("app.dashboard.routes.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_panel.get_server_status.return_value = {
            "current_state": "running",
            "resources": {"memory_bytes": 1048576},
        }
        mock_get_client.return_value.__enter__.return_value = mock_panel

        res = client.get(f"/dashboard/server/{server.id}/status")
        assert res.status_code == 200
        payload = res.get_json()
        assert payload["ok"] is True
        assert payload["data"]["status"] == ServerStatus.RUNNING

    db.session.refresh(server)
    assert server.status == ServerStatus.RUNNING
