"""Comgu's DataHub gateway, over the official MCP server.

Every call is recorded into a tool trace. The trace is persisted with the run's
context snapshot and rendered in the UI: it is the evidence that Comgu's
conclusions came from DataHub rather than from hardcoded topology.

Tool contracts were read off the running server rather than assumed. Two are
easy to get wrong:

  * `get_lineage` takes `upstream: bool` — there is no `direction` argument.
  * `get_lineage` defaults to `max_hops=1`, which silently returns only
    immediate neighbours. Blast radius must ask for more.

Mutation tools only exist when the server runs with TOOLS_IS_MUTATION_ENABLED=true.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

READ_TOOLS = {
    "search",
    "get_entities",
    "get_lineage",
    "get_lineage_paths_between",
    "list_schema_fields",
    "get_dataset_queries",
}

MUTATION_TOOLS = {
    "update_description",
    "add_tags",
    "remove_tags",
    "add_terms",
    "remove_terms",
    "add_owners",
    "remove_owners",
    "set_domains",
    "remove_domains",
    "add_structured_properties",
    "remove_structured_properties",
    "save_document",
}


class DataHubUnavailable(RuntimeError):
    """DataHub could not be reached or returned an unusable response.

    Comgu never substitutes hardcoded lineage when this is raised; the run is
    marked context-failed instead.
    """


@dataclass
class ToolCall:
    """One MCP invocation, recorded for the run's evidence trail."""

    tool: str
    arguments: dict[str, Any]
    started_at: str
    duration_ms: int
    ok: bool
    result_bytes: int
    summary: str
    error: str | None = None


@dataclass
class ToolTrace:
    calls: list[ToolCall] = field(default_factory=list)

    def record(self, call: ToolCall) -> None:
        self.calls.append(call)

    @property
    def total_ms(self) -> int:
        return sum(c.duration_ms for c in self.calls)

    def to_json(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.calls]

    def __len__(self) -> int:
        return len(self.calls)


def _text(result) -> str:
    return "\n".join(getattr(c, "text", None) or str(c) for c in result.content)


def _summarise(payload: Any, limit: int = 160) -> str:
    """A short, human-readable description of what a tool returned."""
    if isinstance(payload, dict):
        for key in ("total", "downstreams", "upstreams", "entities", "searchResults"):
            if key in payload:
                v = payload[key]
                if isinstance(v, dict) and "total" in v:
                    return f"{key}.total={v['total']}"
                if isinstance(v, list):
                    return f"{key}={len(v)} items"
                return f"{key}={v}"
        return ", ".join(list(payload)[:5])
    if isinstance(payload, list):
        return f"{len(payload)} items"
    return str(payload)[:limit]


class DataHubMCP:
    """A live MCP session against DataHub.

    Use via `datahub_session(...)`. The session is held open for the duration of
    a run so we pay the server start-up cost once rather than per tool call.
    """

    def __init__(self, session: ClientSession, trace: ToolTrace, available: set[str]):
        self._session = session
        self.trace = trace
        self.available = available

    @property
    def mutations_enabled(self) -> bool:
        return bool(self.available & MUTATION_TOOLS)

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Invoke a tool, record it, and return the parsed payload."""
        if tool not in self.available:
            raise DataHubUnavailable(
                f"tool {tool!r} is not exposed by this DataHub MCP server "
                f"(available: {sorted(self.available)})"
            )

        started = time.monotonic()
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            result = await self._session.call_tool(tool, arguments)
        except Exception as e:
            self.trace.record(
                ToolCall(
                    tool=tool,
                    arguments=arguments,
                    started_at=stamp,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    ok=False,
                    result_bytes=0,
                    summary="transport error",
                    error=f"{type(e).__name__}: {e}",
                )
            )
            raise DataHubUnavailable(f"{tool} failed: {type(e).__name__}: {e}") from e

        duration = int((time.monotonic() - started) * 1000)
        body = _text(result)

        # The MCP SDK reports tool errors as isError + content, not exceptions.
        if getattr(result, "isError", False):
            self.trace.record(
                ToolCall(
                    tool=tool,
                    arguments=arguments,
                    started_at=stamp,
                    duration_ms=duration,
                    ok=False,
                    result_bytes=len(body),
                    summary="tool error",
                    error=body[:500],
                )
            )
            raise DataHubUnavailable(f"{tool} returned an error: {body[:300]}")

        try:
            payload: Any = json.loads(body)
        except json.JSONDecodeError:
            payload = body

        self.trace.record(
            ToolCall(
                tool=tool,
                arguments=arguments,
                started_at=stamp,
                duration_ms=duration,
                ok=True,
                result_bytes=len(body),
                summary=_summarise(payload),
            )
        )
        return payload

    # --- reads ---------------------------------------------------------------

    async def search(self, query: str, num_results: int = 20, filter: str | None = None) -> Any:
        args: dict[str, Any] = {"query": query, "num_results": num_results}
        if filter:
            args["filter"] = filter
        return await self.call("search", args)

    async def get_entities(self, urns: list[str]) -> Any:
        return await self.call("get_entities", {"urns": urns})

    async def get_lineage(
        self,
        urn: str,
        *,
        upstream: bool = False,
        max_hops: int = 3,
        max_results: int = 100,
    ) -> Any:
        """Traverse lineage.

        `max_hops` defaults to 3 here, not the server's 1: blast radius needs
        the whole fan-out, not just direct neighbours.
        """
        return await self.call(
            "get_lineage",
            {
                "urn": urn,
                "upstream": upstream,
                "max_hops": max_hops,
                "max_results": max_results,
            },
        )

    async def lineage_paths_between(self, source_urn: str, target_urn: str) -> Any:
        return await self.call(
            "get_lineage_paths_between",
            {"source_urn": source_urn, "target_urn": target_urn},
        )

    async def list_schema_fields(self, urn: str, limit: int = 100) -> Any:
        return await self.call("list_schema_fields", {"urn": urn, "limit": limit})

    # --- writes --------------------------------------------------------------

    async def update_description(self, urn: str, description: str) -> Any:
        return await self.call("update_description", {"urn": urn, "description": description})

    async def add_tags(self, urns: list[str], tags: list[str]) -> Any:
        return await self.call("add_tags", {"urns": urns, "tags": tags})

    async def add_owners(self, urns: list[str], owners: list[str]) -> Any:
        return await self.call("add_owners", {"urns": urns, "owners": owners})

    async def set_domains(self, urns: list[str], domains: list[str]) -> Any:
        return await self.call("set_domains", {"urns": urns, "domains": domains})

    async def add_structured_properties(self, urns: list[str], properties: dict[str, Any]) -> Any:
        return await self.call(
            "add_structured_properties", {"urns": urns, "properties": properties}
        )

    async def save_document(self, **kwargs: Any) -> Any:
        return await self.call("save_document", kwargs)


@asynccontextmanager
async def datahub_session(
    gms_url: str | None = None,
    token: str | None = None,
    *,
    enable_mutations: bool = False,
):
    """Open an MCP session against DataHub.

    Mutations stay off unless explicitly requested, so a read-only context
    retrieval cannot accidentally write to the catalog.
    """
    env = dict(os.environ)
    env["DATAHUB_GMS_URL"] = gms_url or env.get("DATAHUB_GMS_URL", "http://localhost:8080")
    env["DATAHUB_GMS_TOKEN"] = token or env.get("DATAHUB_GMS_TOKEN", "")
    env["TOOLS_IS_MUTATION_ENABLED"] = "true" if enable_mutations else "false"
    if enable_mutations:
        env["SAVE_DOCUMENT_TOOL_ENABLED"] = "true"

    params = StdioServerParameters(
        command="uvx", args=["mcp-server-datahub@latest"], env=env
    )

    trace = ToolTrace()
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                available = {t.name for t in tools.tools}
                yield DataHubMCP(session, trace, available)
    except DataHubUnavailable:
        raise
    except Exception as e:
        raise DataHubUnavailable(
            f"could not start the DataHub MCP session: {type(e).__name__}: {e}"
        ) from e
