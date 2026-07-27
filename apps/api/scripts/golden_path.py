"""Run the golden path headlessly against live DataHub and the commerce lab.

    python -m apps.api.scripts.golden_path                    # detect only
    python -m apps.api.scripts.golden_path --remediate        # + patch, validate, write back
    python -m apps.api.scripts.golden_path --remediate --pr-live

Nothing here is mocked: real MCP calls, real lineage, real transforms, real
pytest. Pull requests are dry-run unless --pr-live is passed, and a dry run is
always reported as a dry run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from packages.datahub.context_builder import build_run_context
from packages.datahub.mcp_client import DataHubUnavailable, datahub_session
from packages.datahub.writeback import write_back
from packages.github.pr import open_pull_request
from packages.lab import bridge
from packages.patch.generator import discard, generate
from packages.patch.validator import run_validation
from packages.rules.engine import run_rules

APPROVER = os.environ.get("COMGU_APPROVER", "amir@comgu.site")


def hr(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Comgu golden path")
    ap.add_argument("--assert-findings", type=int, default=0)
    ap.add_argument("--assert-completed", action="store_true",
                    help="require the full chain to succeed; implies --remediate")
    ap.add_argument("--remediate", action="store_true", help="patch, validate and write back")
    ap.add_argument("--pr-live", action="store_true", help="open a real pull request")
    ap.add_argument("--assign-owner", default=None, help="close the ownership gap (human decision)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.assert_completed:
        args.remediate = True

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:18080")
    repo = os.environ.get("GITHUB_LAB_REPO", "")
    run_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()

    change = bridge.load_catalog()
    source_urn = bridge.catalog_source_urn()
    lab = bridge.lab_path()
    projections = bridge.build_projections()

    hr(f"RUN {run_id[:12]}  |  {change.sku} @ {change.price} {change.currency}")
    print(f"source:  {source_urn}")
    print(f"datahub: {gms}")
    print(f"lab:     {lab}")

    # --- 1. context from DataHub --------------------------------------------
    try:
        async with datahub_session(gms) as dh:
            ctx = await build_run_context(dh, change, source_urn, projections, gms_url=gms)
            read_trace = dh.trace
    except DataHubUnavailable as e:
        print(f"\nCONTEXT_FAILED: {e}", file=sys.stderr)
        return 2

    hr("1. DATAHUB CONTEXT")
    print(f"{len(ctx.blast_radius.assets)} downstream assets from "
          f"{ctx.blast_radius.lineage_edges} lineage results "
          f"({len(read_trace)} MCP calls, {read_trace.total_ms}ms)")
    for c in read_trace.calls:
        print(f"   {c.tool:<26} {c.duration_ms:>5}ms  {c.summary}")
    auth = ctx.authoritative_asset()
    print(f"\nauthoritative per DataHub: {auth.name if auth else 'NONE'}")
    for a in sorted(ctx.blast_radius.datasets, key=lambda x: x.name):
        owner = a.owners[0].split(":")[-1] if a.owners else "UNOWNED"
        print(f"   {a.name:<24} crit={a.criticality:<8} channel={a.channel or '-':<15} owner={owner}")

    # --- 2. deterministic checks --------------------------------------------
    report = run_rules(ctx)
    if report.context_error:
        print(f"\nCONTEXT ERROR: {report.context_error}", file=sys.stderr)
        return 2

    hr("2. FINDINGS")
    print(f"{len(report.findings)} findings | max severity {report.max_severity.value} | {report.counts}")
    for f in report.findings:
        print(f"\n  [{f.severity.value.upper()}] {f.title}")
        print(f"    {f.summary}")
        print(f"    fix: {f.remediation_template} -> {f.target_file}")

    if len(report.findings) < args.assert_findings:
        print(f"\nFAILED: expected >= {args.assert_findings} findings", file=sys.stderr)
        return 1

    if not args.remediate:
        print("\nDETECTION_OK  (pass --remediate to patch, validate and write back)")
        return 0

    # --- 3. patch ------------------------------------------------------------
    hr("3. PATCH")
    patch = generate(report.findings, change, lab)
    print(f"workspace: {patch.workspace}")
    print(f"{len(patch.files)} files patched, {len(patch.skipped)} skipped, "
          f"{len(patch.rejected)} rejected")
    for pf in patch.files:
        for e in pf.edits:
            print(f"   {pf.file_path:<40} {e.field}: {e.before} -> {e.after}")
    for s in patch.skipped:
        print(f"   skipped {s['rule']}: {s['reason']}")
    if patch.rejected:
        print("   REJECTED (unsafe or unknown):")
        for r in patch.rejected:
            print(f"     {r}")

    try:
        # --- 4. validation ---------------------------------------------------
        hr("4. VALIDATION")
        validation = run_validation(patch.workspace, ["pytest"])
        print(f"status: {validation.status}  {validation.summary}")
        for s in validation.steps:
            print(f"   {s.command_display} -> {s.status} (exit {s.exit_code}, {s.duration_ms}ms)")
        tail = validation.steps[-1].stdout_redacted.strip().splitlines()[-3:] if validation.steps else []
        for line in tail:
            print(f"     {line}")

        if not validation.passed:
            print("\nVALIDATION_FAILED — refusing to open a pull request", file=sys.stderr)
            return 1

        # --- 5. pull request -------------------------------------------------
        hr("5. PULL REQUEST")
        if not repo:
            print("GITHUB_LAB_REPO not set — skipping (set it to enable PR creation)")
            pr = None
        else:
            pr = open_pull_request(
                run_id=run_id, ctx=ctx, findings=report.findings, patch=patch,
                validation=validation, repo=repo, lab_path=lab,
                approver=APPROVER, approved_at=started,
                dry_run=not args.pr_live,
            )
            print(f"status: {pr.status}  branch: {pr.branch}")
            if pr.url:
                print(f"url:    {pr.url}")
            elif pr.status == "dry_run":
                print("dry run — no pull request was created")
            if pr.error:
                print(f"error:  {pr.error}")

        # --- 6. DataHub write-back -------------------------------------------
        hr("6. DATAHUB WRITE-BACK")
        async with datahub_session(gms, enable_mutations=True) as dhw:
            wb = await write_back(
                dhw, run_id=run_id, ctx=ctx, findings=report.findings,
                validation_summary=validation.summary, approver=APPROVER,
                pr_url=pr.url if pr and pr.is_real else None,
                assign_owner=args.assign_owner,
            )
            write_trace = dhw.trace
        print(f"status: {wb.status}")
        for op in wb.operations:
            mark = "verified" if op.verified else ("ok" if op.ok else "FAILED")
            print(f"   {op.kind:<24} {mark}" + (f"  {op.error[:90]}" if op.error else ""))
        if wb.document_urn:
            print(f"   document: {wb.document_urn}")
        print(f"   ({len(write_trace)} MCP calls, {write_trace.total_ms}ms)")

        if args.json:
            print(json.dumps({
                "run_id": run_id,
                "findings": report.to_json(),
                "patch": patch.to_json(),
                "validation": validation.to_json(),
                "pull_request": pr.to_json() if pr else None,
                "writeback": wb.to_json(),
                "tool_trace": read_trace.to_json() + write_trace.to_json(),
            }, indent=2))

        hr("RESULT")
        ok = validation.passed and wb.status in ("verified", "partial")
        print(f"findings={len(report.findings)} patched={len(patch.files)} "
              f"validation={validation.status} writeback={wb.status} "
              f"pr={pr.status if pr else 'skipped'}")

        if args.assert_completed:
            # Every stage must have genuinely succeeded, not merely been reached.
            problems = []
            if not report.findings:
                problems.append("no findings")
            if patch.is_empty:
                problems.append("empty patch")
            if patch.rejected:
                problems.append(f"{len(patch.rejected)} rejected patch targets")
            if not validation.passed:
                problems.append(f"validation {validation.status}")
            if wb.status != "verified":
                problems.append(f"write-back {wb.status}")
            if problems:
                print("\nGOLDEN_PATH_FAILED: " + "; ".join(problems), file=sys.stderr)
                return 1

        print("\nGOLDEN_PATH_OK" if ok else "\nGOLDEN_PATH_INCOMPLETE")
        return 0 if ok else 1
    finally:
        if not args.pr_live:
            discard(patch)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
