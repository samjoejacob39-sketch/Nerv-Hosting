"""Automated background billing and server hibernation scheduler."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from flask import Flask, current_app
from sqlalchemy import select

from app.constants import ServerStatus
from app.extensions import db, scheduler
from app.models.server import Server
from app.models.user import User
from app.ptero_client import PterodactylError, get_client


def process_hourly_billing(app: Flask | None = None) -> dict[str, Any]:
    """Execute the hourly billing loop across all active containers.

    1. Query all running/starting billable containers with a panel ID.
    2. Calculate hourly credit cost based on the server's ram_tier.
    3. Atomically deduct credits from the server owner's balance.
    4. Auto-suspension: If the owner has insufficient credits (<= 0),
       send a 'stop' power signal to Pterodactyl, mark server status as 'suspended',
       and log the suspension event.
    """
    ctx = app.app_context() if app else None
    if ctx:
        ctx.push()

    try:
        # Query active billable servers that have been provisioned on the panel
        stmt = (
            select(Server)
            .where(
                Server.status.in_(ServerStatus.BILLABLE),
                Server.pterodactyl_server_id.is_not(None),
            )
            .order_by(Server.id)
        )
        servers = list(db.session.scalars(stmt).all())

        processed_count = 0
        total_billed = Decimal("0.0000")
        suspended_count = 0

        current_app.logger.info(
            "Starting hourly billing cycle for %s active servers", len(servers)
        )

        for server in servers:
            processed_count += 1
            cost = server.hourly_credits
            owner = server.owner

            # Free tier servers have 0 cost
            if cost <= Decimal("0"):
                continue

            # Attempt atomic credit deduction
            deducted = owner.deduct_credits(cost)
            if deducted:
                db.session.commit()
                total_billed += cost
                current_app.logger.debug(
                    "Billed %s credits from user id=%s for server id=%s (tier=%sGB)",
                    cost,
                    owner.id,
                    server.id,
                    server.ram_tier,
                )
            else:
                # Insufficient balance -> Trigger automatic suspension and power stop
                db.session.rollback()
                suspended_count += 1
                current_app.logger.warning(
                    "User id=%s has insufficient balance (%s) for server id=%s (cost=%s). Triggering auto-suspension.",
                    owner.id,
                    owner.credits,
                    server.id,
                    cost,
                )

                # Update server status to suspended in DB
                server.set_status(ServerStatus.SUSPENDED)
                db.session.commit()

                # Send stop signal to the Pterodactyl panel
                if server.pterodactyl_server_id:
                    try:
                        with get_client() as panel:
                            panel.send_power_signal(server.pterodactyl_server_id, "stop")
                        current_app.logger.info(
                            "Sent 'stop' power signal to suspended server id=%s (panel_id=%s)",
                            server.id,
                            server.pterodactyl_server_id,
                        )
                    except PterodactylError as exc:
                        current_app.logger.error(
                            "Failed to stop suspended server id=%s on panel: %s",
                            server.id,
                            exc,
                        )

        summary = {
            "servers_processed": processed_count,
            "credits_billed": str(total_billed),
            "servers_suspended": suspended_count,
        }
        current_app.logger.info("Hourly billing cycle completed: %s", summary)
        return summary

    finally:
        if ctx:
            ctx.pop()


def init_scheduler(app: Flask) -> None:
    """Initialize and start the APScheduler background scheduler."""
    if not app.config.get("SCHEDULER_ENABLED", True) or app.config.get("TESTING"):
        app.logger.info("Scheduler is disabled or running in testing mode.")
        return

    scheduler.init_app(app)

    @scheduler.task(
        "interval",
        id="hourly_billing_job",
        minutes=60,
        misfire_grace_time=900,
    )
    def scheduled_hourly_billing():
        with app.app_context():
            process_hourly_billing()

    scheduler.start()
    app.logger.info("APScheduler initialized with hourly billing job.")
