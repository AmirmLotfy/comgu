"""Open a pull request carrying an approved, validated patch.

Rules that matter more than the mechanics:

  * A PR URL is only ever reported when GitHub actually returned one. Dry runs
    say so explicitly rather than inventing a plausible link.
  * Branch names are derived from the run id, so a retried run reuses its
    branch and updates the existing PR instead of opening a second one.
  * Nothing is pushed unless validation passed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.patch.generator import GeneratedPatch
from packages.patch.validator import ValidationRun
from packages.rules.context import RunContext
from packages.rules.models import Finding

BRANCH_PREFIX = "comgu/run-"


class GitHubUnavailable(RuntimeError):
    """GitHub could not be reached or refused the operation."""


@dataclass
class PullRequestResult:
    status: str  # dry_run | open | failed
    branch: str
    repository: str | None = None
    number: int | None = None
    url: str | None = None
    commit_sha: str | None = None
    body: str = ""
    error: str | None = None
    existing: bool = False

    @property
    def is_real(self) -> bool:
        return self.status == "open" and bool(self.url)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "branch": self.branch,
            "repository": self.repository,
            "number": self.number,
            "url": self.url,
            "commit_sha": self.commit_sha,
            "existing": self.existing,
            "error": self.error,
        }


def _run(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=180)
    if check and proc.returncode != 0:
        raise GitHubUnavailable(
            f"{' '.join(args[:3])} failed ({proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    return proc.stdout.strip()


def branch_for(run_id: str) -> str:
    return f"{BRANCH_PREFIX}{run_id[:12]}"


def build_body(
    run_id: str,
    ctx: RunContext,
    findings: list[Finding],
    patch: GeneratedPatch,
    validation: ValidationRun,
    approver: str,
    approved_at: str,
) -> str:
    """The PR body is the audit record; it must stand alone."""
    change = ctx.change
    auth = ctx.authoritative_asset()

    lines: list[str] = []
    lines.append("## Comgu remediation")
    lines.append("")
    lines.append(
        f"A commerce change left {len(findings)} downstream contradiction(s). "
        "Comgu detected them from DataHub lineage, generated this patch from "
        "registered remediation templates, and validated it before opening this PR."
    )
    lines.append("")
    lines.append(f"**Run:** `{run_id}`")
    lines.append("")

    lines.append("### Triggering change")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| SKU | `{change.sku}` |")
    lines.append(f"| Price | {change.price} {change.currency} |")
    lines.append(f"| Sellable inventory | {change.sellable_units} |")
    lines.append(f"| Return window | {change.return_window_days} days |")
    lines.append(f"| Authoritative source | `{auth.urn if auth else 'unknown'}` |")
    lines.append("")

    lines.append("### DataHub context")
    lines.append("")
    lines.append(
        f"Blast radius resolved from **{ctx.blast_radius.lineage_edges} lineage results** "
        f"downstream of the source (max {ctx.blast_radius.max_hops} hops), "
        f"using {len(ctx.tool_trace)} MCP calls."
    )
    lines.append("")
    lines.append("| Asset | Channel | Criticality | Owner |")
    lines.append("|---|---|---|---|")
    for a in sorted(ctx.blast_radius.datasets, key=lambda x: x.name):
        owner = a.owners[0].split(":")[-1] if a.owners else "**unowned**"
        lines.append(f"| `{a.name}` | {a.channel or '-'} | {a.criticality} | {owner} |")
    lines.append("")

    lines.append("### Findings")
    lines.append("")
    for f in findings:
        lines.append(f"- **[{f.severity.value}]** {f.title}")
        lines.append(f"  - {f.summary}")
        lines.append(f"  - expected `{f.expected_value}`, observed `{f.observed_value}`")
        lines.append(f"  - {f.customer_impact}")
    lines.append("")

    lines.append("### Corrections in this PR")
    lines.append("")
    lines.append("| File | Change |")
    lines.append("|---|---|")
    for pf in patch.files:
        for e in pf.edits:
            lines.append(f"| `{pf.file_path}` | `{e.field}`: `{e.before}` → `{e.after}` |")
    lines.append("")
    if patch.skipped:
        lines.append("Not auto-fixed (needs a human):")
        lines.append("")
        for s in patch.skipped:
            lines.append(f"- `{s['rule']}` — {s['reason']}")
        lines.append("")

    lines.append("### Validation")
    lines.append("")
    summary = validation.summary
    verdict = "passed" if validation.passed else "FAILED"
    lines.append(f"`{verdict}` — {summary.get('tests_passed', 0)} passed, "
                 f"{summary.get('tests_failed', 0)} failed in {summary.get('duration_ms', 0)}ms")
    lines.append("")
    for step in validation.steps:
        lines.append(f"- `{step.command_display}` → {step.status} (exit {step.exit_code})")
    lines.append("")

    lines.append("### Rollback")
    lines.append("")
    lines.append("Close this PR without merging, or revert the merge commit. The changes are")
    lines.append("confined to the configuration files listed above; no data migration is involved.")
    lines.append("")

    lines.append("### Approval")
    lines.append("")
    lines.append(f"Approved by **{approver}** at {approved_at}. Comgu does not push without")
    lines.append("an explicit human approval recorded against the plan and context checksums.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated by [Comgu](https://github.com/AmirmLotfy/comgu). The downstream")
    lines.append("systems in this repository are a simulated commerce ecosystem: the files,")
    lines.append("transforms, contradictions and tests are real; the commercial platforms")
    lines.append("behind them are not live integrations.")

    return "\n".join(lines)


def _existing_pr(repo: str, branch: str) -> dict[str, Any] | None:
    out = _run(
        ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "all",
         "--json", "number,url,state"],
        check=False,
    )
    if not out:
        return None
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return None
    return items[0] if items else None


def open_pull_request(
    run_id: str,
    ctx: RunContext,
    findings: list[Finding],
    patch: GeneratedPatch,
    validation: ValidationRun,
    repo: str,
    lab_path: Path,
    approver: str,
    approved_at: str,
    dry_run: bool = True,
    base: str = "main",
) -> PullRequestResult:
    """Push the patched files and open (or update) a PR."""
    branch = branch_for(run_id)
    body = build_body(run_id, ctx, findings, patch, validation, approver, approved_at)

    if not validation.passed:
        return PullRequestResult(
            status="failed",
            branch=branch,
            repository=repo,
            body=body,
            error="validation did not pass; refusing to open a pull request",
        )

    if patch.is_empty:
        return PullRequestResult(
            status="failed", branch=branch, repository=repo, body=body,
            error="patch is empty; nothing to propose",
        )

    if dry_run:
        return PullRequestResult(status="dry_run", branch=branch, repository=repo, body=body)

    title = f"Comgu: correct {len(patch.files)} downstream contradiction(s) for {ctx.change.sku}"

    # Remember where the checkout was so we can put it back. Leaving it on the
    # run branch would mean the next run — and the demo — starts from already
    # corrected files instead of the contradictions.
    original_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=lab_path, check=False)

    try:
        return _push_and_open(
            run_id, ctx, patch, repo, lab_path, branch, base, title, body
        )
    finally:
        if original_branch and original_branch != branch:
            _run(["git", "checkout", "--force", original_branch], cwd=lab_path, check=False)


def _push_and_open(
    run_id: str,
    ctx: RunContext,
    patch: GeneratedPatch,
    repo: str,
    lab_path: Path,
    branch: str,
    base: str,
    title: str,
    body: str,
) -> PullRequestResult:
    approver_note = f"Comgu run {run_id}"

    # Work in the real checkout, on a run-scoped branch.
    _run(["git", "fetch", "origin", base], cwd=lab_path, check=False)
    _run(["git", "checkout", "-B", branch, f"origin/{base}"], cwd=lab_path, check=False) or \
        _run(["git", "checkout", "-B", branch], cwd=lab_path)

    # Copy the validated files out of the workspace rather than re-running the
    # templates, so what is pushed is exactly what was validated.
    for pf in patch.files:
        src = patch.workspace / pf.file_path
        dst = lab_path / pf.file_path
        dst.write_text(src.read_text())

    _run(["git", "add", *[pf.file_path for pf in patch.files]], cwd=lab_path)
    status = _run(["git", "status", "--porcelain"], cwd=lab_path)
    if status:
        _run(
            ["git", "-c", "user.name=Comgu", "-c", "user.email=noreply@comgu.site",
             "commit", "-m", title, "-m", approver_note],
            cwd=lab_path,
        )
    sha = _run(["git", "rev-parse", "HEAD"], cwd=lab_path)

    try:
        _run(["git", "push", "-u", "origin", branch, "--force-with-lease"], cwd=lab_path)
    except GitHubUnavailable as e:
        return PullRequestResult(
            status="failed", branch=branch, repository=repo, commit_sha=sha,
            body=body, error=str(e),
        )

    existing = _existing_pr(repo, branch)
    if existing and existing.get("state") == "OPEN":
        return PullRequestResult(
            status="open", branch=branch, repository=repo,
            number=existing.get("number"), url=existing.get("url"),
            commit_sha=sha, body=body, existing=True,
        )

    body_file = patch.workspace.parent / "pr_body.md"
    body_file.write_text(body)
    try:
        url = _run(
            ["gh", "pr", "create", "--repo", repo, "--head", branch, "--base", base,
             "--title", title, "--body-file", str(body_file)],
        )
    except GitHubUnavailable as e:
        return PullRequestResult(
            status="failed", branch=branch, repository=repo, commit_sha=sha,
            body=body, error=str(e),
        )

    url = url.splitlines()[-1].strip() if url else None
    number = None
    if url and url.rstrip("/").split("/")[-1].isdigit():
        number = int(url.rstrip("/").split("/")[-1])

    return PullRequestResult(
        status="open" if url else "failed",
        branch=branch,
        repository=repo,
        number=number,
        url=url,
        commit_sha=sha,
        body=body,
        error=None if url else "gh returned no pull request URL",
    )
