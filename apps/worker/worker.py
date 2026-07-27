"""Recovery worker.

The API drives runs inline, which is fine while the process lives. This worker
exists for when it doesn't: it finds runs left mid-flight by a crash or a
deploy and resumes them from their persisted state.

Two things keep that safe:

  * runs are leased (`locked_by` / `locked_at`), so two workers do not pick up
    the same run, and a stale lease is reclaimable
  * every external action is already idempotent — a run-scoped PR branch
    updates rather than duplicates, and DataHub writes are upserts — so
    resuming a partially-completed run does not double-act

Runs waiting on a human are never resumed; they are waiting by design.

    python -m apps.worker.worker --once
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from apps.api.db.models import Run
from apps.api.db.session import init_db, session
from apps.api.runner import advance_after_approval, advance_to_approval
from apps.api.workflow import Status, TERMINAL, WAITING_ON_HUMAN

# A run whose lease is older than this is assumed abandoned.
LEASE_SECONDS = int(os.environ.get("COMGU_LEASE_SECONDS", "300"))
POLL_SECONDS = int(os.environ.get("COMGU_POLL_SECONDS", "15"))

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# Statuses that indicate work is owed to a worker rather than to a person.
RESUMABLE = [
    s for s in (
        Status.RECEIVED, Status.SIGNATURE_VERIFIED, Status.NORMALIZED,
        Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED, Status.CHECKS_RUNNING,
        Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
        Status.APPROVED, Status.PATCH_GENERATING, Status.PATCH_GENERATED,
        Status.VALIDATION_RUNNING, Status.VALIDATED,
        Status.PULL_REQUEST_CREATING, Status.PULL_REQUEST_OPENED,
        Status.DATAHUB_WRITEBACK_PENDING, Status.DATAHUB_UPDATED,
    )
    if s not in TERMINAL and s not in WAITING_ON_HUMAN
]

AFTER_APPROVAL = {
    Status.APPROVED, Status.PATCH_GENERATING, Status.PATCH_GENERATED,
    Status.VALIDATION_RUNNING, Status.VALIDATED, Status.PULL_REQUEST_CREATING,
    Status.PULL_REQUEST_OPENED, Status.DATAHUB_WRITEBACK_PENDING, Status.DATAHUB_UPDATED,
}


def claim(db, run: Run) -> bool:
    """Take the lease, refusing if someone else holds a fresh one."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS)
    locked_at = run.locked_at
    if locked_at is not None and locked_at.tzinfo is None:
        locked_at = locked_at.replace(tzinfo=timezone.utc)
    if run.locked_by and locked_at and locked_at > cutoff:
        return False
    run.locked_by = WORKER_ID
    run.locked_at = datetime.now(timezone.utc)
    db.commit()
    return True


def sweep_once(verbose: bool = True) -> int:
    """Resume every stranded run. Returns how many were touched."""
    handled = 0
    with session() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS)
        candidates = (
            db.query(Run)
            .filter(Run.status.in_(RESUMABLE))
            .filter(or_(Run.locked_at.is_(None), Run.locked_at < cutoff))
            .order_by(Run.created_at)
            .limit(20)
            .all()
        )

        for run in candidates:
            if not claim(db, run):
                continue
            if verbose:
                print(f"[worker] resuming {run.id[:8]} from {run.status}")
            try:
                if run.status in AFTER_APPROVAL:
                    asyncio.run(advance_after_approval(db, run))
                else:
                    asyncio.run(advance_to_approval(db, run))
                handled += 1
            except Exception as e:
                print(f"[worker] {run.id[:8]} failed: {type(e).__name__}: {e}")
                run.retry_count += 1
                db.commit()
            finally:
                run.locked_by = None
                run.locked_at = None
                db.commit()
    return handled


def main() -> int:
    ap = argparse.ArgumentParser(description="Comgu recovery worker")
    ap.add_argument("--once", action="store_true", help="sweep once and exit")
    args = ap.parse_args()

    init_db()
    print(f"[worker] {WORKER_ID} lease={LEASE_SECONDS}s poll={POLL_SECONDS}s")

    if args.once:
        n = sweep_once()
        print(f"[worker] resumed {n} run(s)")
        return 0

    while True:
        try:
            sweep_once(verbose=False)
        except Exception as e:
            print(f"[worker] sweep error: {type(e).__name__}: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
