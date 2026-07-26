"""Provider-neutral helpers for bounded public product research.

The module owns query construction, public-URL validation, retry timing, and
response-size limits shared by the Exa client and the orchestration layer. It
does not contain provider credentials or perform network requests itself.
"""

from __future__ import annotations

import ipaddress
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from email.utils import parsedate_to_datetime
from typing import Any

from .render import validate_public_http_url


MAX_PRODUCT_QUERIES = 3
MAX_SEARCH_QUERIES_PER_CALL = 3
MAX_EXTRACT_URLS = 5
SEARCH_BUDGET_UNITS_PER_QUERY = 1
EXTRACT_BUDGET_UNITS_PER_BATCH = 2
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_RETRY_DELAY = 60.0
_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
    }
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an HTTP error without replaying credentials."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        del new_url
        raise urllib.error.HTTPError(
            request.full_url,
            code,
            message,
            headers,
            fp,
        )


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _clean_term(value: Any) -> str:
    """Convert a product attribute into a short, query-safe string."""

    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("name", "label", "title", "code", "value"):
            if key in value:
                return _clean_term(value[key])
        return ""
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            cleaned = _clean_term(item)
            if cleaned:
                return cleaned
        return ""

    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text.replace('"', " ")).strip()
    return text[:120]


def _pick(product: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        cleaned = _clean_term(product.get(key))
        if cleaned:
            return cleaned
    return ""


def _quote(term: str) -> str:
    return f'"{term}"'


def build_queries(product: Mapping[str, Any]) -> list[str]:
    """Build up to three focused official product-document queries."""

    if not isinstance(product, Mapping):
        raise TypeError("product must be a mapping")

    manufacturer = _pick(product, ("manufacturer", "brand", "vendor", "maker"))
    model = _pick(
        product,
        (
            "model",
            "model_number",
            "part_number",
            "mpn",
            "sku",
            "product_code",
            "code",
        ),
    )
    name = _pick(product, ("name", "product_name", "title", "model_name"))
    category = _pick(product, ("category", "product_type", "type"))

    identity: list[str] = []
    for term in (manufacturer, model or name):
        if term and term.casefold() not in {item.casefold() for item in identity}:
            identity.append(term)
    if not identity:
        raise ValueError("product needs a manufacturer, model/part number, or name")

    exact_identity = " ".join(_quote(term) for term in identity)
    candidates = [
        f"{exact_identity} official datasheet PDF",
        f"{exact_identity} official specifications technical manual",
    ]

    category_context = ""
    if category and category.casefold() not in {
        item.casefold() for item in identity
    }:
        category_context = _quote(category)
    candidates.append(
        (
            f"{exact_identity} {category_context} "
            "manufacturer product family official"
        ).strip()
    )

    queries: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        query = re.sub(r"\s+", " ", query).strip()[:400]
        key = query.casefold()
        if query and key not in seen:
            seen.add(key)
            queries.append(query)
        if len(queries) == MAX_PRODUCT_QUERIES:
            break
    return queries


def _validated_queries(queries: Sequence[str]) -> list[str]:
    """Return a small, unambiguous set of caller-supplied search queries."""

    if isinstance(queries, (str, bytes)) or not isinstance(queries, Sequence):
        raise TypeError("queries must be a sequence of strings")
    if not 1 <= len(queries) <= MAX_SEARCH_QUERIES_PER_CALL:
        raise ValueError(
            f"queries must contain 1-{MAX_SEARCH_QUERIES_PER_CALL} items"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_query in queries:
        if not isinstance(raw_query, str):
            raise TypeError("queries must contain only strings")
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in raw_query
        ):
            raise ValueError("queries must not contain control characters")
        query = re.sub(r"\s+", " ", raw_query).strip()
        if not query:
            raise ValueError("queries must not contain empty strings")
        if len(query) > 400:
            raise ValueError("queries must not exceed 400 characters")
        key = query.casefold()
        if key in seen:
            raise ValueError("queries must be unique")
        seen.add(key)
        normalized.append(query)
    return normalized


def _canonical_url(url: str, *, validate_public: bool) -> tuple[str, str]:
    """Return a normalized URL and a less noisy deduplication key."""

    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string")
    candidate = validate_public_http_url(url) if validate_public else url.strip()
    try:
        parts = urllib.parse.urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid URL") from exc

    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("only absolute HTTP(S) URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs containing credentials are not allowed")
    if port not in {None, 80, 443}:
        raise ValueError("non-standard URL ports are not allowed")

    if validate_public:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("non-public IP URLs are not allowed")

    default_port = (scheme == "http" and port in {None, 80}) or (
        scheme == "https" and port in {None, 443}
    )
    netloc = hostname if default_port else f"{hostname}:{port}"
    if ":" in hostname and not hostname.startswith("["):
        netloc = f"[{hostname}]" if default_port else f"[{hostname}]:{port}"

    path = parts.path or "/"
    normalized_query = urllib.parse.urlencode(
        urllib.parse.parse_qsl(parts.query, keep_blank_values=True),
        doseq=True,
    )
    normalized = urllib.parse.urlunsplit(
        (scheme, netloc, path, normalized_query, "")
    )

    dedupe_pairs = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_KEYS:
            continue
        dedupe_pairs.append((key, value))
    dedupe_pairs.sort()
    dedupe_query = urllib.parse.urlencode(dedupe_pairs, doseq=True)
    dedupe_path = path.rstrip("/") or "/"
    dedupe_key = urllib.parse.urlunsplit(
        (scheme, netloc, dedupe_path, dedupe_query, "")
    ).casefold()
    return normalized, dedupe_key


def _finite_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    raw = error.headers.get("Retry-After") if error.headers is not None else None
    if not raw:
        return None
    try:
        seconds = float(raw)
        if not math.isfinite(seconds):
            return MAX_RETRY_DELAY if seconds > 0 else None
        return min(MAX_RETRY_DELAY, max(0.0, seconds))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            return None
        return min(
            MAX_RETRY_DELAY,
            max(0.0, retry_at.timestamp() - time.time()),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _bounded_retry_delay(value: float) -> float:
    if not math.isfinite(value):
        return MAX_RETRY_DELAY if value > 0 else 0.0
    return min(MAX_RETRY_DELAY, max(0.0, value))


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


__all__ = [
    "EXTRACT_BUDGET_UNITS_PER_BATCH",
    "MAX_EXTRACT_URLS",
    "MAX_RESPONSE_BYTES",
    "SEARCH_BUDGET_UNITS_PER_QUERY",
    "build_queries",
]
