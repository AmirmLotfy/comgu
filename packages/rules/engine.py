"""Runs the deterministic checks over a run's context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.rules.checks import ALL_RULES, Rule
from packages.rules.context import MissingContext, RunContext
from packages.rules.models import Finding, RuleResult, RuleStatus, Severity


@dataclass
class EngineReport:
    results: list[RuleResult] = field(default_factory=list)
    context_error: str | None = None

    @property
    def findings(self) -> list[Finding]:
        out: list[Finding] = []
        for r in self.results:
            out.extend(r.findings)
        return sorted(out, key=lambda f: -f.severity.rank)

    @property
    def max_severity(self) -> Severity:
        f = self.findings
        return max((x.severity for x in f), key=lambda s: s.rank) if f else Severity.INFORMATIONAL

    @property
    def counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in Severity}
        for f in self.findings:
            c[f.severity.value] += 1
        return c

    @property
    def executed(self) -> int:
        return sum(1 for r in self.results if r.status != RuleStatus.SKIPPED)

    def to_json(self) -> dict[str, Any]:
        return {
            "context_error": self.context_error,
            "rules_executed": self.executed,
            "finding_count": len(self.findings),
            "max_severity": self.max_severity.value,
            "counts_by_severity": self.counts,
            "results": [r.to_json() for r in self.results],
        }


def run_rules(ctx: RunContext, rules: list[Rule] | None = None) -> EngineReport:
    """Execute every rule.

    If DataHub did not tell us which asset is authoritative we stop rather than
    guess — a wrong answer here would be worse than no answer.
    """
    rules = rules if rules is not None else ALL_RULES

    try:
        ctx.require_authority()
    except MissingContext as e:
        return EngineReport(results=[], context_error=str(e))

    return EngineReport(results=[rule.evaluate(ctx) for rule in rules])
