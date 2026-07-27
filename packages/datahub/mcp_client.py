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

    # Argument names below match the server's published schemas exactly; they
    # are not interchangeable with the more obvious names.

    async def update_description(
        self, entity_urn: str, description: str, operation: str = "replace"
    ) -> Any:
        return await self.call(
            "update_description",
            {"entity_urn": entity_urn, "description": description, "operation": operation},
        )

    async def add_tags(self, entity_urns: list[str], tag_urns: list[str]) -> Any:
        return await self.call(
            "add_tags", {"entity_urns": entity_urns, "tag_urns": tag_urns}
        )

    async def add_owners(
        self,
        entity_urns: list[str],
        owner_urns: list[str],
        ownership_type: str = "__system__technical_owner",
    ) -> Any:
        return await self.call(
            "add_owners",
            {
                "entity_urns": entity_urns,
                "owner_urns": owner_urns,
                "ownership_type": ownership_type,
            },
        )

    async def set_domains(self, entity_urns: list[str], domain_urn: str) -> Any:
        return await self.call(
            "set_domains", {"entity_urns": entity_urns, "domain_urn": domain_urn}
        )

    async def add_structured_properties(
        self, entity_urns: list[str], property_values: dict[str, list[Any]]
    ) -> Any:
        """property_values maps a structured property URN to a list of values."""
        return await self.call(
            "add_structured_properties",
            {"entity_urns": entity_urns, "property_values": property_values},
        )

    async def save_document(
        self,
        title: str,
        content: str,
        document_type: str = "Decision",
        urn: str | None = None,
        topics: list[str] | None = None,
        related_assets: list[str] | None = None,
    ) -> Any:
        """document_type is an enum: Insight, Decision, FAQ, Analysis, Summary,
        Recommendation, Note, Context. Assets are linked via `related_assets`
        (`related_documents` links other documents, not entities)."""
        args: dict[str, Any] = {
            "title": title,
            "content": content,
            "document_type": document_type,
        }
        if urn:
            args["urn"] = urn
        if topics:
            args["topics"] = topics
        if related_assets:
            args["related_assets"] = related_assets
        return await self.call("save_document", args)


@asynccontextmanager
async def datahub_session(
    gms_url: str | None = None,
    token: str | None = None,
    *,
    enable_mutations: bool = False,
    server_log: str | None = None,
):
    """Open an MCP session against DataHub.

    Mutations stay off unless explicitly requested, so a read-only context
    retrieval cannot accidentally write to the catalog.

    The MCP server logs full GraphQL queries at DEBUG to stderr, which buries
    anything else on the terminal. Its stderr goes to `server_log` if given and
    is discarded otherwise.
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
    errlog = open(server_log, "a") if server_log else open(os.devnull, "w")
    try:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                available = {t.name for t in tools.tools}
                yield DataHubMCP(session, trace, available)
    except DataHubUnavailable:
        raise
    except Exception as e:
        raise DataHubUnavailable(
            f"could not start the DataHub MCP session against {env['DATAHUB_GMS_URL']}: "
            f"{_root_cause(e)}"
        ) from e
    finally:
        errlog.close()


def _root_cause(exc: BaseException, depth: int = 0) -> str:
    """Unwrap to something an operator can act on.

    The MCP client runs the server in a TaskGroup, so a failure to connect
    surfaces as `ExceptionGroup: unhandled errors in a TaskGroup (1
    sub-exception)` — which says nothing about DataHub being unreachable.
    """
    if depth > 4:
        return f"{type(exc).__name__}: {exc}"

    inner = getattr(exc, "exceptions", None)
    if inner:
        return "; ".join(_root_cause(e, depth + 1) for e in inner[:3])
    if exc.__cause__ is not None:
        return _root_cause(exc.__cause__, depth + 1)

    detail = str(exc).strip() or type(exc).__name__
    if isinstance(exc, (ConnectionError, OSError)) or "Connection" in type(exc).__name__:
        detail += " — is DataHub reachable, and is the tunnel up?"
    return f"{type(exc).__name__}: {detail}"
