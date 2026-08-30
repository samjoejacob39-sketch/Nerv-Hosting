"""Authenticated dashboard views.

Every view answers in the caller's preferred format: ``wants_json()`` sends API
clients the JSON envelope and browsers the Jinja templates, off the *same* query
results, so the two can never disagree.

:func:`deploy` is the one endpoint here that spends money, and its ordering is
deliberate.  The charge is atomic (``UPDATE ... WHERE credit_balance >= :cost``)
and **committed before** the outbound HTTP call: that way the refund UPDATE has
something real to reverse, and no database transaction is held open across a
ten-second wait on someone else's panel.
"""

from __future__ import annotations

import re
from decimal import Decimal

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.constants import (
    MAX_GUESTS_PER_SERVER,
    MAX_SERVERS_PER_USER,
    PANEL_STATE_TO_STATUS,
    PowerSignal,
    RamTier,
    ServerStatus,
    ServerType,
)
from app.dashboard import dashboard_bp
from app.dashboard.forms import DeployServerForm
from app.dns_client import CloudflareDNSError, get_dns_client
from app.extensions import csrf, db, limiter
from app.models.server import Server
from app.models.shared_access import SharedAccess, SharedAccessError
from app.models.user import User
from app.ptero_client import PterodactylError, get_client
from app.responses import error_response, form_errors, json_response, wants_json


# ---------------------------------------------------------------------- #
# Shared context
# ---------------------------------------------------------------------- #
def _limits(owned: list[Server]) -> dict[str, int]:
    """Slot accounting, identical for the template and the JSON envelope."""
    used = len(owned)
    return {
        "max_servers": MAX_SERVERS_PER_USER,
        "servers_used": used,
        "slots_remaining": max(0, MAX_SERVERS_PER_USER - used),
    }


def _render_dashboard(form: DeployServerForm, status: int = 200):
    """Render the overview around ``form``, re-reading the account's servers.

    Called both for a plain GET and to hand a failed deploy back to the browser,
    so the counts a user sees after an error are the post-failure truth.
    """
    owned = current_user.owned_servers
    page = render_template(
        "dashboard/index.html",
        owned_servers=owned,
        shared_servers=current_user.shared_servers,
        limits=_limits(owned),
        form=form,
    )
    return page if status == 200 else (page, status)


def _reject(form: DeployServerForm, status: int, message: str, *, code: str | None = None):
    """Fail a deploy in the caller's format.

    Browsers get the dashboard back at the failing status with the reason flashed
    and their typed input still in the form; API clients get the error envelope
    with per-field messages.
    """
    if wants_json():
        return error_response(status, message, errors=form_errors(form), code=code)
    flash(message, "error")
    return _render_dashboard(form, status)


# ---------------------------------------------------------------------- #
# Credit rollback
# ---------------------------------------------------------------------- #
def _refund(cost: Decimal) -> None:
    """Reverse a charge with ``UPDATE ... SET credit_balance = credit_balance + :cost``.

    Failures are logged, never raised: this only ever runs while we are already
    handling another error, and masking that error with a second one would leave
    the caller with no idea what went wrong.  A failed refund is an operator
    problem, hence ``critical``.
    """
    try:
        current_user.add_credits(cost)
        db.session.commit()
    except (SQLAlchemyError, ValueError):  # pragma: no cover - defensive
        db.session.rollback()
        current_app.logger.critical(
            "Failed to refund %s credits to user id=%s; balance is now short.",
            cost,
            current_user.id,
        )


def _discard_orphan(panel_server_id: int) -> None:
    """Destroy a panel server we could not record locally.

    Without this, a failed INSERT after a successful build leaves a container
    running that nothing in our database points at -- unbillable and invisible.
    """
    try:
        with get_client() as panel:
            panel.delete_server(panel_server_id, force=True)
    except PterodactylError:
        current_app.logger.critical(
            "Orphaned panel server id=%s: not recorded locally and not deletable.",
            panel_server_id,
        )


# ---------------------------------------------------------------------- #
# Read-only views
# ---------------------------------------------------------------------- #
@dashboard_bp.get("/")
def index():
    """Overview: the account, its servers, and servers shared with it."""
    owned = current_user.owned_servers
    shared = current_user.shared_servers

    if wants_json():
        return json_response(
            data={
                "user": current_user.to_dict(include_email=True),
                "owned_servers": [server.to_dict() for server in owned],
                "shared_servers": [server.to_dict(include_owner=True) for server in shared],
                "limits": _limits(owned),
            }
        )

    # The template receives the model objects, not the serialised dicts: it needs
    # the ``tier`` / ``created_at_utc`` properties that ``to_dict`` flattens away.
    return _render_dashboard(DeployServerForm())


@dashboard_bp.get("/credits")
def credits():
    return json_response(
        data={"credit_balance": str(current_user.credits), "currency": "credits"}
    )


@dashboard_bp.get("/tiers")
def tiers():
    """The purchasable container sizes, for a plan-picker UI."""
    return json_response(
        data={
            "tiers": [
                {
                    "ram_tier": int(tier),
                    "label": tier.label,
                    "ram_mb": tier.ram_mb,
                    "disk_mb": tier.disk_mb,
                    "cpu_percent": tier.cpu_percent,
                    "startup_credits": str(tier.startup_credits),
                    "hourly_credits": str(tier.hourly_credits),
                    "deployable": tier.deployable,
                }
                for tier in RamTier
            ]
        }
    )


# ---------------------------------------------------------------------- #
# Provisioning
# ---------------------------------------------------------------------- #
@dashboard_bp.post("/deploy")
@limiter.limit(
    lambda: current_app.config["DEPLOY_RATELIMIT"],
    error_message="Too many deployments. Please wait before starting another.",
)
def deploy():
    """Provision a Minecraft container on the panel and charge for it.

    The sequence, and why it is in this order:

    1. Refuse if the account is out of slots -- cheapest check, no side effects.
    2. Validate the inputs (tier in the Minecraft allowlist, name well-formed
       and not already used by this account).
    3. Debit the startup cost atomically and commit.  ``deduct_credits`` carries
       its own ``WHERE credit_balance >= :cost`` guard, so two simultaneous
       deploys cannot both spend the same credits: the loser gets the 402.
    4. Ask the panel for a free port, then create the server.
    5. Record the row.  Anything that fails from step 4 onwards refunds.
    """
    form = DeployServerForm()

    owned = current_user.owned_servers
    if len(owned) >= MAX_SERVERS_PER_USER:
        return _reject(
            form,
            409,
            f"You are already using all {MAX_SERVERS_PER_USER} of your container slots.",
            code="server_limit_reached",
        )

    if not form.validate_on_submit():
        return _reject(form, 422, "Those deployment details are invalid.")

    tier = form.tier
    name = form.server_name.data
    cost = tier.startup_credits

    # -- 3. Charge, and make the charge durable before leaving the process --
    if not current_user.deduct_credits(cost):
        db.session.rollback()
        return _reject(
            form,
            402,
            f"A {tier.ram_gb} GB server costs {cost} credits and your balance is "
            f"{current_user.credits}.",
            code="insufficient_funds",
        )
    try:
        db.session.commit()
    except SQLAlchemyError:  # pragma: no cover - defensive
        db.session.rollback()
        current_app.logger.exception("Could not commit the deploy charge")
        return _reject(form, 503, "Could not reserve your credits. Please try again.")

    # -- 4. Talk to the panel ----------------------------------------------
    config = current_app.config
    try:
        with get_client() as panel:
            allocation = panel.get_free_allocation(config["PTERO_NODE_ID"])
            created = panel.create_server(
                user_id=config["PTERO_OWNER_USER_ID"],
                name=name,
                memory_mb=tier.ram_mb,
                disk_mb=tier.disk_mb,
                cpu_limit=tier.cpu_percent,
                nest_id=config["PTERO_MINECRAFT_NEST_ID"],
                egg_id=config["PTERO_MINECRAFT_EGG_ID"],
                allocation_id=int(allocation["id"]),
                docker_image=config["PTERO_MINECRAFT_IMAGE"],
            )
    except PterodactylError as exc:
        # Covers timeouts, refused connections, a 4xx/5xx from the panel and a
        # node with no free ports.  ``http_status`` is 502 for "the panel is
        # broken" and 503 for "our side is not ready" (misconfigured, or full).
        _refund(cost)
        current_app.logger.error(
            "Deploy failed for user id=%s (%s): %s", current_user.id, exc.code, exc
        )
        return _reject(
            form,
            exc.http_status,
            "The hosting panel could not build your server. Your credits have "
            "been refunded -- please try again shortly.",
            code=exc.code,
        )

    # -- 5. Record it -------------------------------------------------------
    panel_id = int(created["id"])
    server = Server(
        owner_id=current_user.id,
        name=name,
        ram_tier=int(tier),
        server_type=ServerType.MINECRAFT,
        pterodactyl_server_id=panel_id,
        # The panel is technically still installing; we asked it to start on
        # completion, so "running" is where this row is headed and what the
        # brief specifies.  A status reconciler can correct it later.
        status=ServerStatus.RUNNING,
    )
    db.session.add(server)
    try:
        db.session.commit()
    except SQLAlchemyError:
        # Includes the IntegrityError from losing a name race after the form's
        # advisory check passed.  The container exists upstream but nothing here
        # points at it, so tear it down rather than leak it.
        db.session.rollback()
        current_app.logger.exception(
            "Built panel server id=%s for user id=%s but could not record it",
            panel_id,
            current_user.id,
        )
        _refund(cost)
        _discard_orphan(panel_id)
        return _reject(
            form,
            502,
            "Your server was built but could not be saved. Your credits have "
            "been refunded.",
            code="server_record_failed",
        )

    current_app.logger.info(
        "Deployed server id=%s panel_id=%s tier=%sGB for user id=%s (%s credits)",
        server.id,
        panel_id,
        tier.ram_gb,
        current_user.id,
        cost,
    )

    if wants_json():
        return json_response(
            201,
            message=f"{server.name} is being built.",
            data={
                "server": server.to_dict(),
                "allocation": {
                    "ip": allocation.get("ip"),
                    "port": allocation.get("port"),
                    "alias": allocation.get("alias"),
                },
                "charged": str(cost),
                "credit_balance": str(current_user.credits),
            },
        )

    flash(f"{server.name} is being built and will be online shortly.", "success")
    return redirect(url_for("dashboard.index"))


# ---------------------------------------------------------------------- #
# Server Management & Power Controls
# ---------------------------------------------------------------------- #
@dashboard_bp.post("/server/<int:server_id>/power")
@login_required
def power(server_id: int):
    """Send a power action signal (start, stop, restart, kill) to a server.

    Permissions: Server owner or an authorized guest with SharedAccess.
    """
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.can_access(server):
        return error_response(
            403,
            "You do not have permission to control this server.",
            code="forbidden",
        )

    data = request.get_json(silent=True) or {}
    signal = str(data.get("signal") or request.form.get("signal") or "").strip().lower()

    if not signal or signal not in PowerSignal.ALL:
        valid = ", ".join(PowerSignal.ALL)
        return error_response(
            422,
            f"Invalid power signal {signal!r}. Must be one of: {valid}.",
            code="invalid_signal",
        )

    if not server.is_provisioned or not server.pterodactyl_server_id:
        return error_response(
            400,
            "This server has not been provisioned on the hosting panel yet.",
            code="server_not_provisioned",
        )

    try:
        with get_client() as panel:
            panel.send_power_signal(server.pterodactyl_server_id, signal)
    except PterodactylError as exc:
        current_app.logger.error(
            "Power action '%s' failed for server id=%s (panel_id=%s) by user id=%s: %s",
            signal,
            server.id,
            server.pterodactyl_server_id,
            current_user.id,
            exc,
        )
        if wants_json():
            return error_response(
                exc.http_status,
                f"The hosting panel could not execute '{signal}': {exc}",
                code=exc.code,
            )
        flash(f"Power action failed: {exc}", "error")
        return redirect(url_for("dashboard.index"))

    new_status = PowerSignal.RESULTING_STATUS.get(signal, ServerStatus.STARTING)
    server.set_status(new_status)
    db.session.commit()

    current_app.logger.info(
        "Power signal '%s' sent to server id=%s by user id=%s -> status=%s",
        signal,
        server.id,
        current_user.id,
        new_status,
    )

    if wants_json():
        return json_response(
            200,
            message=f"Power signal '{signal}' sent to {server.name}.",
            data={"server": server.to_dict(), "signal": signal, "status": server.status},
        )

    flash(f"Power signal '{signal}' sent to {server.name}.", "success")
    return redirect(url_for("dashboard.index"))


@dashboard_bp.get("/server/<int:server_id>/status")
@login_required
def status(server_id: int):
    """Poll live container status and resources from Pterodactyl."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.can_access(server):
        return error_response(403, "Forbidden.", code="forbidden")

    resources = None
    if server.is_provisioned and server.pterodactyl_server_id:
        try:
            with get_client() as panel:
                resources = panel.get_server_status(server.pterodactyl_server_id)
                current_state = resources.get("current_state")
                if current_state in PANEL_STATE_TO_STATUS:
                    mapped = PANEL_STATE_TO_STATUS[current_state]
                    if server.status != mapped:
                        server.set_status(mapped)
                        db.session.commit()
        except PterodactylError as exc:
            current_app.logger.warning(
                "Could not poll resources for server id=%s: %s", server.id, exc
            )

    return json_response(
        200,
        data={
            "server": server.to_dict(),
            "status": server.status,
            "resources": resources,
        },
    )


# ---------------------------------------------------------------------- #
# Shared Access / Collaboration
# ---------------------------------------------------------------------- #
@dashboard_bp.post("/server/<int:server_id>/share")
@login_required
def share(server_id: int):
    """Grant co-management access to another user (Owner-only)."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.owns(server) and not current_user.is_admin:
        return error_response(
            403,
            "Only the server owner can manage shared access.",
            code="forbidden",
        )

    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or request.form.get("username") or "").strip()

    if not username:
        return error_response(422, "Username is required.", code="validation_error")

    target_user = User.by_username(username)
    if target_user is None:
        return error_response(
            404,
            f"User '{username}' does not exist.",
            code="user_not_found",
        )

    try:
        grant = SharedAccess.grant(server, target_user)
        db.session.commit()
    except SharedAccessError as exc:
        db.session.rollback()
        msg = str(exc)
        if "already has access" in msg:
            return error_response(409, msg, code="duplicate_share")
        if "at most" in msg:
            return error_response(409, msg, code="max_guests_reached")
        if "owner" in msg.lower():
            return error_response(422, msg, code="self_share")
        return error_response(422, msg, code="invalid_grant")

    current_app.logger.info(
        "User id=%s shared server id=%s with guest id=%s (@%s)",
        current_user.id,
        server.id,
        target_user.id,
        target_user.username,
    )

    if wants_json():
        return json_response(
            201,
            message=f"Access granted to {target_user.username}.",
            data={
                "grant": grant.to_dict(),
                "server": server.to_dict(),
                "guests": [g.to_dict() for g in server.shared_accesses],
            },
        )

    flash(f"Access granted to {target_user.username}.", "success")
    return redirect(url_for("dashboard.index"))


@dashboard_bp.post("/server/<int:server_id>/unshare")
@login_required
def unshare(server_id: int):
    """Revoke co-management access for a guest user (Owner-only)."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.owns(server) and not current_user.is_admin:
        return error_response(
            403,
            "Only the server owner can manage shared access.",
            code="forbidden",
        )

    data = request.get_json(silent=True) or {}
    user_id_raw = data.get("user_id") or request.form.get("user_id")
    username_raw = data.get("username") or request.form.get("username")

    guest_user_id: int | None = None
    if user_id_raw is not None:
        try:
            guest_user_id = int(user_id_raw)
        except (ValueError, TypeError):
            return error_response(422, "Invalid user_id.", code="validation_error")
    elif username_raw:
        guest = User.by_username(str(username_raw))
        if guest:
            guest_user_id = guest.id
        else:
            return error_response(404, f"User '{username_raw}' not found.", code="user_not_found")
    else:
        return error_response(422, "Either user_id or username is required.", code="validation_error")

    revoked = SharedAccess.revoke(server.id, guest_user_id)
    if not revoked:
        return error_response(
            404,
            "That user does not have shared access to this server.",
            code="grant_not_found",
        )

    db.session.commit()
    current_app.logger.info(
        "User id=%s revoked access for guest id=%s on server id=%s",
        current_user.id,
        guest_user_id,
        server.id,
    )

    if wants_json():
        return json_response(
            200,
            message="Shared access revoked.",
            data={"server_id": server.id, "revoked_user_id": guest_user_id},
        )

    flash("Shared access revoked.", "info")
    return redirect(url_for("dashboard.index"))


@dashboard_bp.get("/server/<int:server_id>/guests")
@login_required
def guests(server_id: int):
    """List all guest collaborators for a server."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.can_access(server):
        return error_response(403, "Forbidden.", code="forbidden")

    return json_response(
        200,
        data={
            "server_id": server.id,
            "guests": [grant.to_dict() for grant in server.shared_accesses],
        },
    )


# ---------------------------------------------------------------------- #
# Ad Monetization & Credit Reward Webhook
# ---------------------------------------------------------------------- #
@dashboard_bp.post("/api/credit-reward")
@csrf.exempt
def credit_reward():
    """External ad-network callback / credit reward webhook.

    Validates the X-Ad-Reward-Secret header and atomically adds credits to the user.
    """
    secret = request.headers.get("X-Ad-Reward-Secret") or ""
    expected_secret = current_app.config.get("AD_WEBHOOK_SECRET") or ""

    if not secret or secret != expected_secret:
        current_app.logger.warning("Unauthorized credit-reward webhook attempt with secret: %r", secret)
        return error_response(401, "Unauthorized: Invalid or missing webhook secret.", code="unauthorized")

    data = request.get_json(silent=True) or {}
    user_id_raw = data.get("user_id")
    reward_amount_raw = data.get("reward_amount")

    if user_id_raw is None or reward_amount_raw is None:
        return error_response(422, "Missing user_id or reward_amount in payload.", code="validation_error")

    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        return error_response(422, "user_id must be an integer.", code="validation_error")

    user = db.session.get(User, user_id)
    if user is None:
        return error_response(404, f"User id {user_id} not found.", code="user_not_found")

    try:
        reward_amount = User.quantise_credits(reward_amount_raw)
        if reward_amount <= Decimal("0"):
            return error_response(422, "reward_amount must be greater than zero.", code="validation_error")
    except (ValueError, TypeError):
        return error_response(422, "Invalid reward_amount format.", code="validation_error")

    # Atomically add credits
    user.add_credits(reward_amount)
    db.session.commit()

    current_app.logger.info(
        "Credit reward webhook: Added %s credits to user id=%s (@%s). New balance: %s",
        reward_amount,
        user.id,
        user.username,
        user.credits,
    )

    return json_response(
        200,
        message=f"Successfully credited {reward_amount} credits to {user.username}.",
        data={
            "user_id": user.id,
            "username": user.username,
            "reward_amount": str(reward_amount),
            "credit_balance": str(user.credits),
        },
    )


# ---------------------------------------------------------------------- #
# Phase 6: Live Console & WebSocket Terminal
# ---------------------------------------------------------------------- #
@dashboard_bp.get("/server/<int:server_id>/console-token")
@login_required
def console_token(server_id: int):
    """Generate a WebSocket authentication token for the live server console."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.can_access(server):
        return error_response(403, "Forbidden.", code="forbidden")

    if not server.is_provisioned:
        return error_response(400, "Server is not yet provisioned.", code="server_not_provisioned")

    try:
        with get_client() as panel:
            credentials = panel.get_websocket_credentials(server.pterodactyl_server_id)
    except PterodactylError as exc:
        current_app.logger.error("Failed to fetch websocket credentials for server %s: %s", server.id, exc)
        return error_response(exc.status_code or 502, f"Panel error: {exc}", code="panel_error")

    return json_response(
        200,
        data={
            "server_id": server.id,
            "token": credentials.get("token"),
            "socket": credentials.get("socket"),
        },
    )


# ---------------------------------------------------------------------- #
# Phase 6: File Manager & Backups
# ---------------------------------------------------------------------- #
@dashboard_bp.get("/server/<int:server_id>/files")
@login_required
def files(server_id: int):
    """List directory contents on the server container."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.can_access(server):
        return error_response(403, "Forbidden.", code="forbidden")

    if not server.is_provisioned:
        return error_response(400, "Server is not yet provisioned.", code="server_not_provisioned")

    directory = request.args.get("directory", "/")
    try:
        with get_client() as panel:
            file_list = panel.list_files(server.pterodactyl_server_id, directory=directory)
    except PterodactylError as exc:
        current_app.logger.error("Failed to list files for server %s: %s", server.id, exc)
        return error_response(exc.status_code or 502, f"Panel error: {exc}", code="panel_error")

    return json_response(200, data={"server_id": server.id, "directory": directory, "files": file_list})


@dashboard_bp.get("/server/<int:server_id>/files/content")
@login_required
def file_content(server_id: int):
    """Read file contents on the server container."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.can_access(server):
        return error_response(403, "Forbidden.", code="forbidden")

    if not server.is_provisioned:
        return error_response(400, "Server is not yet provisioned.", code="server_not_provisioned")

    file_path = request.args.get("file", "")
    if not file_path:
        return error_response(422, "Missing file parameter.", code="validation_error")

    try:
        with get_client() as panel:
            content = panel.read_file(server.pterodactyl_server_id, file_path=file_path)
    except PterodactylError as exc:
        current_app.logger.error("Failed to read file %r on server %s: %s", file_path, server.id, exc)
        return error_response(exc.status_code or 502, f"Panel error: {exc}", code="panel_error")

    return json_response(200, data={"server_id": server.id, "file": file_path, "content": content})


@dashboard_bp.post("/server/<int:server_id>/files/save")
@login_required
def file_save(server_id: int):
    """Save/write file contents on the server container."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.can_access(server):
        return error_response(403, "Forbidden.", code="forbidden")

    if not server.is_provisioned:
        return error_response(400, "Server is not yet provisioned.", code="server_not_provisioned")

    data = request.get_json(silent=True) or request.form
    file_path = data.get("file")
    content = data.get("content", "")

    if not file_path:
        return error_response(422, "Missing file parameter.", code="validation_error")

    try:
        with get_client() as panel:
            panel.save_file(server.pterodactyl_server_id, file_path=file_path, content=content)
    except PterodactylError as exc:
        current_app.logger.error("Failed to write file %r on server %s: %s", file_path, server.id, exc)
        return error_response(exc.status_code or 502, f"Panel error: {exc}", code="panel_error")

    return json_response(200, message=f"File {file_path} saved successfully.", data={"server_id": server.id, "file": file_path})


@dashboard_bp.post("/server/<int:server_id>/backups")
@login_required
def backups(server_id: int):
    """Trigger a manual backup for the server container."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.can_access(server):
        return error_response(403, "Forbidden.", code="forbidden")

    if not server.is_provisioned:
        return error_response(400, "Server is not yet provisioned.", code="server_not_provisioned")

    try:
        with get_client() as panel:
            backup_data = panel.create_backup(server.pterodactyl_server_id)
    except PterodactylError as exc:
        current_app.logger.error("Failed to create backup for server %s: %s", server.id, exc)
        return error_response(exc.status_code or 502, f"Panel error: {exc}", code="panel_error")

    return json_response(201, message="Backup creation started.", data={"server_id": server.id, "backup": backup_data})


# ---------------------------------------------------------------------- #
# Phase 6: Cloudflare DNS Subdomain Management
# ---------------------------------------------------------------------- #
SUBDOMAIN_REGEX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])?$")


@dashboard_bp.post("/server/<int:server_id>/subdomain")
@login_required
def subdomain_claim(server_id: int):
    """Claim a custom subdomain and generate corresponding Cloudflare DNS record."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.owns(server) and not current_user.is_admin:
        return error_response(403, "Only the server owner can configure subdomains.", code="forbidden")

    data = request.get_json(silent=True) or request.form
    subdomain_raw = (data.get("subdomain") or "").strip().lower()

    if not subdomain_raw or not SUBDOMAIN_REGEX.match(subdomain_raw):
        return error_response(
            422,
            "Invalid subdomain format. Must be 1-32 alphanumeric characters and hyphens.",
            code="invalid_subdomain",
        )

    # Check for existing claims in DB
    existing = db.session.scalar(
        select(Server).where(Server.subdomain == subdomain_raw, Server.id != server.id)
    )
    if existing is not None:
        return error_response(409, "This subdomain is already claimed.", code="subdomain_taken")

    target_ip = data.get("target_ip") or current_app.config.get("DEFAULT_NODE_IP", "127.0.0.1")
    port = data.get("port")

    dns_client = get_dns_client()
    try:
        # Delete old record if server had a different subdomain previously
        if server.cloudflare_record_id:
            dns_client.delete_subdomain_record(server.cloudflare_record_id)

        record = dns_client.create_subdomain_record(
            subdomain=subdomain_raw,
            target_ip=target_ip,
            port=port,
        )
    except CloudflareDNSError as exc:
        current_app.logger.error("Cloudflare DNS error for subdomain %s: %s", subdomain_raw, exc)
        return error_response(exc.status_code or 502, f"DNS error: {exc}", code="dns_error")

    server.subdomain = subdomain_raw
    server.cloudflare_record_id = record.get("id")
    db.session.commit()

    return json_response(
        200,
        message=f"Subdomain {record.get('full_domain')} configured successfully.",
        data={
            "server_id": server.id,
            "subdomain": server.subdomain,
            "full_domain": record.get("full_domain"),
            "record_id": server.cloudflare_record_id,
        },
    )


@dashboard_bp.delete("/server/<int:server_id>/subdomain")
@login_required
def subdomain_release(server_id: int):
    """Release a custom subdomain and delete its Cloudflare DNS record."""
    server = db.session.get(Server, server_id)
    if server is None:
        return error_response(404, "Server not found.", code="server_not_found")

    if not current_user.owns(server) and not current_user.is_admin:
        return error_response(403, "Only the server owner can release subdomains.", code="forbidden")

    if not server.subdomain:
        return error_response(404, "No subdomain is configured for this server.", code="no_subdomain")

    if server.cloudflare_record_id:
        try:
            get_dns_client().delete_subdomain_record(server.cloudflare_record_id)
        except CloudflareDNSError as exc:
            current_app.logger.warning("Error deleting DNS record %s: %s", server.cloudflare_record_id, exc)

    server.subdomain = None
    server.cloudflare_record_id = None
    db.session.commit()

    return json_response(200, message="Subdomain released successfully.", data={"server_id": server.id})








