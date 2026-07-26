"""Constraints on what a generated patch may touch.

The model proposes which registered template to apply; it never chooses a path.
Even so, every write is re-checked here, because a template bug or a crafted
DataHub description must not be able to reach outside the workspace.
"""

from __future__ import annotations

import os
from pathlib import Path

# Directories a patch may write inside, relative to the lab checkout.
ALLOWED_DIRS = ("feeds", "promotions", "bundles", "ai", "policies")

ALLOWED_EXTENSIONS = (".yaml", ".yml", ".json")

MAX_FILE_BYTES = 256 * 1024

# Files that must never be touched regardless of location.
DENIED_NAMES = frozenset(
    {
        ".env",
        ".git",
        ".gitignore",
        "id_rsa",
        "credentials",
        "authoritative.json",  # the source of truth is not patchable
    }
)


class UnsafePath(ValueError):
    """A patch tried to write somewhere it is not allowed to."""


def resolve_within(workspace: Path, relative: str) -> Path:
    """Resolve `relative` inside `workspace`, refusing to escape it.

    Catches `../` traversal, absolute paths, and symlinks pointing outside —
    the resolved real path must still sit under the resolved workspace.
    """
    if os.path.isabs(relative):
        raise UnsafePath(f"absolute paths are not allowed: {relative}")

    root = workspace.resolve(strict=True)
    candidate = (root / relative).resolve()

    if candidate == root or root not in candidate.parents:
        raise UnsafePath(f"path escapes the workspace: {relative}")

    return candidate


def check_writable(workspace: Path, relative: str) -> Path:
    """Full safety check for a single patch target."""
    target = resolve_within(workspace, relative)

    parts = Path(relative).parts
    if not parts:
        raise UnsafePath("empty path")

    if parts[0] not in ALLOWED_DIRS:
        raise UnsafePath(
            f"{relative!r} is outside the allowed directories {ALLOWED_DIRS}"
        )

    if target.suffix not in ALLOWED_EXTENSIONS:
        raise UnsafePath(
            f"{target.suffix!r} is not an allowed extension {ALLOWED_EXTENSIONS}"
        )

    if any(p in DENIED_NAMES for p in parts):
        raise UnsafePath(f"{relative!r} touches a denied name")

    # A symlink anywhere along the path could redirect the write.
    probe = workspace.resolve()
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            raise UnsafePath(f"{relative!r} traverses a symlink at {part!r}")

    if target.exists():
        if not target.is_file():
            raise UnsafePath(f"{relative!r} is not a regular file")
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            raise UnsafePath(f"{relative!r} is {size} bytes, over the {MAX_FILE_BYTES} limit")

    return target
