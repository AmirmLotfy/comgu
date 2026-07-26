"""Generate a constrained patch in an isolated workspace.

The lab checkout is never edited in place. Comgu copies it to a scratch
workspace, applies only registered templates to allowlisted paths, and produces
an immutable unified diff. Nothing is pushed anywhere until validation passes
and a human approves.
"""

from __future__ import annotations

import difflib
import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.patch.safety import UnsafePath, check_writable
from packages.patch.templates import (
    NON_FILE_TEMPLATES,
    Edit,
    UnknownTemplate,
    apply_template,
)
from packages.rules.context import CommerceState
from packages.rules.models import Finding

EXCLUDE = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class PatchFile:
    file_path: str
    operation: str  # create | update | delete
    before_checksum: str | None
    after_checksum: str | None
    unified_diff: str
    file_size_bytes: int
    is_allowed_path: bool
    edits: list[Edit] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "operation": self.operation,
            "before_checksum": self.before_checksum,
            "after_checksum": self.after_checksum,
            "unified_diff": self.unified_diff,
            "file_size_bytes": self.file_size_bytes,
            "is_allowed_path": self.is_allowed_path,
            "edits": [
                {"field": e.field, "before": e.before, "after": e.after} for e in self.edits
            ],
        }


@dataclass
class GeneratedPatch:
    workspace: Path
    files: list[PatchFile] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    @property
    def checksum(self) -> str:
        return sha256("".join(f.unified_diff for f in self.files))

    @property
    def combined_diff(self) -> str:
        return "\n".join(f.unified_diff for f in self.files)

    @property
    def is_empty(self) -> bool:
        return not self.files

    def to_json(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "checksum": self.checksum,
            "file_count": len(self.files),
            "files": [f.to_json() for f in self.files],
            "skipped": self.skipped,
            "rejected": self.rejected,
        }


def prepare_workspace(lab_path: Path, root: Path | None = None) -> Path:
    """Copy the lab checkout somewhere disposable."""
    base = Path(tempfile.mkdtemp(prefix="comgu-patch-", dir=str(root) if root else None))
    dest = base / "lab"
    shutil.copytree(
        lab_path,
        dest,
        ignore=shutil.ignore_patterns(*EXCLUDE),
        symlinks=False,  # never copy symlinks into the workspace
    )
    return dest


def generate(
    findings: list[Finding],
    change: CommerceState,
    lab_path: Path,
    workspace_root: Path | None = None,
) -> GeneratedPatch:
    """Apply every auto-fixable finding's template inside a fresh workspace."""
    workspace = prepare_workspace(lab_path, workspace_root)
    patch = GeneratedPatch(workspace=workspace)

    for finding in findings:
        template = finding.remediation_template
        if not template:
            patch.skipped.append({"rule": finding.rule_code, "reason": "no remediation template"})
            continue
        if template in NON_FILE_TEMPLATES:
            patch.skipped.append(
                {"rule": finding.rule_code, "reason": f"{template} requires a human decision"}
            )
            continue
        if not finding.auto_fix_eligible:
            patch.skipped.append({"rule": finding.rule_code, "reason": "not auto-fix eligible"})
            continue
        if not finding.target_file:
            patch.skipped.append({"rule": finding.rule_code, "reason": "no target file"})
            continue

        try:
            target = check_writable(workspace, finding.target_file)
        except UnsafePath as e:
            patch.rejected.append({"rule": finding.rule_code, "path": finding.target_file, "reason": str(e)})
            continue

        before = target.read_text()
        try:
            edits = apply_template(template, target, change)
        except UnknownTemplate as e:
            patch.rejected.append({"rule": finding.rule_code, "path": finding.target_file, "reason": str(e)})
            continue

        after = target.read_text()
        if before == after:
            patch.skipped.append(
                {"rule": finding.rule_code, "reason": "template produced no change"}
            )
            continue

        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{finding.target_file}",
                tofile=f"b/{finding.target_file}",
                n=3,
            )
        )

        patch.files.append(
            PatchFile(
                file_path=finding.target_file,
                operation="update",
                before_checksum=sha256(before),
                after_checksum=sha256(after),
                unified_diff=diff,
                file_size_bytes=len(after.encode()),
                is_allowed_path=True,
                edits=edits,
            )
        )

    return patch


def discard(patch: GeneratedPatch) -> None:
    """Remove the scratch workspace."""
    base = patch.workspace.parent
    if base.exists() and base.name.startswith("comgu-patch-"):
        shutil.rmtree(base, ignore_errors=True)
