"""Bridge to the comgu-commerce-lab checkout.

The lab is a separate repository with its own dependencies, so its transforms
run as a subprocess rather than being imported. That keeps Comgu decoupled from
the downstream code it inspects, and mirrors how validation executes later.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from packages.rules.context import CommerceState

DEFAULT_LAB_PATH = Path.home() / "Desktop" / "comgu-commerce-lab"


class LabUnavailable(RuntimeError):
    """The commerce lab checkout is missing or its transforms failed."""


def lab_path() -> Path:
    p = Path(os.environ.get("COMGU_LAB_PATH", DEFAULT_LAB_PATH)).expanduser()
    if not p.exists():
        raise LabUnavailable(
            f"commerce lab not found at {p}; set COMGU_LAB_PATH to the checkout"
        )
    return p


def _python(path: Path) -> str:
    """Prefer the lab's own interpreter so its deps resolve."""
    venv = path / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def load_catalog(path: Path | None = None) -> CommerceState:
    """The authoritative commerce values the lab was last told about."""
    root = path or lab_path()
    data = json.loads((root / "catalog" / "authoritative.json").read_text())
    products = data.get("products") or []
    if not products:
        raise LabUnavailable("authoritative catalog contains no products")
    return CommerceState.from_dict(products[0])


def catalog_source_urn(path: Path | None = None) -> str:
    root = path or lab_path()
    data = json.loads((root / "catalog" / "authoritative.json").read_text())
    urn = data.get("source_urn")
    if not urn:
        raise LabUnavailable("authoritative catalog does not name its source_urn")
    return urn


def build_projections(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Execute the downstream transforms and return what they produce."""
    root = path or lab_path()
    proc = subprocess.run(
        [_python(root), "-m", "build.builders"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise LabUnavailable(
            f"lab transforms failed (exit {proc.returncode}): {proc.stderr[-500:]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LabUnavailable(f"lab transforms produced non-JSON output: {e}") from e


def update_catalog(change: CommerceState, path: Path | None = None) -> None:
    """Write a verified commerce change into the lab's authoritative snapshot.

    This is the only value Comgu treats as truth; downstream configs are never
    edited here, only by an approved patch.
    """
    root = path or lab_path()
    f = root / "catalog" / "authoritative.json"
    data = json.loads(f.read_text())
    for p in data.get("products", []):
        if p.get("sku") == change.sku:
            p["price"] = str(change.price)
            p["inventory_quantity"] = change.inventory_quantity
            p["return_window_days"] = change.return_window_days
            break
    f.write_text(json.dumps(data, indent=2) + "\n")
