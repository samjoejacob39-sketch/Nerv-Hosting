"""Admin blueprint routes for financial analytics, user management, and server monitoring."""

from __future__ import annotations

from decimal import Decimal

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func, select

from app.admin import admin_bp
from app.admin.decorators import admin_required
from app.constants import ServerStatus
from app.extensions import db
from app.models.server import Server
from app.models.user import User
from app.responses import error_response, json_response, wants_json


@admin_bp.get("/")
@admin_required
def index():
    """Admin dashboard landing view: financial analytics and container metrics."""
    total_users = db.session.scalar(select(func.count(User.id))) or 0
    active_users = db.session.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0
    total_credits = db.session.scalar(select(func.sum(User.credit_balance))) or Decimal("0.0000")

    total_servers = db.session.scalar(select(func.count(Server.id))) or 0
    running_servers = list(
        db.session.scalars(
            select(Server).where(Server.status.in_(ServerStatus.BILLABLE))
        ).all()
    )
    active_containers_count = len(running_servers)
    total_ram_mb = sum(s.ram_mb for s in running_servers)
    hourly_run_rate = sum(s.hourly_credits for s in running_servers)

    metrics = {
        "total_users": total_users,
        "active_users": active_users,
        "total_credits": str(total_credits),
        "total_servers": total_servers,
        "active_containers": active_containers_count,
        "total_ram_mb": total_ram_mb,
        "hourly_run_rate": str(hourly_run_rate),
    }

    if wants_json():
        return json_response(200, data=metrics)

    return render_template("admin/index.html", metrics=metrics)


@admin_bp.get("/users")
@admin_required
def users():
    """User search and list interface."""
    q = (request.args.get("q") or "").strip().lower()
    stmt = select(User).order_by(User.id.desc())

    if q:
        stmt = stmt.where(
            (func.lower(User.username).contains(q)) | (func.lower(User.email).contains(q))
        )

    users_list = list(db.session.scalars(stmt.limit(50)).all())

    if wants_json():
        return json_response(
            200,
            data={"users": [u.to_dict() for u in users_list], "query": q},
        )

    return render_template("admin/users.html", users=users_list, query=q)


@admin_bp.post("/users/<int:user_id>/credits")
@admin_required
def adjust_credits(user_id: int):
    """Manually add or deduct user credits."""
    user = db.session.get(User, user_id)
    if user is None:
        return error_response(404, "User not found.", code="user_not_found")

    data = request.get_json(silent=True) or request.form
    amount_raw = data.get("amount")
    action = (data.get("action") or "add").lower()

    if not amount_raw:
        return error_response(422, "Missing amount parameter.", code="validation_error")

    try:
        amount = User.quantise_credits(amount_raw)
        if amount <= Decimal("0"):
            return error_response(422, "Amount must be greater than zero.", code="validation_error")
    except (ValueError, TypeError):
        return error_response(422, "Invalid credit amount.", code="validation_error")

    if action == "deduct":
        if not user.deduct_credits(amount):
            return error_response(409, "User does not have enough credits to deduct.", code="insufficient_balance")
    else:
        user.add_credits(amount)

    db.session.commit()
    current_app.logger.info(
        "Admin user id=%s (%s) %sed %s credits for user id=%s (%s). New balance: %s",
        current_user.id,
        current_user.username,
        action,
        amount,
        user.id,
        user.username,
        user.credits,
    )

    if wants_json():
        return json_response(
            200,
            message=f"Successfully {action}ed {amount} credits.",
            data={"user_id": user.id, "credit_balance": str(user.credits)},
        )

    flash(f"Successfully {action}ed {amount} credits for @{user.username}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<int:user_id>/suspend")
@admin_required
def toggle_suspend_user(user_id: int):
    """Suspend or unsuspend a user account."""
    user = db.session.get(User, user_id)
    if user is None:
        return error_response(404, "User not found.", code="user_not_found")

    if user.id == current_user.id:
        return error_response(400, "Cannot suspend own admin account.", code="self_suspension")

    user.is_active = not user.is_active
    db.session.commit()

    action_label = "unsuspended" if user.is_active else "suspended"
    current_app.logger.info(
        "Admin user id=%s (%s) %s user id=%s (%s)",
        current_user.id,
        current_user.username,
        action_label,
        user.id,
        user.username,
    )

    if wants_json():
        return json_response(
            200,
            message=f"User @{user.username} has been {action_label}.",
            data={"user_id": user.id, "is_active": user.is_active},
        )

    flash(f"User @{user.username} has been {action_label}.", "info")
    return redirect(url_for("admin.users"))


@admin_bp.get("/servers")
@admin_required
def servers():
    """System-wide server monitoring overview."""
    stmt = select(Server).order_by(Server.id.desc()).limit(100)
    servers_list = list(db.session.scalars(stmt).all())

    if wants_json():
        return json_response(
            200,
            data={"servers": [s.to_dict(include_owner=True) for s in servers_list]},
        )

    return render_template("admin/servers.html", servers=servers_list)
