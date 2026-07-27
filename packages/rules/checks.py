"""The five deterministic commerce checks.

Each rule compares an authoritative value against what a downstream surface
actually produces, and explains the commercial consequence. Rules never call a
model and never mutate anything.

Severity is derived from DataHub metadata (`comgu.criticality`,
`comgu.customer_facing`) rather than hardcoded, so re-governing an asset in the
catalog changes how Comgu grades its failures.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from packages.rules.context import AssetContext, RunContext
from packages.rules.models import (
    Evidence,
    EvidenceType,
    Finding,
    RuleResult,
    RuleStatus,
    Severity,
)

CRITICALITY_TO_SEVERITY = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
}


def grade(asset: AssetContext, floor: Severity = Severity.MEDIUM) -> Severity:
    """Severity for a failure on this asset, per its DataHub governance."""
    sev = CRITICALITY_TO_SEVERITY.get(asset.criticality, Severity.MEDIUM)
    if not asset.customer_facing and sev.rank > Severity.MEDIUM.rank:
        sev = Severity.MEDIUM
    return sev if sev.rank >= floor.rank else floor


def lineage_evidence(ctx: RunContext, asset: AssetContext) -> Evidence:
    """Records that this asset was reached by traversing DataHub lineage."""
    return Evidence(
        evidence_type=EvidenceType.LINEAGE,
        source="datahub.get_lineage",
        source_reference=asset.urn,
        content={
            "root": ctx.blast_radius.root_urn,
            "downstream_asset": asset.urn,
            "degree": asset.degree,
            "max_hops": ctx.blast_radius.max_hops,
            "produced_by": asset.lab_file,
        },
    )


def owner_evidence(asset: AssetContext) -> Evidence:
    return Evidence(
        evidence_type=EvidenceType.OWNER,
        source="datahub.get_entities",
        source_reference=asset.urn,
        content={
            "owners": asset.owners,
            "has_owner": asset.has_owner,
            "note": (
                "no owner recorded in DataHub — nobody is accountable for this surface"
                if not asset.has_owner
                else None
            ),
        },
    )


def assertion_evidence(asset: AssetContext) -> Evidence | None:
    """A failing DataHub assertion corroborating what Comgu found.

    Never the trigger — Comgu's own check decides. This says the catalog
    already knew, which tells the operator how long it has been wrong.
    """
    if not asset.failing_assertions:
        return None
    a = asset.failing_assertions[0]
    return Evidence(
        evidence_type=EvidenceType.ASSERTION,
        source="datahub.assertions",
        source_reference=a.get("urn"),
        content={
            "description": a.get("description"),
            "result": a.get("result"),
            "failed_runs": a.get("failed_runs"),
            "expected": a.get("expected"),
            "observed": a.get("observed"),
            "note": "DataHub already recorded this asset as failing quality",
        },
    )


def comparison(expected: Any, observed: Any, field: str, source: str) -> Evidence:
    return Evidence(
        evidence_type=EvidenceType.VALUE_COMPARISON,
        source=source,
        content={
            "field": field,
            "expected": str(expected),
            "observed": str(observed),
            "match": str(expected) == str(observed),
        },
    )


class Rule:
    code: str = ""
    version: int = 1
    category: str = ""
    description: str = ""
    remediation_template: str = ""

    def asset(self, ctx: RunContext) -> AssetContext | None:
        return ctx.blast_radius.by_rule(self.code)

    def evaluate(self, ctx: RunContext) -> RuleResult:
        started = time.monotonic()
        asset = self.asset(ctx)
        if asset is None:
            return RuleResult(
                rule_code=self.code,
                rule_version=self.version,
                status=RuleStatus.SKIPPED,
                skip_reason=(
                    f"no downstream asset tagged comgu_rule={self.code} was reachable "
                    "from the change through DataHub lineage"
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        try:
            findings = self.check(ctx, asset)
            status = RuleStatus.FAILED if findings else RuleStatus.PASSED
            return RuleResult(
                rule_code=self.code,
                rule_version=self.version,
                status=status,
                findings=findings,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as e:
            return RuleResult(
                rule_code=self.code,
                rule_version=self.version,
                status=RuleStatus.ERROR,
                error=f"{type(e).__name__}: {e}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    def check(self, ctx: RunContext, asset: AssetContext) -> list[Finding]:
        raise NotImplementedError

    @staticmethod
    def with_quality(evidence: list[Evidence], asset: AssetContext) -> list[Evidence]:
        """Append the catalog's own quality signal when there is one."""
        extra = assertion_evidence(asset)
        return [*evidence, extra] if extra else evidence


# --- 1. price parity ---------------------------------------------------------


class PriceParity(Rule):
    code = "price_parity"
    version = 1
    category = "price"
    description = "Downstream feeds must advertise the authoritative catalog price."
    remediation_template = "set_feed_price"

    def check(self, ctx: RunContext, asset: AssetContext) -> list[Finding]:
        source = ctx.require_authority()
        row = ctx.row_for("google_merchant_feed", ctx.change.sku)
        if row is None:
            return []

        expected = ctx.change.price
        observed = Decimal(str(row["price"]))
        if observed == expected:
            return []

        delta = expected - observed
        return [
            Finding(
                rule_code=self.code,
                rule_version=self.version,
                severity=grade(asset, Severity.HIGH),
                title="Merchant feed advertises a stale price",
                summary=(
                    f"The {asset.name} still lists {ctx.change.sku} at {observed} "
                    f"while the catalog price is {expected}."
                ),
                expected_value=str(expected),
                observed_value=str(observed),
                source_asset_urn=source.urn,
                downstream_asset_urn=asset.urn,
                owner_reference=asset.owner_reference,
                customer_impact=(
                    f"Shoppers see {observed} in Shopping ads and free listings, then are "
                    f"charged {expected} at checkout."
                ),
                business_risk=(
                    f"Under-charging by {delta} per unit if the advertised price is honoured, "
                    "or item disapproval and a landing-page mismatch penalty if it is not."
                ),
                confidence=1.0,
                auto_fix_eligible=True,
                remediation_template=self.remediation_template,
                target_file=asset.lab_file,
                evidence=self.with_quality(
                    [
                        comparison(expected, observed, "price", "comgu.builders.merchant_feed"),
                        lineage_evidence(ctx, asset),
                        owner_evidence(asset),
                    ],
                    asset,
                ),
            )
        ]


# --- 2. inventory safety -----------------------------------------------------


class InventorySafety(Rule):
    code = "inventory_safety"
    version = 1
    category = "inventory"
    description = "Committed bundle quantities must not exceed sellable inventory."
    remediation_template = "cap_bundle_commitment"

    def check(self, ctx: RunContext, asset: AssetContext) -> list[Finding]:
        source = ctx.require_authority()
        findings: list[Finding] = []
        for row in ctx.projection("bundle_availability"):
            committed = int(row["committed_units"])
            sellable = int(row["sellable_units"])
            if committed <= sellable:
                continue
            oversell = committed - sellable
            findings.append(
                Finding(
                    rule_code=self.code,
                    rule_version=self.version,
                    severity=grade(asset, Severity.CRITICAL),
                    title="Bundle can oversell available inventory",
                    summary=(
                        f"Bundle {row['bundle_id']} commits {committed} units of "
                        f"{row['component_sku']} but only {sellable} are sellable."
                    ),
                    expected_value=sellable,
                    observed_value=committed,
                    source_asset_urn=source.urn,
                    downstream_asset_urn=asset.urn,
                    owner_reference=asset.owner_reference,
                    customer_impact=(
                        f"Up to {oversell} customers can complete checkout for stock that "
                        "does not exist, and their orders must be cancelled after payment."
                    ),
                    business_risk=(
                        "Forced cancellations, refund handling, and marketplace "
                        "seller-metric damage from unfulfilled orders."
                    ),
                    confidence=1.0,
                    auto_fix_eligible=True,
                    remediation_template=self.remediation_template,
                    target_file=asset.lab_file,
                    evidence=[
                        comparison(sellable, committed, "committed_units", "comgu.builders.bundles"),
                        Evidence(
                            evidence_type=EvidenceType.VALUE_COMPARISON,
                            source="comgu.inventory_math",
                            content={
                                "inventory_quantity": ctx.change.inventory_quantity,
                                "reserved_units": ctx.change.reserved_units,
                                "safety_stock": ctx.change.safety_stock,
                                "sellable_units": ctx.change.sellable_units,
                            },
                        ),
                        lineage_evidence(ctx, asset),
                        owner_evidence(asset),
                    ],
                )
            )
        return findings


# --- 3. promotion integrity --------------------------------------------------


class PromotionIntegrity(Rule):
    code = "promotion_integrity"
    version = 1
    category = "promotion"
    description = "Promotions must discount against the live catalog price."
    remediation_template = "rebase_promotion"

    def check(self, ctx: RunContext, asset: AssetContext) -> list[Finding]:
        source = ctx.require_authority()
        findings: list[Finding] = []
        for row in ctx.projection("promotions_active"):
            if row.get("sku") != ctx.change.sku:
                continue
            basis = Decimal(str(row["price_basis"]))
            if basis == ctx.change.price:
                continue
            overstated = ctx.change.price - basis
            findings.append(
                Finding(
                    rule_code=self.code,
                    rule_version=self.version,
                    severity=grade(asset, Severity.HIGH),
                    title="Promotion is anchored to a stale price",
                    summary=(
                        f"Promotion {row['promo_code']} discounts {row['discount_pct']}% from a "
                        f"{basis} basis, but the catalog price is {ctx.change.price}."
                    ),
                    expected_value=str(ctx.change.price),
                    observed_value=str(basis),
                    source_asset_urn=source.urn,
                    downstream_asset_urn=asset.urn,
                    owner_reference=asset.owner_reference,
                    customer_impact=(
                        f"The advertised sale price of {row['advertised_price']} is calculated "
                        "from a price the store no longer charges."
                    ),
                    business_risk=(
                        f"Margin loss of up to {overstated} per unit, and a misleading-savings "
                        "claim if the basis is presented as the reference price."
                    ),
                    confidence=1.0,
                    auto_fix_eligible=True,
                    remediation_template=self.remediation_template,
                    target_file=asset.lab_file,
                    evidence=[
                        comparison(ctx.change.price, basis, "price_basis", "comgu.builders.promotions"),
                        lineage_evidence(ctx, asset),
                        owner_evidence(asset),
                    ],
                )
            )
        return findings


# --- 4. AI commerce freshness ------------------------------------------------


class AiCommerceFreshness(Rule):
    code = "ai_commerce_freshness"
    version = 1
    category = "ai_commerce"
    description = "Machine-readable manifests must reflect current price and availability."
    remediation_template = "refresh_ai_manifest"

    def check(self, ctx: RunContext, asset: AssetContext) -> list[Finding]:
        source = ctx.require_authority()
        row = ctx.row_for("ai_shopping_manifest", ctx.change.sku)
        if row is None:
            return []

        findings: list[Finding] = []
        stale: list[str] = []

        observed_price = Decimal(str(row["price"]))
        if observed_price != ctx.change.price:
            stale.append(f"price {observed_price} (catalog {ctx.change.price})")
        observed_avail = int(row["available"])
        if observed_avail != ctx.change.sellable_units:
            stale.append(
                f"availability {observed_avail} (sellable {ctx.change.sellable_units})"
            )

        if not stale:
            return []

        findings.append(
            Finding(
                rule_code=self.code,
                rule_version=self.version,
                severity=grade(asset, Severity.HIGH),
                title="AI shopping manifest is stale",
                summary=(
                    f"The {asset.name} reports " + "; ".join(stale) + "."
                ),
                expected_value={
                    "price": str(ctx.change.price),
                    "available": ctx.change.sellable_units,
                },
                observed_value={"price": str(observed_price), "available": observed_avail},
                source_asset_urn=source.urn,
                downstream_asset_urn=asset.urn,
                owner_reference=asset.owner_reference,
                customer_impact=(
                    "Third-party shopping agents quote these values directly to shoppers, so "
                    "the wrong price and stock level are stated as fact before the customer "
                    "ever reaches the store."
                ),
                business_risk=(
                    "Agent-driven orders that cannot be fulfilled at the quoted price, with "
                    "no human step in which to catch the error."
                ),
                confidence=1.0,
                auto_fix_eligible=True,
                remediation_template=self.remediation_template,
                target_file=asset.lab_file,
                evidence=[
                    comparison(ctx.change.price, observed_price, "price", "comgu.builders.ai_manifest"),
                    comparison(
                        ctx.change.sellable_units,
                        observed_avail,
                        "available",
                        "comgu.builders.ai_manifest",
                    ),
                    lineage_evidence(ctx, asset),
                    owner_evidence(asset),
                ],
            )
        )

        # An unowned customer-facing surface is its own finding: there is nobody
        # to route the fix to.
        if not asset.has_owner:
            findings.append(
                Finding(
                    rule_code=self.code,
                    rule_version=self.version,
                    severity=Severity.MEDIUM,
                    title="Customer-facing asset has no owner in DataHub",
                    summary=(
                        f"{asset.name} is customer-facing and out of date, but DataHub records "
                        "no owner, so there is nobody to route this correction to."
                    ),
                    expected_value="at least one owner",
                    observed_value="none",
                    source_asset_urn=source.urn,
                    downstream_asset_urn=asset.urn,
                    owner_reference=None,
                    customer_impact=(
                        "Errors on this surface persist until someone notices, because no "
                        "team is accountable for it."
                    ),
                    business_risk="Unassigned remediation and slower time to resolution.",
                    confidence=1.0,
                    auto_fix_eligible=False,
                    remediation_template="assign_owner",
                    target_file=None,
                    evidence=[owner_evidence(asset), lineage_evidence(ctx, asset)],
                )
            )
        return findings


# --- 5. policy consistency ---------------------------------------------------


class PolicyConsistency(Rule):
    code = "policy_consistency"
    version = 1
    category = "policy"
    description = "Advertised policy must match the authoritative policy."
    remediation_template = "align_policy"

    def check(self, ctx: RunContext, asset: AssetContext) -> list[Finding]:
        source = ctx.require_authority()
        row = ctx.row_for("storefront_policy", ctx.change.sku)
        if row is None:
            return []

        observed = int(row["return_window_days"])
        expected = ctx.change.return_window_days
        if observed == expected:
            return []

        shorter = observed < expected
        return [
            Finding(
                rule_code=self.code,
                rule_version=self.version,
                severity=grade(asset, Severity.MEDIUM),
                title="Storefront return policy contradicts the authoritative policy",
                summary=(
                    f"The storefront advertises a {observed}-day return window; the "
                    f"authoritative policy is {expected} days."
                ),
                expected_value=expected,
                observed_value=observed,
                source_asset_urn=source.urn,
                downstream_asset_urn=asset.urn,
                owner_reference=asset.owner_reference,
                customer_impact=(
                    f"Customers are told they have {observed} days to return this item when "
                    f"they actually have {expected}."
                    if shorter
                    else f"Customers are promised {observed} days but only {expected} are honoured."
                ),
                business_risk=(
                    "Consumer-protection exposure and disputed returns: the advertised terms "
                    "are the ones a customer can hold the merchant to."
                ),
                confidence=1.0,
                auto_fix_eligible=True,
                remediation_template=self.remediation_template,
                target_file=asset.lab_file,
                evidence=[
                    comparison(expected, observed, "return_window_days", "comgu.builders.policy"),
                    lineage_evidence(ctx, asset),
                    owner_evidence(asset),
                ],
            )
        ]


ALL_RULES: list[Rule] = [
    PriceParity(),
    InventorySafety(),
    PromotionIntegrity(),
    AiCommerceFreshness(),
    PolicyConsistency(),
]
