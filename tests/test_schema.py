"""The migration and the models must not diverge.

This test exists because they did. A column was added to `findings`, the
deployed database had been created by `create_all` (which adds tables but never
columns), and stamping it at head told Alembic there was nothing to do. The
mismatch surfaced in production as `no such column: findings.rule_execution_id`.
"""

from __future__ import annotations

import pytest


def test_migration_produces_exactly_the_model_schema(tmp_path, monkeypatch):
    """`alembic upgrade head` on an empty database must leave zero drift."""
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path}/m.db"
    monkeypatch.setenv("DATABASE_URL", url)

    import apps.api.db.session as sess

    sess._engine = None
    sess._Session = None

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    drift = sess.schema_drift()
    assert drift == [], (
        "the migration and the models disagree — regenerate it with\n"
        "  alembic revision --autogenerate\n" + "\n".join(drift)
    )


def test_stamping_a_lagging_database_is_refused(tmp_path, monkeypatch):
    """Stamping a schema that does not match hides the problem from Alembic."""
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path}/lag.db"
    monkeypatch.setenv("DATABASE_URL", url)

    import apps.api.db.session as sess

    sess._engine = None
    sess._Session = None
    sess.init_db()

    # Simulate the real failure: a table built before a column was added.
    # (Dropping `target_file` rather than the FK column — SQLite refuses to drop
    # a column named in a foreign key, but the drift is the same shape.)
    with sess.engine().begin() as conn:
        conn.execute(text("ALTER TABLE findings DROP COLUMN target_file"))

    assert sess.schema_drift(), "the drop should be visible as drift"
    with pytest.raises(RuntimeError, match="refusing to stamp"):
        sess.stamp_if_unversioned()
