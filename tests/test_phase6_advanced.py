"""Phase 6: Advanced Features (Live Console, File Manager, Cloudflare DNS & Admin Dashboard)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import create_app
from app.constants import RamTier, ServerStatus
from app.dns_client import CloudflareDNSClient, CloudflareDNSError
from app.extensions import db as _db
from app.models import Server, SharedAccess, User
from app.ptero_client import PterodactylAPIError
from tests.conftest import VALID_PASSWORD, login


@pytest.fixture
def app():
    application = create_app(
        "testing",
        {
            "PTERO_URL": "https://panel.example.test",
            "PTERO_APP_API_KEY": "ptla_test_app_key",
            "CLOUDFLARE_API_TOKEN": "test-cf-token",
            "CLOUDFLARE_ZONE_ID": "test-cf-zone",
            "CLOUDFLARE_DOMAIN": "testdomain.test",
            "SCHEDULER_ENABLED": False,
        },
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


import itertools

_panel_counter = itertools.count(6001)


def make_provisioned_server(db, owner, name="phase6-srv", panel_id=None):
    server = Server(
        owner_id=owner.id,
        name=name,
        ram_tier=int(RamTier.BASIC),
        status=ServerStatus.RUNNING,
        pterodactyl_server_id=panel_id if panel_id is not None else next(_panel_counter),
    )
    db.session.add(server)
    db.session.commit()
    return server


# ---------------------------------------------------------------------- #
# 1. Live Console & WebSocket Token Tests
# ---------------------------------------------------------------------- #
def test_console_token_success_for_owner(client, db, user_factory):
    owner = user_factory(username="terminalowner")
    login(client, owner.username)
    server = make_provisioned_server(db, owner)

    with patch("app.dashboard.routes.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_panel.get_websocket_credentials.return_value = {
            "token": "jwt-console-token-123",
            "socket": "wss://daemon.panel.test:8080/api/servers/6001/ws",
        }
        mock_get_client.return_value.__enter__.return_value = mock_panel

        res = client.get(f"/dashboard/server/{server.id}/console-token")
        assert res.status_code == 200
        payload = res.get_json()
        assert payload["ok"] is True
        assert payload["data"]["token"] == "jwt-console-token-123"
        assert payload["data"]["socket"] == "wss://daemon.panel.test:8080/api/servers/6001/ws"
        mock_panel.get_websocket_credentials.assert_called_once_with(server.pterodactyl_server_id)


def test_console_token_success_for_shared_guest(client, db, user_factory):
    owner = user_factory(username="srvboss")
    guest = user_factory(username="srvguest")
    server = make_provisioned_server(db, owner)

    SharedAccess.grant(server, guest)
    db.session.commit()

    login(client, guest.username)

    with patch("app.dashboard.routes.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_panel.get_websocket_credentials.return_value = {
            "token": "guest-token-456",
            "socket": "wss://daemon.panel.test:8080/api/servers/6001/ws",
        }
        mock_get_client.return_value.__enter__.return_value = mock_panel

        res = client.get(f"/dashboard/server/{server.id}/console-token")
        assert res.status_code == 200
        assert res.get_json()["data"]["token"] == "guest-token-456"


def test_console_token_forbidden_for_stranger(client, db, user_factory):
    owner = user_factory(username="privowner")
    stranger = user_factory(username="privstranger")
    server = make_provisioned_server(db, owner)

    login(client, stranger.username)
    res = client.get(f"/dashboard/server/{server.id}/console-token")
    assert res.status_code == 403


# ---------------------------------------------------------------------- #
# 2. File Manager & Backups Tests
# ---------------------------------------------------------------------- #
def test_file_manager_list_and_read_files(client, db, user_factory):
    owner = user_factory(username="fileowner")
    login(client, owner.username)
    server = make_provisioned_server(db, owner)

    with patch("app.dashboard.routes.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_panel.list_files.return_value = [
            {"name": "server.properties", "is_file": True, "size": 1024},
            {"name": "world", "is_file": False, "size": 0},
        ]
        mock_panel.read_file.return_value = "difficulty=hard\nmotd=A Minecraft Server"
        mock_get_client.return_value.__enter__.return_value = mock_panel

        # Test listing files
        res1 = client.get(f"/dashboard/server/{server.id}/files?directory=/")
        assert res1.status_code == 200
        assert len(res1.get_json()["data"]["files"]) == 2

        # Test reading file
        res2 = client.get(f"/dashboard/server/{server.id}/files/content?file=server.properties")
        assert res2.status_code == 200
        assert "difficulty=hard" in res2.get_json()["data"]["content"]


def test_file_manager_save_file_and_create_backup(client, db, user_factory):
    owner = user_factory(username="saveowner")
    login(client, owner.username)
    server = make_provisioned_server(db, owner)

    with patch("app.dashboard.routes.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_panel.create_backup.return_value = {
            "uuid": "backup-uuid-789",
            "name": "manual_backup_1",
            "is_successful": True,
        }
        mock_get_client.return_value.__enter__.return_value = mock_panel

        # Test saving file
        res1 = client.post(
            f"/dashboard/server/{server.id}/files/save",
            json={"file": "server.properties", "content": "difficulty=easy"},
        )
        assert res1.status_code == 200
        assert res1.get_json()["ok"] is True
        mock_panel.save_file.assert_called_once_with(server.pterodactyl_server_id, file_path="server.properties", content="difficulty=easy")

        # Test creating backup
        res2 = client.post(f"/dashboard/server/{server.id}/backups")
        assert res2.status_code == 201
        assert res2.get_json()["data"]["backup"]["uuid"] == "backup-uuid-789"


# ---------------------------------------------------------------------- #
# 3. Cloudflare DNS Integration Tests
# ---------------------------------------------------------------------- #
def test_cloudflare_dns_client_create_and_delete():
    session = MagicMock(spec=requests.Session)
    create_response = MagicMock(spec=requests.Response)
    create_response.ok = True
    create_response.status_code = 200
    create_response.json.return_value = {
        "result": {
            "id": "cf-rec-999",
            "name": "sammjoe.testdomain.test",
            "content": "192.0.2.1",
            "type": "A",
        }
    }

    delete_response = MagicMock(spec=requests.Response)
    delete_response.ok = True
    delete_response.status_code = 200
    delete_response.json.return_value = {"result": {"id": "cf-rec-999"}}

    session.post.return_value = create_response
    session.delete.return_value = delete_response

    dns_client = CloudflareDNSClient(
        api_token="mock-token",
        zone_id="mock-zone",
        domain="testdomain.test",
        session=session,
    )

    # Test Create
    rec = dns_client.create_subdomain_record("sammjoe", "192.0.2.1")
    assert rec["id"] == "cf-rec-999"
    assert rec["full_domain"] == "sammjoe.testdomain.test"

    # Test Delete
    assert dns_client.delete_subdomain_record("cf-rec-999") is True


def test_subdomain_claim_and_release_routes(client, db, user_factory):
    owner = user_factory(username="subowner")
    login(client, owner.username)
    server = make_provisioned_server(db, owner)

    with patch("app.dashboard.routes.get_dns_client") as mock_get_dns:
        mock_dns = MagicMock()
        mock_dns.create_subdomain_record.return_value = {
            "id": "rec-12345",
            "full_domain": "sammjoe.testdomain.test",
            "name": "sammjoe",
        }
        mock_get_dns.return_value = mock_dns

        # Claim subdomain
        res1 = client.post(
            f"/dashboard/server/{server.id}/subdomain",
            json={"subdomain": "sammjoe"},
        )
        assert res1.status_code == 200
        payload1 = res1.get_json()
        assert payload1["data"]["subdomain"] == "sammjoe"
        assert payload1["data"]["record_id"] == "rec-12345"

        db.session.refresh(server)
        assert server.subdomain == "sammjoe"
        assert server.cloudflare_record_id == "rec-12345"

        # Release subdomain
        res2 = client.delete(f"/dashboard/server/{server.id}/subdomain")
        assert res2.status_code == 200
        mock_dns.delete_subdomain_record.assert_called_once_with("rec-12345")

        db.session.refresh(server)
        assert server.subdomain is None
        assert server.cloudflare_record_id is None


def test_subdomain_claim_duplicate_rejected(client, db, user_factory):
    user1 = user_factory(username="user1")
    user2 = user_factory(username="user2")

    server1 = make_provisioned_server(db, user1, name="srv1")
    server2 = make_provisioned_server(db, user2, name="srv2")

    server1.subdomain = "popular"
    db.session.commit()

    login(client, user2.username)
    res = client.post(
        f"/dashboard/server/{server2.id}/subdomain",
        json={"subdomain": "popular"},
    )
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "subdomain_taken"


# ---------------------------------------------------------------------- #
# 4. Admin Dashboard & Management Tests
# ---------------------------------------------------------------------- #
def test_admin_routes_forbidden_for_regular_users(client, user_factory):
    regular = user_factory(username="regularuser", is_admin=False)
    login(client, regular.username)

    for path in ("/admin/", "/admin/users", "/admin/servers"):
        res = client.get(path)
        assert res.status_code == 403
        assert res.get_json()["error"]["code"] == "forbidden"


def test_admin_dashboard_metrics_for_admin_user(client, db, user_factory):
    admin = user_factory(username="adminuser", is_admin=True)
    normal = user_factory(username="normaluser", is_admin=False)
    normal.add_credits("50.0000")
    db.session.commit()

    make_provisioned_server(db, normal, name="running-srv")

    login(client, admin.username)

    res = client.get("/admin/")
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["total_users"] >= 2
    assert data["active_containers"] == 1
    assert Decimal(data["total_credits"]) >= Decimal("50.0000")


def test_admin_users_search_and_credit_adjustment(client, db, user_factory):
    admin = user_factory(username="theadmin", is_admin=True)
    target = user_factory(username="targetuser", is_admin=False)

    login(client, admin.username)

    # Search user
    res1 = client.get("/admin/users?q=target")
    assert res1.status_code == 200
    assert len(res1.get_json()["data"]["users"]) == 1

    # Add credits
    res2 = client.post(
        f"/admin/users/{target.id}/credits",
        json={"amount": 25.5, "action": "add"},
    )
    assert res2.status_code == 200
    assert res2.get_json()["data"]["credit_balance"] == "25.5000"

    # Deduct credits
    res3 = client.post(
        f"/admin/users/{target.id}/credits",
        json={"amount": 10.0, "action": "deduct"},
    )
    assert res3.status_code == 200
    assert res3.get_json()["data"]["credit_balance"] == "15.5000"


def test_admin_user_suspension_toggle(client, db, user_factory):
    admin = user_factory(username="superadmin", is_admin=True)
    bad_actor = user_factory(username="spammer", is_admin=False)

    login(client, admin.username)

    # Suspend user
    res1 = client.post(f"/admin/users/{bad_actor.id}/suspend")
    assert res1.status_code == 200
    assert res1.get_json()["data"]["is_active"] is False

    db.session.refresh(bad_actor)
    assert bad_actor.is_active is False

    # Unsuspend user
    res2 = client.post(f"/admin/users/{bad_actor.id}/suspend")
    assert res2.status_code == 200
    assert res2.get_json()["data"]["is_active"] is True

    # Admin self-suspension prevented
    res3 = client.post(f"/admin/users/{admin.id}/suspend")
    assert res3.status_code == 400
