"""Day-2 checkpoint: prove the Commerce Lab graph is traversable through MCP.

Deliberately goes through the MCP server rather than GraphQL, because that is
the path Comgu itself uses. If this passes, blast radius is possible.

    python -m seed.verify
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from seed import topology as T

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:18080")
# DataHub indexes asynchronously through Kafka -> OpenSearch, so a freshly
# seeded graph is not immediately searchable.
MAX_ATTEMPTS = 12
BACKOFF_SECONDS = 10


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", None) or str(c) for c in result.content)


def failed(result) -> bool:
    return bool(getattr(result, "isError", False))


def urns_in(payload: str) -> set[str]:
    """Pull every URN out of a lineage response, whichever shape it arrives in."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return set()

    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "urn" and isinstance(v, str):
                    found.add(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


async def lineage(session, urn: str, hops: int) -> set[str]:
    result = await session.call_tool(
        "get_lineage",
        {"urn": urn, "upstream": False, "max_hops": hops, "max_results": 100},
    )
    if failed(result):
        print(f"    get_lineage error: {text_of(result)[:300]}")
        return set()
    return urns_in(text_of(result))


async def check(session) -> tuple[bool, list[str]]:
    problems: list[str] = []

    # 1. Downstream fan-out from the authoritative catalog.
    seen = await lineage(session, T.SHOPIFY_PRODUCTS, hops=2)

    missing_ds = [p.dataset for p in T.PROJECTIONS if p.dataset not in seen]
    missing_jobs = [p.job_urn for p in T.PROJECTIONS if p.job_urn not in seen]

    print(f"  lineage reachable URNs: {len(seen)}")
    print(f"  projections found:      {len(T.PROJECTIONS) - len(missing_ds)}/{len(T.PROJECTIONS)}")
    print(f"  dataJobs found:         {len(T.PROJECTIONS) - len(missing_jobs)}/{len(T.PROJECTIONS)}")

    if missing_ds:
        problems.append(f"projections not reachable via lineage: {missing_ds}")
    if missing_jobs:
        problems.append(f"dataJobs not reachable via lineage: {missing_jobs}")

    # 2. Governance metadata that the rule engine depends on.
    result = await session.call_tool(
        "get_entities", {"urns": [T.SHOPIFY_PRODUCTS] + [p.dataset for p in T.PROJECTIONS]}
    )
    if failed(result):
        problems.append(f"get_entities failed: {text_of(result)[:200]}")
        return (not problems), problems

    body = text_of(result)
    if "authoritative" not in body:
        problems.append("comgu.authority=authoritative not visible on the catalog")

    ownerless = T.PROJECTIONS_BY_KEY["ai_commerce_freshness"]
    print(f"  ownership gap target:   {ownerless.display}")

    return (not problems), problems


async def main() -> int:
    env = dict(os.environ)
    env["DATAHUB_GMS_URL"] = GMS_URL
    env.setdefault("DATAHUB_GMS_TOKEN", "")

    params = StdioServerParameters(
        command="uvx", args=["mcp-server-datahub@latest"], env=env
    )

    print(f"verifying Commerce Lab via MCP (GMS={GMS_URL})")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            for attempt in range(1, MAX_ATTEMPTS + 1):
                print(f"\nattempt {attempt}/{MAX_ATTEMPTS}")
                ok, problems = await check(session)
                if ok:
                    print("\nCOMMERCE_LAB_OK")
                    return 0
                if attempt < MAX_ATTEMPTS:
                    for p in problems:
                        print(f"    pending: {p[:160]}")
                    print(f"  waiting {BACKOFF_SECONDS}s for indexing...")
                    await asyncio.sleep(BACKOFF_SECONDS)

            print("\nCOMMERCE_LAB_FAILED")
            for p in problems:
                print(f"  - {p}")
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
