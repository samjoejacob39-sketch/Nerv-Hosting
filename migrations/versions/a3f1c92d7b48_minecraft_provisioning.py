"""minecraft provisioning: servers.server_type, gigabyte-valued ram tiers

Revision ID: a3f1c92d7b48
Revises: e6464d47a6c5
Create Date: 2026-08-29 12:05:44.201118

Phase 3 changes two things about ``servers``:

* a ``server_type`` column, because the panel needs a nest/egg per workload and
  ``minecraft`` is now only one of them;
* ``ram_tier`` values are **gigabytes** (1, 2, 4, 6, 8) instead of the old
  ordinal ladder (1..4), so a tier doubles as its size and can key the price
  table.

The remap in :func:`upgrade` preserves each row's actual memory:

=============  =========  ==============================
old value      old RAM    new value
=============  =========  ==============================
1 (Free)       512 MB     1  (Free, 1 GB -- rounded up)
2 (Basic)      1024 MB    1  (Free, 1 GB)
3 (Standard)   2048 MB    2  (Basic, 2 GB)
4 (Pro)        4096 MB    4  (Standard, 4 GB)
=============  =========  ==============================

It runs before the CHECK constraint is replaced, and every value it writes is
legal under both the old and the new constraint, so the batch copy cannot fail
part-way.

SQLite needs ``batch_alter_table`` (copy-and-swap) for all of this, and needs
``copy_from`` in particular: it cannot reflect named CHECK constraints, so
without the explicit table below the recreate would silently drop
``ck_servers_status_known``.  For the same reason the helper spells out the
existing indexes -- batch mode rebuilds only what the passed table declares.

Constraint names are wrapped in ``op.f()`` throughout.  The app's metadata sets
``ck: "ck_%(table_name)s_%(constraint_name)s"``, and without ``op.f`` Alembic
applies that convention a second time to a name that already carries the prefix,
looking for ``ck_servers_ck_servers_ram_tier_known``.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3f1c92d7b48'
down_revision = 'e6464d47a6c5'
branch_labels = None
depends_on = None


_STATUSES = "'pending', 'installing', 'running', 'stopped', 'suspended', 'error', 'deleting'"
_OLD_TIERS = '1, 2, 3, 4'
_NEW_TIERS = '1, 2, 4, 6, 8'
_TYPES = "'minecraft', 'generic'"


def _servers_table(*, tier_check: str | None, with_type: bool) -> sa.Table:
    """``servers`` as it stands at a given point in this migration.

    ``tier_check`` is the ``ram_tier IN (...)`` list, or ``None`` for the window
    in which no tier constraint exists; ``with_type`` adds the ``server_type``
    column with its own constraint and index.
    """
    columns: list = [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=48), nullable=False),
        sa.Column('ram_tier', sa.Integer(), nullable=False),
        sa.Column('pterodactyl_server_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=24), server_default='pending', nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
    ]
    constraints: list = [
        sa.CheckConstraint(f'status IN ({_STATUSES})', name='ck_servers_status_known'),
        sa.ForeignKeyConstraint(
            ['owner_id'], ['users.id'], name='fk_servers_owner_id_users', ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id', name='pk_servers'),
        sa.UniqueConstraint('owner_id', 'name', name='uq_servers_owner_id_name'),
    ]
    indexes: list = [
        sa.Index('ix_servers_created_at', 'created_at'),
        sa.Index('ix_servers_owner_id', 'owner_id'),
        sa.Index('ix_servers_pterodactyl_server_id', 'pterodactyl_server_id', unique=True),
        sa.Index('ix_servers_status', 'status'),
    ]

    if tier_check:
        constraints.append(
            sa.CheckConstraint(
                f'ram_tier IN ({tier_check})', name='ck_servers_ram_tier_known'
            )
        )
    if with_type:
        columns.append(
            sa.Column(
                'server_type',
                sa.String(length=24),
                server_default='minecraft',
                nullable=False,
            )
        )
        constraints.append(
            sa.CheckConstraint(
                f'server_type IN ({_TYPES})', name='ck_servers_server_type_known'
            )
        )
        indexes.append(sa.Index('ix_servers_server_type', 'server_type'))

    return sa.Table('servers', sa.MetaData(), *columns, *constraints, *indexes)


def upgrade():
    # Memory-preserving remap, run while the old CHECK still allows every value
    # this writes (1, 2 and 4 are all in the old list as well as the new one).
    op.execute(
        sa.text(
            """
            UPDATE servers SET ram_tier = CASE ram_tier
                WHEN 1 THEN 1
                WHEN 2 THEN 1
                WHEN 3 THEN 2
                WHEN 4 THEN 4
                ELSE ram_tier
            END
            """
        )
    )

    with op.batch_alter_table(
        'servers',
        schema=None,
        copy_from=_servers_table(tier_check=_OLD_TIERS, with_type=False),
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                'server_type',
                sa.String(length=24),
                server_default='minecraft',
                nullable=False,
            )
        )
        batch_op.drop_constraint(op.f('ck_servers_ram_tier_known'), type_='check')
        batch_op.create_check_constraint(
            op.f('ck_servers_ram_tier_known'), f'ram_tier IN ({_NEW_TIERS})'
        )
        batch_op.create_check_constraint(
            op.f('ck_servers_server_type_known'), f'server_type IN ({_TYPES})'
        )
        batch_op.create_index(
            batch_op.f('ix_servers_server_type'), ['server_type'], unique=False
        )


def downgrade():
    """Reverse the schema change.

    Lossy by nature: 6 GB and 8 GB have no ordinal equivalent, so both collapse
    onto the old ``PRO`` (4).  The containers keep the size the panel gave them;
    only our label for them changes.

    Done in three steps because the tier constraint has to be absent while the
    reverse remap writes ``3``, which neither the new nor the old list allows at
    the same time as the values it replaces.
    """
    with op.batch_alter_table(
        'servers',
        schema=None,
        copy_from=_servers_table(tier_check=_NEW_TIERS, with_type=True),
    ) as batch_op:
        batch_op.drop_index(batch_op.f('ix_servers_server_type'))
        batch_op.drop_constraint(op.f('ck_servers_server_type_known'), type_='check')
        batch_op.drop_constraint(op.f('ck_servers_ram_tier_known'), type_='check')
        batch_op.drop_column('server_type')

    op.execute(
        sa.text(
            """
            UPDATE servers SET ram_tier = CASE ram_tier
                WHEN 1 THEN 1
                WHEN 2 THEN 3
                WHEN 4 THEN 4
                WHEN 6 THEN 4
                WHEN 8 THEN 4
                ELSE ram_tier
            END
            """
        )
    )

    with op.batch_alter_table(
        'servers',
        schema=None,
        copy_from=_servers_table(tier_check=None, with_type=False),
    ) as batch_op:
        batch_op.create_check_constraint(
            op.f('ck_servers_ram_tier_known'), f'ram_tier IN ({_OLD_TIERS})'
        )



