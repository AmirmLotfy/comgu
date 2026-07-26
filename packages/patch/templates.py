"""Registered remediation templates.

A template is the only way Comgu may modify a downstream config. Each one is a
narrow, declarative edit: it names the file and the key it changes, and derives
the new value from the authoritative catalog rather than from anything a model
produced. The model's only influence is choosing *which* registered template to
apply — it cannot invent an edit, a path, or a value.

An unknown template name is rejected, never improvised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ruamel.yaml import YAML

from packages.rules.context import CommerceState

# round-trip mode keeps the comments that explain why each value exists
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


class UnknownTemplate(ValueError):
    """The planner asked for a template that is not registered."""


@dataclass(frozen=True)
class Edit:
    """One concrete change, for the diff explanation."""

    path: str
    field: str
    before: Any
    after: Any


def _load_yaml(p: Path):
    with p.open() as f:
        return _yaml.load(f)


def _dump_yaml(p: Path, data) -> None:
    with p.open("w") as f:
        _yaml.dump(data, f)


def _load_json(p: Path) -> dict:
    return json.loads(p.read_text())


def _dump_json(p: Path, data: dict) -> None:
    p.write_text(json.dumps(data, indent=2) + "\n")


# --- templates ---------------------------------------------------------------


def set_feed_price(path: Path, change: CommerceState) -> list[Edit]:
    """Re-point the merchant feed at the authoritative price."""
    data = _load_yaml(path)
    edits: list[Edit] = []
    for item in data.get("items", []):
        if item.get("sku") != change.sku:
            continue
        before = item.get("price_override")
        after = str(change.price)
        if str(before) != after:
            item["price_override"] = after
            edits.append(Edit(str(path.name), "items[].price_override", before, after))
    if edits:
        _dump_yaml(path, data)
    return edits


def cap_bundle_commitment(path: Path, change: CommerceState) -> list[Edit]:
    """Cap committed bundles at what inventory can actually support."""
    data = _load_yaml(path)
    edits: list[Edit] = []
    for bundle in data.get("bundles", []):
        per_bundle = max(
            (int(c.get("units_per_bundle", 1)) for c in bundle.get("components", [])
             if c.get("sku") == change.sku),
            default=None,
        )
        if per_bundle is None:
            continue
        buildable = change.sellable_units // per_bundle
        before = int(bundle.get("committed_units", 0))
        if before > buildable:
            bundle["committed_units"] = buildable
            edits.append(Edit(str(path.name), "bundles[].committed_units", before, buildable))
    if edits:
        _dump_yaml(path, data)
    return edits


def rebase_promotion(path: Path, change: CommerceState) -> list[Edit]:
    """Re-anchor promotions to the live price so the saving is honest."""
    data = _load_yaml(path)
    edits: list[Edit] = []
    for promo in data.get("promotions", []):
        if promo.get("sku") != change.sku or not promo.get("active"):
            continue
        before = promo.get("price_basis")
        after = str(change.price)
        if str(before) != after:
            promo["price_basis"] = after
            edits.append(Edit(str(path.name), "promotions[].price_basis", before, after))
    if edits:
        _dump_yaml(path, data)
    return edits


def refresh_ai_manifest(path: Path, change: CommerceState) -> list[Edit]:
    """Bring the agent-facing manifest back in line with the catalog."""
    data = _load_json(path)
    edits: list[Edit] = []
    for offer in data.get("offers", []):
        if offer.get("sku") != change.sku:
            continue
        if offer.get("price_source") == "pinned":
            before = offer.get("pinned_price")
            after = str(change.price)
            if str(before) != after:
                offer["pinned_price"] = after
                edits.append(Edit(str(path.name), "offers[].pinned_price", before, after))
        if offer.get("availability_source") == "pinned":
            before = offer.get("pinned_available")
            after = change.sellable_units
            if before != after:
                offer["pinned_available"] = after
                edits.append(Edit(str(path.name), "offers[].pinned_available", before, after))
    if edits:
        _dump_json(path, data)
    return edits


def align_policy(path: Path, change: CommerceState) -> list[Edit]:
    """Make the advertised policy match the one actually honoured."""
    data = _load_yaml(path)
    edits: list[Edit] = []
    for row in data.get("policies", []):
        if row.get("sku") != change.sku:
            continue
        before = int(row.get("return_window_days", 0))
        after = change.return_window_days
        if before != after:
            row["return_window_days"] = after
            edits.append(Edit(str(path.name), "policies[].return_window_days", before, after))
    if edits:
        _dump_yaml(path, data)
    return edits


TEMPLATES: dict[str, Callable[[Path, CommerceState], list[Edit]]] = {
    "set_feed_price": set_feed_price,
    "cap_bundle_commitment": cap_bundle_commitment,
    "rebase_promotion": rebase_promotion,
    "refresh_ai_manifest": refresh_ai_manifest,
    "align_policy": align_policy,
}

# Templates that require a human rather than a file edit.
NON_FILE_TEMPLATES = {"assign_owner"}


def apply_template(name: str, path: Path, change: CommerceState) -> list[Edit]:
    fn = TEMPLATES.get(name)
    if fn is None:
        raise UnknownTemplate(
            f"{name!r} is not a registered remediation template "
            f"(registered: {sorted(TEMPLATES)})"
        )
    return fn(path, change)
