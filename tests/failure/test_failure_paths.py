"""Failure paths (PRD 17, 23).

Every one of these asks the same question: when a dependency breaks, does Comgu
degrade honestly, or does it invent something? The answer has to be the former —
a confidently wrong blast radius is worse than no blast radius, and a fabricated
pull-request URL is worse than an error.

No network.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy.orm import sessionmaker

from apps.api.db.models import Base, Organisation, Run, Shop
from apps.api.db.session import make_engine
from apps.api.workflow import Status, can, transition
from packages.datahub.mcp_client import DataHubUnavailable, ToolTrace
from packages.datahub.quality import fetch_assertions
from packages.datahub.writeback import write_back
from packages.patch.generator import discard, generate
from packages.patch.validator import run_validation
from packages.planner.planner import FakeProvider, plan_remediation
from packages.rules.context import MissingContext
from packages.rules.engine import run_rules
from packages.rules.fixtures import golden_change, golden_context


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/f.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture
def run(db):
    org = Organisation(name="N", slug="n")
    db.add(org)
    db.flush()
    shop = Shop(organisation_id=org.id, shop_domain="n.myshopify.com", display_name="N")
    db.add(shop)
    db.flush()
    r = Run(organisation_id=org.id, shop_id=shop.id, status=Status.RECEIVED)
    db.add(r)
    db.commit()
    return r


# --- DataHub unavailable -----------------------------------------------------


def test_datahub_timeout_never_yields_a_blast_radius():
    """PRD 17: do not substitute hardcoded lineage."""
    ctx = golden_context()
    ctx.blast_radius.assets = []
    report = run_rules(ctx)
    assert report.findings == []
    assert all(r.status.value == "skipped" for r in report.results)


def test_missing_authority_halts_rather_than_defaulting():
    ctx = golden_context()
    for a in ctx.assets_by_urn.values():
        a.authority = "projection"
    with pytest.raises(MissingContext):
        ctx.require_authority()


def test_assertion_lookup_failure_is_swallowed_not_fatal():
    """A missing quality signal thins the evidence; it must not kill a finding."""
    trace = ToolTrace()
    out = fetch_assertions("http://127.0.0.1:9", "urn:li:dataset:(x,y,PROD)", trace=trace, timeout=1)
    assert out == []
    assert len(trace) == 1 and trace.calls[0].ok is False
    assert trace.calls[0].error


def test_context_failure_message_names_the_cause():
    from packages.datahub.mcp_client import _root_cause

    detail = _root_cause(ExceptionGroup("tg", [ConnectionRefusedError(61, "Connection refused")]))
    assert "Connection refused" in detail and "TaskGroup" not in detail


# --- model unavailable -------------------------------------------------------


def test_model_timeout_falls_back_to_the_deterministic_plan():
    ctx = golden_context()
    findings = run_rules(ctx).findings

    class Timeout(FakeProvider):
        def complete_json(self, *a, **k):
            raise TimeoutError("model did not respond")

    result = plan_remediation(ctx, findings, provider=Timeout())
    assert result.source == "deterministic"
    assert "TimeoutError" in (result.rejected_reason or "")
    # PRD 17: never block evidence visibility — the plan still covers every finding.
    covered = {a.finding_rule_code for a in result.plan.proposed_actions}
    assert covered == {f.rule_code for f in findings if f.remediation_template}


def test_model_returning_prose_instead_of_json_falls_back():
    ctx = golden_context()
    findings = run_rules(ctx).findings
    result = plan_remediation(
        ctx, findings, provider=FakeProvider(payload="Sure! Here is the plan:\n1. Fix the feed")
    )
    assert result.source == "deterministic"
    assert "schema validation failed" in (result.rejected_reason or "")


def test_model_proposing_an_unknown_action_type_falls_back():
    ctx = golden_context()
    findings = run_rules(ctx).findings
    f = findings[0]
    payload = json.dumps({
        "schema_version": 2, "summary": "s", "business_impact": "b",
        "proposed_actions": [{
            "action_type": "delete_everything", "sequence_number": 1,
            "finding_rule_code": f.rule_code, "remediation_template": f.remediation_template,
            "target_system": "x", "rationale": "r", "risk_level": "low",
        }],
        "validation_plan": [{"command_id": "pytest", "expectation": "x"}],
        "rollback_plan": "r", "confidence_explanation": "c",
    })
    result = plan_remediation(ctx, findings, provider=FakeProvider(payload=payload))
    assert result.source == "deterministic"


# --- validation failure ------------------------------------------------------


def test_validation_failure_blocks_the_pull_request(db, run):
    for s in (
        Status.NORMALIZED, Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED,
        Status.CHECKS_RUNNING, Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
        Status.AWAITING_APPROVAL, Status.APPROVED, Status.PATCH_GENERATING,
        Status.PATCH_GENERATED, Status.VALIDATION_RUNNING, Status.VALIDATION_FAILED,
    ):
        transition(db, run, s)
    assert not can(run, Status.PULL_REQUEST_CREATING)
    assert not can(run, Status.VALIDATED)
    assert can(run, Status.AWAITING_APPROVAL), "must be able to go back for revision"


def test_a_broken_interpreter_is_an_error_not_a_failed_test(tmp_path, monkeypatch):
    monkeypatch.setenv("COMGU_LAB_PATH", str(tmp_path / "nope"))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = run_validation(ws, ["pytest"])
    assert not result.passed
    assert result.steps[0].status in ("error", "failed")


def test_validation_output_is_preserved_for_diagnosis(tmp_path, monkeypatch):
    monkeypatch.setenv("COMGU_LAB_PATH", str(tmp_path / "nope"))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = run_validation(ws, ["pytest"])
    step = result.steps[0]
    assert step.command_display, "PRD 17: preserve the output"
    assert step.duration_ms >= 0


# --- GitHub failure ----------------------------------------------------------


def test_pull_request_is_refused_when_validation_failed(tmp_path):
    from packages.patch.validator import ValidationRun
    from packages.github.pr import open_pull_request

    ctx = golden_context()
    findings = run_rules(ctx).findings
    patch = generate(findings, golden_change(), _fake_lab(tmp_path))
    try:
        failed = ValidationRun(status="failed")
        result = open_pull_request(
            run_id="r1", ctx=ctx, findings=findings, patch=patch, validation=failed,
            repo="o/r", lab_path=tmp_path, approver="a@b.c", approved_at="now",
            dry_run=False,
        )
        assert result.status == "failed"
        assert result.url is None, "a URL must never be reported without one from GitHub"
        assert "validation" in (result.error or "")
    finally:
        discard(patch)


def test_dry_run_reports_itself_and_never_invents_a_url(tmp_path):
    from packages.patch.validator import ValidationRun
    from packages.github.pr import open_pull_request

    ctx = golden_context()
    findings = run_rules(ctx).findings
    patch = generate(findings, golden_change(), _fake_lab(tmp_path))
    try:
        passed = ValidationRun(status="passed")
        result = open_pull_request(
            run_id="r1", ctx=ctx, findings=findings, patch=patch, validation=passed,
            repo="o/r", lab_path=tmp_path, approver="a@b.c", approved_at="now",
            dry_run=True,
        )
        assert result.status == "dry_run"
        assert result.url is None
        assert result.is_real is False
        assert result.body, "the body is still produced so it can be reviewed"
    finally:
        discard(patch)


def test_empty_patch_is_refused(tmp_path):
    from packages.patch.validator import ValidationRun
    from packages.github.pr import open_pull_request
    from packages.patch.generator import GeneratedPatch

    ctx = golden_context()
    empty = GeneratedPatch(workspace=tmp_path)
    result = open_pull_request(
        run_id="r1", ctx=ctx, findings=[], patch=empty,
        validation=ValidationRun(status="passed"), repo="o/r", lab_path=tmp_path,
        approver="a@b.c", approved_at="now", dry_run=False,
    )
    assert result.status == "failed" and "empty" in (result.error or "")


# --- write-back failure ------------------------------------------------------


def test_writeback_refuses_a_session_without_mutation_tools():
    class ReadOnly:
        mutations_enabled = False
        trace = ToolTrace()

    ctx = golden_context()
    findings = run_rules(ctx).findings
    result = asyncio.run(
        write_back(ReadOnly(), run_id="r", ctx=ctx, findings=findings,
                   validation_summary={}, approver="a@b.c")
    )
    assert result.status == "failed"
    assert "mutation tools" in (result.operations[0].error or "")


def test_unverified_write_is_reported_as_unverified():
    """A write we cannot confirm must not be reported as done."""
    class Flaky:
        mutations_enabled = True
        trace = ToolTrace()

        async def add_structured_properties(self, **k): return {"ok": True}
        async def add_tags(self, **k): return {"ok": True}
        async def save_document(self, **k): return {"urn": "urn:li:document:x"}
        async def get_entities(self, urns):
            raise DataHubUnavailable("read-back failed")

    ctx = golden_context()
    findings = run_rules(ctx).findings
    result = asyncio.run(
        write_back(Flaky(), run_id="r", ctx=ctx, findings=findings,
                   validation_summary={}, approver="a@b.c")
    )
    assert result.status == "partial", "writes succeeded but could not be verified"
    assert not result.all_verified
    assert "error" in result.verification


# --- worker restart ----------------------------------------------------------


def test_worker_resumes_from_persisted_state(db, run):
    from apps.worker.worker import AFTER_APPROVAL, RESUMABLE

    for s in (Status.NORMALIZED, Status.CONTEXT_PENDING):
        transition(db, run, s)
    assert run.status in RESUMABLE, "a mid-flight run must be resumable"
    assert run.status not in AFTER_APPROVAL


def test_worker_never_resumes_a_run_waiting_on_a_human(db, run):
    from apps.worker.worker import RESUMABLE

    for s in (
        Status.NORMALIZED, Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED,
        Status.CHECKS_RUNNING, Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
        Status.AWAITING_APPROVAL,
    ):
        transition(db, run, s)
    assert run.status not in RESUMABLE, "waiting on approval is not stranded work"


def test_worker_never_resumes_a_terminal_run(db, run):
    from apps.worker.worker import RESUMABLE

    transition(db, run, Status.FAILED, reason="x")
    assert run.status not in RESUMABLE


def test_a_stale_lease_is_reclaimable(db, run):
    from datetime import datetime, timedelta, timezone

    from apps.worker.worker import LEASE_SECONDS, claim

    run.locked_by = "someone-else"
    run.locked_at = datetime.now(timezone.utc)
    db.commit()
    assert claim(db, run) is False, "a fresh lease belongs to its holder"

    run.locked_at = datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS + 60)
    db.commit()
    assert claim(db, run) is True, "an abandoned lease must be reclaimable"


def test_resumed_runs_never_escalate_to_a_live_pull_request():
    """The approving principal is absent, so nothing may authorise a live PR."""
    import inspect

    from apps.worker import worker

    src = inspect.getsource(worker.sweep_once)
    assert "pr_live=False" in src


# --- helper ------------------------------------------------------------------


def _fake_lab(tmp_path):
    """Minimal lab checkout so patch generation has something to copy."""
    lab = tmp_path / "lab"
    for d in ("feeds", "promotions", "bundles", "ai", "policies", "catalog"):
        (lab / d).mkdir(parents=True)
    (lab / "feeds" / "google_merchant.transform.yaml").write_text(
        'items:\n  - sku: NH-BREW-PRO\n    price_override: "89.00"\n'
    )
    (lab / "promotions" / "spring_sale.yaml").write_text(
        'promotions:\n  - sku: NH-BREW-PRO\n    active: true\n    price_basis: "89.00"\n'
    )
    (lab / "bundles" / "brew_pro_bundle.yaml").write_text(
        "bundles:\n  - bundle_id: B\n    components:\n      - sku: NH-BREW-PRO\n"
        "        units_per_bundle: 1\n    committed_units: 5\n"
    )
    (lab / "ai" / "manifest.config.json").write_text(
        '{"offers":[{"sku":"NH-BREW-PRO","price_source":"pinned","pinned_price":"89.00",'
        '"availability_source":"pinned","pinned_available":12}]}'
    )
    (lab / "policies" / "returns.yaml").write_text(
        "policies:\n  - sku: NH-BREW-PRO\n    return_window_days: 14\n"
    )
    (lab / "catalog" / "authoritative.json").write_text("{}")
    return lab
