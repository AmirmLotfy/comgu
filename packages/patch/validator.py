"""Run registered validation commands against a generated patch.

Only commands on this list can ever execute — there is no path from a model, a
DataHub description, or a finding to an arbitrary shell string. Commands run in
the scratch workspace with a timeout and no inherited credentials.

A failed validation blocks pull-request creation.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 300

# The only executable commands. Keyed by id so a plan references the id, never
# a string to run.
REGISTERED_COMMANDS: dict[str, list[str]] = {
    "pytest": ["-m", "pytest", "-q", "--no-header"],
    "builders": ["-m", "build.builders"],
}

# Anything resembling a credential is masked before output is stored or shown.
_REDACT = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|authorization|bearer)\s*[:=]\s*\S+"
)


def redact(text: str) -> str:
    return _REDACT.sub(r"\1=<redacted>", text)


@dataclass
class ValidationStep:
    sequence_number: int
    command_id: str
    command_display: str
    status: str  # passed | failed | timed_out | error
    exit_code: int | None = None
    stdout_redacted: str = ""
    stderr_redacted: str = ""
    duration_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "sequence_number": self.sequence_number,
            "command_id": self.command_id,
            "command_display": self.command_display,
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout_redacted": self.stdout_redacted,
            "stderr_redacted": self.stderr_redacted,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ValidationRun:
    status: str = "pending"
    steps: list[ValidationStep] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def summary(self) -> dict[str, Any]:
        tests = self._counts()
        return {
            "status": self.status,
            "steps": len(self.steps),
            "failed_steps": sum(1 for s in self.steps if s.status != "passed"),
            "duration_ms": self.duration_ms,
            **tests,
        }

    def _counts(self) -> dict[str, int]:
        """Pull test counts out of pytest's summary line."""
        out: dict[str, int] = {}
        for s in self.steps:
            if s.command_id != "pytest":
                continue
            blob = f"{s.stdout_redacted}\n{s.stderr_redacted}"
            for key, pattern in (
                ("tests_passed", r"(\d+) passed"),
                ("tests_failed", r"(\d+) failed"),
                ("tests_errored", r"(\d+) error"),
            ):
                m = re.search(pattern, blob)
                if m:
                    out[key] = int(m.group(1))
        return out

    def to_json(self) -> dict[str, Any]:
        return {"summary": self.summary, "steps": [s.to_json() for s in self.steps]}


class UnregisteredCommand(ValueError):
    """Something tried to run a command that is not on the allowlist."""


def _interpreter(workspace: Path) -> str:
    venv = workspace / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    import sys

    return sys.executable


def _clean_env() -> dict[str, str]:
    """A minimal environment — no tokens, no cloud credentials."""
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COMGU_VALIDATION"] = "1"
    return env


def run_validation(
    workspace: Path,
    command_ids: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> ValidationRun:
    """Execute the registered commands in the patched workspace."""
    ids = command_ids or ["pytest"]
    run = ValidationRun(status="running")
    started = time.monotonic()

    # The lab's venv lives outside the copied workspace, so fall back to a
    # sibling checkout's interpreter when the workspace has none.
    python = _interpreter(workspace)

    for i, cid in enumerate(ids, 1):
        args = REGISTERED_COMMANDS.get(cid)
        if args is None:
            raise UnregisteredCommand(
                f"{cid!r} is not a registered validation command "
                f"(registered: {sorted(REGISTERED_COMMANDS)})"
            )

        display = f"python {' '.join(args)}"
        step_started = time.monotonic()
        try:
            proc = subprocess.run(
                [python, *args],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_clean_env(),
            )
            status = "passed" if proc.returncode == 0 else "failed"
            step = ValidationStep(
                sequence_number=i,
                command_id=cid,
                command_display=display,
                status=status,
                exit_code=proc.returncode,
                stdout_redacted=redact(proc.stdout)[-8000:],
                stderr_redacted=redact(proc.stderr)[-4000:],
                duration_ms=int((time.monotonic() - step_started) * 1000),
            )
        except subprocess.TimeoutExpired:
            step = ValidationStep(
                sequence_number=i,
                command_id=cid,
                command_display=display,
                status="timed_out",
                duration_ms=int((time.monotonic() - step_started) * 1000),
            )
        except Exception as e:
            step = ValidationStep(
                sequence_number=i,
                command_id=cid,
                command_display=display,
                status="error",
                stderr_redacted=redact(f"{type(e).__name__}: {e}"),
                duration_ms=int((time.monotonic() - step_started) * 1000),
            )

        run.steps.append(step)
        if step.status != "passed":
            break  # stop at the first failure; the patch is not shippable

    run.duration_ms = int((time.monotonic() - started) * 1000)
    run.status = "passed" if all(s.status == "passed" for s in run.steps) else "failed"
    return run
