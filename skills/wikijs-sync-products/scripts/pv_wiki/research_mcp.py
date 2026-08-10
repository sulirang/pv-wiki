"""Small MCP boundary between Hermes and durable PV Wiki state.

Hermes owns autonomous research and talks to Exa through a different MCP
server.  This server intentionally exposes only catalogue selection,
completion persistence, and idempotent Wiki.js publication.  It contains no
research loop, lease, retry ledger, query budget, credit budget, or deadline.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from .config import redact_environment_secrets
from .hermes_store import (
    HermesCompletionStore,
    ResearchCompletion,
    publish_researched,
)


MCP_SERVER_VERSION = "0.1.0"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8001
_SECRET_ENVIRONMENT = (
    "WIKIJS_TOKEN",
    "PV_WIKI_STATE_DATABASE_URL",
    "PGPASSWORD",
)


def _time_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _completion_payload(completion: ResearchCompletion) -> dict[str, Any]:
    return {
        "product_id": completion.product_id,
        "source_hash": completion.source_hash,
        "product": completion.product,
        "decision": completion.decision,
        "evidence": completion.evidence,
        "outcome": completion.outcome,
        "publishable": completion.publishable,
        "completed_at": _time_text(completion.completed_at),
        "published_at": _time_text(completion.published_at),
        "wiki_path": completion.wiki_path,
        "wiki_action": completion.wiki_action,
    }


def _safe_error(exc: BaseException) -> str:
    message = " ".join(str(exc).split()) or type(exc).__name__
    return redact_environment_secrets(
        message,
        _SECRET_ENVIRONMENT,
        limit=1000,
    )


def _refresh_catalogue() -> Mapping[str, Any]:
    # Imported lazily: the legacy CLI is large and must not be initialized
    # merely to list MCP tools or retry a pending publication.
    from .cli import sync_catalogue

    return sync_catalogue(resume_quota=False)


def _next_product(
    *,
    store_factory: Callable[[], HermesCompletionStore],
    refresh: Callable[[], Mapping[str, Any]],
    refresh_catalogue: bool,
    after_product_id: str | None = None,
) -> dict[str, Any]:
    refreshed: Mapping[str, Any] | None = None
    if refresh_catalogue:
        refreshed = refresh()
    with store_factory() as store:
        product = store.next_product(after_product_id=after_product_id)
    if product is None:
        return {
            "ok": True,
            "found": False,
            "reason": "no_unresearched_product",
            "catalogue": dict(refreshed) if refreshed is not None else None,
        }
    return {
        "ok": True,
        "found": True,
        "product_id": product.product_id,
        "source_hash": product.source_hash,
        "product": product.payload,
        "catalogue": dict(refreshed) if refreshed is not None else None,
    }


def build_server(
    *,
    store_factory: Callable[[], HermesCompletionStore] | None = None,
    catalogue_refresh: Callable[[], Mapping[str, Any]] | None = None,
    publisher: Callable[..., dict[str, Any]] | None = None,
) -> Any:
    """Build an official MCP v2 server with injectable boundaries for tests."""

    try:
        import mcp.types as types
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - packaging/operator error.
        raise RuntimeError(
            "The MCP runtime is not installed; install the project dependencies"
        ) from exc

    active_store_factory = store_factory or HermesCompletionStore
    active_refresh = catalogue_refresh or _refresh_catalogue
    active_publisher = publisher or publish_researched
    selection_lock = threading.Lock()
    selection_cursor: str | None = None
    server = MCPServer(
        "pv-wiki-research-mcp",
        title="PV Wiki Research State",
        description=(
            "Select unresearched products, save completed evidence decisions, "
            "and idempotently publish already-completed results"
        ),
        version=MCP_SERVER_VERSION,
    )

    def annotations(*, read_only: bool, open_world: bool = False) -> Any:
        return types.ToolAnnotations.model_validate(
            {
                "readOnlyHint": read_only,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": open_world,
            }
        )

    def result(payload: Mapping[str, Any], *, is_error: bool = False) -> Any:
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        dict(payload),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            ],
            is_error=is_error,
        )

    def failed(tool_name: str, exc: BaseException) -> Any:
        return result(
            {"ok": False, "tool": tool_name, "error": _safe_error(exc)},
            is_error=True,
        )

    @server.tool(
        name="pv_pending_publication",
        description=(
            "Return the oldest completed publishable result not yet applied to "
            "Wiki.js. Always check this before selecting new research."
        ),
        annotations=annotations(read_only=True),
        structured_output=False,
    )
    def pv_pending_publication() -> Any:
        try:
            with active_store_factory() as store:
                completion = store.pending_publication()
            if completion is None:
                return result({"ok": True, "found": False})
            return result(
                {
                    "ok": True,
                    "found": True,
                    "completion": _completion_payload(completion),
                }
            )
        except Exception as exc:
            return failed("pv_pending_publication", exc)

    @server.tool(
        name="pv_next_product",
        description=(
            "Refresh the catalogue snapshot and fairly return a product with no "
            "durable completion. This is an anti-join with a process-local "
            "wraparound cursor, not a lease or retry schedule."
        ),
        annotations=annotations(read_only=False),
        structured_output=False,
    )
    def pv_next_product(refresh_catalogue: bool = True) -> Any:
        nonlocal selection_cursor
        try:
            if not isinstance(refresh_catalogue, bool):
                raise ValueError("refresh_catalogue must be boolean")
            with selection_lock:
                payload = _next_product(
                    store_factory=active_store_factory,
                    refresh=active_refresh,
                    refresh_catalogue=refresh_catalogue,
                    after_product_id=selection_cursor,
                )
                selection_cursor = (
                    str(payload["product_id"])
                    if payload.get("found") is True
                    else None
                )
            return result(payload)
        except Exception as exc:
            return failed("pv_next_product", exc)

    @server.tool(
        name="pv_save_research",
        description=(
            "Validate and durably complete one product. Replays never overwrite "
            "the first completion; publish and non-publish outcomes both complete "
            "it. Every evidence document requires the unchanged URL, exact content, "
            "and receipt returned together by web_fetch_exa."
        ),
        annotations=annotations(read_only=False),
        structured_output=False,
    )
    def pv_save_research(
        product_id: str,
        source_hash: str,
        decision: dict[str, Any],
        evidence_documents: list[dict[str, Any]],
    ) -> Any:
        try:
            with active_store_factory() as store:
                saved = store.save_completion(
                    product_id=product_id,
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=evidence_documents,
                )
            return result(
                {
                    "ok": True,
                    "created": saved.created,
                    "completion": _completion_payload(saved.completion),
                }
            )
        except Exception as exc:
            return failed("pv_save_research", exc)

    @server.tool(
        name="pv_publish_result",
        description=(
            "Idempotently publish one completed result to Wiki.js. A Wiki failure "
            "leaves it pending and never causes product research to run again."
        ),
        annotations=annotations(read_only=False, open_world=True),
        structured_output=False,
    )
    def pv_publish_result(product_id: str | None = None) -> Any:
        try:
            with active_store_factory() as store:
                payload = active_publisher(store, product_id)
            return result(payload)
        except Exception as exc:
            return failed("pv_publish_result", exc)

    @server.tool(
        name="pv_research_status",
        description="Return durable completion and publication counts.",
        annotations=annotations(read_only=True),
        structured_output=False,
    )
    def pv_research_status() -> Any:
        try:
            with active_store_factory() as store:
                counts = store.counts()
                next_product = store.next_product()
            return result(
                {
                    "ok": True,
                    "counts": counts,
                    "unresearched_available": next_product is not None,
                }
            )
        except Exception as exc:
            return failed("pv_research_status", exc)

    return server


def _boolean_environment(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be true or false")


def main(argv: list[str] | None = None) -> int:
    """Run stdio by default, with explicitly opt-in streamable HTTP."""

    parser = argparse.ArgumentParser(description="PV Wiki research-state MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.getenv("PV_WIKI_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument(
        "--host",
        default=os.getenv("PV_WIKI_MCP_HOST", DEFAULT_MCP_HOST),
    )
    try:
        default_port = int(os.getenv("PV_WIKI_MCP_PORT", str(DEFAULT_MCP_PORT)))
    except ValueError as exc:
        raise SystemExit("PV_WIKI_MCP_PORT must be an integer") from exc
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if (
        args.transport == "streamable-http"
        and args.host not in {"127.0.0.1", "::1", "localhost"}
        and not _boolean_environment("PV_WIKI_MCP_ALLOW_REMOTE")
    ):
        parser.error(
            "non-loopback HTTP binding requires PV_WIKI_MCP_ALLOW_REMOTE=true"
        )

    server = build_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_server", "main"]
