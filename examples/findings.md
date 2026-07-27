# Findings — real run output

Run `08b4644c4f5e47819c886cc1b5231e08`. Produced by `python -m apps.api.scripts.golden_path --remediate`
against a live DataHub Core instance. Not hand-written.

**6 findings**, max severity `critical`.

| severity | rule | title |
| --- | --- | --- |
| `critical` | `price_parity` | Merchant feed advertises a stale price |
| `critical` | `inventory_safety` | Bundle can oversell available inventory |
| `high` | `promotion_integrity` | Promotion is anchored to a stale price |
| `high` | `ai_commerce_freshness` | AI shopping manifest is stale |
| `medium` | `ai_commerce_freshness` | Customer-facing asset has no owner in DataHub |
| `medium` | `policy_consistency` | Storefront return policy contradicts the authoritative policy |

## [critical] Merchant feed advertises a stale price

The google_merchant_feed still lists NH-BREW-PRO at 89.00 while the catalog price is 109.00.

- **expected** `109.00`
- **observed** `89.00`
- **source** `urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)`
- **downstream** `urn:li:dataset:(urn:li:dataPlatform:s3,northstar_home.feeds.google_merchant_feed,PROD)`
- **owner** urn:li:corpuser:commerce_ops
- **auto-fixable** True via `set_feed_price` → `feeds/google_merchant.transform.yaml`

**Customer impact.** Shoppers see 89.00 in Shopping ads and free listings, then are charged 109.00 at checkout.

**Business risk.** Under-charging by 20.00 per unit if the advertised price is honoured, or item disapproval and a landing-page mismatch penalty if it is not.

**Evidence**

- `value_comparison` from `comgu.builders.merchant_feed`
  ```json
  {
    "field": "price",
    "expected": "109.00",
    "observed": "89.00",
    "match": false
  }
  ```
- `lineage` from `datahub.get_lineage`
  ```json
  {
    "root": "urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)",
    "downstream_asset": "urn:li:dataset:(urn:li:dataPlatform:s3,northstar_home.feeds.google_merchant_feed,PROD)",
    "degree": 1,
    "max_hops": 3,
    "produced_by": "feeds/google_merchant.transform.yaml"
  }
  ```
- `owner` from `datahub.get_entities`
  ```json
  {
    "owners": [
      "urn:li:corpuser:commerce_ops"
    ],
    "has_owner": true,
    "note": null
  }
  ```

## [critical] Bundle can oversell available inventory

Bundle BREW-PRO-STARTER commits 5 units of NH-BREW-PRO but only 3 are sellable.

- **expected** `3`
- **observed** `5`
- **source** `urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)`
- **downstream** `urn:li:dataset:(urn:li:dataPlatform:postgres,northstar_home.bundles.bundle_availability,PROD)`
- **owner** urn:li:corpuser:commerce_ops
- **auto-fixable** True via `cap_bundle_commitment` → `bundles/brew_pro_bundle.yaml`

**Customer impact.** Up to 2 customers can complete checkout for stock that does not exist, and their orders must be cancelled after payment.

**Business risk.** Forced cancellations, refund handling, and marketplace seller-metric damage from unfulfilled orders.

**Evidence**

- `value_comparison` from `comgu.builders.bundles`
  ```json
  {
    "field": "committed_units",
    "expected": "3",
    "observed": "5",
    "match": false
  }
  ```
- `value_comparison` from `comgu.inventory_math`
  ```json
  {
    "inventory_quantity": 3,
    "reserved_units": 0,
    "safety_stock": 0,
    "sellable_units": 3
  }
  ```
- `lineage` from `datahub.get_lineage`
  ```json
  {
    "root": "urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)",
    "downstream_asset": "urn:li:dataset:(urn:li:dataPlatform:postgres,northstar_home.bundles.bundle_availability,PROD)",
    "degree": 1,
    "max_hops": 3,
    "produced_by": "bundles/brew_pro_bundle.yaml"
  }
  ```
- `owner` from `datahub.get_entities`
  ```json
  {
    "owners": [
      "urn:li:corpuser:commerce_ops"
    ],
    "has_owner": true,
    "note": null
  }
  ```

## [high] Promotion is anchored to a stale price

Promotion SPRING15 discounts 15% from a 89.00 basis, but the catalog price is 109.00.

- **expected** `109.00`
- **observed** `89.00`
- **source** `urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)`
- **downstream** `urn:li:dataset:(urn:li:dataPlatform:postgres,northstar_home.promotions.promotions_active,PROD)`
- **owner** urn:li:corpuser:commerce_ops
- **auto-fixable** True via `rebase_promotion` → `promotions/spring_sale.yaml`

**Customer impact.** The advertised sale price of 75.65 is calculated from a price the store no longer charges.

**Business risk.** Margin loss of up to 20.00 per unit, and a misleading-savings claim if the basis is presented as the reference price.

**Evidence**

- `value_comparison` from `comgu.builders.promotions`
  ```json
  {
    "field": "price_basis",
    "expected": "109.00",
    "observed": "89.00",
    "match": false
  }
  ```
- `lineage` from `datahub.get_lineage`
  ```json
  {
    "root": "urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)",
    "downstream_asset": "urn:li:dataset:(urn:li:dataPlatform:postgres,northstar_home.promotions.promotions_active,PROD)",
    "degree": 1,
    "max_hops": 3,
    "produced_by": "promotions/spring_sale.yaml"
  }
  ```
- `owner` from `datahub.get_entities`
  ```json
  {
    "owners": [
      "urn:li:corpuser:commerce_ops"
    ],
    "has_owner": true,
    "note": null
  }
  ```

## [high] AI shopping manifest is stale

The ai_shopping_manifest reports price 89.00 (catalog 109.00); availability 12 (sellable 3).

- **expected** `{'price': '109.00', 'available': 3}`
- **observed** `{'price': '89.00', 'available': 12}`
- **source** `urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)`
- **downstream** `urn:li:dataset:(urn:li:dataPlatform:s3,northstar_home.ai.shopping_manifest,PROD)`
- **owner** _none recorded_
- **auto-fixable** True via `refresh_ai_manifest` → `ai/manifest.config.json`

**Customer impact.** Third-party shopping agents quote these values directly to shoppers, so the wrong price and stock level are stated as fact before the customer ever reaches the store.

**Business risk.** Agent-driven orders that cannot be fulfilled at the quoted price, with no human step in which to catch the error.

**Evidence**

- `value_comparison` from `comgu.builders.ai_manifest`
  ```json
  {
    "field": "price",
    "expected": "109.00",
    "observed": "89.00",
    "match": false
  }
  ```
- `value_comparison` from `comgu.builders.ai_manifest`
  ```json
  {
    "field": "available",
    "expected": "3",
    "observed": "12",
    "match": false
  }
  ```
- `lineage` from `datahub.get_lineage`
  ```json
  {
    "root": "urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)",
    "downstream_asset": "urn:li:dataset:(urn:li:dataPlatform:s3,northstar_home.ai.shopping_manifest,PROD)",
    "degree": 1,
    "max_hops": 3,
    "produced_by": "ai/manifest.config.json"
  }
  ```
- `owner` from `datahub.get_entities`
  ```json
  {
    "owners": [],
    "has_owner": false,
    "note": "no owner recorded in DataHub \u2014 nobody is accountable for this surface"
  }
  ```

## [medium] Customer-facing asset has no owner in DataHub

ai_shopping_manifest is customer-facing and out of date, but DataHub records no owner, so there is nobody to route this correction to.

- **expected** `at least one owner`
- **observed** `none`
- **source** `urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)`
- **downstream** `urn:li:dataset:(urn:li:dataPlatform:s3,northstar_home.ai.shopping_manifest,PROD)`
- **owner** _none recorded_
- **auto-fixable** False

**Customer impact.** Errors on this surface persist until someone notices, because no team is accountable for it.

**Business risk.** Unassigned remediation and slower time to resolution.

**Evidence**

- `owner` from `datahub.get_entities`
  ```json
  {
    "owners": [],
    "has_owner": false,
    "note": "no owner recorded in DataHub \u2014 nobody is accountable for this surface"
  }
  ```
- `lineage` from `datahub.get_lineage`
  ```json
  {
    "root": "urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)",
    "downstream_asset": "urn:li:dataset:(urn:li:dataPlatform:s3,northstar_home.ai.shopping_manifest,PROD)",
    "degree": 1,
    "max_hops": 3,
    "produced_by": "ai/manifest.config.json"
  }
  ```

## [medium] Storefront return policy contradicts the authoritative policy

The storefront advertises a 14-day return window; the authoritative policy is 30 days.

- **expected** `30`
- **observed** `14`
- **source** `urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)`
- **downstream** `urn:li:dataset:(urn:li:dataPlatform:postgres,northstar_home.policy.storefront_policy,PROD)`
- **owner** urn:li:corpuser:data_platform
- **auto-fixable** True via `align_policy` → `policies/returns.yaml`

**Customer impact.** Customers are told they have 14 days to return this item when they actually have 30.

**Business risk.** Consumer-protection exposure and disputed returns: the advertised terms are the ones a customer can hold the merchant to.

**Evidence**

- `value_comparison` from `comgu.builders.policy`
  ```json
  {
    "field": "return_window_days",
    "expected": "30",
    "observed": "14",
    "match": false
  }
  ```
- `lineage` from `datahub.get_lineage`
  ```json
  {
    "root": "urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)",
    "downstream_asset": "urn:li:dataset:(urn:li:dataPlatform:postgres,northstar_home.policy.storefront_policy,PROD)",
    "degree": 1,
    "max_hops": 3,
    "produced_by": "policies/returns.yaml"
  }
  ```
- `owner` from `datahub.get_entities`
  ```json
  {
    "owners": [
      "urn:li:corpuser:data_platform"
    ],
    "has_owner": true,
    "note": null
  }
  ```
