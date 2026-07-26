"""The Comgu Commerce Lab topology.

Single source of truth for the demo graph. The seeder emits it into DataHub,
the rule engine reads it to know which downstream artifact each check owns, and
the lab repo mirrors it on disk.

Golden scenario (PRD 11): Northstar Home changes the Northstar Brew Pro from
$89 -> $109 and available inventory from 12 -> 3. Five downstream projections
fail to keep up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- merchant / product ------------------------------------------------------

MERCHANT = "Northstar Home"
PRODUCT_TITLE = "Northstar Brew Pro"
PRODUCT_SKU = "NH-BREW-PRO"

# Authoritative values *after* the golden change.
PRICE_BEFORE = "89.00"
PRICE_AFTER = "109.00"
INVENTORY_BEFORE = 12
INVENTORY_AFTER = 3
RETURN_WINDOW_DAYS = 30
CURRENCY = "USD"

# --- platforms ---------------------------------------------------------------

P_SHOPIFY = "urn:li:dataPlatform:shopify"
P_S3 = "urn:li:dataPlatform:s3"
P_POSTGRES = "urn:li:dataPlatform:postgres"
P_AIRFLOW = "airflow"

ENV = "PROD"


def dataset_urn(platform: str, name: str) -> str:
    return f"urn:li:dataset:({platform},{name},{ENV})"


# --- the authoritative source ------------------------------------------------

SHOPIFY_PRODUCTS = dataset_urn(P_SHOPIFY, "northstar_home.catalog.products")

# --- orchestration -----------------------------------------------------------

FLOW_URN = f"urn:li:dataFlow:({P_AIRFLOW},northstar_home.commerce_sync,{ENV})"


def datajob_urn(job: str) -> str:
    return f"urn:li:dataJob:({FLOW_URN},{job})"


# --- structured properties ---------------------------------------------------

SP_AUTHORITY = "comgu.authority"
SP_CUSTOMER_FACING = "comgu.customer_facing"
SP_CRITICALITY = "comgu.criticality"
SP_CHANNEL = "comgu.channel"
SP_LAST_RUN = "comgu.last_run"
SP_LAST_VALIDATION = "comgu.last_validation_at"
SP_PR_URL = "comgu.pull_request_url"


def sp_urn(qualified_name: str) -> str:
    return f"urn:li:structuredProperty:{qualified_name}"


# --- governance --------------------------------------------------------------

DOMAIN_URN = "urn:li:domain:comgu_commerce_channels"
DOMAIN_NAME = "Commerce Channels"

TERM_AUTHORITATIVE_PRICE = "urn:li:glossaryTerm:comgu.AuthoritativePrice"
TERM_SELLABLE_INVENTORY = "urn:li:glossaryTerm:comgu.SellableInventory"
TERM_CUSTOMER_SURFACE = "urn:li:glossaryTerm:comgu.CustomerFacingSurface"

TAG_SIMULATED = "urn:li:tag:comgu:simulated-downstream"
TAG_AUTHORITATIVE = "urn:li:tag:comgu:authoritative"

OWNER_COMMERCE = "urn:li:corpuser:commerce_ops"
OWNER_DATA = "urn:li:corpuser:data_platform"


# --- the five downstream projections ----------------------------------------


@dataclass(frozen=True)
class Projection:
    """One downstream commerce surface that can contradict the source."""

    key: str  # matches the rule that checks it
    job: str  # dataJob name
    dataset: str  # dataset URN
    display: str
    description: str
    channel: str
    customer_facing: bool
    criticality: str
    owner: str | None  # None => deliberate ownership gap
    lab_file: str  # file in comgu-commerce-lab that produces it
    contradiction: str
    fields: list[tuple[str, str]] = field(default_factory=list)

    @property
    def job_urn(self) -> str:
        return datajob_urn(self.job)


PROJECTIONS: list[Projection] = [
    Projection(
        key="price_parity",
        job="feed_builder",
        dataset=dataset_urn(P_S3, "northstar_home.feeds.google_merchant_feed"),
        display="google_merchant_feed",
        description=(
            "Google Merchant Center product feed generated from the Shopify catalog. "
            "Customer-facing: powers Shopping ads and free listings."
        ),
        channel="google_merchant",
        customer_facing=True,
        criticality="critical",
        owner=OWNER_COMMERCE,
        lab_file="feeds/google_merchant.transform.yaml",
        contradiction=f"feed still advertises ${PRICE_BEFORE} after the source moved to ${PRICE_AFTER}",
        fields=[
            ("sku", "string"),
            ("title", "string"),
            ("price", "string"),
            ("availability", "string"),
        ],
    ),
    Projection(
        key="inventory_safety",
        job="bundle_planner",
        dataset=dataset_urn(P_POSTGRES, "northstar_home.bundles.bundle_availability"),
        display="bundle_availability",
        description=(
            "Sellable quantities for multi-item bundles. Derived from catalog "
            "inventory minus reservations."
        ),
        channel="bundles",
        customer_facing=True,
        criticality="critical",
        owner=OWNER_COMMERCE,
        lab_file="bundles/brew_pro_bundle.yaml",
        contradiction=(
            f"bundle commits 5 units while only {INVENTORY_AFTER} are sellable — oversell risk"
        ),
        fields=[
            ("bundle_id", "string"),
            ("component_sku", "string"),
            ("committed_units", "number"),
            ("sellable_units", "number"),
        ],
    ),
    Projection(
        key="promotion_integrity",
        job="promo_engine",
        dataset=dataset_urn(P_POSTGRES, "northstar_home.promotions.promotions_active"),
        display="promotions_active",
        description=(
            "Active promotions with their price basis. A promotion whose basis no "
            "longer matches the catalog price discounts against a stale anchor."
        ),
        channel="promotions",
        customer_facing=True,
        criticality="high",
        owner=OWNER_COMMERCE,
        lab_file="promotions/spring_sale.yaml",
        contradiction=(
            f"promotion still discounts against a ${PRICE_BEFORE} basis, overstating the saving"
        ),
        fields=[
            ("promo_code", "string"),
            ("sku", "string"),
            ("price_basis", "string"),
            ("discount_pct", "number"),
        ],
    ),
    Projection(
        key="ai_commerce_freshness",
        job="ai_manifest_builder",
        dataset=dataset_urn(P_S3, "northstar_home.ai.shopping_manifest"),
        display="ai_shopping_manifest",
        description=(
            "Machine-readable product manifest consumed by AI shopping agents. "
            "Stale values here are quoted directly to shoppers by third-party agents."
        ),
        channel="ai_agents",
        customer_facing=True,
        criticality="high",
        # Deliberate ownership gap (PRD 11.4) — Comgu must surface this.
        owner=None,
        lab_file="ai/manifest.config.json",
        contradiction=(
            f"manifest reports {INVENTORY_BEFORE} units available and ${PRICE_BEFORE}; "
            "AI agents will quote both to shoppers"
        ),
        fields=[
            ("sku", "string"),
            ("price", "string"),
            ("available", "number"),
            ("return_window_days", "number"),
        ],
    ),
    Projection(
        key="policy_consistency",
        job="policy_projector",
        dataset=dataset_urn(P_POSTGRES, "northstar_home.policy.storefront_policy"),
        display="storefront_policy",
        description=(
            "Storefront-facing returns and shipping policy projection. Must match "
            "the authoritative policy recorded against the catalog."
        ),
        channel="storefront",
        customer_facing=True,
        criticality="medium",
        owner=OWNER_DATA,
        lab_file="policies/returns.yaml",
        contradiction=(
            f"storefront advertises a 14-day return window; authoritative policy is "
            f"{RETURN_WINDOW_DAYS} days"
        ),
        fields=[
            ("sku", "string"),
            ("return_window_days", "number"),
            ("restocking_fee_pct", "number"),
        ],
    ),
]

PROJECTIONS_BY_KEY = {p.key: p for p in PROJECTIONS}

# The asset carrying a deliberately failing data-quality assertion (PRD 11.4).
FAILING_ASSERTION_TARGET = PROJECTIONS_BY_KEY["price_parity"].dataset

ALL_DATASETS = [SHOPIFY_PRODUCTS] + [p.dataset for p in PROJECTIONS]
ALL_DATAJOBS = [p.job_urn for p in PROJECTIONS]
