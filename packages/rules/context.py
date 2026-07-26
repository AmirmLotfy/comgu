"""What the rule engine receives from DataHub.

This is where DataHub becomes load-bearing. The engine does not know which
asset is authoritative, which surfaces are customer-facing, or which files
produce them — it asks the catalog. Strip `comgu.authority` out of DataHub and
`authoritative_asset()` returns None, which halts the checks rather than
falling back to a hardcoded guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


class MissingContext(RuntimeError):
    """DataHub did not supply something the rules require to be correct."""


@dataclass(frozen=True)
class CommerceState:
    """Authoritative commerce values carried by the normalized event."""

    sku: str
    title: str
    price: Decimal
    currency: str
    inventory_quantity: int
    reserved_units: int = 0
    safety_stock: int = 0
    return_window_days: int = 0
    restocking_fee_pct: int = 0

    @property
    def sellable_units(self) -> int:
        return max(0, self.inventory_quantity - self.reserved_units - self.safety_stock)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CommerceState:
        return cls(
            sku=d["sku"],
            title=d.get("title", ""),
            price=Decimal(str(d["price"])),
            currency=d.get("currency", "USD"),
            inventory_quantity=int(d["inventory_quantity"]),
            reserved_units=int(d.get("reserved_units", 0)),
            safety_stock=int(d.get("safety_stock", 0)),
            return_window_days=int(d.get("return_window_days", 0)),
            restocking_fee_pct=int(d.get("restocking_fee_pct", 0)),
        )


@dataclass
class AssetContext:
    """A DataHub asset plus the governance metadata Comgu reasons over."""

    urn: str
    name: str
    entity_type: str = "DATASET"
    authority: str | None = None  # comgu.authority
    customer_facing: bool = False  # comgu.customer_facing
    criticality: str = "medium"  # comgu.criticality
    channel: str | None = None  # comgu.channel
    owners: list[str] = field(default_factory=list)
    lab_file: str | None = None
    comgu_rule: str | None = None
    failing_assertions: list[dict[str, Any]] = field(default_factory=list)
    degree: int = 1
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_authoritative(self) -> bool:
        return self.authority == "authoritative"

    @property
    def has_owner(self) -> bool:
        return bool(self.owners)

    @property
    def owner_reference(self) -> dict[str, Any] | None:
        if not self.owners:
            return None
        return {"owners": self.owners, "primary": self.owners[0]}


@dataclass
class BlastRadius:
    """Assets reachable downstream of the change, derived from DataHub lineage."""

    root_urn: str
    assets: list[AssetContext] = field(default_factory=list)
    max_hops: int = 3
    lineage_edges: int = 0

    @property
    def customer_facing(self) -> list[AssetContext]:
        return [a for a in self.assets if a.customer_facing]

    @property
    def critical(self) -> list[AssetContext]:
        return [a for a in self.assets if a.criticality == "critical"]

    @property
    def unowned(self) -> list[AssetContext]:
        return [a for a in self.assets if not a.has_owner]

    @property
    def datasets(self) -> list[AssetContext]:
        return [a for a in self.assets if a.entity_type == "DATASET"]

    def by_rule(self, rule_code: str) -> AssetContext | None:
        for a in self.assets:
            if a.comgu_rule == rule_code:
                return a
        return None


@dataclass
class RunContext:
    """Everything a run retrieved from DataHub, plus the observed projections."""

    change: CommerceState
    blast_radius: BlastRadius
    assets_by_urn: dict[str, AssetContext] = field(default_factory=dict)
    # Built by executing the lab repo's transforms — the observed side.
    projections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    datahub_version: str | None = None

    def authoritative_asset(self) -> AssetContext | None:
        """The source of truth, per DataHub — never per hardcoded rule."""
        for a in self.assets_by_urn.values():
            if a.is_authoritative:
                return a
        return None

    def require_authority(self) -> AssetContext:
        asset = self.authoritative_asset()
        if asset is None:
            raise MissingContext(
                "no asset in DataHub carries comgu.authority=authoritative; "
                "Comgu cannot determine which value is correct and will not guess"
            )
        return asset

    def projection(self, target: str) -> list[dict[str, Any]]:
        return self.projections.get(target, [])

    def row_for(self, target: str, sku: str, key: str = "sku") -> dict[str, Any] | None:
        for row in self.projection(target):
            if row.get(key) == sku:
                return row
        return None
