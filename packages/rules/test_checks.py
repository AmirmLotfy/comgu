"""Deterministic tests for the rule engine. No DataHub, no network."""

from __future__ import annotations

import dataclasses

import pytest

from packages.rules.checks import ALL_RULES
from packages.rules.engine import run_rules
from packages.rules.fixtures import (
    contradictory_projections,
    corrected_projections,
    golden_context,
)
from packages.rules.models import RuleStatus, Severity


def test_golden_change_produces_at_least_five_findings():
    report = run_rules(golden_context())
    assert report.context_error is None
    assert len(report.findings) >= 5, (
        f"expected >=5 findings, got {len(report.findings)}: "
        f"{[f.rule_code for f in report.findings]}"
    )


def test_every_rule_fires_on_the_golden_change():
    report = run_rules(golden_context())
    fired = {f.rule_code for f in report.findings}
    expected = {r.code for r in ALL_RULES}
    assert fired == expected, f"rules that did not fire: {expected - fired}"


def test_no_findings_once_corrected():
    report = run_rules(golden_context(projections=corrected_projections()))
    assert report.findings == [], [f.title for f in report.findings]
    assert all(r.status == RuleStatus.PASSED for r in report.results)


def test_oversell_is_critical():
    report = run_rules(golden_context())
    inv = [f for f in report.findings if f.rule_code == "inventory_safety"]
    assert inv and inv[0].severity is Severity.CRITICAL


def test_ownership_gap_is_reported():
    report = run_rules(golden_context())
    gaps = [f for f in report.findings if f.remediation_template == "assign_owner"]
    assert gaps, "the unowned customer-facing asset should produce its own finding"
    assert gaps[0].auto_fix_eligible is False, "assigning an owner needs a human decision"


def test_findings_carry_expected_and_observed_values():
    for f in run_rules(golden_context()).findings:
        assert f.expected_value is not None
        assert f.observed_value is not None
        assert f.expected_value != f.observed_value


def test_findings_are_evidence_backed_and_lineage_derived():
    for f in run_rules(golden_context()).findings:
        assert f.evidence, f"{f.rule_code} produced no evidence"
        kinds = {e.evidence_type.value for e in f.evidence}
        assert "lineage" in kinds, (
            f"{f.rule_code} did not record that the asset came from DataHub lineage"
        )


def test_findings_reference_both_source_and_downstream_assets():
    for f in run_rules(golden_context()).findings:
        assert f.source_asset_urn, f"{f.rule_code} lost the authoritative source"
        assert f.downstream_asset_urn, f"{f.rule_code} lost the downstream asset"


def test_auto_fixable_findings_name_a_target_file():
    for f in run_rules(golden_context()).findings:
        if f.auto_fix_eligible:
            assert f.target_file, f"{f.rule_code} is auto-fixable but names no file to patch"
            assert f.remediation_template


def test_severity_follows_datahub_criticality():
    """Re-governing an asset in DataHub must change how Comgu grades it."""
    ctx = golden_context()
    feed = ctx.blast_radius.by_rule("price_parity")
    feed.criticality = "low"
    feed.customer_facing = False

    report = run_rules(ctx)
    price = [f for f in report.findings if f.rule_code == "price_parity"][0]
    assert price.severity.rank < Severity.CRITICAL.rank, (
        "severity ignored the asset's DataHub criticality"
    )


# --- the DataHub dependency proof -------------------------------------------


def test_engine_refuses_to_guess_without_datahub_authority():
    """Strip comgu.authority and Comgu must stop, not fall back to a guess."""
    ctx = golden_context()
    for asset in ctx.assets_by_urn.values():
        asset.authority = "projection"

    report = run_rules(ctx)
    assert report.findings == []
    assert report.context_error is not None
    assert "authoritative" in report.context_error


def test_engine_finds_nothing_without_lineage():
    """With an empty blast radius there is nothing to check."""
    ctx = golden_context()
    ctx.blast_radius.assets = []

    report = run_rules(ctx)
    assert report.findings == []
    assert all(r.status == RuleStatus.SKIPPED for r in report.results)
    assert all("lineage" in (r.skip_reason or "") for r in report.results)


def test_report_serialises():
    payload = run_rules(golden_context()).to_json()
    assert payload["finding_count"] >= 5
    assert payload["max_severity"] == "critical"
    assert payload["counts_by_severity"]["critical"] >= 1
