"""Phase 5: Ads, Anti-adblock, Credit Rewards Webhook & Automated Billing."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.constants import RamTier, ServerStatus
from app.extensions import db as _db
from app.models import Server, User
from app.ptero_client import PterodactylAPIError
from app.scheduler import process_hourly_billing
from tests.conftest import VALID_PASSWORD, login

TEST_SECRET = "test-ad-webhook-secret"


@pytest.fixture
def app():
    application = create_app(
        "testing",
        {
            "AD_WEBHOOK_SECRET": TEST_SECRET,
            "PTERO_URL": "https://panel.example.test",
            "PTERO_APP_API_KEY": "ptla_test_app_key",
            "SCHEDULER_ENABLED": False,
        },
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


def make_server(db, owner, name="billed-srv", tier=RamTier.BASIC, status=ServerStatus.RUNNING, panel_id=3001):
    server = Server(
        owner_id=owner.id,
        name=name,
        ram_tier=int(tier),
        status=status,
        pterodactyl_server_id=panel_id,
    )
    db.session.add(server)
    db.session.commit()
    return server


# ---------------------------------------------------------------------- #
# Credit Reward Webhook Tests
# ---------------------------------------------------------------------- #
def test_credit_reward_webhook_success(client, db, user_factory):
    user = user_factory(username="adwatcher")
    assert user.credits == Decimal("0.0000")

    res = client.post(
        "/api/credit-reward",
        headers={"X-Ad-Reward-Secret": TEST_SECRET},
        json={"user_id": user.id, "reward_amount": 10.5},
    )

    assert res.status_code == 200
    payload = res.get_json()
    assert payload["ok"] is True
    assert payload["data"]["user_id"] == user.id
    assert payload["data"]["reward_amount"] == "10.5000"
    assert payload["data"]["credit_balance"] == "10.5000"

    db.session.refresh(user)
    assert user.credits == Decimal("10.5000")


def test_credit_reward_webhook_unauthorized_with_missing_or_bad_secret(client, user_factory):
    user = user_factory(username="hacker")

    # Missing secret header
    res1 = client.post(
        "/api/credit-reward",
        json={"user_id": user.id, "reward_amount": 100},
    )
    assert res1.status_code == 401
    assert res1.get_json()["error"]["code"] == "unauthorized"

    # Wrong secret header
    res2 = client.post(
        "/api/credit-reward",
        headers={"X-Ad-Reward-Secret": "wrong-secret-key"},
        json={"user_id": user.id, "reward_amount": 100},
    )
    assert res2.status_code == 401
    assert res2.get_json()["error"]["code"] == "unauthorized"


def test_credit_reward_webhook_missing_payload_fields(client, user_factory):
    user = user_factory(username="payloadtester")

    # Missing reward_amount
    res1 = client.post(
        "/api/credit-reward",
        headers={"X-Ad-Reward-Secret": TEST_SECRET},
        json={"user_id": user.id},
    )
    assert res1.status_code == 422
    assert res1.get_json()["error"]["code"] == "validation_error"

    # Missing user_id
    res2 = client.post(
        "/api/credit-reward",
        headers={"X-Ad-Reward-Secret": TEST_SECRET},
        json={"reward_amount": 10},
    )
    assert res2.status_code == 422

    # Non-integer user_id
    res3 = client.post(
        "/api/credit-reward",
        headers={"X-Ad-Reward-Secret": TEST_SECRET},
        json={"user_id": "not_an_int", "reward_amount": 10},
    )
    assert res3.status_code == 422


def test_credit_reward_webhook_nonexistent_user(client):
    res = client.post(
        "/api/credit-reward",
        headers={"X-Ad-Reward-Secret": TEST_SECRET},
        json={"user_id": 999999, "reward_amount": 10},
    )
    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "user_not_found"


def test_credit_reward_webhook_non_positive_reward(client, user_factory):
    user = user_factory(username="freeloader")

    for bad_amount in (0, -5, "-1.5"):
        res = client.post(
            "/api/credit-reward",
            headers={"X-Ad-Reward-Secret": TEST_SECRET},
            json={"user_id": user.id, "reward_amount": bad_amount},
        )
        assert res.status_code == 422
        assert res.get_json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------- #
# Automated Background Billing Tests
# ---------------------------------------------------------------------- #
def test_process_hourly_billing_deducts_credits_for_running_servers(app, db, user_factory):
    alice = user_factory(username="alice")
    bob = user_factory(username="bob")

    alice.add_credits("20.0000")
    bob.add_credits("10.0000")
    db.session.commit()

    # Basic tier = 1.0 credit/hr, Standard tier = 2.0 credits/hr
    srv1 = make_server(db, alice, name="alice-srv1", tier=RamTier.BASIC, panel_id=101)
    srv2 = make_server(db, alice, name="alice-srv2", tier=RamTier.STANDARD, panel_id=102)
    srv3 = make_server(db, bob, name="bob-srv", tier=RamTier.BASIC, panel_id=103)

    summary = process_hourly_billing()

    assert summary["servers_processed"] == 3
    assert summary["credits_billed"] == "4.0000"  # 1.0 + 2.0 + 1.0
    assert summary["servers_suspended"] == 0

    db.session.refresh(alice)
    db.session.refresh(bob)
    assert alice.credits == Decimal("17.0000")  # 20 - 3
    assert bob.credits == Decimal("9.0000")    # 10 - 1


def test_process_hourly_billing_skips_stopped_or_unprovisioned_servers(app, db, user_factory):
    charlie = user_factory(username="charlie")
    charlie.add_credits("10.0000")
    db.session.commit()

    make_server(db, charlie, name="stopped-srv", status=ServerStatus.STOPPED, panel_id=201)
    make_server(db, charlie, name="suspended-srv", status=ServerStatus.SUSPENDED, panel_id=202)

    # Unprovisioned server
    unprov = Server(
        owner_id=charlie.id,
        name="pending-srv",
        ram_tier=int(RamTier.BASIC),
        status=ServerStatus.RUNNING,
        pterodactyl_server_id=None,
    )
    db.session.add(unprov)
    db.session.commit()

    summary = process_hourly_billing()
    assert summary["servers_processed"] == 0
    assert summary["credits_billed"] == "0.0000"

    db.session.refresh(charlie)
    assert charlie.credits == Decimal("10.0000")


def test_process_hourly_billing_free_tier_no_charge(app, db, user_factory):
    free_user = user_factory(username="freeuser")
    make_server(db, free_user, name="free-srv", tier=RamTier.FREE, panel_id=301)

    summary = process_hourly_billing()
    assert summary["servers_processed"] == 1
    assert summary["credits_billed"] == "0.0000"
    assert summary["servers_suspended"] == 0


def test_process_hourly_billing_auto_suspends_when_balance_exhausted(app, db, user_factory):
    broke_user = user_factory(username="brokeuser")
    broke_user.add_credits("0.5000")  # less than Basic tier (1.0000 / hr)
    db.session.commit()

    server = make_server(db, broke_user, name="broke-srv", tier=RamTier.BASIC, panel_id=401)

    with patch("app.scheduler.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_get_client.return_value.__enter__.return_value = mock_panel

        summary = process_hourly_billing()

        assert summary["servers_processed"] == 1
        assert summary["servers_suspended"] == 1
        mock_panel.send_power_signal.assert_called_once_with(401, "stop")

    db.session.refresh(server)
    assert server.status == ServerStatus.SUSPENDED
    db.session.refresh(broke_user)
    assert broke_user.credits == Decimal("0.5000")  # untouched on insufficient deduction failure


def test_process_hourly_billing_handles_panel_error_on_suspension(app, db, user_factory):
    broke_user2 = user_factory(username="brokeuser2")
    server = make_server(db, broke_user2, name="broke-srv2", tier=RamTier.BASIC, panel_id=402)

    with patch("app.scheduler.get_client") as mock_get_client:
        mock_panel = MagicMock()
        mock_panel.send_power_signal.side_effect = PterodactylAPIError("Panel unavailable", status_code=502)
        mock_get_client.return_value.__enter__.return_value = mock_panel

        summary = process_hourly_billing()
        assert summary["servers_suspended"] == 1

    db.session.refresh(server)
    assert server.status == ServerStatus.SUSPENDED


# ---------------------------------------------------------------------- #
# Ad Layout and Anti-Adblock Markup Tests
# ---------------------------------------------------------------------- #
def test_dashboard_renders_ad_slots_and_anti_adblock_modal(client, user_factory):
    user = user_factory(username="adlayoutuser")
    login(client, user.username)

    response = client.get("/dashboard/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    # Ad slots & fallback text
    assert "Ad-blocker active! Please whitelist us—ads keep your servers free." in body
    assert "adsbygoogle" in body
    assert "ad-banner" in body

    # Anti-adblock penalty modal
    assert 'id="adblock-modal"' in body
    assert 'id="adblock-dismiss-btn"' in body
    assert "adblock.js" in body
