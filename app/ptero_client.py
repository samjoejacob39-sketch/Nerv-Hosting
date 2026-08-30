"""Thin client for the Pterodactyl **Application** API.

Scope and shape
---------------
This module knows how to talk to a panel and nothing about our domain: it takes
ids and limits, returns the panel's ``attributes`` dicts, and raises on anything
that is not a 2xx.  Deciding *what* to provision, charging for it and recording
it belongs to ``app/dashboard/routes.py``.

Failure model
-------------
Every method raises a subclass of :class:`PterodactylError`, so a caller can
wrap one ``try`` around a whole provisioning sequence and treat any failure as
"the panel did not do what we asked" -- which is what makes the credit refund in
the deploy route reliable.  Nothing here retries: a ``POST /servers`` that timed
out may or may not have created a server, so replaying it risks a double build.

Credentials
-----------
``PTERO_APP_API_KEY`` must be an *Application* key from the panel's admin area
(a client key gets a 403 on ``/api/application/*``).  The key is never logged,
and :meth:`PterodactylClient.__repr__` deliberately omits it.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

import requests
from flask import current_app

from app.constants import (
    MINECRAFT_DOCKER_IMAGE,
    MINECRAFT_ENVIRONMENT,
    MINECRAFT_FEATURE_LIMITS,
    MINECRAFT_STARTUP,
    PTERO_TIMEOUT_SECONDS,
    PowerSignal,
)

__all__ = [
    "PterodactylClient",
    "PterodactylError",
    "PterodactylAPIError",
    "PterodactylTimeoutError",
    "PterodactylConnectionError",
    "PterodactylConfigurationError",
    "NoAllocationAvailableError",
]

#: Pages of allocations to walk before giving up.  A node with more than this
#: many pages of *assigned* ports is misconfigured, not merely busy.
_MAX_ALLOCATION_PAGES = 20
_ALLOCATIONS_PER_PAGE = 100


# ---------------------------------------------------------------------- #
# Exceptions
# ---------------------------------------------------------------------- #
class PterodactylError(Exception):
    """Base class: any failure to complete a panel operation."""

    #: Suggested HTTP status when this bubbles up to a view.
    http_status = 502
    #: Machine-readable code for the JSON error envelope.
    code = "pterodactyl_error"


class PterodactylConfigurationError(PterodactylError):
    """The panel URL or application key is missing or malformed.

    Ours to fix, not the upstream's, hence 503 rather than 502.
    """

    http_status = 503
    code = "pterodactyl_not_configured"


class PterodactylTimeoutError(PterodactylError):
    """The panel did not answer within the configured timeout."""

    code = "pterodactyl_timeout"


class PterodactylConnectionError(PterodactylError):
    """DNS, TLS or socket-level failure reaching the panel."""

    code = "pterodactyl_unreachable"


class NoAllocationAvailableError(PterodactylError):
    """The node has no unassigned port left to hand out."""

    http_status = 503
    code = "no_allocation_available"


class PterodactylAPIError(PterodactylError):
    """The panel answered with a 4xx or 5xx.

    ``errors`` holds the panel's own error list (JSON:API shaped -- each entry
    has ``code``/``detail``), which is far more useful in a log than the status
    line alone.
    """

    code = "pterodactyl_api_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        errors: list[dict[str, Any]] | None = None,
        method: str = "",
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []
        self.method = method
        self.url = url

    @property
    def details(self) -> list[str]:
        """The panel's messages, flattened for logging."""
        return [
            str(entry.get("detail") or entry.get("code") or entry)
            for entry in self.errors
        ]

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} ({'; '.join(self.details)})" if self.details else base


class PterodactylClient:
    """Authenticated session against one panel's Application API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float | None = None,
        verify: bool | None = None,
        session: requests.Session | None = None,
    ) -> None:
        """Read anything not passed explicitly from ``current_app.config``.

        Injecting ``session`` is what makes this testable without patching
        ``requests`` globally.
        """
        config = current_app.config if current_app else {}

        self.base_url = str(base_url or config.get("PTERO_URL") or "").rstrip("/")
        self._api_key = str(api_key or config.get("PTERO_APP_API_KEY") or "")
        self.timeout = float(
            timeout if timeout is not None else config.get("PTERO_TIMEOUT", PTERO_TIMEOUT_SECONDS)
        )
        self.verify = bool(config.get("PTERO_VERIFY_TLS", True) if verify is None else verify)

        if not self.base_url or not self._api_key:
            raise PterodactylConfigurationError(
                "Pterodactyl is not configured: set PTERO_URL and PTERO_APP_API_KEY."
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise PterodactylConfigurationError(
                f"PTERO_URL must include a scheme, got {self.base_url!r}."
            )

        self._session = session or requests.Session()
        #: True when we own the session and may therefore close it.
        self._owns_session = session is None

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #
    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/application/{path.lstrip('/')}"

    def _client_url(self, path: str) -> str:
        return f"{self.base_url}/api/client/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        api: str = "application",
    ) -> dict[str, Any]:
        """Issue one call and return the decoded body (``{}`` for 204s)."""
        url = self._client_url(path) if api == "client" else self._url(path)
        try:
            response = self._session.request(
                method,
                url,
                json=json,
                params=params,
                headers=self._headers,
                timeout=self.timeout,
                verify=self.verify,
            )
        except requests.Timeout as exc:
            raise PterodactylTimeoutError(
                f"{method} {path} timed out after {self.timeout:g}s."
            ) from exc
        except requests.RequestException as exc:
            # Covers DNS failures, refused connections and TLS errors.  ``exc``
            # can embed the request URL but never the Authorization header.
            raise PterodactylConnectionError(f"Could not reach the panel: {exc}") from exc

        if response.status_code >= 400:
            raise self._api_error(method, path, response)

        if response.status_code == 204 or not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise PterodactylError(
                f"{method} {path} returned {response.status_code} with a non-JSON body."
            ) from exc
        return body if isinstance(body, dict) else {"data": body}

    @staticmethod
    def _api_error(method: str, path: str, response: requests.Response) -> PterodactylAPIError:
        try:
            payload = response.json()
            errors = payload.get("errors") if isinstance(payload, dict) else None
        except ValueError:
            errors = None
        return PterodactylAPIError(
            f"{method} {path} failed with HTTP {response.status_code}.",
            status_code=response.status_code,
            errors=errors if isinstance(errors, list) else None,
            method=method,
            url=path,
        )

    @staticmethod
    def _attributes(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Unwrap a single JSON:API object into its ``attributes``."""
        attributes = payload.get("attributes")
        if not isinstance(attributes, dict):
            raise PterodactylError("Panel response is missing an 'attributes' object.")
        return attributes

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "PterodactylClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - never include the key
        return f"<PterodactylClient url={self.base_url!r} timeout={self.timeout:g}>"

    # ------------------------------------------------------------------ #
    # Allocations
    # ------------------------------------------------------------------ #
    def iter_allocations(self, node_id: int) -> Iterator[dict[str, Any]]:
        """Yield every allocation on ``node_id``, following pagination."""
        page = 1
        while page <= _MAX_ALLOCATION_PAGES:
            payload = self._request(
                "GET",
                f"nodes/{int(node_id)}/allocations",
                params={"page": page, "per_page": _ALLOCATIONS_PER_PAGE},
            )
            entries = payload.get("data") or []
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("attributes"), dict):
                    yield entry["attributes"]

            pagination = (payload.get("meta") or {}).get("pagination") or {}
            total_pages = int(pagination.get("total_pages") or 0)
            if not entries or page >= total_pages:
                return
            page += 1

    def get_free_allocation(self, node_id: int) -> dict[str, Any]:
        """The first unassigned allocation on ``node_id``.

        Returns the panel's allocation attributes (``id``, ``ip``, ``port``,
        ``alias``, ...).  Raises :class:`NoAllocationAvailableError` when the node
        is full -- a distinct failure from the panel being broken, because the fix
        is "add ports to the node", not "debug the API".

        This is inherently racy: two deploys a millisecond apart can both see the
        same free port, and the loser gets a 4xx from ``create_server``.  The
        panel is the authority on that, so we do not try to reserve here.
        """
        for allocation in self.iter_allocations(node_id):
            if not allocation.get("assigned"):
                return allocation
        raise NoAllocationAvailableError(
            f"Node {node_id} has no unassigned allocation left."
        )

    # ------------------------------------------------------------------ #
    # Servers
    # ------------------------------------------------------------------ #
    def create_server(
        self,
        user_id: int,
        name: str,
        memory_mb: int,
        disk_mb: int,
        cpu_limit: int,
        nest_id: int,
        egg_id: int,
        allocation_id: int,
        *,
        docker_image: str | None = None,
        startup: str | None = None,
        environment: Mapping[str, str] | None = None,
        feature_limits: Mapping[str, int] | None = None,
        start_on_completion: bool = True,
    ) -> dict[str, Any]:
        """Create a server and return its attributes.

        ``user_id`` is the *panel's* user id, not ours.  ``cpu_limit`` is a
        percentage where 100 means one full core, and ``0`` would mean unlimited
        -- so callers should pass a real limit.

        The returned dict includes ``id`` (what we persist as
        ``Server.pterodactyl_server_id``), ``identifier``, ``uuid`` and the
        applied ``limits``.
        """
        payload: dict[str, Any] = {
            "name": name,
            "user": int(user_id),
            "nest": int(nest_id),
            "egg": int(egg_id),
            "docker_image": docker_image or MINECRAFT_DOCKER_IMAGE,
            "startup": startup or MINECRAFT_STARTUP,
            "environment": dict(environment or MINECRAFT_ENVIRONMENT),
            "limits": {
                "memory": int(memory_mb),
                "disk": int(disk_mb),
                "cpu": int(cpu_limit),
                # No swap: an over-committed container should be OOM-killed and
                # restarted rather than crawl and take the node's IO with it.
                "swap": 0,
                "io": 500,
            },
            "feature_limits": dict(feature_limits or MINECRAFT_FEATURE_LIMITS),
            "allocation": {"default": int(allocation_id)},
            "start_on_completion": bool(start_on_completion),
        }

        attributes = self._attributes(self._request("POST", "servers", json=payload))
        if not attributes.get("id"):
            raise PterodactylError("Panel created a server but returned no id.")
        return attributes

    def get_server(self, server_id: int) -> dict[str, Any]:
        """Read one server by its panel id."""
        return self._attributes(self._request("GET", f"servers/{int(server_id)}"))

    def delete_server(self, server_id: int, *, force: bool = False) -> None:
        """Destroy a server on the panel.

        Used to clean up after a provision that succeeded upstream but could not
        be recorded locally; ``force`` tells the panel to delete even if its own
        teardown reports an error, which is what an orphan needs.
        """
        suffix = "/force" if force else ""
        self._request("DELETE", f"servers/{int(server_id)}{suffix}")

    # ------------------------------------------------------------------ #
    # Client API (Power & Resource monitoring)
    # ------------------------------------------------------------------ #
    def send_power_signal(self, server_id: int | str, signal: str) -> None:
        """Send a power action signal ('start', 'stop', 'restart', 'kill') to a server.

        Communicates with the Pterodactyl Client API on ``POST /api/client/servers/{identifier}/power``.
        """
        if signal not in PowerSignal.ALL:
            valid = ", ".join(PowerSignal.ALL)
            raise ValueError(f"Invalid power signal {signal!r}. Valid signals: {valid}.")

        self._request(
            "POST",
            f"servers/{server_id}/power",
            json={"signal": signal},
            api="client",
        )

    def get_server_status(self, server_id: int | str) -> dict[str, Any]:
        """Fetch real-time resource usage and power state for a server.

        Communicates with the Pterodactyl Client API on ``GET /api/client/servers/{identifier}/resources``.
        Returns the unwrapped attributes dictionary (e.g. ``current_state``, ``resources``).
        """
        payload = self._request("GET", f"servers/{server_id}/resources", api="client")
        return self._attributes(payload)

    def get_websocket_credentials(self, server_id: int | str) -> dict[str, Any]:
        """Fetch WebSocket authentication token and socket URL for the server console.

        Communicates with the Pterodactyl Client API on ``GET /api/client/servers/{identifier}/websocket``.
        Returns ``{"token": str, "socket": str}``.
        """
        payload = self._request("GET", f"servers/{server_id}/websocket", api="client")
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        return {
            "token": data.get("token"),
            "socket": data.get("socket"),
        }

    def list_files(self, server_id: int | str, directory: str = "/") -> list[dict[str, Any]]:
        """List files and folders within a given directory.

        Communicates with ``GET /api/client/servers/{identifier}/files/list?directory={directory}``.
        Returns a list of unwrapped file attribute dictionaries.
        """
        params = {"directory": directory}
        payload = self._request("GET", f"servers/{server_id}/files/list", params=params, api="client")
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return [self._attributes(item) for item in data]

    def read_file(self, server_id: int | str, file_path: str) -> str:
        """Read the raw text content of a file on the server.

        Communicates with ``GET /api/client/servers/{identifier}/files/contents?file={file_path}``.
        """
        params = {"file": file_path}
        url = self._client_url(f"servers/{server_id}/files/contents")
        headers = self._headers()
        response = self.session.request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_tls,
        )
        if not response.ok:
            self._raise_for_status(response)
        return response.text

    def save_file(self, server_id: int | str, file_path: str, content: str) -> None:
        """Write raw text content to a file on the server.

        Communicates with ``POST /api/client/servers/{identifier}/files/write?file={file_path}``.
        """
        params = {"file": file_path}
        url = self._client_url(f"servers/{server_id}/files/write")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "text/plain",
        }
        response = self.session.request(
            "POST",
            url,
            params=params,
            data=content.encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_tls,
        )
        if not response.ok:
            self._raise_for_status(response)

    def create_backup(self, server_id: int | str) -> dict[str, Any]:
        """Trigger a manual backup for the server.

        Communicates with ``POST /api/client/servers/{identifier}/backups``.
        Returns the unwrapped backup attributes dictionary.
        """
        payload = self._request("POST", f"servers/{server_id}/backups", api="client")
        return self._attributes(payload)


def get_client(**kwargs: Any) -> PterodactylClient:
    """Build a client for the current app.

    A seam for tests and for a future pooled/cached implementation: route code
    calls this rather than the constructor.
    """
    return PterodactylClient(**kwargs)
