"""Model-level coverage: constraints, cascades, credits and sharing."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.constants import RamTier, ServerStatus
from app.models import Server, SharedAccess, SharedAccessError, User


def make_server(db, owner, name="bot-one", tier=RamTier.BASIC):
    server = Server(owner_id=owner.id, name=name, ram_tier=int(tier))
    db.session.add(server)
    db.session.commit()
    return server


# --------------------------------------------------------------------- #
# User
# --------------------------------------------------------------------- #
def test_defaults(registered_user):
    assert registered_user.credits == Decimal("0.0000")
    assert registered_user.is_active is True
    assert registered_user.is_admin is False
    assert registered_user.created_at_utc is not None
    assert registered_user.created_at_utc.tzinfo is not None


def test_username_uniqueness_enforced_by_database(db, registered_user):
    duplicate = User(username=registered_user.username, email="other@example.com")
    duplicate.set_password("An0ther-Pass!")
    db.session.add(duplicate)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_credit_top_up_and_spend(db, registered_user):
    registered_user.add_credits("10.5")
    db.session.commit()
    assert registered_user.credits == Decimal("10.5000")

    assert registered_user.deduct_credits("3.25") is True
    db.session.commit()
    assert registered_user.credits == Decimal("7.2500")


def test_overspend_is_refused_and_leaves_balance_intact(db, registered_user):
    registered_user.add_credits("5")
    db.session.commit()

    assert registered_user.deduct_credits("5.0001") is False
    db.session.commit()
    assert registered_user.credits == Decimal("5.0000")


def test_non_positive_amounts_rejected(registered_user):
    for bad in ("0", "-1"):
        with pytest.raises(ValueError):
            registered_user.add_credits(bad)
        with pytest.raises(ValueError):
            registered_user.deduct_credits(bad)


def test_by_identity_resolves_username_or_email(registered_user):
    assert User.by_identity(registered_user.username).id == registered_user.id
    assert User.by_identity(registered_user.email.upper()).id == registered_user.id
    assert User.by_identity("nobody") is None
    assert User.by_identity("") is None


# --------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------- #
def test_server_defaults_and_tier_lookup(db, registered_user):
    server = make_server(db, registered_user, tier=RamTier.BASIC)
    assert server.status == ServerStatus.PENDING
    # A tier's value *is* its size in gigabytes.
    assert server.tier.ram_gb == 2
    assert server.ram_mb == 2048
    assert server.is_provisioned is False
    assert server.hourly_credits == Decimal("1.0000")
    assert registered_user.owns(server)


def test_duplicate_name_per_owner_rejected_but_allowed_across_owners(db, user_factory):
    alice = user_factory(username="alice")
    bob = user_factory(username="bob")
    make_server(db, alice, name="shared-name")
    make_server(db, bob, name="shared-name")  # different owner: fine

    clash = Server(owner_id=alice.id, name="shared-name", ram_tier=int(RamTier.FREE))
    db.session.add(clash)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_unknown_tier_and_status_are_rejected(db, registered_user):
    bad_tier = Server(owner_id=registered_user.id, name="bad-tier", ram_tier=99)
    db.session.add(bad_tier)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    server = make_server(db, registered_user)
    with pytest.raises(ValueError):
        server.set_status("exploded")


def test_deleting_owner_cascades_to_servers(db, registered_user):
    make_server(db, registered_user)
    db.session.delete(registered_user)
    db.session.commit()
    assert db.session.scalars(db.select(Server)).all() == []


# --------------------------------------------------------------------- #
# SharedAccess
# --------------------------------------------------------------------- #
def test_grant_gives_guest_access(db, user_factory):
    owner = user_factory(username="owner")
    guest = user_factory(username="guest")
    server = make_server(db, owner)

    SharedAccess.grant(server, guest)
    db.session.commit()

    assert guest.can_access(server) is True
    assert guest.owns(server) is False
    assert [s.id for s in guest.shared_servers] == [server.id]
    assert server.is_shared_with(guest)


def test_grant_rejects_self_share_and_duplicates(db, user_factory):
    owner = user_factory(username="owner2")
    guest = user_factory(username="guest2")
    server = make_server(db, owner)

    with pytest.raises(SharedAccessError):
        SharedAccess.grant(server, owner)

    SharedAccess.grant(server, guest)
    db.session.commit()
    with pytest.raises(SharedAccessError):
        SharedAccess.grant(server, guest)


def test_revoke_removes_access(db, user_factory):
    owner = user_factory(username="owner3")
    guest = user_factory(username="guest3")
    server = make_server(db, owner)
    SharedAccess.grant(server, guest)
    db.session.commit()

    assert SharedAccess.revoke(server.id, guest.id) is True
    db.session.commit()
    assert guest.can_access(server) is False
    assert SharedAccess.revoke(server.id, guest.id) is False


def test_deleting_server_cascades_to_grants(db, user_factory):
    owner = user_factory(username="owner4")
    guest = user_factory(username="guest4")
    server = make_server(db, owner)
    SharedAccess.grant(server, guest)
    db.session.commit()

    db.session.delete(server)
    db.session.commit()
    assert db.session.scalars(db.select(SharedAccess)).all() == []
