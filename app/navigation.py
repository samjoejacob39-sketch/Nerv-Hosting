"""The sidebar navigation tree.

The tree lives in Python rather than inline in the template for three reasons:

* **The level-one / sub-menu split is structural.**  ``NavItem`` entries that sit
  directly in a section are level one; only a ``NavGroup``'s ``items`` are
  sub-menu rows.  ``static/js/sidebar.js`` is only ever allowed to restyle the
  latter, and having the two be different types makes that boundary explicit
  rather than a CSS-selector convention.
* **The column rule is computed once.**  ``submenu_columns`` is the server-side
  half of the identical rule in ``sidebar.js``, so first paint (and a client with
  JavaScript disabled) already has the right layout.
* **Endpoints that do not exist yet cannot be built.**  A ``planned`` entry
  renders as an inert row instead of blowing up ``url_for`` with a
  ``BuildError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from flask import request, url_for

#: A sub-menu with this many rows or fewer stays a single column.  The user-facing
#: rule is "split only when the row count exceeds five", so the comparison below
#: is deliberately ``<=``.
SUBMENU_ROW_THRESHOLD = 5
#: Target rows per column once a sub-menu does split.
SUBMENU_ROWS_PER_COLUMN = 5
#: Ceiling, so a very long sub-menu grows taller instead of unboundedly wider.
SUBMENU_MAX_COLUMNS = 3


def submenu_columns(row_count: int) -> int:
    """Columns a sub-menu of ``row_count`` rows should use.

    Mirrored by ``columnsFor()`` in ``static/js/sidebar.js``; keep the two in
    step or the layout will jump on the first resize.
    """
    if row_count <= SUBMENU_ROW_THRESHOLD:
        return 1
    return min(SUBMENU_MAX_COLUMNS, ceil(row_count / SUBMENU_ROWS_PER_COLUMN))


def submenu_rows(row_count: int) -> int:
    """Height of the tallest column, for ``grid-rows-*`` under column flow."""
    if row_count <= 0:
        return 0
    return ceil(row_count / submenu_columns(row_count))


@dataclass(frozen=True)
class NavItem:
    """A leaf row.  Level one when listed directly in a :class:`NavSection`."""

    label: str
    endpoint: str | None = None
    href: str | None = None
    icon: str = "dot"
    planned: bool = False
    external: bool = False

    # Plain class attributes (no annotation) so dataclass does not treat these
    # as fields; they exist purely so a template can branch without isinstance.
    is_group = False

    @property
    def url(self) -> str:
        if self.endpoint:
            return url_for(self.endpoint)
        return self.href or "#"

    @property
    def navigable(self) -> bool:
        return not self.planned and bool(self.endpoint or self.href)

    @property
    def active(self) -> bool:
        return bool(self.endpoint) and request.endpoint == self.endpoint


@dataclass(frozen=True)
class NavGroup:
    """A level-one row that owns a collapsible sub-menu."""

    label: str
    icon: str
    items: tuple[NavItem, ...]
    slug: str = ""

    is_group = True

    @property
    def active(self) -> bool:
        return any(item.active for item in self.items)

    @property
    def columns(self) -> int:
        return submenu_columns(len(self.items))

    @property
    def rows(self) -> int:
        return submenu_rows(len(self.items))

    @property
    def key(self) -> str:
        """Stable id used for ``aria-controls`` and open-state persistence."""
        return self.slug or self.label.lower().replace(" ", "-")


@dataclass(frozen=True)
class NavSection:
    """A labelled run of level-one entries."""

    title: str | None
    entries: tuple[NavItem | NavGroup, ...]


#: ``dashboard.index`` is the only HTML endpoint that exists in Phase 2, so every
#: other row is ``planned``: visible, inert, and marked "Soon" in the UI.
SIDEBAR: tuple[NavSection, ...] = (
    NavSection(
        None,
        (NavItem("Dashboard", endpoint="dashboard.index", icon="grid"),),
    ),
    NavSection(
        "Hosting",
        (
            # Seven rows: over the threshold, so this one splits into columns.
            NavGroup(
                "Servers",
                "server",
                (
                    NavItem("All servers", endpoint="dashboard.index"),
                    NavItem("Create server", planned=True),
                    NavItem("Shared with me", planned=True),
                    NavItem("Backups", planned=True),
                    NavItem("Startup & config", planned=True),
                    NavItem("Console", planned=True),
                    NavItem("File manager", planned=True),
                ),
            ),
            # Four rows: at or under the threshold, so it stays one column.
            NavGroup(
                "Store",
                "wallet",
                (
                    NavItem("Buy credits", planned=True),
                    NavItem("Credit history", planned=True),
                    NavItem("Tiers & pricing", planned=True),
                    NavItem("Redeem a code", planned=True),
                ),
            ),
        ),
    ),
    NavSection(
        "Account",
        (
            NavGroup(
                "Settings",
                "gear",
                (
                    NavItem("Profile", planned=True),
                    NavItem("Security", planned=True),
                    NavItem("Active sessions", planned=True),
                    NavItem("API tokens", planned=True),
                    NavItem("Notifications", planned=True),
                    NavItem("Close account", planned=True),
                ),
            ),
            NavItem("Support", icon="help", planned=True),
        ),
    ),
)
