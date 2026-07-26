"""Patch generation, safety and validation tests. No network."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from packages.patch.generator import discard, generate, prepare_workspace
from packages.patch.safety import (
    ALLOWED_DIRS,
    UnsafePath,
    check_writable,
    resolve_within,
)
from packages.patch.templates import UnknownTemplate, apply_template
from packages.patch.validator import (
    REGISTERED_COMMANDS,
    UnregisteredCommand,
    redact,
    run_validation,
)
from packages.rules.engine import run_rules
from packages.rules.fixtures import golden_change, golden_context

LAB = Path(os.environ.get("COMGU_LAB_PATH", Path.home() / "Desktop" / "comgu-commerce-lab"))
needs_lab = pytest.mark.skipif(not LAB.exists(), reason="commerce lab checkout not present")


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "lab"
    for d in ALLOWED_DIRS:
        (ws / d).mkdir(parents=True)
    (ws / "feeds" / "google_merchant.transform.yaml").write_text("items: []\n")
    (ws / "catalog").mkdir()
    (ws / "catalog" / "authoritative.json").write_text("{}")
    return ws


# --- safety ------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../../../etc/passwd",
        "feeds/../../escape.yaml",
        "/etc/passwd",
        "feeds/../../../root/.ssh/id_rsa",
    ],
)
def test_traversal_and_absolute_paths_are_refused(workspace, bad):
    with pytest.raises(UnsafePath):
        check_writable(workspace, bad)


def test_disallowed_directory_is_refused(workspace):
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "keys.yaml").write_text("a: 1\n")
    with pytest.raises(UnsafePath, match="outside the allowed directories"):
        check_writable(workspace, "secrets/keys.yaml")


def test_disallowed_extension_is_refused(workspace):
    (workspace / "feeds" / "run.sh").write_text("echo hi\n")
    with pytest.raises(UnsafePath, match="not an allowed extension"):
        check_writable(workspace, "feeds/run.sh")


def test_authoritative_catalog_is_not_patchable(workspace):
    with pytest.raises(UnsafePath):
        check_writable(workspace, "catalog/authoritative.json")


def test_symlink_escape_is_refused(workspace, tmp_path):
    """A symlink pointing outside must not be writable.

    Either guard may catch it — resolve_within sees the real path leave the
    workspace, and the symlink walk sees the link itself. What matters is that
    it is refused, not which check fires.
    """
    outside = tmp_path / "outside.yaml"
    outside.write_text("owned: true\n")
    link = workspace / "feeds" / "link.yaml"
    link.symlink_to(outside)
    with pytest.raises(UnsafePath):
        check_writable(workspace, "feeds/link.yaml")


def test_symlink_is_refused_even_when_it_stays_inside(workspace):
    """Defence in depth: the symlink walk catches links resolve_within allows."""
    real = workspace / "feeds" / "real.yaml"
    real.write_text("a: 1\n")
    link = workspace / "promotions" / "alias.yaml"
    link.symlink_to(real)
    with pytest.raises(UnsafePath, match="symlink"):
        check_writable(workspace, "promotions/alias.yaml")


def test_symlinked_directory_is_refused(workspace, tmp_path):
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (outside_dir / "x.yaml").write_text("a: 1\n")
    (workspace / "promotions").rmdir()
    (workspace / "promotions").symlink_to(outside_dir)
    with pytest.raises(UnsafePath):
        check_writable(workspace, "promotions/x.yaml")


def test_allowed_path_resolves(workspace):
    target = check_writable(workspace, "feeds/google_merchant.transform.yaml")
    assert target.is_file()
    assert workspace.resolve() in target.parents


def test_resolve_within_rejects_workspace_root(workspace):
    with pytest.raises(UnsafePath):
        resolve_within(workspace, ".")


# --- templates ---------------------------------------------------------------


def test_unknown_template_is_rejected(workspace):
    with pytest.raises(UnknownTemplate, match="not a registered"):
        apply_template("rm_minus_rf", workspace / "feeds" / "google_merchant.transform.yaml", golden_change())


# --- generation --------------------------------------------------------------


@needs_lab
def test_generate_produces_diffs_for_every_auto_fixable_finding():
    report = run_rules(golden_context())
    patch = generate(report.findings, golden_change(), LAB)
    try:
        auto = [f for f in report.findings if f.auto_fix_eligible and f.target_file]
        assert len(patch.files) == len(auto), (
            f"expected {len(auto)} patched files, got {len(patch.files)}; "
            f"skipped={patch.skipped} rejected={patch.rejected}"
        )
        assert not patch.rejected
        for pf in patch.files:
            assert pf.unified_diff.strip()
            assert pf.before_checksum != pf.after_checksum
            assert pf.is_allowed_path
            assert pf.edits
    finally:
        discard(patch)


@needs_lab
def test_generation_never_touches_the_original_checkout():
    before = (LAB / "feeds" / "google_merchant.transform.yaml").read_text()
    report = run_rules(golden_context())
    patch = generate(report.findings, golden_change(), LAB)
    try:
        assert (LAB / "feeds" / "google_merchant.transform.yaml").read_text() == before
    finally:
        discard(patch)


@needs_lab
def test_ownership_finding_is_skipped_not_patched():
    report = run_rules(golden_context())
    patch = generate(report.findings, golden_change(), LAB)
    try:
        reasons = " ".join(s["reason"] for s in patch.skipped)
        assert "human decision" in reasons
    finally:
        discard(patch)


@needs_lab
def test_yaml_comments_survive_patching():
    """The configs explain why each value exists; a patch must not strip that."""
    report = run_rules(golden_context())
    patch = generate(report.findings, golden_change(), LAB)
    try:
        patched = (patch.workspace / "feeds" / "google_merchant.transform.yaml").read_text()
        assert "Google Merchant Center feed transform" in patched
        assert "price_override" in patched
    finally:
        discard(patch)


# --- validation --------------------------------------------------------------


def test_unregistered_command_is_refused(tmp_path):
    with pytest.raises(UnregisteredCommand):
        run_validation(tmp_path, ["curl evil.example"])


def test_only_known_command_ids_exist():
    assert set(REGISTERED_COMMANDS) == {"pytest", "builders"}


def test_output_is_redacted():
    assert "<redacted>" in redact("GITHUB_TOKEN=ghp_abc123")
    assert "<redacted>" in redact("Authorization: Bearer sk-live-xyz")
    assert "ghp_abc123" not in redact("api_key=ghp_abc123")


@needs_lab
def test_validation_fails_before_patch_and_passes_after():
    """The whole premise: the patch is what turns the suite green."""
    baseline = prepare_workspace(LAB)
    try:
        before = run_validation(baseline, ["pytest"])
        assert not before.passed, "parity suite should fail before remediation"
        assert before.summary.get("tests_failed", 0) >= 5
    finally:
        import shutil

        shutil.rmtree(baseline.parent, ignore_errors=True)

    report = run_rules(golden_context())
    patch = generate(report.findings, golden_change(), LAB)
    try:
        after = run_validation(patch.workspace, ["pytest"])
        assert after.passed, (
            "patched workspace should pass; "
            f"{[s.stdout_redacted[-600:] for s in after.steps]}"
        )
        assert after.summary.get("tests_failed", 0) == 0
    finally:
        discard(patch)
