"""Domain constants shared across models, services and templates."""

from __future__ import annotations

from decimal import Decimal
from enum import IntEnum


class RamTier(IntEnum):
    """Purchasable container sizes, valued in **gigabytes of RAM**.

    ``Server.ram_tier`` stores the integer value of one of these members, so the
    value is both the primary key of the pricing table and a human-readable
    "how big is it" -- ``RamTier.PLUS == 6`` means six gigabytes.

    Not every tier is deployable for every workload: Minecraft needs at least
    2 GB to be usable, so :data:`MINECRAFT_RAM_TIERS` is the allowlist the deploy
    route validates against, and ``FREE`` is reserved for the lighter
    bot/web containers.
    """

    FREE = 1
    BASIC = 2
    STANDARD = 4
    PLUS = 6
    PRO = 8

    @property
    def ram_gb(self) -> int:
        return int(self)

    @property
    def label(self) -> str:
        return _TIER_SPECS[self]["label"]

    @property
    def ram_mb(self) -> int:
        return self.ram_gb * 1024

    @property
    def disk_mb(self) -> int:
        return _TIER_SPECS[self]["disk_mb"]

    @property
    def cpu_percent(self) -> int:
        return _TIER_SPECS[self]["cpu_percent"]

    @property
    def hourly_credits(self) -> Decimal:
        """Credits burned per hour while the container runs."""
        return _TIER_SPECS[self]["hourly_credits"]

    @property
    def startup_credits(self) -> Decimal:
        """One-off charge taken when the container is provisioned."""
        return RAM_TIER_COSTS[self.ram_gb]

    @property
    def deployable(self) -> bool:
        """True when this tier may back a Minecraft server."""
        return self.ram_gb in MINECRAFT_RAM_TIERS

    @classmethod
    def coerce(cls, value: object) -> "RamTier":
        """Convert user input to a tier, raising ``ValueError`` when invalid."""
        try:
            return cls(int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            valid = ", ".join(str(int(t)) for t in cls)
            raise ValueError(f"Unknown RAM tier {value!r}. Valid tiers: {valid}.") from None


_TIER_SPECS: dict[RamTier, dict] = {
    RamTier.FREE: {
        "label": "Free",
        "disk_mb": 4096,
        "cpu_percent": 50,
        "hourly_credits": Decimal("0"),
    },
    RamTier.BASIC: {
        "label": "Basic",
        "disk_mb": 10240,
        "cpu_percent": 100,
        "hourly_credits": Decimal("1.0000"),
    },
    RamTier.STANDARD: {
        "label": "Standard",
        "disk_mb": 20480,
        "cpu_percent": 150,
        "hourly_credits": Decimal("2.0000"),
    },
    RamTier.PLUS: {
        "label": "Plus",
        "disk_mb": 30720,
        "cpu_percent": 200,
        "hourly_credits": Decimal("3.0000"),
    },
    RamTier.PRO: {
        "label": "Pro",
        "disk_mb": 40960,
        "cpu_percent": 250,
        "hourly_credits": Decimal("4.0000"),
    },
}

RAM_TIER_VALUES: tuple[int, ...] = tuple(int(tier) for tier in RamTier)

#: Gigabytes of RAM -> one-off credit cost to provision a container at that size.
#: Keyed by the plain integer (not the enum) so the table reads as a price list
#: and can be re-tuned from configuration without touching the enum.
RAM_TIER_COSTS: dict[int, Decimal] = {
    1: Decimal("0.0000"),
    2: Decimal("25.0000"),
    4: Decimal("45.0000"),
    6: Decimal("65.0000"),
    8: Decimal("85.0000"),
}

#: The sizes a Minecraft server may be deployed at.  Anything smaller than 2 GB
#: cannot hold a modern Paper server, so the deploy route refuses it outright.
MINECRAFT_RAM_TIERS: tuple[int, ...] = (2, 4, 6, 8)


class ServerType:
    """What software a container runs.

    Plain strings, like :class:`ServerStatus`, so the column stays readable in
    the database and comparable in templates.
    """

    MINECRAFT = "minecraft"
    #: Reserved for the generic bot/web containers the platform started with.
    GENERIC = "generic"

    ALL: tuple[str, ...] = (MINECRAFT, GENERIC)


class ServerStatus:
    """Lifecycle states for a hosted container.

    Plain string constants (not an Enum) so the column stays human-readable in
    the database and trivially comparable in Jinja templates.

    ``STARTING`` and ``STOPPING`` mirror the panel's own transitional states: a
    power signal is an *asynchronous request*, so the row records "we asked for
    this" and :data:`PANEL_STATE_TO_STATUS` corrects it from the panel's live
    answer on the next poll.
    """

    PENDING = "pending"          # row created, not yet sent to the panel
    INSTALLING = "installing"    # panel is building the container
    STARTING = "starting"        # start/restart signal sent, not yet up
    RUNNING = "running"
    STOPPING = "stopping"        # stop signal sent, still shutting down
    STOPPED = "stopped"
    SUSPENDED = "suspended"      # out of credits or policy hold
    ERROR = "error"
    DELETING = "deleting"

    ALL: tuple[str, ...] = (
        PENDING,
        INSTALLING,
        STARTING,
        RUNNING,
        STOPPING,
        STOPPED,
        SUSPENDED,
        ERROR,
        DELETING,
    )
    #: States in which the container consumes credits.  A container that has been
    #: asked to start is already holding memory on the node, so it pays.
    BILLABLE: frozenset[str] = frozenset({RUNNING, STARTING})
    #: States from which a user may trigger a start.
    STARTABLE: frozenset[str] = frozenset({STOPPED, ERROR})
    #: States from which a stop makes sense.
    STOPPABLE: frozenset[str] = frozenset({RUNNING, STARTING})
    #: Waiting on the panel: the UI polls these rather than trusting them.
    TRANSIENT: frozenset[str] = frozenset({PENDING, INSTALLING, STARTING, STOPPING, DELETING})
    #: A power signal is pointless (or destructive) in these states.
    POWER_LOCKED: frozenset[str] = frozenset({PENDING, DELETING})


class PowerSignal:
    """Signals the panel's client API accepts on ``/power``.

    ``KILL`` is deliberately **not** in :data:`USER_TRIGGERED`: it SIGKILLs the
    container, which for a Minecraft server means an unsaved world.  It stays
    available to operators through the client method.
    """

    START = "start"
    STOP = "stop"
    RESTART = "restart"
    KILL = "kill"

    ALL: tuple[str, ...] = (START, STOP, RESTART, KILL)
    #: What the dashboard's Start / Stop / Restart buttons may send.
    USER_TRIGGERED: tuple[str, ...] = (START, STOP, RESTART)

    #: The state a row moves to the moment a signal is accepted.  Optimistic by
    #: design -- the panel is still working -- and reconciled by a status poll.
    RESULTING_STATUS: dict[str, str] = {
        START: ServerStatus.STARTING,
        RESTART: ServerStatus.STARTING,
        STOP: ServerStatus.STOPPING,
        KILL: ServerStatus.STOPPED,
    }


#: Panel ``current_state`` -> our ``servers.status``.  The panel reports four
#: states; anything unrecognised is left alone rather than guessed at.
PANEL_STATE_TO_STATUS: dict[str, str] = {
    "running": ServerStatus.RUNNING,
    "starting": ServerStatus.STARTING,
    "stopping": ServerStatus.STOPPING,
    "offline": ServerStatus.STOPPED,
}



# ---------------------------------------------------------------------- #
# Pterodactyl deployment defaults
# ---------------------------------------------------------------------- #
# These are the fallbacks: ``config.py`` reads each one from the environment
# using the value below as its default, and application code should prefer
# ``current_app.config[...]`` so a deployment can retarget its panel without a
# code change.  A panel's own node/nest/egg ids are installation-specific --
# 1/1/1 is only ever right for a stock single-node install.

#: Node the containers are placed on.
DEFAULT_PTERO_NODE_ID = 1
#: Panel user account that owns every container we create.  We do not mirror our
#: accounts into the panel: the platform is the tenant, and access is mediated by
#: our own ``SharedAccess`` rows, so one service account owns the lot.
DEFAULT_PTERO_OWNER_USER_ID = 1
#: Nest holding the Minecraft eggs (stock panels ship "Minecraft" as nest 1).
MINECRAFT_NEST_ID = 1
#: Egg inside that nest.  On a stock panel egg 1 is Vanilla and egg 3 is Paper;
#: Paper is the sane default for a hosted server.
MINECRAFT_EGG_ID = 3
#: Java image the egg boots with.
MINECRAFT_DOCKER_IMAGE = "ghcr.io/pterodactyl/yolks:java_17"
#: Startup command; the placeholders are expanded by the panel, not by us.
MINECRAFT_STARTUP = (
    "java -Xms128M -XX:MaxRAMPercentage=95.0 -jar {{SERVER_JARFILE}}"
)
#: Egg variables the Paper egg requires.  ``latest`` lets the egg's install
#: script resolve the newest build at provision time.
MINECRAFT_ENVIRONMENT: dict[str, str] = {
    "SERVER_JARFILE": "server.jar",
    "MINECRAFT_VERSION": "latest",
    "BUILD_NUMBER": "latest",
}
#: Panel resources a Minecraft server is allowed beyond CPU/RAM/disk.
MINECRAFT_FEATURE_LIMITS: dict[str, int] = {
    "databases": 0,
    "allocations": 1,
    "backups": 1,
}
#: Seconds to wait on any single Pterodactyl API call.
PTERO_TIMEOUT_SECONDS = 10


#: Hard ceiling on concurrently owned servers per account.
MAX_SERVERS_PER_USER = 3
#: Hard ceiling on guests a single server may be shared with.
MAX_GUESTS_PER_SERVER = 5

#: Credit amounts are stored as NUMERIC(12, 4).
CREDIT_PRECISION = 12
CREDIT_SCALE = 4
CREDIT_QUANTUM = Decimal("0.0001")
