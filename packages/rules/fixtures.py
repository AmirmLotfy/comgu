"""Offline fixtures mirroring the Commerce Lab.

Lets the rule engine be tested deterministically with no DataHub and no
network, which is what `pytest packages/rules` relies on.
"""

from __future__ import annotations

from decimal import Decimal

from packages.rules.context import AssetContext, BlastRadius, CommerceState, RunContext

CATALOG_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)"
)


def golden_change() -> CommerceState:
    """Northstar Brew Pro after the $89 -> $109 / 12 -> 3 change."""
    return CommerceState(
        sku="NH-BREW-PRO",
        title="Northstar Brew Pro",
        price=Decimal("109.00"),
        currency="USD",
        inventory_quantity=3,
        return_window_days=30,
    )


def _asset(
    urn: str,
    name: str,
    rule: str,
    channel: str,
    criticality: str,
    lab_file: str,
    owners: list[str] | None = None,
) -> AssetContext:
    return AssetContext(
        urn=urn,
        name=name,
        authority="projection",
        customer_facing=True,
        criticality=criticality,
        channel=channel,
        owners=owners if owners is not None else ["urn:li:corpuser:commerce_ops"],
        lab_file=lab_file,
        comgu_rule=rule,
    )


def golden_context(projections: dict | None = None) -> RunContext:
    catalog = AssetContext(
        urn=CATALOG_URN,
        name="products",
        authority="authoritative",
        customer_facing=True,
        criticality="critical",
        channel="shopify",
        owners=["urn:li:corpuser:commerce_ops", "urn:li:corpuser:data_platform"],
        degree=0,
    )

    assets = [
        _asset(
            "urn:li:dataset:(urn:li:dataPlatform:s3,northstar_home.feeds.google_merchant_feed,PROD)",
            "google_merchant_feed",
            "price_parity",
            "google_merchant",
            "critical",
            "feeds/google_merchant.transform.yaml",
        ),
        _asset(
            "urn:li:dataset:(urn:li:dataPlatform:postgres,northstar_home.bundles.bundle_availability,PROD)",
            "bundle_availability",
            "inventory_safety",
            "bundles",
            "critical",
            "bundles/brew_pro_bundle.yaml",
        ),
        _asset(
            "urn:li:dataset:(urn:li:dataPlatform:postgres,northstar_home.promotions.promotions_active,PROD)",
            "promotions_active",
            "promotion_integrity",
            "promotions",
            "high",
            "promotions/spring_sale.yaml",
        ),
        # Deliberate ownership gap.
        _asset(
            "urn:li:dataset:(urn:li:dataPlatform:s3,northstar_home.ai.shopping_manifest,PROD)",
            "ai_shopping_manifest",
            "ai_commerce_freshness",
            "ai_agents",
            "high",
            "ai/manifest.config.json",
            owners=[],
        ),
        _asset(
            "urn:li:dataset:(urn:li:dataPlatform:postgres,northstar_home.policy.storefront_policy,PROD)",
            "storefront_policy",
            "policy_consistency",
            "storefront",
            "medium",
            "policies/returns.yaml",
        ),
    ]

    blast = BlastRadius(root_urn=CATALOG_URN, assets=assets, max_hops=3, lineage_edges=10)

    return RunContext(
        change=golden_change(),
        blast_radius=blast,
        assets_by_urn={a.urn: a for a in [catalog, *assets]},
        projections=projections if projections is not None else contradictory_projections(),
    )


def contradictory_projections() -> dict:
    """What the lab repo's transforms produce before remediation."""
    return {
        "google_merchant_feed": [
            {
                "sku": "NH-BREW-PRO",
                "title": "Northstar Brew Pro",
                "price": "89.00",
                "availability": "in stock",
            }
        ],
        "bundle_availability": [
            {
                "bundle_id": "BREW-PRO-STARTER",
                "component_sku": "NH-BREW-PRO",
                "committed_units": 5,
                "sellable_units": 3,
            }
        ],
        "promotions_active": [
            {
                "promo_code": "SPRING15",
                "sku": "NH-BREW-PRO",
                "price_basis": "89.00",
                "discount_pct": 15,
                "advertised_price": "75.65",
            }
        ],
        "ai_shopping_manifest": [
            {
                "sku": "NH-BREW-PRO",
                "price": "89.00",
                "available": 12,
                "return_window_days": 30,
            }
        ],
        "storefront_policy": [
            {"sku": "NH-BREW-PRO", "return_window_days": 14, "restocking_fee_pct": 0}
        ],
    }


def corrected_projections() -> dict:
    """What they produce after Comgu's patch is applied."""
    return {
        "google_merchant_feed": [
            {
                "sku": "NH-BREW-PRO",
                "title": "Northstar Brew Pro",
                "price": "109.00",
                "availability": "in stock",
            }
        ],
        "bundle_availability": [
            {
                "bundle_id": "BREW-PRO-STARTER",
                "component_sku": "NH-BREW-PRO",
                "committed_units": 3,
                "sellable_units": 3,
            }
        ],
        "promotions_active": [
            {
                "promo_code": "SPRING15",
                "sku": "NH-BREW-PRO",
                "price_basis": "109.00",
                "discount_pct": 15,
                "advertised_price": "92.65",
            }
        ],
        "ai_shopping_manifest": [
            {
                "sku": "NH-BREW-PRO",
                "price": "109.00",
                "available": 3,
                "return_window_days": 30,
            }
        ],
        "storefront_policy": [
            {"sku": "NH-BREW-PRO", "return_window_days": 30, "restocking_fee_pct": 0}
        ],
    }
