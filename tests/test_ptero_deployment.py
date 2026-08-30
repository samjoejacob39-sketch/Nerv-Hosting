"""Pterodactyl client and ``POST /dashboard/deploy``.

The panel is never contacted: every test either injects a fake
``requests.Session`` into :class:`PterodactylClient` or patches the ``get_client``
seam the route imports, so the suite is offline and deterministic.

What matters here is the money.  A deploy debits credits *before* the outbound
call, so each failure mode is asserted twice: the status code the caller sees,
and the balance left behind.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import create_app
from app.constants import MAX_SERVERS_PER_USER, RamTier, ServerStatus, ServerType
from app.extensions import db as _db
from app.models import Server, User
from app.ptero_client import (
    NoAllocationAvailableError,
    PterodactylAPIError,
    PterodactylClient,
    PterodactylConfigurationError,
    PterodactylConnectionError,
    PterodactylTimeoutError,
    get_client,
)
from tests.conftest import VALID_PASSWORD, login

HTML = {"Accept": "text/html"}

PANEL_SERVER_ID = 4242
ALLOCATION = {"id": 7, "ip": "10.20.30.40", "port": 25565, "alias": None, "assigned": False}
CREATED = {
    "id": PANEL_SERVER_ID,
    "identifier": "1a2b3c4d",
    "uuid": "4f0c1b0e-0000-4000-8000-000000000000",
    "name": "Survival One",
}


@pytest.fixture
def app():
    """Override the shared fixture: these tests need a configured panel.

    ``TestingConfig`` deliberately leaves ``PTERO_URL`` unset, which is what makes
    the "not configured" case testable elsewhere.
    """
    application = create_app(
        "testing",
        {
            "PTERO_URL": "https://panel.example.test",
            "PTERO_APP_API_KEY": "ptla_not_a_real_key",
            "PTERO_NODE_ID": 1,
            "PTERO_OWNER_USER_ID": 1,
            "PTERO_MINECRAFT_NEST_ID": 1,
            "PTERO_MINECRAFT_EGG_ID": 3,
        },
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


# ---------------------------------------------------------------------- #
# Fakes
# ---------------------------------------------------------------------- #
def fake_response(status: int = 200, payload: dict | None = None):
    """A stand-in for ``requests.Response`` with just what ``_request`` reads."""
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.content = b"{}" if payload is not None else b""
    response.json.return_value = payload if payload is not None else {}
    return response


def fake_session(*responses):
    """A ``requests.Session`` that replays ``responses`` in order.

    Pass an exception class or instance to have that call raise instead.
    """
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = list(responses)
    return session


def allocations_page(entries: list[dict], *, total_pages: int = 1) -> dict:
    """The panel's paginated allocation envelope."""
    return {
        "object": "list",
        "data": [{"object": "allocation", "attributes": entry} for entry in entries],
        "meta": {"pagination": {"total_pages": total_pages}},
    }


def fake_panel(*, allocation: dict | None = None, created: dict | None = None) -> MagicMock:
    """A client mock that also works as the route's context manager."""
    panel = MagicMock()
    panel.__enter__.return_value = panel
    panel.__exit__.return_value = False
    panel.get_free_allocation.return_value = dict(allocation or ALLOCATION)
    panel.create_server.return_value = dict(created or CREATED)
    return panel


@pytest.fixture
def funded_user(user_factory):
    """A signed-up account with enough credits for any single tier."""
    user = user_factory(username="deployer")
    user.add_credits("100")
    _db.session.commit()
    return user


@pytest.fixture
def deployer(client, funded_user):
    """``client`` with ``funded_user`` signed in."""
    assert login(client, funded_user.username).status_code == 200
    return client


# ---------------------------------------------------------------------- #
# Client: configuration
# ---------------------------------------------------------------------- #
class TestClientConfiguration:
    def test_reads_url_and_key_from_config(self, app):
        panel = PterodactylClient(session=MagicMock(spec=requests.Session))
        assert panel.base_url == "https://panel.example.test"
        assert panel.timeout == 10

    def test_missing_key_is_a_configuration_error(self, app):
        app.config["PTERO_APP_API_KEY"] = ""
        with pytest.raises(PterodactylConfigurationError):
            get_client()

    def test_url_without_scheme_is_rejected(self, app):
        with pytest.raises(PterodactylConfigurationError):
            PterodactylClient(base_url="panel.example.test", api_key="ptla_x")

    def test_trailing_slash_does_not_double_up(self, app):
        panel = PterodactylClient(
            base_url="https://panel.example.test/", api_key="ptla_x"
        )
        assert panel._url("servers") == "https://panel.example.test/api/application/servers"

    def test_repr_never_leaks_the_key(self, app):
        panel = PterodactylClient(api_key="ptla_super_secret")
        assert "ptla_super_secret" not in repr(panel)

    def test_key_is_sent_as_a_bearer_token(self, app):
        session = fake_session(fake_response(200, {"attributes": {"id": 1}}))
        PterodactylClient(api_key="ptla_abc", session=session).get_server(1)
        headers = session.request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer ptla_abc"
        assert headers["Accept"] == "application/json"


# ---------------------------------------------------------------------- #
# Client: allocations
# ---------------------------------------------------------------------- #
class TestAllocations:
    def test_returns_the_first_unassigned_port(self, app):
        session = fake_session(
            fake_response(
                200,
                allocations_page(
                    [
                        {"id": 1, "ip": "10.0.0.1", "port": 25565, "assigned": True},
                        {"id": 2, "ip": "10.0.0.1", "port": 25566, "assigned": False},
                    ]
                ),
            )
        )
        allocation = PterodactylClient(session=session).get_free_allocation(1)
        assert allocation["id"] == 2
        assert allocation["port"] == 25566

    def test_walks_pagination_until_it_finds_one(self, app):
        session = fake_session(
            fake_response(
                200,
                allocations_page(
                    [{"id": 1, "port": 25565, "assigned": True}], total_pages=2
                ),
            ),
            fake_response(
                200,
                allocations_page(
                    [{"id": 9, "port": 25600, "assigned": False}], total_pages=2
                ),
            ),
        )
        allocation = PterodactylClient(session=session).get_free_allocation(3)
        assert allocation["id"] == 9
        assert session.request.call_count == 2
        first, second = session.request.call_args_list
        assert first.kwargs["params"]["page"] == 1
        assert second.kwargs["params"]["page"] == 2
        assert first.args[1].endswith("/api/application/nodes/3/allocations")

    def test_a_full_node_raises_no_allocation_available(self, app):
        session = fake_session(
            fake_response(
                200, allocations_page([{"id": 1, "port": 25565, "assigned": True}])
            )
        )
        with pytest.raises(NoAllocationAvailableError):
            PterodactylClient(session=session).get_free_allocation(1)

    def test_an_empty_node_raises_too(self, app):
        session = fake_session(fake_response(200, allocations_page([])))
        with pytest.raises(NoAllocationAvailableError) as excinfo:
            PterodactylClient(session=session).get_free_allocation(2)
        assert excinfo.value.http_status == 503


# ---------------------------------------------------------------------- #
# Client: failure mapping
# ---------------------------------------------------------------------- #
class TestClientFailures:
    def test_timeout_becomes_a_timeout_error(self, app):
        session = fake_session(requests.Timeout("too slow"))
        with pytest.raises(PterodactylTimeoutError) as excinfo:
            PterodactylClient(session=session).get_server(1)
        assert excinfo.value.http_status == 502
        assert excinfo.value.code == "pterodactyl_timeout"

    def test_socket_failure_becomes_a_connection_error(self, app):
        session = fake_session(requests.ConnectionError("no route to host"))
        with pytest.raises(PterodactylConnectionError):
            PterodactylClient(session=session).get_server(1)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500, 503])
    def test_any_4xx_or_5xx_raises(self, app, status):
        session = fake_session(fake_response(status, {"errors": []}))
        with pytest.raises(PterodactylAPIError) as excinfo:
            PterodactylClient(session=session).get_server(1)
        assert excinfo.value.status_code == status

    def test_panel_error_details_are_kept_for_the_log(self, app):
        session = fake_session(
            fake_response(
                422,
                {
                    "errors": [
                        {"code": "ValidationException", "detail": "The egg field is required."}
                    ]
                },
            )
        )
        with pytest.raises(PterodactylAPIError) as excinfo:
            PterodactylClient(session=session).get_server(1)
        assert excinfo.value.details == ["The egg field is required."]
        assert "The egg field is required." in str(excinfo.value)

    def test_a_non_json_body_still_raises_our_error(self, app):
        response = MagicMock(spec=requests.Response)
        response.status_code = 500
        response.content = b"<html>502 Bad Gateway</html>"
        response.json.side_effect = ValueError("not json")
        with pytest.raises(PterodactylAPIError) as excinfo:
            PterodactylClient(session=fake_session(response)).get_server(1)
        assert excinfo.value.errors == []

    def test_the_configured_timeout_is_passed_to_requests(self, app):
        session = fake_session(fake_response(200, {"attributes": {"id": 1}}))
        PterodactylClient(session=session, timeout=10).get_server(1)
        assert session.request.call_args.kwargs["timeout"] == 10


# ---------------------------------------------------------------------- #
# Client: create / delete
# ---------------------------------------------------------------------- #
class TestCreateServer:
    def _create(self, session):
        return PterodactylClient(session=session).create_server(
            user_id=1,
            name="Survival One",
            memory_mb=4096,
            disk_mb=20480,
            cpu_limit=150,
            nest_id=1,
            egg_id=3,
            allocation_id=7,
        )

    def test_posts_the_limits_and_allocation(self, app):
        session = fake_session(fake_response(201, {"attributes": CREATED}))
        attributes = self._create(session)

        assert attributes["id"] == PANEL_SERVER_ID
        call = session.request.call_args
        assert call.args[0] == "POST"
        assert call.args[1].endswith("/api/application/servers")

        body = call.kwargs["json"]
        assert body["user"] == 1
        assert body["nest"] == 1
        assert body["egg"] == 3
        assert body["allocation"] == {"default": 7}
        assert body["limits"]["memory"] == 4096
        assert body["limits"]["disk"] == 20480
        assert body["limits"]["cpu"] == 150
        assert body["limits"]["swap"] == 0
        assert body["feature_limits"]["allocations"] == 1
        assert body["environment"]["SERVER_JARFILE"] == "server.jar"
        assert body["start_on_completion"] is True

    def test_a_response_without_an_id_is_an_error(self, app):
        session = fake_session(fake_response(201, {"attributes": {"identifier": "x"}}))
        with pytest.raises(Exception) as excinfo:
            self._create(session)
        assert "no id" in str(excinfo.value)

    def test_a_response_without_attributes_is_an_error(self, app):
        session = fake_session(fake_response(201, {"data": []}))
        with pytest.raises(Exception) as excinfo:
            self._create(session)
        assert "attributes" in str(excinfo.value)

    def test_force_delete_hits_the_force_endpoint(self, app):
        session = fake_session(fake_response(204))
        PterodactylClient(session=session).delete_server(PANEL_SERVER_ID, force=True)
        assert session.request.call_args.args[1].endswith(f"servers/{PANEL_SERVER_ID}/force")


# ---------------------------------------------------------------------- #
# POST /dashboard/deploy -- the happy path
# ---------------------------------------------------------------------- #
def deploy(client, *, name="Survival One", tier=2, **kwargs):
    return client.post(
        "/dashboard/deploy", json={"server_name": name, "ram_tier": tier}, **kwargs
    )


class TestDeploySuccess:
    def test_charges_provisions_and_records(self, deployer, funded_user):
        panel = fake_panel()
        with patch("app.dashboard.routes.get_client", return_value=panel) as factory:
            response = deploy(deployer, tier=4)

        assert response.status_code == 201
        payload = response.get_json()
        assert payload["ok"] is True
        assert payload["data"]["charged"] == "45.0000"
        assert payload["data"]["credit_balance"] == "55.0000"
        assert payload["data"]["allocation"]["port"] == 25565

        server = payload["data"]["server"]
        assert server["pterodactyl_server_id"] == PANEL_SERVER_ID
        assert server["server_type"] == ServerType.MINECRAFT
        assert server["status"] == ServerStatus.RUNNING
        assert server["ram_tier"] == 4

        factory.assert_called_once_with()
        assert _db.session.get(User, funded_user.id).credits == Decimal("55.0000")

        rows = Server.for_owner(funded_user.id)
        assert len(rows) == 1
        assert rows[0].pterodactyl_server_id == PANEL_SERVER_ID
        assert rows[0].server_type == ServerType.MINECRAFT

    def test_passes_the_tier_specs_through_to_the_panel(self, deployer, app):
        panel = fake_panel()
        with patch("app.dashboard.routes.get_client", return_value=panel):
            assert deploy(deployer, tier=8).status_code == 201

        panel.get_free_allocation.assert_called_once_with(app.config["PTERO_NODE_ID"])
        kwargs = panel.create_server.call_args.kwargs
        tier = RamTier(8)
        assert kwargs["memory_mb"] == tier.ram_mb == 8192
        assert kwargs["disk_mb"] == tier.disk_mb
        assert kwargs["cpu_limit"] == tier.cpu_percent
        assert kwargs["nest_id"] == app.config["PTERO_MINECRAFT_NEST_ID"]
        assert kwargs["egg_id"] == app.config["PTERO_MINECRAFT_EGG_ID"]
        assert kwargs["user_id"] == app.config["PTERO_OWNER_USER_ID"]
        assert kwargs["allocation_id"] == ALLOCATION["id"]
        assert kwargs["name"] == "Survival One"

    def test_a_browser_post_redirects_to_the_dashboard(self, deployer):
        with patch("app.dashboard.routes.get_client", return_value=fake_panel()):
            response = deployer.post(
                "/dashboard/deploy",
                data={"server_name": "Creative Flat", "ram_tier": "2"},
                headers=HTML,
            )
        assert response.status_code == 302
        assert response.headers["Location"] == "/dashboard/"


# ---------------------------------------------------------------------- #
# POST /dashboard/deploy -- refusals, before any money moves
# ---------------------------------------------------------------------- #
class TestDeployRefusals:
    def test_anonymous_callers_are_turned_away(self, client):
        with patch("app.dashboard.routes.get_client") as factory:
            response = deploy(client)
        assert response.status_code == 401
        factory.assert_not_called()

    def test_insufficient_credits_is_402_and_charges_nothing(self, client, user_factory):
        user = user_factory(username="skint")
        user.add_credits("10")
        _db.session.commit()
        login(client, "skint")

        with patch("app.dashboard.routes.get_client") as factory:
            response = deploy(client, tier=2)

        assert response.status_code == 402
        assert response.get_json()["error"]["code"] == "insufficient_funds"
        factory.assert_not_called()
        assert _db.session.get(User, user.id).credits == Decimal("10.0000")
        assert Server.for_owner(user.id) == []

    @pytest.mark.parametrize("tier", [1, 3, 16, 0, -2, "large", None])
    def test_only_the_minecraft_tiers_are_accepted(self, deployer, funded_user, tier):
        with patch("app.dashboard.routes.get_client") as factory:
            response = deploy(deployer, tier=tier)

        assert response.status_code == 422
        assert "ram_tier" in response.get_json()["error"]["fields"]
        factory.assert_not_called()
        assert _db.session.get(User, funded_user.id).credits == Decimal("100.0000")

    @pytest.mark.parametrize("name", ["", "x", "  ", "bad/name", "-leading", "a" * 60])
    def test_a_malformed_name_is_rejected(self, deployer, funded_user, name):
        with patch("app.dashboard.routes.get_client") as factory:
            response = deploy(deployer, name=name)

        assert response.status_code == 422
        assert "server_name" in response.get_json()["error"]["fields"]
        factory.assert_not_called()
        assert _db.session.get(User, funded_user.id).credits == Decimal("100.0000")

    def test_a_duplicate_name_is_rejected_before_charging(self, deployer, funded_user):
        _db.session.add(
            Server(owner_id=funded_user.id, name="Survival One", ram_tier=2)
        )
        _db.session.commit()

        with patch("app.dashboard.routes.get_client") as factory:
            response = deploy(deployer, name="Survival One")

        assert response.status_code == 422
        assert "server_name" in response.get_json()["error"]["fields"]
        factory.assert_not_called()
        assert _db.session.get(User, funded_user.id).credits == Decimal("100.0000")

    def test_the_slot_limit_is_enforced(self, deployer, funded_user):
        for index in range(MAX_SERVERS_PER_USER):
            _db.session.add(
                Server(owner_id=funded_user.id, name=f"Existing {index}", ram_tier=2)
            )
        _db.session.commit()

        with patch("app.dashboard.routes.get_client") as factory:
            response = deploy(deployer, name="One Too Many")

        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "server_limit_reached"
        factory.assert_not_called()
        assert _db.session.get(User, funded_user.id).credits == Decimal("100.0000")

    def test_a_browser_refusal_re_renders_the_dashboard(self, client, user_factory):
        user_factory(username="skint")
        login(client, "skint")

        response = client.post(
            "/dashboard/deploy",
            data={"server_name": "Survival One", "ram_tier": "8"},
            headers=HTML,
        )

        assert response.status_code == 402
        body = response.get_data(as_text=True)
        assert "credits" in body
        assert 'action="/dashboard/deploy"' in body


# ---------------------------------------------------------------------- #
# POST /dashboard/deploy -- rollback after the money has moved
# ---------------------------------------------------------------------- #
class TestDeployRollback:
    """Every case here debits first, so every case must refund."""

    def _panel_that_fails(self, exception, *, at="create_server"):
        panel = fake_panel()
        getattr(panel, at).side_effect = exception
        return panel

    @pytest.mark.parametrize(
        ("exception", "status", "code"),
        [
            (
                PterodactylAPIError("boom", status_code=500),
                502,
                "pterodactyl_api_error",
            ),
            (PterodactylTimeoutError("too slow"), 502, "pterodactyl_timeout"),
            (PterodactylConnectionError("no route"), 502, "pterodactyl_unreachable"),
            (NoAllocationAvailableError("node full"), 503, "no_allocation_available"),
        ],
    )
    def test_a_panel_failure_refunds_and_reports_upstream(
        self, deployer, funded_user, exception, status, code
    ):
        panel = self._panel_that_fails(exception)
        with patch("app.dashboard.routes.get_client", return_value=panel):
            response = deploy(deployer, tier=6)

        assert response.status_code == status
        assert response.get_json()["error"]["code"] == code
        # 65 credits were taken and given back.
        assert _db.session.get(User, funded_user.id).credits == Decimal("100.0000")
        assert Server.for_owner(funded_user.id) == []

    def test_a_failure_while_reserving_a_port_also_refunds(self, deployer, funded_user):
        panel = self._panel_that_fails(
            PterodactylTimeoutError("allocations timed out"), at="get_free_allocation"
        )
        with patch("app.dashboard.routes.get_client", return_value=panel):
            response = deploy(deployer, tier=2)

        assert response.status_code == 502
        panel.create_server.assert_not_called()
        assert _db.session.get(User, funded_user.id).credits == Decimal("100.0000")

    def test_an_unconfigured_panel_refunds_and_reports_503(self, deployer, funded_user, app):
        app.config["PTERO_APP_API_KEY"] = ""
        response = deploy(deployer, tier=2)

        assert response.status_code == 503
        assert response.get_json()["error"]["code"] == "pterodactyl_not_configured"
        assert _db.session.get(User, funded_user.id).credits == Decimal("100.0000")
        assert Server.for_owner(funded_user.id) == []

    def test_a_browser_sees_the_refund_message(self, deployer, funded_user):
        panel = self._panel_that_fails(PterodactylTimeoutError("too slow"))
        with patch("app.dashboard.routes.get_client", return_value=panel):
            response = deployer.post(
                "/dashboard/deploy",
                data={"server_name": "Survival One", "ram_tier": "2"},
                headers=HTML,
            )

        assert response.status_code == 502
        assert "refunded" in response.get_data(as_text=True)
        assert _db.session.get(User, funded_user.id).credits == Decimal("100.0000")

    def test_an_unrecordable_server_is_refunded_and_torn_down(
        self, deployer, funded_user, user_factory
    ):
        """The panel built it, but the row will not insert.

        Forced by giving another account a server that already holds the panel id
        the mock returns, which trips the unique index on
        ``pterodactyl_server_id`` -- the same shape as losing a race.
        """
        other = user_factory(username="squatter")
        _db.session.add(
            Server(
                owner_id=other.id,
                name="Prior Claim",
                ram_tier=2,
                pterodactyl_server_id=PANEL_SERVER_ID,
            )
        )
        _db.session.commit()

        panel = fake_panel()
        with patch("app.dashboard.routes.get_client", return_value=panel):
            response = deploy(deployer, tier=2)

        assert response.status_code == 502
        assert response.get_json()["error"]["code"] == "server_record_failed"
        panel.delete_server.assert_called_once_with(PANEL_SERVER_ID, force=True)
        assert _db.session.get(User, funded_user.id).credits == Decimal("100.0000")
        assert Server.for_owner(funded_user.id) == []







