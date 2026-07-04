"""Alembic environment configuration for robotsix-mill.

Wires ``SQLModel.metadata`` as the migration target so autogenerate
can detect schema changes across all models.  The per-board SQLite
URL is set at runtime by ``db.py`` before each ``upgrade()`` or
``stamp()`` call — the ``sqlalchemy.url`` in ``alembic.ini`` is
a placeholder.

References
----------
* TestDriven.io FastAPI + SQLModel tutorial:
  https://testdriven.io/blog/fastapi-sqlmodel/
* igorbenav/fastapi-sqlmodel-template (production-ready example)
* Stack Overflow: "How to get Alembic to recognise SQLModel database
  model?" — https://stackoverflow.com/questions/68932099
  (``target_metadata = SQLModel.metadata`` + ``import sqlmodel`` in
  ``script.py.mako``)
"""

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

# Import all models so SQLModel.metadata is populated before Alembic
# inspects it.  The ``noqa: F401`` comment silences the "unused import" lint.
from robotsix_mill.core import models  # noqa: F401

# Alembic Config object (reads alembic.ini).
config = context.config

# Set up Python logging from the config file section (optional).
# Save the root logger's handlers before fileConfig replaces them
# (alembic.ini [logger_root] sets handlers=console, which nukes any
# pre-existing handlers such as pytest caplog).  Restore them after
# so both the alembic console handler and any prior handlers coexist.
if config.config_file_name is not None:
    import logging

    _root = logging.getLogger()
    _saved_handlers = list(_root.handlers)
    try:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    finally:
        for h in _saved_handlers:
            if h not in _root.handlers:
                _root.addHandler(h)

# The metadata object that autogenerate compares against the live DB.
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout).

    Used by ``alembic upgrade --sql`` to produce a SQL script without
    connecting to a database.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite does not support ALTER TABLE … DROP COLUMN /
        # ALTER COLUMN / RENAME COLUMN natively — Alembic batch
        # mode handles these by recreating the table.
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to the DB).

    The per-board SQLite URL must be set on *config* before this
    function is called — see ``db._run_alembic_migrations()``.
    """
    from sqlalchemy import create_engine

    connectable = create_engine(
        config.get_main_option("sqlalchemy.url"),
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # batch mode: recreate tables for operations SQLite
            # doesn't support natively (DROP COLUMN, ALTER COLUMN,
            # RENAME COLUMN).
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
