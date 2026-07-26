"""Core types for Comgu's deterministic rule engine.

Findings are facts, not opinions: every one carries the expected value, the
observed value, the DataHub assets involved, and the evidence that produced it.
The AI planner may rank and explain findings but may never create or alter one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return list(Severity).index(self)


class EvidenceType(str, Enum):
    VALUE_COMPARISON = "value_comparison"
    LINEAGE = "lineage"
    SCHEMA = "schema"
    OWNER = "owner"
    ASSERTION = "assertion"
    QUERY = "query"
    DOCUMENT = "document"
    LOG = "log"


@dataclass(frozen=True)
class Evidence:
    """A single supporting fact. Content must be reproducible, not narrative."""

    evidence_type: EvidenceType
    source: str
    content: dict[str, Any]
    source_reference: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type.value,
            "source": self.source,
            "source_reference": self.source_reference,
            "content": self.content,
        }


@dataclass
class Finding:
    rule_code: str
    rule_version: int
    severity: Severity
    title: str
    summary: str
    expected_value: Any
    observed_value: Any
    source_asset_urn: str | None
    downstream_asset_urn: str | None
    customer_impact: str
    business_risk: str
    confidence: float
    auto_fix_eligible: bool
    owner_reference: dict[str, Any] | None = None
    remediation_template: str | None = None
    target_file: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_json(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "rule_version": self.rule_version,
            "severity": self.severity.value,
            "title": self.title,
            "summary": self.summary,
            "expected_value": self.expected_value,
            "observed_value": self.observed_value,
            "source_asset_urn": self.source_asset_urn,
            "downstream_asset_urn": self.downstream_asset_urn,
            "owner_reference": self.owner_reference,
            "customer_impact": self.customer_impact,
            "business_risk": self.business_risk,
            "confidence": self.confidence,
            "auto_fix_eligible": self.auto_fix_eligible,
            "remediation_template": self.remediation_template,
            "target_file": self.target_file,
            "detected_at": self.detected_at,
            "evidence": [e.to_json() for e in self.evidence],
        }


class RuleStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class RuleResult:
    rule_code: str
    rule_version: int
    status: RuleStatus
    findings: list[Finding] = field(default_factory=list)
    duration_ms: int = 0
    skip_reason: str | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "rule_version": self.rule_version,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "skip_reason": self.skip_reason,
            "error": self.error,
            "findings": [f.to_json() for f in self.findings],
        }
