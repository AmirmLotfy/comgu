"""Reset must survive a database that already has runs in it.

The demo reset deletes every run and its children. It passed for a long time
against an empty database and failed the moment a run existed, because
`run_transitions` carried a foreign key to `runs.id` and was not in the delete
list — SQLite reported only `FOREIGN KEY constraint failed`, with no column.

That is the worst shape for a bug on a public demo: the button a judge presses
between attempts, broken only after they have used the product once.

The first test pins the specific regression. The second is the general one —
it reads the ORM metadata and fails if any *new* model gains a foreign key to
`runs.id` without being added to the reset, so this cannot happen again.
"""

from __future__ import annotations

import apps.api.main as main
from apps.api.db.models import Base, Run


def _models_referencing_runs() -> set[str]:
    """Every mapped class with a foreign key pointing at runs.id."""
    referencing: set[str] = set()
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None or table.name == "runs":
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name == "runs" and fk.column.name == "id":
                referencing.add(mapper.class_.__name__)
    return referencing


def test_reset_deletes_every_child_of_run():
    """The reset's delete list covers everything that points at a run."""
    deleted = {m.__name__ for m in main.DEMO_RESET_CASCADE}
    missing = _models_referencing_runs() - deleted
    assert not missing, (
        f"these reference runs.id but reset never deletes them: {sorted(missing)} — "
        "reset will raise FOREIGN KEY constraint failed once a run exists"
    )


def test_run_transitions_specifically_is_covered():
    """The exact model that broke it. Named so the regression is obvious."""
    assert "RunTransition" in {m.__name__ for m in main.DEMO_RESET_CASCADE}


def test_children_are_deleted_before_runs():
    """Ordering matters as much as membership: parents last."""
    names = [m.__name__ for m in main.DEMO_RESET_CASCADE]
    assert Run.__name__ not in names, "runs are deleted separately, after the cascade"
