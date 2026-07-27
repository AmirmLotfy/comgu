"""Read data-quality signals (assertions) from DataHub.

This goes over GraphQL rather than MCP, deliberately. The MCP server does have a
`get_dataset_assertions` tool, but on DataHub Core v1.5.0.6 with
`DATA_QUALITY_TOOLS_ENABLED=true` the gate logs "Data Quality Tools ENABLED" and
the tool still never appears in `list_tools`. Rather than pretend the signal is
unavailable, Comgu reads it directly and labels the call honestly in the tool
trace as `graphql:` rather than as an MCP call.

Assertions are corroborating evidence, never the trigger: Comgu's own checks
decide whether something is wrong. A failing assertion tells the operator the
catalog already knew.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from packages.datahub.mcp_client import ToolCall, ToolTrace

ASSERTIONS_QUERY = """
query($urn: String!) {
  dataset(urn: $urn) {
    assertions(start: 0, count: 20) {
      total
      assertions {
        urn
        info { type description }
        runEvents(status: COMPLETE, limit: 3) {
          total
          failed
          succeeded
          runEvents {
            status
            timestampMillis
            result { type nativeResults { key value } }
          }
        }
      }
    }
  }
}
"""


def _graphql_endpoint(gms_url: str) -> str:
    return gms_url.rstrip("/") + "/api/graphql"


def fetch_assertions(
    gms_url: str,
    urn: str,
    token: str | None = None,
    trace: ToolTrace | None = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Return the assertions on a dataset, newest run result first.

    Never raises: a missing quality signal degrades the evidence, it does not
    invalidate a finding, so a failure here is recorded and swallowed.
    """
    started = time.monotonic()
    stamp = datetime.now(timezone.utc).isoformat()
    payload = json.dumps({"query": ASSERTIONS_QUERY, "variables": {"urn": urn}}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def record(ok: bool, summary: str, size: int = 0, error: str | None = None) -> None:
        if trace is not None:
            trace.record(
                ToolCall(
                    tool="graphql:dataset.assertions",
                    arguments={"urn": urn},
                    started_at=stamp,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    ok=ok,
                    result_bytes=size,
                    summary=summary,
                    error=error,
                )
            )

    try:
        req = urllib.request.Request(
            _graphql_endpoint(gms_url), data=payload, headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        record(False, "assertion lookup failed", error=f"{type(e).__name__}: {e}")
        return []

    block = ((data.get("data") or {}).get("dataset") or {}).get("assertions")
    if not block:
        record(True, "no assertions", size=len(raw))
        return []

    out: list[dict[str, Any]] = []
    for a in block.get("assertions") or []:
        events = a.get("runEvents") or {}
        latest = (events.get("runEvents") or [{}])[0]
        result = latest.get("result") or {}
        native = {
            kv["key"]: kv["value"] for kv in (result.get("nativeResults") or [])
        }
        out.append(
            {
                "urn": a.get("urn"),
                "type": (a.get("info") or {}).get("type"),
                "description": (a.get("info") or {}).get("description"),
                "result": result.get("type"),
                "failed_runs": events.get("failed", 0),
                "succeeded_runs": events.get("succeeded", 0),
                "expected": native.get("expected"),
                "observed": native.get("observed"),
                "detail": native.get("detail"),
            }
        )

    failing = [a for a in out if a.get("result") == "FAILURE"]
    record(
        True,
        f"{block.get('total', 0)} assertions, {len(failing)} failing",
        size=len(raw),
    )
    return out


def failing_only(assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in assertions if a.get("result") == "FAILURE"]
