"""Verification step 1: is DataHub reachable and is MCP answering?

    DATAHUB_GMS_URL=... python -m packages.datahub.smoke

Deliberately the smallest possible check that still proves the thing Comgu
depends on. It exercises tool discovery, `search`, and `get_lineage` — because
lineage is what the blast radius is made of, and a DataHub that answers `search`
but returns no lineage looks healthy while being useless to Comgu.

Exits 0 on success, 1 if DataHub is reachable but unusable, 2 if unreachable.
"""

from __future__ import annotations

import asyncio
import os
import sys

from packages.datahub.mcp_client import (
    MUTATION_TOOLS,
    READ_TOOLS,
    DataHubUnavailable,
    datahub_session,
)

DEFAULT_ROOT = (
    "urn:li:dataset:(urn:li:dataPlatform:shopify,northstar_home.catalog.products,PROD)"
)


async def main() -> int:
    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:18080")
    root = os.environ.get("COMGU_SOURCE_URN", DEFAULT_ROOT)
    print(f"datahub: {gms}")

    try:
        async with datahub_session(gms) as dh:
            missing = sorted(READ_TOOLS - dh.available)
            print(f"\n[1] tools: {len(dh.available)} available")
            if missing:
                print(f"    !! missing read tools: {missing}")
                return 1
            print(f"    all {len(READ_TOOLS)} read tools present")
            print(f"    mutation tools: {'enabled' if dh.mutations_enabled else 'disabled (expected here)'}")

            print("\n[2] search")
            hits = await dh.search("northstar", num_results=5)
            total = hits.get("total") if isinstance(hits, dict) else None
            print(f"    total={total}")

            print(f"\n[3] get_lineage downstream of\n    {root}")
            lineage = await dh.get_lineage(root, upstream=False, max_hops=3, max_results=50)
            block = lineage.get("downstreams") if isinstance(lineage, dict) else None
            edges = (block or {}).get("total", 0)
            print(f"    downstream results: {edges}")

            if not edges:
                print(
                    "\n    !! no downstream lineage. Comgu cannot compute a blast radius.\n"
                    "       Seed the graph:  python -m seed.commerce_lab\n"
                    "       If it was seeded, the indexer may be stalled — see infra/README.md"
                )
                return 1

            print(f"\n{len(dh.trace)} MCP calls, {dh.trace.total_ms}ms")
            for c in dh.trace.calls:
                print(f"   {c.tool:<28} {c.duration_ms:>6}ms  {c.summary}")

    except DataHubUnavailable as e:
        print(f"\nDATAHUB_UNREACHABLE: {e}", file=sys.stderr)
        return 2

    print("\nSMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
