"""Run the golden path headlessly against live DataHub and the commerce lab.

    python -m apps.api.scripts.golden_path

This is Comgu's read -> context -> check chain with nothing mocked: real MCP
calls, real lineage, real downstream transforms. Use --assert-findings to make
it a pass/fail gate in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from packages.datahub.context_builder import build_run_context
from packages.datahub.mcp_client import DataHubUnavailable, datahub_session
from packages.lab import bridge
from packages.rules.engine import run_rules


async def main() -> int:
    ap = argparse.ArgumentParser(description="Comgu golden path")
    ap.add_argument("--assert-findings", type=int, default=0, help="fail below this many findings")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args()

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:18080")

    # Observed side: execute the downstream transforms as they are today.
    change = bridge.load_catalog()
    source_urn = bridge.catalog_source_urn()
    projections = bridge.build_projections()

    if not args.json:
        print(f"change:  {change.sku} @ {change.price} {change.currency}, "
              f"{change.sellable_units} sellable, {change.return_window_days}d returns")
        print(f"source:  {source_urn}")
        print(f"datahub: {gms}\n")

    # Expected side and blast radius: DataHub, via MCP.
    try:
        async with datahub_session(gms) as dh:
            ctx = await build_run_context(dh, change, source_urn, projections)
            trace = dh.trace
    except DataHubUnavailable as e:
        print(f"CONTEXT_FAILED: {e}", file=sys.stderr)
        return 2

    report = run_rules(ctx)

    if args.json:
        print(json.dumps(
            {
                "context": {
                    "root": ctx.blast_radius.root_urn,
                    "lineage_edges": ctx.blast_radius.lineage_edges,
                    "assets": len(ctx.blast_radius.assets),
                    "tool_calls": trace.to_json(),
                },
                "report": report.to_json(),
            },
            indent=2,
        ))
        return 0 if len(report.findings) >= args.assert_findings else 1

    print(f"DataHub context: {len(ctx.blast_radius.assets)} downstream assets from "
          f"{ctx.blast_radius.lineage_edges} lineage results "
          f"({len(trace)} MCP calls, {trace.total_ms}ms)")
    for c in trace.calls:
        print(f"   {c.tool:<28} {c.duration_ms:>5}ms  {c.summary}")

    auth = ctx.authoritative_asset()
    print(f"\nauthoritative source per DataHub: {auth.name if auth else 'NONE'}")
    print("blast radius:")
    for a in sorted(ctx.blast_radius.assets, key=lambda x: x.name):
        owner = a.owners[0].split(":")[-1] if a.owners else "UNOWNED"
        print(f"   {a.name:<26} {a.entity_type:<9} crit={a.criticality:<8} "
              f"channel={a.channel or '-':<16} owner={owner}")

    if report.context_error:
        print(f"\nCONTEXT ERROR: {report.context_error}")
        return 2

    print(f"\n{len(report.findings)} findings | max severity {report.max_severity.value}")
    for f in report.findings:
        print(f"\n  [{f.severity.value.upper()}] {f.title}")
        print(f"    {f.summary}")
        print(f"    expected={f.expected_value} observed={f.observed_value}")
        print(f"    fix: {f.remediation_template} -> {f.target_file}")

    if len(report.findings) < args.assert_findings:
        print(f"\nFAILED: expected >= {args.assert_findings} findings", file=sys.stderr)
        return 1

    print("\nGOLDEN_PATH_OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
