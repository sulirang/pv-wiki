"""Official-compatible Exa MCP tools backed by the local rotating key pool.

The MCP dependency is imported lazily so importing the dependency-free Exa
transport never starts or configures an MCP server.  Install the project before
running the ``pv-wiki-exa-mcp`` console script.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections.abc import Mapping
from typing import Any

from .evidence_receipt import (
    EvidenceReceiptError,
    content_sha256,
    issue_evidence_receipt,
    normalize_evidence_url,
    resolve_evidence_hmac_key,
)
from .exa_pool import ExaPoolClient, ExaPoolError, ExaPoolUnavailableError


MCP_SERVER_VERSION = "0.1.0"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000
_BASIC_TIMEOUT = 60.0
_ADVANCED_TIMEOUT = 300.0
_CATEGORY_PATTERN = re.compile(
    r"\bcategory:(company|publication|news|personal\s*site|people)\b",
    re.IGNORECASE,
)
_ADVANCED_CATEGORIES = frozenset(
    {
        "company",
        "publication",
        "news",
        "pdf",
        "github",
        "personal site",
        "people",
        "financial report",
    }
)
_SEARCH_TYPES = frozenset({"auto", "fast", "instant"})
_SENSITIVE_RESPONSE_KEYS = frozenset({"requestTags"})


def _is_record(value: Any) -> bool:
    return isinstance(value, Mapping)


def _strip_sensitive_keys(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_sensitive_keys(item) for item in value]
    if not _is_record(value):
        return value
    return {
        str(key): _strip_sensitive_keys(nested)
        for key, nested in value.items()
        if key not in _SENSITIVE_RESPONSE_KEYS
    }


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = [item for item in value if isinstance(item, str)]
    return result or None


def _number_list(value: Any) -> list[int | float] | None:
    if not isinstance(value, list):
        return None
    result = [
        item
        for item in value
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    ]
    return result or None


def _sanitize_search_result(value: Any) -> dict[str, Any] | None:
    if not _is_record(value):
        return None
    result: dict[str, Any] = {}
    for field in (
        "id",
        "url",
        "publishedDate",
        "author",
        "text",
        "summary",
        "image",
        "favicon",
    ):
        if isinstance(value.get(field), str):
            result[field] = value[field]
    if isinstance(value.get("title"), str) or value.get("title") is None:
        result["title"] = value.get("title")
    score = value.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        result["score"] = score
    highlights = _string_list(value.get("highlights"))
    if highlights:
        result["highlights"] = highlights
    highlight_scores = _number_list(value.get("highlightScores"))
    if highlight_scores:
        result["highlightScores"] = highlight_scores
    entities = value.get("entities")
    if isinstance(entities, list):
        clean_entities = [
            _strip_sensitive_keys(item) for item in entities if _is_record(item)
        ]
        if clean_entities:
            result["entities"] = clean_entities
    extras = value.get("extras")
    if _is_record(extras):
        clean_extras: dict[str, Any] = {}
        links = _string_list(extras.get("links"))
        images = _string_list(extras.get("imageLinks"))
        if links:
            clean_extras["links"] = links
        if images:
            clean_extras["imageLinks"] = images
        if clean_extras:
            result["extras"] = clean_extras
    subpages = value.get("subpages")
    if isinstance(subpages, list):
        clean_subpages = [
            clean
            for clean in (_sanitize_search_result(item) for item in subpages)
            if clean is not None
        ]
        if clean_subpages:
            result["subpages"] = clean_subpages
    return result


def _sanitize_search_output(value: Any) -> dict[str, Any] | None:
    if not _is_record(value):
        return None
    result: dict[str, Any] = {}
    if "content" in value:
        result["content"] = _strip_sensitive_keys(value["content"])
    grounding = value.get("grounding")
    if isinstance(grounding, list):
        clean_grounding: list[dict[str, Any]] = []
        for entry in grounding:
            if not _is_record(entry):
                continue
            citations = []
            for citation in entry.get("citations", []):
                if (
                    _is_record(citation)
                    and isinstance(citation.get("url"), str)
                    and isinstance(citation.get("title"), str)
                ):
                    citations.append(
                        {"url": citation["url"], "title": citation["title"]}
                    )
            clean_entry: dict[str, Any] = {"citations": citations}
            if isinstance(entry.get("field"), str):
                clean_entry["field"] = entry["field"]
            if isinstance(entry.get("confidence"), str):
                clean_entry["confidence"] = entry["confidence"]
            clean_grounding.append(clean_entry)
        if clean_grounding:
            result["grounding"] = clean_grounding
    return result or None


def _sanitize_response(value: Any) -> dict[str, Any]:
    """Port the allow-list used by Exa's official MCP server."""

    if not _is_record(value):
        return {}
    result: dict[str, Any] = {}
    for field in (
        "requestId",
        "autopromptString",
        "autoDate",
        "resolvedSearchType",
        "context",
    ):
        if isinstance(value.get(field), str):
            result[field] = value[field]
    output = _sanitize_search_output(value.get("output"))
    if output:
        result["output"] = output
    statuses = value.get("statuses")
    if isinstance(statuses, list):
        clean_statuses = [
            {
                "id": item["id"],
                "status": item["status"],
                "source": item["source"],
            }
            for item in statuses
            if (
                _is_record(item)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("status"), str)
                and isinstance(item.get("source"), str)
            )
        ]
        if clean_statuses:
            result["statuses"] = clean_statuses
    raw_results = value.get("results")
    if isinstance(raw_results, list):
        clean_results = [
            clean
            for clean in (_sanitize_search_result(item) for item in raw_results)
            if clean is not None
        ]
        if clean_results:
            result["results"] = clean_results
    search_time = value.get("searchTime")
    if (
        isinstance(search_time, (int, float))
        and not isinstance(search_time, bool)
    ):
        result["searchTime"] = search_time
    cost = _strip_sensitive_keys(value.get("costDollars"))
    if _is_record(cost):
        result["costDollars"] = cost
    return result


def _required_string(value: Any, name: str) -> str:
    if isinstance(value, (int, float, bool)):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _optional_positive_number(value: Any) -> int | float | None:
    number = _optional_number(value)
    return number if number is not None and number >= 1 else None


def _optional_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _normalise_urls(value: list[str] | str) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = [value]
        value = decoded if isinstance(decoded, list) else [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("urls must be an array of strings")
    return value


def _error_message(error: ExaPoolError) -> str:
    message = str(error)
    if (
        isinstance(error, ExaPoolUnavailableError)
        and error.retry_after is not None
    ):
        message += f"; retry after approximately {math.ceil(error.retry_after)}s"
    return message


def build_server(
    client: ExaPoolClient | None = None,
    *,
    evidence_hmac_key: bytes | bytearray | memoryview | None = None,
) -> Any:
    """Build the MCP v2 server, permitting an injected client for tests."""

    try:
        import mcp.types as types
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - packaging/operator error.
        raise RuntimeError(
            "The MCP runtime is not installed; install the project dependencies"
        ) from exc

    active_client = client or ExaPoolClient(environ=os.environ)
    active_receipt_key = resolve_evidence_hmac_key(
        evidence_hmac_key,
        environ=os.environ,
    )
    server = MCPServer(
        "exa-pool-mcp",
        title="Exa Pool",
        description="Exa web search and fetch with health-aware key rotation",
        version=MCP_SERVER_VERSION,
    )
    annotations = types.ToolAnnotations.model_validate(
        {
            "readOnlyHint": True,
            "destructiveHint": False,
            # A repeated search/fetch can consume credits even though it is
            # read-only, so clients must not infer that blind replay is free.
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )

    def text_result(text: str, *, is_error: bool = False) -> Any:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            is_error=is_error,
        )

    @server.tool(
        name="web_search_exa",
        description=(
            "Search the web for any topic and return clean highlighted results. "
            "Use web_fetch_exa when a result needs full-page content."
        ),
        annotations=annotations,
        structured_output=False,
    )
    def web_search_exa(
        query: str,
        numResults: int | float | str | None = None,
    ) -> Any:
        try:
            clean_query = _required_string(query, "query")
            category_match = _CATEGORY_PATTERN.search(clean_query)
            category = None
            if category_match:
                category = re.sub(
                    r"\s+",
                    " ",
                    category_match.group(1).casefold(),
                )
                clean_query = _CATEGORY_PATTERN.sub("", clean_query)
                clean_query = re.sub(r"\s+", " ", clean_query).strip()
            search_type = os.getenv("DEFAULT_SEARCH_TYPE", "auto").strip()
            if search_type not in _SEARCH_TYPES:
                search_type = "auto"
            payload: dict[str, Any] = {
                "query": clean_query,
                "type": search_type,
                "numResults": _optional_number(numResults) or 10,
                "contents": {"highlights": True},
            }
            if category:
                payload["category"] = category
            response = active_client.post(
                "/search",
                payload,
                timeout=_BASIC_TIMEOUT,
            )
            sanitized = _sanitize_response(response)
            results = sanitized.get("results")
            if not isinstance(results, list) or not results:
                return text_result(
                    "No search results found. Please try a different query."
                )
            formatted: list[str] = []
            for result in results:
                lines = [
                    f"Title: {result.get('title') or 'N/A'}",
                    f"URL: {result.get('url') or ''}",
                    f"Published: {result.get('publishedDate') or 'N/A'}",
                    f"Author: {result.get('author') or 'N/A'}",
                ]
                highlights = result.get("highlights")
                if isinstance(highlights, list) and highlights:
                    lines.append("Highlights:\n" + "\n".join(highlights))
                elif result.get("text"):
                    lines.append(f"Text: {result['text']}")
                formatted.append("\n".join(lines))
            return text_result("\n\n---\n\n".join(formatted))
        except (ExaPoolError, ValueError, TypeError) as exc:
            message = _error_message(exc) if isinstance(exc, ExaPoolError) else str(exc)
            return text_result(f"web_search_exa error: {message}", is_error=True)

    @server.tool(
        name="web_search_advanced_exa",
        description=(
            "Advanced web search with domain, date, category, text, summary, "
            "highlight, freshness, and subpage controls."
        ),
        annotations=annotations,
        structured_output=False,
    )
    def web_search_advanced_exa(
        query: str,
        numResults: int | float | str | None = None,
        type: str | None = None,
        category: str | None = None,
        includeDomains: list[str] | None = None,
        excludeDomains: list[str] | None = None,
        startPublishedDate: str | None = None,
        endPublishedDate: str | None = None,
        startCrawlDate: str | None = None,
        endCrawlDate: str | None = None,
        includeText: list[str] | None = None,
        excludeText: list[str] | None = None,
        userLocation: str | None = None,
        moderation: bool | str | None = None,
        additionalQueries: list[str] | None = None,
        textMaxCharacters: int | float | str | None = None,
        contextMaxCharacters: int | float | str | None = None,
        enableSummary: bool | str | None = None,
        summaryQuery: str | None = None,
        enableHighlights: bool | str | None = None,
        highlightsMaxCharacters: int | float | str | None = None,
        highlightsNumSentences: int | float | str | None = None,
        highlightsPerUrl: int | float | str | None = None,
        highlightsQuery: str | None = None,
        maxAgeHours: int | float | str | None = None,
        livecrawlTimeout: int | float | str | None = None,
        subpages: int | float | str | None = None,
        subpageTarget: list[str] | None = None,
    ) -> Any:
        try:
            clean_query = _required_string(query, "query")
            search_type = (type or "auto").strip()
            if search_type not in _SEARCH_TYPES:
                raise ValueError("type must be auto, fast, or instant")
            if category is not None and category not in _ADVANCED_CATEGORIES:
                raise ValueError("category is not supported by Exa advanced search")

            text_max = _optional_positive_number(textMaxCharacters)
            contents: dict[str, Any] = {
                "text": {"maxCharacters": text_max} if text_max else True,
            }
            max_age = _optional_number(maxAgeHours)
            if max_age is None:
                contents["livecrawl"] = "fallback"
            else:
                contents["maxAgeHours"] = max_age
            live_timeout = _optional_number(livecrawlTimeout)
            if live_timeout:
                contents["livecrawlTimeout"] = live_timeout
            context_max = _optional_positive_number(contextMaxCharacters)
            if context_max:
                contents["context"] = {"maxCharacters": context_max}
            if _optional_boolean(enableSummary):
                contents["summary"] = (
                    {"query": summaryQuery} if summaryQuery else True
                )
            if _optional_boolean(enableHighlights):
                highlights: dict[str, Any] = {}
                for name, value in (
                    ("maxCharacters", highlightsMaxCharacters),
                    ("numSentences", highlightsNumSentences),
                    ("highlightsPerUrl", highlightsPerUrl),
                ):
                    number = _optional_number(value)
                    if number is not None:
                        highlights[name] = number
                if highlightsQuery:
                    highlights["query"] = highlightsQuery
                contents["highlights"] = highlights
            subpage_count = _optional_number(subpages)
            if subpage_count:
                contents["subpages"] = subpage_count
            if subpageTarget:
                contents["subpageTarget"] = subpageTarget

            payload: dict[str, Any] = {
                "query": clean_query,
                "type": search_type,
                "numResults": _optional_number(numResults) or 10,
                "contents": contents,
            }
            optional_values = {
                "category": category,
                "includeDomains": includeDomains,
                "excludeDomains": excludeDomains,
                "startPublishedDate": startPublishedDate,
                "endPublishedDate": endPublishedDate,
                "startCrawlDate": startCrawlDate,
                "endCrawlDate": endCrawlDate,
                "includeText": includeText,
                "excludeText": excludeText,
                "userLocation": userLocation,
                "additionalQueries": additionalQueries,
            }
            for name, value in optional_values.items():
                if value:
                    payload[name] = value
            clean_moderation = _optional_boolean(moderation)
            if clean_moderation is not None:
                payload["moderation"] = clean_moderation

            response = active_client.post(
                "/search",
                payload,
                timeout=_ADVANCED_TIMEOUT,
            )
            sanitized = _sanitize_response(response)
            if not sanitized:
                return text_result(
                    "No search results found. Please try a different query "
                    "or adjust your filters."
                )
            return text_result(
                json.dumps(
                    sanitized,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        except (ExaPoolError, ValueError, TypeError) as exc:
            message = _error_message(exc) if isinstance(exc, ExaPoolError) else str(exc)
            return text_result(
                f"web_search_advanced_exa error: {message}",
                is_error=True,
            )

    @server.tool(
        name="web_fetch_exa",
        description=(
            "Fetch one or more webpages and return JSON evidence documents. "
            "Each successful result has an exact content body and a receipt "
            "required by pv_save_research; search snippets have no receipt."
        ),
        annotations=annotations,
        structured_output=False,
    )
    def web_fetch_exa(
        urls: list[str] | str,
        maxCharacters: int | float | str | None = None,
    ) -> Any:
        try:
            clean_urls = _normalise_urls(urls)
            response = active_client.post(
                "/contents",
                {
                    "urls": clean_urls,
                    "text": {
                        "maxCharacters": (
                            _optional_positive_number(maxCharacters) or 3_000
                        )
                    },
                },
                timeout=_BASIC_TIMEOUT,
            )
            raw_statuses = response.get("statuses")
            url_errors = [
                item
                for item in raw_statuses
                if _is_record(item) and item.get("status") == "error"
            ] if isinstance(raw_statuses, list) else []
            sanitized = _sanitize_response(response)
            results = sanitized.get("results")
            clean_results = results if isinstance(results, list) else []
            if not clean_results and url_errors:
                messages = []
                for error in url_errors:
                    raw_error = error.get("error")
                    tag = (
                        raw_error.get("tag")
                        if _is_record(raw_error)
                        else "unknown error"
                    )
                    messages.append(f"{error.get('id')}: {tag or 'unknown error'}")
                return text_result(
                    "Error fetching URL(s): " + "; ".join(messages),
                    is_error=True,
                )
            if not clean_results:
                return text_result("No content found for the provided URL(s).")

            evidence_results: list[dict[str, str]] = []
            for result in clean_results:
                raw_url = result.get("url")
                content = result.get("text")
                if not isinstance(raw_url, str) or not isinstance(content, str):
                    url_errors.append(
                        {"id": str(raw_url or "unknown URL"), "error": {"tag": "missing content"}}
                    )
                    continue
                normalized_url = normalize_evidence_url(raw_url)
                evidence: dict[str, str] = {
                    "url": normalized_url,
                    "content": content,
                    "content_sha256": content_sha256(content),
                    "receipt": issue_evidence_receipt(
                        active_receipt_key,
                        url=normalized_url,
                        content=content,
                    ),
                }
                for field in ("title", "publishedDate", "author"):
                    value = result.get(field)
                    if isinstance(value, str):
                        evidence[field] = value
                evidence_results.append(evidence)
            errors: list[dict[str, str]] = []
            for error in url_errors:
                raw_error = error.get("error")
                tag = (
                    raw_error.get("tag")
                    if _is_record(raw_error)
                    else "unknown error"
                )
                errors.append(
                    {
                        "url": str(error.get("id") or ""),
                        "error": str(tag or "unknown error"),
                    }
                )
            payload: dict[str, Any] = {"results": evidence_results}
            if errors:
                payload["errors"] = errors
            return text_result(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
        except (ExaPoolError, EvidenceReceiptError, ValueError, TypeError) as exc:
            message = _error_message(exc) if isinstance(exc, ExaPoolError) else str(exc)
            return text_result(f"web_fetch_exa error: {message}", is_error=True)

    return server


def _positive_float_environment(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise SystemExit(f"{name} must be finite and greater than zero")
    return value


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
    """Run stdio by default, with an explicitly opt-in HTTP transport."""

    parser = argparse.ArgumentParser(description="Rotating-key Exa MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.getenv("EXA_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument(
        "--host",
        default=os.getenv("EXA_MCP_HOST", DEFAULT_MCP_HOST),
    )
    try:
        default_port = int(os.getenv("EXA_MCP_PORT", str(DEFAULT_MCP_PORT)))
    except ValueError as exc:
        raise SystemExit("EXA_MCP_PORT must be an integer") from exc
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if (
        args.transport == "streamable-http"
        and args.host not in {"127.0.0.1", "::1", "localhost"}
        and not _boolean_environment("EXA_MCP_ALLOW_REMOTE")
    ):
        parser.error(
            "non-loopback HTTP binding requires EXA_MCP_ALLOW_REMOTE=true"
        )

    timeout = _positive_float_environment("EXA_HTTP_TIMEOUT_SECONDS", 60.0)
    client = ExaPoolClient(environ=os.environ, timeout=timeout)
    server = build_server(client)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run(
            "streamable-http",
            host=args.host,
            port=args.port,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_server", "main"]
