"""Structured output contract for the AI remediation planner.

The model's job is narrow and its output is validated before anything acts on
it. It may summarise, explain business impact, rank options and draft prose. It
may not invent a price, an asset, a file path, or an action type: every action
must name a registered remediation template and a finding that Comgu produced
deterministically.

Anything failing this schema is rejected — Comgu falls back to the deterministic
templates rather than acting on a malformed plan.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 2

# Action types Comgu knows how to execute. Anything else is rejected.
ALLOWED_ACTIONS = {
    "update_config",
    "update_metadata",
    "notify_owner",
    "create_pr",
}


class ProposedAction(BaseModel):
    action_type: Literal["update_config", "update_metadata", "notify_owner", "create_pr"]
    sequence_number: int = Field(ge=1)
    finding_rule_code: str = Field(min_length=1)
    remediation_template: str = Field(min_length=1)
    target_system: str = Field(min_length=1)
    target_reference: str | None = None
    rationale: str = Field(min_length=1, max_length=1000)
    risk_level: Literal["low", "medium", "high", "critical"]
    requires_approval: bool = True

    @field_validator("remediation_template")
    @classmethod
    def known_template(cls, v: str) -> str:
        # Imported lazily so the schema module stays dependency-light.
        from packages.patch.templates import NON_FILE_TEMPLATES, TEMPLATES

        if v not in TEMPLATES and v not in NON_FILE_TEMPLATES:
            raise ValueError(
                f"{v!r} is not a registered remediation template; "
                "the planner may only select registered templates"
            )
        return v


class ValidationPlanStep(BaseModel):
    command_id: str = Field(min_length=1)
    expectation: str = Field(min_length=1)

    @field_validator("command_id")
    @classmethod
    def registered_command(cls, v: str) -> str:
        from packages.patch.validator import REGISTERED_COMMANDS

        if v not in REGISTERED_COMMANDS:
            raise ValueError(
                f"{v!r} is not a registered validation command; "
                "the planner may not invent commands to run"
            )
        return v


class RemediationPlan(BaseModel):
    schema_version: int = SCHEMA_VERSION
    summary: str = Field(min_length=1, max_length=2000)
    business_impact: str = Field(min_length=1, max_length=2000)
    proposed_actions: list[ProposedAction] = Field(min_length=1)
    required_approvals: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    validation_plan: list[ValidationPlanStep] = Field(min_length=1)
    rollback_plan: str = Field(min_length=1, max_length=2000)
    confidence_explanation: str = Field(min_length=1, max_length=1000)

    @field_validator("proposed_actions")
    @classmethod
    def sequential(cls, v: list[ProposedAction]) -> list[ProposedAction]:
        seen = sorted(a.sequence_number for a in v)
        if seen != list(range(1, len(v) + 1)):
            raise ValueError(f"action sequence_numbers must be 1..n, got {seen}")
        return v

    def grounded_in(self, finding_codes: set[str]) -> list[str]:
        """Actions referencing findings Comgu did not produce."""
        return [
            a.finding_rule_code
            for a in self.proposed_actions
            if a.finding_rule_code not in finding_codes
        ]


def json_schema() -> dict[str, Any]:
    """The schema handed to the model."""
    return RemediationPlan.model_json_schema()
