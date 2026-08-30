"""Phase 2: the rendered HTML.

These tests assert the *contract* between the templates and everything else --
the WTForms field names, the CSRF meta tag, the credit format in the header, and
the sidebar's column rule -- rather than the styling, which is free to change.

Every request here sends ``Accept: text/html``.  Without it ``wants_json()``
returns True (Werkzeug's test client sends no ``Accept`` header at all), which
is what keeps the JSON API tests on the JSON path.
"""

from __future__ import annotations

import re

import pytest

from app import create_app
from app.extensions import db
from app.navigation import SIDEBAR, NavGroup, submenu_columns, submenu_rows
from tests.conftest import VALID_PASSWORD, login

HTML = {"Accept": "text/html"}

#: Opening tag of every sub-menu list, in document order.
SUBMENU_LIST_TAG = re.compile(r"<ul[^>]*\bdata-nav-submenu-list\b[^>]*>")
#: Opening tag of every level-one control.
LEVEL_ONE_TAG = re.compile(r"<(?:a|button)[^>]*\bdata-nav-level=\"1\"[^>]*>")


def classes_of(tag: str) -> set[str]:
    match = re.search(r"class=\"([^\"]*)\"", tag)
    return set(match.group(1).split()) if match else set()


def html_of(response) -> str:
    assert response.mimetype == "text/html", f"expected HTML, got {response.mimetype}"
    return response.get_data(as_text=True)


# ---------------------------------------------------------------------- #
# The column rule
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("rows", "expected"),
    [(0, 1), (1, 1), (4, 1), (5, 1), (6, 2), (7, 2), (10, 2), (11, 3), (15, 3), (100, 3)],
)
def test_submenu_columns_splits_only_above_five(rows, expected):
    """The brief: split "only when row counts exceed five", capped at three."""
    assert submenu_columns(rows) == expected


@pytest.mark.parametrize(
    ("rows", "expected"), [(0, 0), (5, 5), (6, 3), (7, 4), (11, 4), (100, 34)]
)
def test_submenu_rows_is_the_tallest_column(rows, expected):
    assert submenu_rows(rows) == expected


def test_sidebar_tree_exercises_both_sides_of_the_threshold():
    """Guard the fixtures the render tests below depend on."""
    groups = {
        entry.label: entry
        for section in SIDEBAR
        for entry in section.entries
        if isinstance(entry, NavGroup)
    }
    assert len(groups["Servers"].items) == 7 and groups["Servers"].columns == 2
    assert len(groups["Store"].items) == 4 and groups["Store"].columns == 1


# ---------------------------------------------------------------------- #
# Auth pages
# ---------------------------------------------------------------------- #
def test_login_page_renders_the_wtforms_fields(client):
    body = html_of(client.get("/login", headers=HTML))
    assert 'name="identity"' in body
    assert 'name="password"' in body
    assert 'name="remember_me"' in body
    assert 'action="/login"' in body


def test_register_page_renders_every_wtforms_field(client):
    body = html_of(client.get("/register", headers=HTML))
    for name in ("username", "email", "password", "confirm_password", "accept_terms"):
        assert f'name="{name}"' in body, f"{name} is missing from register.html"


def test_pages_expose_a_csrf_meta_tag(client):
    """AJAX callers read this to populate ``X-CSRFToken``."""
    body = html_of(client.get("/login", headers=HTML))
    assert re.search(r'<meta name="csrf-token" content="[^"]+">', body)


def test_invalid_login_re_renders_the_form_with_the_error(client, registered_user):
    response = client.post(
        "/login",
        data={"identity": registered_user.username, "password": "wrong-password"},
        headers=HTML,
    )
    assert response.status_code == 401
    body = html_of(response)
    assert "Incorrect credentials." in body
    # The form comes back rather than a redirect, so the typed identity survives.
    assert registered_user.username in body


def test_login_form_post_redirects_to_the_dashboard(client, registered_user):
    response = client.post(
        "/login",
        data={"identity": registered_user.username, "password": VALID_PASSWORD},
        headers=HTML,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard/")


def test_anonymous_dashboard_redirects_to_login(client):
    response = client.get("/dashboard/", headers=HTML)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


# ---------------------------------------------------------------------- #
# Dashboard
# ---------------------------------------------------------------------- #
@pytest.fixture
def dashboard(client, user_factory):
    """The rendered dashboard for a signed-in account with a known balance."""
    user = user_factory(username="grid.tester")
    user.add_credits("12.5")
    login(client, user.username)
    response = client.get("/dashboard/", headers=HTML)
    assert response.status_code == 200
    return user, html_of(response)


def test_dashboard_welcomes_the_user(dashboard):
    user, body = dashboard
    assert user.username in body


def test_dashboard_header_shows_the_balance_to_two_places(dashboard):
    _, body = dashboard
    balance = re.search(r"data-credit-balance[^>]*>\s*([^<\s]+)", body)
    assert balance, "the header credit pill is missing"
    assert balance.group(1) == "12.50"


def test_dashboard_has_an_empty_server_grid_ready_for_cards(dashboard):
    _, body = dashboard
    assert "data-server-grid" in body
    assert "No containers yet" in body


def test_long_submenu_splits_into_columns_short_one_does_not(dashboard):
    """First paint already matches the rule -- no JavaScript involved."""
    _, body = dashboard
    lists = [classes_of(tag) for tag in SUBMENU_LIST_TAG.findall(body)]
    assert len(lists) == 3, "expected the Servers, Store and Settings sub-menus"

    servers, store, settings = lists
    # Seven rows -> two columns of four, filled column-first.
    assert {"grid-cols-2", "grid-flow-col", "grid-rows-4"} <= servers
    # Four rows -> one column, and no column flow to reorder it.
    assert "grid-cols-1" in store
    assert "grid-flow-col" not in store
    # Six rows -> two columns of three.
    assert {"grid-cols-2", "grid-flow-col", "grid-rows-3"} <= settings


def test_level_one_navigation_is_never_column_split(dashboard):
    """The brief forbids the division rules touching level one."""
    _, body = dashboard
    tags = LEVEL_ONE_TAG.findall(body)
    assert tags, "no level-one navigation elements were rendered"
    for tag in tags:
        assert not any(name.startswith("grid-cols-") for name in classes_of(tag))
        assert "data-nav-submenu-list" not in tag


def test_nav_root_publishes_the_python_thresholds(dashboard):
    """`sidebar.js` reads these, so Python stays the single source of truth."""
    _, body = dashboard
    for attribute, value in (
        ("data-nav-row-threshold", 5),
        ("data-nav-rows-per-column", 5),
        ("data-nav-max-columns", 3),
    ):
        assert f'{attribute}="{value}"' in body


def test_logout_is_a_post_form_not_a_link(dashboard):
    _, body = dashboard
    assert re.search(r'<form[^>]*method="post"[^>]*action="/logout"', body)
    assert 'href="/logout"' not in body


def test_error_page_renders_for_browsers(client):
    response = client.get("/no-such-page", headers=HTML)
    assert response.status_code == 404
    body = html_of(response)
    assert "404" in body
    assert "does not exist" in body


def test_error_payload_stays_json_for_api_clients(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------- #
# CSRF-enabled rendering (the production path)
# ---------------------------------------------------------------------- #
def test_forms_render_a_hidden_csrf_field_when_protection_is_on():
    """The rest of the suite runs with ``WTF_CSRF_ENABLED = False``, where
    WTForms omits the field entirely and ``{{ form.csrf_token }}`` renders as
    nothing.  Production has it on, so assert the hidden input really appears."""
    app = create_app("testing", {"WTF_CSRF_ENABLED": True})
    with app.app_context():
        db.create_all()
        try:
            body = app.test_client().get("/login", headers=HTML).get_data(as_text=True)
        finally:
            db.session.remove()
            db.drop_all()

    assert re.search(r'<input[^>]*name="csrf_token"[^>]*value="[^"]+"', body)


def test_server_card_renders_power_controls_and_ownership(client, user_factory):
    from app.constants import RamTier, ServerStatus
    from app.models import Server, SharedAccess

    owner = user_factory(username="cardowner")
    guest = user_factory(username="cardguest")
    server = Server(
        owner_id=owner.id,
        name="alpha-survival",
        ram_tier=int(RamTier.BASIC),
        status=ServerStatus.RUNNING,
    )
    db.session.add(server)
    SharedAccess.grant(server, guest)
    db.session.commit()

    # Check owner view
    login(client, owner.username)
    owner_body = html_of(client.get("/dashboard/", headers=HTML))
    assert "alpha-survival" in owner_body
    assert "Owner" in owner_body
    assert 'data-power-action="start"' in owner_body
    assert 'data-power-action="restart"' in owner_body
    assert 'data-power-action="stop"' in owner_body
    assert "Share (" in owner_body
    assert 'id="share-modal"' in owner_body

    # Check guest view
    client.post("/logout")
    login(client, guest.username)
    guest_body = html_of(client.get("/dashboard/", headers=HTML))
    assert "alpha-survival" in guest_body
    assert "Shared by @cardowner" in guest_body

