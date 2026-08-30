"""HTTP response helpers and content negotiation.

Routes call ``wants_json()`` to decide between a JSON envelope and a rendered
template (or a redirect), so one handler serves API clients and browsers from
the same validation and session logic.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

from flask import jsonify, request, url_for
from werkzeug.wrappers import Response


def wants_json() -> bool:
    """True when the caller is an API client rather than a browser navigation.

    Checks, in order: an explicit JSON request body, ``X-Requested-With``, and
    finally content negotiation where JSON outranks HTML.
    """
    if request.is_json:
        return True
    if request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest":
        return True
    accept = request.accept_mimetypes
    if not accept or accept.provided is False:
        return True
    return accept["application/json"] >= accept["text/html"]


def json_response(
    status: int = 200,
    *,
    message: str | None = None,
    data: Mapping[str, Any] | None = None,
    **extra: Any,
) -> tuple[Response, int]:
    """Build a consistently shaped success envelope."""
    payload: dict[str, Any] = {"ok": 200 <= status < 400}
    if message is not None:
        payload["message"] = message
    if data is not None:
        payload["data"] = dict(data)
    payload.update(extra)
    return jsonify(payload), status


def error_response(
    status: int,
    message: str,
    *,
    errors: Mapping[str, list[str]] | None = None,
    code: str | None = None,
    **extra: Any,
) -> tuple[Response, int]:
    """Build a consistently shaped error envelope.

    ``errors`` maps a field name to its validation messages, so a future
    front-end can attach messages to the right input without parsing prose.
    """
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"status": status, "code": code or _default_code(status), "message": message},
    }
    if errors:
        payload["error"]["fields"] = {k: list(v) for k, v in errors.items()}
    payload.update(extra)
    return jsonify(payload), status


def form_errors(form) -> dict[str, list[str]]:
    """Flatten a WTForms error dict into ``{field: [messages]}``."""
    flattened: dict[str, list[str]] = {}
    for field_name, messages in form.errors.items():
        if isinstance(messages, dict):  # nested form fields
            for sub_name, sub_messages in messages.items():
                flattened[f"{field_name}.{sub_name}"] = [str(m) for m in sub_messages]
        else:
            flattened[field_name] = [str(m) for m in messages]
    return flattened


def is_safe_redirect_url(target: str | None) -> bool:
    """Reject off-site ``?next=`` values (open-redirect protection)."""
    if not target:
        return False
    host_url = urlsplit(request.host_url)
    candidate = urlsplit(urljoin(request.host_url, target))
    return candidate.scheme in {"http", "https"} and candidate.netloc == host_url.netloc


def safe_redirect_target(fallback_endpoint: str = "dashboard.index") -> str:
    """Resolve ``?next=`` if it points at this host, else the fallback route."""
    target = request.args.get("next") or (request.form.get("next") if request.form else None)
    if is_safe_redirect_url(target):
        return target  # type: ignore[return-value]
    return url_for(fallback_endpoint)


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    402: "insufficient_funds",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
}


def _default_code(status: int) -> str:
    return _STATUS_CODES.get(status, "error")
