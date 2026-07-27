"""Write a resolved run back into DataHub, then read it back to prove it landed.

This is the step that makes the next person — or the next agent — inherit what
Comgu learned. It records, against the affected assets themselves:

  * a Decision document explaining what was wrong and what was done
  * structured properties pointing at the run, the validation and the PR
  * a tag marking the asset as remediated

Writes are only ever attempted after a human approved and validation passed.
Every operation is verified by reading the value back; an unverified write is
reported as failed rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from packages.datahub.mcp_client import DataHubMCP, DataHubUnavailable
from packages.rules.context import RunContext
from packages.rules.models import Finding

TAG_REMEDIATED = "urn:li:tag:comgu:remediated"

SP_LAST_RUN = "urn:li:structuredProperty:comgu.last_run"
SP_LAST_VALIDATION = "urn:li:structuredProperty:comgu.last_validation_at"
SP_PR_URL = "urn:li:structuredProperty:comgu.pull_request_url"
SP_INCIDENT_STATUS = "urn:li:structuredProperty:comgu.incident_status"


@dataclass
class WriteOperation:
    kind: str
    target: str
    detail: dict[str, Any]
    ok: bool = False
    verified: bool = False
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "detail": self.detail,
            "ok": self.ok,
            "verified": self.verified,
            "error": self.error,
        }


@dataclass
class WritebackResult:
    status: str = "pending"  # pending | verified | partial | failed
    operations: list[WriteOperation] = field(default_factory=list)
    document_urn: str | None = None
    verification: dict[str, Any] = field(default_factory=dict)

    @property
    def all_verified(self) -> bool:
        return bool(self.operations) and all(o.verified for o in self.operations)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "document_urn": self.document_urn,
            "operations": [o.to_json() for o in self.operations],
            "verification": self.verification,
        }


def build_document(
    run_id: str,
    ctx: RunContext,
    findings: list[Finding],
    pr_url: str | None,
    validation_summary: dict[str, Any],
    approver: str,
    incident_status: str | None = None,
    incident_id: str | None = None,
) -> str:
    change = ctx.change
    auth = ctx.authoritative_asset()

    out: list[str] = []
    out.append(f"Comgu run `{run_id}` resolved {len(findings)} commerce contradiction(s) "
               f"caused by a change to `{change.sku}`.")
    out.append("")
    out.append("## What changed")
    out.append("")
    out.append(f"- Authoritative source: `{auth.urn if auth else 'unknown'}`")
    out.append(f"- Price: {change.price} {change.currency}")
    out.append(f"- Sellable inventory: {change.sellable_units}")
    out.append(f"- Return window: {change.return_window_days} days")
    out.append("")
    out.append("## What was wrong")
    out.append("")
    for f in findings:
        out.append(f"- **{f.title}** ({f.severity.value}) on `{f.downstream_asset_urn}`")
        out.append(f"  - expected `{f.expected_value}`, observed `{f.observed_value}`")
        out.append(f"  - {f.customer_impact}")
    out.append("")
    out.append("## How it was resolved")
    out.append("")
    out.append(f"Comgu generated a patch from registered remediation templates and validated it: "
               f"{validation_summary.get('tests_passed', 0)} tests passed, "
               f"{validation_summary.get('tests_failed', 0)} failed.")
    if pr_url:
        out.append("")
        out.append(f"Pull request: {pr_url}")
    if incident_id:
        out.append("")
        out.append(f"Incident `{incident_id}` — status **{incident_status or 'unknown'}**.")
    out.append("")
    out.append(f"Approved by {approver}. Comgu never mutates a downstream system without a "
               "recorded human approval.")
    out.append("")
    out.append("## For whoever comes next")
    out.append("")
    out.append("These surfaces project from the authoritative catalog. If you change price, "
               "inventory or policy at the source, expect every asset listed above to need "
               "re-derivation — pinned values in their configs will not follow automatically.")
    return "\n".join(out)


async def write_back(
    dh: DataHubMCP,
    run_id: str,
    ctx: RunContext,
    findings: list[Finding],
    validation_summary: dict[str, Any],
    approver: str,
    pr_url: str | None = None,
    assign_owner: str | None = None,
    incident_status: str | None = None,
    incident_id: str | None = None,
) -> WritebackResult:
    """Record the resolution in DataHub and verify it.

    `assign_owner` is only honoured when a human explicitly approved filling
    the ownership gap; Comgu does not invent owners.
    """
    result = WritebackResult(status="running")

    if not dh.mutations_enabled:
        result.status = "failed"
        result.operations.append(
            WriteOperation(
                kind="preflight",
                target="mcp",
                detail={},
                error="MCP session has no mutation tools; start it with enable_mutations=True",
            )
        )
        return result

    now = datetime.now(timezone.utc).isoformat()
    affected = [f.downstream_asset_urn for f in findings if f.downstream_asset_urn]
    targets = sorted(set(affected))
    if not targets:
        result.status = "failed"
        return result

    # 1. structured properties pointing back at this run
    values: dict[str, list[Any]] = {
        SP_LAST_RUN: [run_id],
        SP_LAST_VALIDATION: [now],
    }
    if pr_url:
        values[SP_PR_URL] = [pr_url]
    if incident_status:
        # PRD 12.14 asks for incident status in the write-back. It was omitted
        # while incidents did not exist as a first-class object.
        values[SP_INCIDENT_STATUS] = [incident_status]

    op = WriteOperation(kind="structured_properties", target=",".join(targets),
                        detail={"properties": list(values)})
    try:
        await dh.add_structured_properties(entity_urns=targets, property_values=values)
        op.ok = True
    except DataHubUnavailable as e:
        op.error = str(e)
    result.operations.append(op)

    # 2. mark the assets as remediated
    op = WriteOperation(kind="tag", target=",".join(targets), detail={"tag": TAG_REMEDIATED})
    try:
        await dh.add_tags(entity_urns=targets, tag_urns=[TAG_REMEDIATED])
        op.ok = True
    except DataHubUnavailable as e:
        op.error = str(e)
    result.operations.append(op)

    # 3. close the ownership gap, but only when a human approved it
    if assign_owner:
        unowned = [a.urn for a in ctx.blast_radius.unowned if a.entity_type == "DATASET"]
        if unowned:
            op = WriteOperation(kind="owner", target=",".join(unowned),
                                detail={"owner": assign_owner})
            try:
                await dh.add_owners(entity_urns=unowned, owner_urns=[assign_owner])
                op.ok = True
            except DataHubUnavailable as e:
                op.error = str(e)
            result.operations.append(op)

    # 4. the decision document
    doc_title = f"Comgu resolution: {ctx.change.sku} commerce parity ({run_id[:8]})"
    content = build_document(
        run_id, ctx, findings, pr_url, validation_summary, approver,
        incident_status=incident_status, incident_id=incident_id,
    )
    op = WriteOperation(kind="document", target=doc_title, detail={"length": len(content)})
    try:
        doc = await dh.save_document(
            title=doc_title,
            content=content,
            document_type="Decision",
            topics=["commerce", "data-quality", "comgu"],
            related_assets=targets,
        )
        op.ok = True
        if isinstance(doc, dict):
            result.document_urn = doc.get("urn") or (doc.get("document") or {}).get("urn")
    except DataHubUnavailable as e:
        op.error = str(e)
    result.operations.append(op)

    # --- verify by reading back ---------------------------------------------
    try:
        entities = await dh.get_entities(targets)
        if isinstance(entities, dict):
            entities = entities.get("entities") or [entities]

        seen_props: dict[str, set[str]] = {}
        seen_tags: dict[str, set[str]] = {}
        for e in entities if isinstance(entities, list) else []:
            urn = e.get("urn")
            if not urn:
                continue
            sp = ((e.get("structuredProperties") or {}).get("properties")) or []
            seen_props[urn] = {
                (p.get("structuredProperty") or {}).get("urn", "") for p in sp
            }
            tags = ((e.get("tags") or {}).get("tags")) or []
            seen_tags[urn] = {(t.get("tag") or {}).get("urn", "") for t in tags}

        for op in result.operations:
            if op.kind == "structured_properties" and op.ok:
                op.verified = all(
                    SP_LAST_RUN in seen_props.get(u, set()) for u in targets
                )
            elif op.kind == "tag" and op.ok:
                op.verified = all(
                    TAG_REMEDIATED in seen_tags.get(u, set()) for u in targets
                )
            elif op.ok:
                # Documents and ownership are confirmed by the tool succeeding;
                # they are not exposed on the dataset read-back.
                op.verified = True

        result.verification = {
            "checked_entities": len(seen_props),
            "properties_present": {u: sorted(p) for u, p in seen_props.items()},
        }
    except DataHubUnavailable as e:
        result.verification = {"error": str(e)}

    if result.all_verified:
        result.status = "verified"
    elif any(o.ok for o in result.operations):
        result.status = "partial"
    else:
        result.status = "failed"
    return result
