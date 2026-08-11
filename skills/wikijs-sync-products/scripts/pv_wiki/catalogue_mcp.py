"""Manual-only MCP for one-shot product-catalogue refreshes.

This server is deliberately separate from the recurring research MCP.  Source
credentials exist only as one tool call's arguments and explicit psycopg keyword
arguments; this module never writes them to environment variables, files, PV
Wiki state, logs, or tool results.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from .db import CatalogueEndpoint, DatabaseConfigurationError, ProductReader
from .state import StateError


MCP_SERVER_VERSION = "0.1.0"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8002


class CatalogueRefreshError(RuntimeError):
    """A credential-free refresh failure safe to return through MCP."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sync_catalogue(product_reader: ProductReader) -> Mapping[str, Any]:
    # The legacy CLI remains import-heavy, so load it only for an actual manual
    # refresh rather than during MCP discovery.
    from .cli import sync_catalogue

    return sync_catalogue(
        resume_quota=False,
        product_reader=product_reader,
    )


def _public_refresh_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce internal sync output to a fixed, credential-free result shape."""

    if payload.get("ok") is not True:
        raise ValueError("catalogue sync did not report success")
    source_records = payload.get("source_records")
    snapshot = payload.get("snapshot")
    if (
        isinstance(source_records, bool)
        or not isinstance(source_records, int)
        or source_records < 1
        or not isinstance(snapshot, Mapping)
    ):
        raise ValueError("catalogue sync result is invalid")

    result: dict[str, Any] = {
        "ok": True,
        "source_records": source_records,
    }
    for field in ("generation", "added", "changed", "removed", "unchanged"):
        value = snapshot.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("catalogue sync result is invalid")
        result[field] = value

    checksum = snapshot.get("checksum")
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(character not in "0123456789abcdef" for character in checksum)
    ):
        raise ValueError("catalogue sync result is invalid")
    refreshed_at = snapshot.get("refreshed_at")
    if not isinstance(refreshed_at, str) or not refreshed_at or len(refreshed_at) > 64:
        raise ValueError("catalogue sync result is invalid")
    result["checksum"] = checksum
    result["refreshed_at"] = refreshed_at
    return result


def refresh_catalogue_once(
    *,
    username: str,
    password: str,
    endpoint: CatalogueEndpoint | None = None,
    endpoint_factory: Callable[[], CatalogueEndpoint] | None = None,
    syncer: Callable[[ProductReader], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use transient credentials once and return only non-secret sync metadata."""

    if endpoint is not None and endpoint_factory is not None:
        raise ValueError("provide endpoint or endpoint_factory, not both")
    active_syncer = syncer or _sync_catalogue
    payload: dict[str, Any] | None = None
    error_code: str | None = None
    try:
        active_endpoint = endpoint or (
            endpoint_factory or CatalogueEndpoint.from_environment
        )()
        reader = ProductReader(
            connect_factory=lambda: active_endpoint.connect(
                username=username,
                password=password,
            )
        )
        payload = _public_refresh_result(active_syncer(reader))
    except DatabaseConfigurationError:
        error_code = "configuration_invalid"
    except StateError:
        error_code = "snapshot_rejected"
    except (TypeError, ValueError):
        error_code = "request_invalid"
    except Exception:
        # Database exceptions can include endpoint or role details.  Do not pass
        # their text across the MCP boundary, even after best-effort redaction.
        error_code = "source_unavailable"
    finally:
        # CPython strings cannot be guaranteed to be zeroed, but dropping these
        # references keeps their in-process lifetime to this one call.
        username = ""
        password = ""
    # Raise only after leaving the source exception's handler. This prevents
    # Python from retaining a credential-bearing exception in ``__context__``.
    if error_code is not None:
        raise CatalogueRefreshError(error_code)
    if payload is None:  # pragma: no cover - exhaustive handlers above.
        raise CatalogueRefreshError("source_unavailable")
    payload["credentials_stored_by_pv_wiki"] = False
    payload["history_retention_possible"] = True
    payload["refresh_mode"] = "manual_one_shot"
    return payload


def build_server(
    *,
    endpoint_factory: Callable[[], CatalogueEndpoint] | None = None,
    syncer: Callable[[ProductReader], Mapping[str, Any]] | None = None,
) -> Any:
    """Build the isolated manual catalogue MCP with one mutating tool."""

    try:
        import mcp.types as types
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - packaging/operator error.
        raise RuntimeError(
            "The MCP runtime is not installed; install the project dependencies"
        ) from exc

    active_endpoint_factory = endpoint_factory or CatalogueEndpoint.from_environment
    server = MCPServer(
        "pv-wiki-catalogue-admin-mcp",
        title="PV Wiki Catalogue Admin",
        description=(
            "Manually replace the server-side product snapshot after an explicit "
            "user request; never expose this server to unattended cron sessions"
        ),
        version=MCP_SERVER_VERSION,
    )
    annotations = types.ToolAnnotations.model_validate(
        {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
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

    @server.tool(
        name="pv_refresh_catalogue",
        description=(
            "After the user explicitly requests a product-list update, connect "
            "once with the supplied temporary SELECT-only username/password and "
            "atomically replace the local snapshot. Never call from cron."
        ),
        annotations=annotations,
        structured_output=False,
    )
    def pv_refresh_catalogue(username: str, password: str) -> Any:
        try:
            payload = refresh_catalogue_once(
                username=username,
                password=password,
                endpoint_factory=active_endpoint_factory,
                syncer=syncer,
            )
            return result(payload)
        except CatalogueRefreshError as exc:
            return result(
                {
                    "ok": False,
                    "tool": "pv_refresh_catalogue",
                    "error_code": exc.code,
                    "credentials_stored_by_pv_wiki": False,
                    "history_retention_possible": True,
                },
                is_error=True,
            )

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
    parser = argparse.ArgumentParser(description="PV Wiki manual catalogue MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.getenv("PV_WIKI_CATALOGUE_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument(
        "--host",
        default=os.getenv("PV_WIKI_CATALOGUE_MCP_HOST", DEFAULT_MCP_HOST),
    )
    try:
        default_port = int(
            os.getenv("PV_WIKI_CATALOGUE_MCP_PORT", str(DEFAULT_MCP_PORT))
        )
    except ValueError as exc:
        raise SystemExit("PV_WIKI_CATALOGUE_MCP_PORT must be an integer") from exc
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if (
        args.transport == "streamable-http"
        and args.host not in {"127.0.0.1", "::1", "localhost"}
        and not _boolean_environment("PV_WIKI_CATALOGUE_MCP_ALLOW_REMOTE")
    ):
        parser.error(
            "non-loopback HTTP binding requires "
            "PV_WIKI_CATALOGUE_MCP_ALLOW_REMOTE=true"
        )

    server = build_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CatalogueRefreshError",
    "build_server",
    "main",
    "refresh_catalogue_once",
]
