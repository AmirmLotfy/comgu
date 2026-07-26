"""Build a run's context from live DataHub metadata.

Two MCP calls, in this order:

  1. `get_lineage` downstream of the changed asset — this, and nothing else,
     determines the blast radius.
  2. `get_entities` on whatever came back — governance metadata (authority,
     criticality, ownership, which file produces the asset).

Everything the rule engine later reasons about comes from here. Replacing this
module with hardcoded topology is exactly what Comgu must never do, so it
raises rather than falling back when DataHub is unusable.
"""

from __future__ import annotations

from typing import Any

from packages.datahub.mcp_client import DataHubMCP, DataHubUnavailable
from packages.rules.context import AssetContext, BlastRadius, CommerceState, RunContext

# How far downstream to walk. The MCP server defaults to 1 hop, which would
# only ever surface direct neighbours and silently understate the blast radius.
MAX_HOPS = 3
MAX_RESULTS = 100


def _custom_properties(entity: dict[str, Any]) -> dict[str, str]:
    """customProperties arrives as a list of {key, value}, not a mapping."""
    props = ((entity.get("properties") or {}).get("customProperties")) or []
    out: dict[str, str] = {}
    for p in props:
        if isinstance(p, dict) and "key" in p:
            out[p["key"]] = p.get("value", "")
    return out


def _structured_properties(entity: dict[str, Any]) -> dict[str, str]:
    """Flatten structuredProperties to {qualifiedName: first string value}."""
    entries = ((entity.get("structuredProperties") or {}).get("properties")) or []
    out: dict[str, str] = {}
    for e in entries:
        sp = e.get("structuredProperty") or {}
        name = (sp.get("definition") or {}).get("qualifiedName") or sp.get("urn", "").split(":")[-1]
        values = e.get("values") or []
        if not name or not values:
            continue
        v = values[0]
        out[name] = (
            v.get("stringValue")
            if isinstance(v, dict)
            else str(v)
        ) or ""
    return out


def _owners(entity: dict[str, Any]) -> list[str]:
    owners = ((entity.get("ownership") or {}).get("owners")) or []
    out: list[str] = []
    for o in owners:
        urn = (o.get("owner") or {}).get("urn")
        if urn:
            out.append(urn)
    return out


def _asset_from_entity(entity: dict[str, Any], degree: int = 1) -> AssetContext:
    sp = _structured_properties(entity)
    cp = _custom_properties(entity)
    return AssetContext(
        urn=entity.get("urn", ""),
        name=entity.get("name") or (entity.get("properties") or {}).get("name") or "",
        entity_type=entity.get("type", "DATASET"),
        authority=sp.get("comgu.authority"),
        customer_facing=sp.get("comgu.customer_facing", "").lower() == "true",
        criticality=sp.get("comgu.criticality") or "medium",
        channel=sp.get("comgu.channel"),
        owners=_owners(entity),
        lab_file=cp.get("lab_file"),
        comgu_rule=cp.get("comgu_rule"),
        degree=degree,
        raw={"structured_properties": sp, "custom_properties": cp},
    )


def _lineage_results(payload: Any) -> list[dict[str, Any]]:
    """Pull searchResults out of a get_lineage payload, either direction."""
    if not isinstance(payload, dict):
        return []
    for key in ("downstreams", "upstreams"):
        block = payload.get(key)
        if isinstance(block, dict):
            return block.get("searchResults") or []
    return []


async def build_run_context(
    dh: DataHubMCP,
    change: CommerceState,
    source_urn: str,
    projections: dict[str, list[dict[str, Any]]],
) -> RunContext:
    """Retrieve context for one commerce change.

    `projections` is the observed side — what the downstream transforms
    actually produce right now. DataHub supplies the expected side and the map
    of what is affected.
    """
    lineage = await dh.get_lineage(
        source_urn, upstream=False, max_hops=MAX_HOPS, max_results=MAX_RESULTS
    )
    results = _lineage_results(lineage)
    if not results:
        raise DataHubUnavailable(
            f"DataHub returned no downstream lineage for {source_urn}; "
            "Comgu cannot determine a blast radius and will not assume one"
        )

    degrees: dict[str, int] = {}
    for r in results:
        entity = r.get("entity") or {}
        urn = entity.get("urn")
        if urn:
            degrees[urn] = int(r.get("degree") or 1)

    # Hydrate the downstream assets plus the source itself.
    urns = [source_urn] + [u for u in degrees if u != source_urn]
    entities = await dh.get_entities(urns)
    if isinstance(entities, dict):
        entities = entities.get("entities") or [entities]
    if not isinstance(entities, list):
        raise DataHubUnavailable(f"unexpected get_entities payload: {type(entities).__name__}")

    assets_by_urn: dict[str, AssetContext] = {}
    for e in entities:
        if not isinstance(e, dict) or not e.get("urn"):
            continue
        urn = e["urn"]
        assets_by_urn[urn] = _asset_from_entity(e, degree=degrees.get(urn, 0))

    downstream = [a for urn, a in assets_by_urn.items() if urn != source_urn]

    return RunContext(
        change=change,
        blast_radius=BlastRadius(
            root_urn=source_urn,
            assets=downstream,
            max_hops=MAX_HOPS,
            lineage_edges=len(results),
        ),
        assets_by_urn=assets_by_urn,
        projections=projections,
        tool_trace=dh.trace.to_json(),
    )
