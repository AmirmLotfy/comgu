"""Emit the Comgu Commerce Lab overlay into DataHub.

Layers Comgu's commerce topology on top of the showcase-ecommerce datapack:
an authoritative Shopify catalog, five downstream projections joined by real
dataJob lineage, governance metadata, a deliberate ownership gap and a failing
data-quality assertion.

Idempotent: DataHub upserts by (urn, aspect), so re-running converges.

    python -m seed.commerce_lab            # emit
    python -m seed.commerce_lab --reset    # clear Comgu write-back properties
"""

from __future__ import annotations

import argparse
import os
import time

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter, EmitMode
from datahub.metadata.schema_classes import (
    AssertionInfoClass,
    AssertionResultClass,
    AssertionResultTypeClass,
    AssertionRunEventClass,
    AssertionRunStatusClass,
    AssertionStdAggregationClass,
    AssertionStdOperatorClass,
    AssertionTypeClass,
    AuditStampClass,
    CorpUserInfoClass,
    DataFlowInfoClass,
    DataJobInfoClass,
    DataJobInputOutputClass,
    DatasetAssertionInfoClass,
    DatasetAssertionScopeClass,
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    DomainPropertiesClass,
    DomainsClass,
    GlobalTagsClass,
    GlossaryTermAssociationClass,
    GlossaryTermInfoClass,
    GlossaryTermsClass,
    NumberTypeClass,
    OtherSchemaClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    PropertyValueClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StringTypeClass,
    StructuredPropertiesClass,
    StructuredPropertyDefinitionClass,
    StructuredPropertyValueAssignmentClass,
    SubTypesClass,
    TagAssociationClass,
    TagPropertiesClass,
    UpstreamClass,
    UpstreamLineageClass,
)

from seed import topology as T

ACTOR = "urn:li:corpuser:comgu"
DATA_TYPE_STRING = "urn:li:dataType:datahub.string"
ENTITY_TYPE_DATASET = "urn:li:entityType:datahub.dataset"


def now_ms() -> int:
    return int(time.time() * 1000)


def audit() -> AuditStampClass:
    return AuditStampClass(time=now_ms(), actor=ACTOR)


def mcp(entity_urn: str, aspect) -> MetadataChangeProposalWrapper:
    return MetadataChangeProposalWrapper(entityUrn=entity_urn, aspect=aspect)


# --- governance scaffolding --------------------------------------------------


def structured_property_definitions() -> list[MetadataChangeProposalWrapper]:
    """Comgu's governance vocabulary.

    `authority` is the important one: the rule engine asks DataHub which asset
    is authoritative rather than hardcoding it, so removing this property
    changes Comgu's behaviour.
    """
    defs = [
        (
            T.SP_AUTHORITY,
            "Comgu Authority",
            "Whether this asset is the source of truth for commerce values, or a projection of it.",
            [("authoritative", "Source of truth"), ("projection", "Derived from an authoritative source")],
        ),
        (
            T.SP_CUSTOMER_FACING,
            "Customer Facing",
            "Whether incorrect values on this asset are visible to customers.",
            [("true", "Visible to customers"), ("false", "Internal only")],
        ),
        (
            T.SP_CRITICALITY,
            "Comgu Criticality",
            "Business impact if this asset carries a contradictory value.",
            [
                ("low", "Minimal commercial risk"),
                ("medium", "Customer-visible inconsistency possible"),
                ("high", "Likely customer impact or financial loss"),
                ("critical", "Active unsafe commerce state"),
            ],
        ),
        (
            T.SP_CHANNEL,
            "Commerce Channel",
            "Which commerce channel this asset serves.",
            None,
        ),
        (T.SP_LAST_RUN, "Last Comgu Run", "Most recent Comgu run that evaluated this asset.", None),
        (
            T.SP_LAST_VALIDATION,
            "Last Validation",
            "Timestamp of the most recent Comgu validation that passed for this asset.",
            None,
        ),
        (
            T.SP_PR_URL,
            "Remediation Pull Request",
            "Pull request opened by Comgu to correct this asset.",
            None,
        ),
    ]

    out = []
    for qualified, display, description, allowed in defs:
        out.append(
            mcp(
                T.sp_urn(qualified),
                StructuredPropertyDefinitionClass(
                    qualifiedName=qualified,
                    displayName=display,
                    description=description,
                    valueType=DATA_TYPE_STRING,
                    entityTypes=[ENTITY_TYPE_DATASET],
                    cardinality="SINGLE",
                    allowedValues=(
                        [PropertyValueClass(value=v, description=d) for v, d in allowed]
                        if allowed
                        else None
                    ),
                ),
            )
        )
    return out


def governance() -> list[MetadataChangeProposalWrapper]:
    out = [
        mcp(
            T.DOMAIN_URN,
            DomainPropertiesClass(
                name=T.DOMAIN_NAME,
                description="Surfaces that expose commerce values to customers and partners.",
            ),
        ),
        mcp(
            T.TAG_SIMULATED,
            TagPropertiesClass(
                name="comgu:simulated-downstream",
                description=(
                    "Simulated downstream system in the Comgu Commerce Lab. The transformation "
                    "logic, files, contradictions and tests are real; the commercial system "
                    "behind them is not a live production integration."
                ),
            ),
        ),
        mcp(
            T.TAG_AUTHORITATIVE,
            TagPropertiesClass(
                name="comgu:authoritative",
                description="Source of truth for commerce values.",
            ),
        ),
    ]

    terms = [
        (
            T.TERM_AUTHORITATIVE_PRICE,
            "AuthoritativePrice",
            "The price a customer is actually charged at checkout. All other prices are projections of it.",
        ),
        (
            T.TERM_SELLABLE_INVENTORY,
            "SellableInventory",
            "Units that can be sold right now: on-hand minus reservations and safety stock.",
        ),
        (
            T.TERM_CUSTOMER_SURFACE,
            "CustomerFacingSurface",
            "An asset whose values reach customers or shopping agents directly.",
        ),
    ]
    for urn, name, definition in terms:
        out.append(mcp(urn, GlossaryTermInfoClass(name=name, definition=definition, termSource="INTERNAL")))

    for urn, title, email in [
        (T.OWNER_COMMERCE, "Commerce Operations", "commerce-ops@northstarhome.example"),
        (T.OWNER_DATA, "Data Platform", "data-platform@northstarhome.example"),
    ]:
        out.append(
            mcp(urn, CorpUserInfoClass(active=True, displayName=title, email=email, title=title))
        )
    return out


# --- schema helpers ----------------------------------------------------------


def schema_for(urn: str, fields: list[tuple[str, str]]) -> SchemaMetadataClass:
    return SchemaMetadataClass(
        schemaName=urn.split(",")[1] if "," in urn else urn,
        platform=urn.split("(")[1].split(",")[0],
        version=0,
        hash="",
        platformSchema=OtherSchemaClass(rawSchema=""),
        fields=[
            SchemaFieldClass(
                fieldPath=name,
                type=SchemaFieldDataTypeClass(
                    type=NumberTypeClass() if ftype == "number" else StringTypeClass()
                ),
                nativeDataType=ftype,
            )
            for name, ftype in fields
        ],
    )


def sp_values(pairs: list[tuple[str, str]]) -> StructuredPropertiesClass:
    return StructuredPropertiesClass(
        properties=[
            StructuredPropertyValueAssignmentClass(propertyUrn=T.sp_urn(q), values=[v])
            for q, v in pairs
        ]
    )


# --- the authoritative source ------------------------------------------------


def authoritative_source() -> list[MetadataChangeProposalWrapper]:
    urn = T.SHOPIFY_PRODUCTS
    return [
        mcp(
            urn,
            DatasetPropertiesClass(
                name="products",
                qualifiedName="northstar_home.catalog.products",
                description=(
                    f"{T.MERCHANT} Shopify product catalog. This is the source of truth for "
                    "price, sellable inventory and returns policy. Every other commerce "
                    "surface is a projection of this dataset."
                ),
                customProperties={
                    "merchant": T.MERCHANT,
                    "sku": T.PRODUCT_SKU,
                    "price": T.PRICE_AFTER,
                    "currency": T.CURRENCY,
                    "inventory_quantity": str(T.INVENTORY_AFTER),
                    "return_window_days": str(T.RETURN_WINDOW_DAYS),
                },
            ),
        ),
        mcp(
            urn,
            schema_for(
                urn,
                [
                    ("sku", "string"),
                    ("title", "string"),
                    ("price", "string"),
                    ("inventory_quantity", "number"),
                    ("return_window_days", "number"),
                    ("status", "string"),
                ],
            ),
        ),
        mcp(urn, SubTypesClass(typeNames=["Table"])),
        mcp(
            urn,
            OwnershipClass(
                owners=[
                    OwnerClass(owner=T.OWNER_COMMERCE, type=OwnershipTypeClass.BUSINESS_OWNER),
                    OwnerClass(owner=T.OWNER_DATA, type=OwnershipTypeClass.TECHNICAL_OWNER),
                ]
            ),
        ),
        mcp(urn, GlobalTagsClass(tags=[TagAssociationClass(tag=T.TAG_AUTHORITATIVE)])),
        mcp(
            urn,
            GlossaryTermsClass(
                terms=[
                    GlossaryTermAssociationClass(urn=T.TERM_AUTHORITATIVE_PRICE),
                    GlossaryTermAssociationClass(urn=T.TERM_SELLABLE_INVENTORY),
                ],
                auditStamp=audit(),
            ),
        ),
        mcp(urn, DomainsClass(domains=[T.DOMAIN_URN])),
        mcp(
            urn,
            sp_values(
                [
                    (T.SP_AUTHORITY, "authoritative"),
                    (T.SP_CUSTOMER_FACING, "true"),
                    (T.SP_CRITICALITY, "critical"),
                    (T.SP_CHANNEL, "shopify"),
                ]
            ),
        ),
    ]


# --- downstream projections + lineage ---------------------------------------


def orchestration() -> list[MetadataChangeProposalWrapper]:
    out = [
        mcp(
            T.FLOW_URN,
            DataFlowInfoClass(
                name="commerce_sync",
                description=(
                    f"Projects the {T.MERCHANT} catalog onto every downstream commerce surface."
                ),
                externalUrl=None,
            ),
        )
    ]

    for p in T.PROJECTIONS:
        out.append(
            mcp(
                p.job_urn,
                DataJobInfoClass(
                    name=p.job,
                    type="BATCH_SCHEDULED",
                    description=(
                        f"Builds {p.display} from the authoritative catalog. "
                        f"Defined by {p.lab_file} in comgu-commerce-lab."
                    ),
                    customProperties={"lab_file": p.lab_file, "comgu_rule": p.key},
                ),
            )
        )
        # This is what makes blast radius real: catalog -> job -> projection.
        out.append(
            mcp(
                p.job_urn,
                DataJobInputOutputClass(
                    inputDatasets=[T.SHOPIFY_PRODUCTS],
                    outputDatasets=[p.dataset],
                ),
            )
        )
    return out


def projections() -> list[MetadataChangeProposalWrapper]:
    out: list[MetadataChangeProposalWrapper] = []
    for p in T.PROJECTIONS:
        out.append(
            mcp(
                p.dataset,
                DatasetPropertiesClass(
                    name=p.display,
                    description=f"{p.description}\n\nProduced by {p.job} ({p.lab_file}).",
                    customProperties={
                        "comgu_rule": p.key,
                        "lab_file": p.lab_file,
                        "channel": p.channel,
                    },
                ),
            )
        )
        out.append(mcp(p.dataset, schema_for(p.dataset, p.fields)))
        out.append(mcp(p.dataset, SubTypesClass(typeNames=["Table"])))
        out.append(mcp(p.dataset, DomainsClass(domains=[T.DOMAIN_URN])))
        out.append(mcp(p.dataset, GlobalTagsClass(tags=[TagAssociationClass(tag=T.TAG_SIMULATED)])))

        # Dataset-level lineage in addition to the job edges, so both
        # dataset->dataset and dataset->job->dataset traversals resolve.
        out.append(
            mcp(
                p.dataset,
                UpstreamLineageClass(
                    upstreams=[
                        UpstreamClass(
                            dataset=T.SHOPIFY_PRODUCTS,
                            type=DatasetLineageTypeClass.TRANSFORMED,
                        )
                    ]
                ),
            )
        )

        if p.owner:
            out.append(
                mcp(
                    p.dataset,
                    OwnershipClass(
                        owners=[OwnerClass(owner=p.owner, type=OwnershipTypeClass.TECHNICAL_OWNER)]
                    ),
                )
            )
        # else: deliberate ownership gap — Comgu reports it as a finding.

        if p.customer_facing:
            out.append(
                mcp(
                    p.dataset,
                    GlossaryTermsClass(
                        terms=[GlossaryTermAssociationClass(urn=T.TERM_CUSTOMER_SURFACE)],
                        auditStamp=audit(),
                    ),
                )
            )

        out.append(
            mcp(
                p.dataset,
                sp_values(
                    [
                        (T.SP_AUTHORITY, "projection"),
                        (T.SP_CUSTOMER_FACING, "true" if p.customer_facing else "false"),
                        (T.SP_CRITICALITY, p.criticality),
                        (T.SP_CHANNEL, p.channel),
                    ]
                ),
            )
        )
    return out


# --- failing data-quality assertion -----------------------------------------

ASSERTION_URN = "urn:li:assertion:comgu-feed-price-parity"


def failing_assertion() -> list[MetadataChangeProposalWrapper]:
    """A failing assertion on the merchant feed.

    Emitted as metadata. DataHub Core stores and serves it; evaluation is done
    by Comgu's own rule engine, not by a DataHub Cloud monitor.
    """
    target = T.FAILING_ASSERTION_TARGET
    return [
        mcp(
            ASSERTION_URN,
            AssertionInfoClass(
                type=AssertionTypeClass.DATASET,
                description="Merchant feed price must equal the authoritative catalog price.",
                datasetAssertion=DatasetAssertionInfoClass(
                    dataset=target,
                    scope=DatasetAssertionScopeClass.DATASET_COLUMN,
                    fields=[f"urn:li:schemaField:({target},price)"],
                    operator=AssertionStdOperatorClass.EQUAL_TO,
                    aggregation=AssertionStdAggregationClass.IDENTITY,
                    nativeType="comgu.price_parity",
                ),
            ),
        ),
        mcp(
            ASSERTION_URN,
            AssertionRunEventClass(
                timestampMillis=now_ms(),
                runId="comgu-seed",
                assertionUrn=ASSERTION_URN,
                asserteeUrn=target,
                status=AssertionRunStatusClass.COMPLETE,
                result=AssertionResultClass(
                    type=AssertionResultTypeClass.FAILURE,
                    actualAggValue=float(T.PRICE_BEFORE),
                    nativeResults={
                        "expected": T.PRICE_AFTER,
                        "observed": T.PRICE_BEFORE,
                        "detail": "feed price has not caught up with the catalog",
                    },
                ),
            ),
        ),
    ]


# --- write-back reset --------------------------------------------------------


def clear_writeback() -> list[MetadataChangeProposalWrapper]:
    """Drop Comgu's write-back properties so a demo reset starts clean."""
    out = []
    for p in T.PROJECTIONS:
        out.append(
            mcp(
                p.dataset,
                sp_values(
                    [
                        (T.SP_AUTHORITY, "projection"),
                        (T.SP_CUSTOMER_FACING, "true" if p.customer_facing else "false"),
                        (T.SP_CRITICALITY, p.criticality),
                        (T.SP_CHANNEL, p.channel),
                    ]
                ),
            )
        )
    return out


# --- entrypoint --------------------------------------------------------------


def build(reset: bool = False) -> list[tuple[str, list[MetadataChangeProposalWrapper]]]:
    """Ordered emission phases.

    Order matters and batching does not preserve it: DataHub rejects a
    structuredProperties value whose property definition is not yet committed
    ("Unexpected null value found for ... Structured Property Definition").
    So definitions and vocabulary go first, in their own batches, and only then
    the assets that reference them.
    """
    if reset:
        return [("writeback-reset", clear_writeback())]
    return [
        ("structured-property-definitions", structured_property_definitions()),
        ("governance-vocabulary", governance()),
        (
            "assets-and-lineage",
            [
                *authoritative_source(),
                *orchestration(),
                *projections(),
                *failing_assertion(),
            ],
        ),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the Comgu Commerce Lab into DataHub")
    ap.add_argument("--reset", action="store_true", help="clear Comgu write-back properties")
    ap.add_argument("--dry-run", action="store_true", help="print counts without emitting")
    args = ap.parse_args()

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:18080")
    token = os.environ.get("DATAHUB_GMS_TOKEN") or None

    phases = build(reset=args.reset)
    total = sum(len(p) for _, p in phases)
    print(f"{'reset' if args.reset else 'seed'}: {total} aspects in {len(phases)} phases -> {gms}")
    if args.dry_run:
        for name, props in phases:
            print(f"  {name}: {len(props)}")
        return 0

    # openapi_ingestion is required for ASYNC_WAIT; without it the emitter
    # silently downgrades to fire-and-forget and we lose the propagation
    # guarantee that keeps verification from racing the indexer.
    emitter = DatahubRestEmitter(gms_server=gms, token=token, openapi_ingestion=True)
    emitter.test_connection()

    # Batch each phase rather than emitting per aspect. Firing many aspects for
    # the same entity individually makes DataHub's indexer race itself on the
    # same OpenSearch document, producing version_conflict_engine_exception,
    # failing the bulk request and stalling the MAE consumer. ASYNC_WAIT batches
    # the writes and blocks until DataHub confirms they propagated, so the graph
    # is queryable the moment this returns.
    try:
        for name, props in phases:
            if not props:
                continue
            emitter.emit_mcps(props, emit_mode=EmitMode.ASYNC_WAIT)
            print(f"  {name}: {len(props)} aspects confirmed")
    except Exception as e:
        print(f"  !! emit failed in phase {name!r}: {type(e).__name__}: {str(e)[:400]}")
        return 1
    finally:
        emitter.flush()

    print(f"emitted {total} aspects (phased + batched, propagation confirmed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
