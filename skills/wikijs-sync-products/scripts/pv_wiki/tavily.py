"""Small, dependency-free Tavily Search/Extract client.

The module deliberately talks only to Tavily's fixed API endpoints.  It never
downloads a search result URL itself; callers may pass at most five public web
URLs to Tavily Extract.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from email.utils import parsedate_to_datetime
from typing import Any

from .config import is_placeholder_value
from .render import validate_public_http_url


API_BASE_URL = "https://api.tavily.com"
DEFAULT_TIMEOUT = 20.0
MAX_PRODUCT_QUERIES = 3
MAX_SEARCH_QUERIES_PER_CALL = 3
MAX_EXTRACT_URLS = 5
BASIC_SEARCH_CREDITS_PER_QUERY = 1
ADVANCED_EXTRACT_CREDITS_PER_BATCH = 2
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_RETRY_DELAY = 60.0
_TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
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


class TavilyError(RuntimeError):
    """Base class for Tavily client errors."""


class TavilyConfigError(TavilyError):
    """Raised when required Tavily configuration is missing or invalid."""


class TavilyHTTPError(TavilyError):
    """Raised for a non-retryable or exhausted Tavily HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(message)


class TavilyQuotaExhaustedError(TavilyError):
    """Raised after every configured key has exhausted its monthly credits."""


class TavilyNetworkError(TavilyError):
    """Raised when Tavily cannot be reached after retries."""


class TavilyResponseError(TavilyError):
    """Raised when Tavily returns malformed JSON."""


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
            request.full_url, code, message, headers, fp
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
    """Build up to three focused datasheet/documentation queries.

    Common PostgreSQL column aliases are accepted so the skill does not need a
    hard dependency on one product schema.  At least one human-meaningful name,
    manufacturer, or model/part number is required.
    """

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
        f"{exact_identity} datasheet PDF",
        f"{exact_identity} specifications technical manual",
    ]

    category_context = ""
    if category and category.casefold() not in {
        item.casefold() for item in identity
    }:
        category_context = _quote(category)
    candidates.append(
        f"{exact_identity} {category_context} manufacturer product type official".strip()
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
        if any(ord(character) < 32 or ord(character) == 127 for character in raw_query):
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
        # validate_public_http_url already rejects private, ambiguous numeric,
        # malformed IDNA, credential-bearing, and local/internal hostnames.
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:  # Defense in depth.
            raise ValueError("non-public IP URLs are not allowed")

    default_port = (scheme == "http" and port in {None, 80}) or (
        scheme == "https" and port in {None, 443}
    )
    netloc = hostname if default_port else f"{hostname}:{port}"
    if ":" in hostname and not hostname.startswith("["):
        netloc = f"[{hostname}]" if default_port else f"[{hostname}]:{port}"

    path = parts.path or "/"
    normalized_query = urllib.parse.urlencode(
        urllib.parse.parse_qsl(parts.query, keep_blank_values=True), doseq=True
    )
    normalized = urllib.parse.urlunsplit((scheme, netloc, path, normalized_query, ""))

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


def _credit_value(value: Any) -> int | float:
    number = _finite_number(value)
    return int(number) if number.is_integer() else number


def _required_response_credits(response: Mapping[str, Any]) -> int | float:
    """Return provider-reported credits without treating bad audit data as zero."""

    usage = response.get("usage")
    if not isinstance(usage, Mapping) or "credits" not in usage:
        raise TavilyResponseError(
            "Tavily response is missing usage.credits"
        )
    value = usage["credits"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TavilyResponseError(
            "Tavily response usage.credits must be a number"
        )
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise TavilyResponseError(
            "Tavily response usage.credits must be finite and non-negative"
        )
    return int(number) if number.is_integer() else number


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


class _RateLimitSignal(Exception):
    """Internal signal: current API key got a 429, try rotating to the next."""

    def __init__(
        self,
        *,
        retry_after: float | None = None,
        inner: Exception | None = None,
    ) -> None:
        self.retry_after = retry_after
        self.inner = inner
        super().__init__("rate limited")


class _QuotaExhaustedSignal(Exception):
    """Internal signal: current key cannot spend more credits this month."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("monthly quota exhausted")


class TavilyClient:
    """Tavily REST client with safe 429/quota rotation and no secret persistence.

    Paid POSTs are not replayed after a timeout, connection failure, 5xx, or
    malformed response because the provider may already have consumed the
    request. Only explicit 429 and exhausted-key responses are safe to rotate
    or retry.
    """

    def __init__(
        self,
        api_key: str | Sequence[str] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        keys = self._resolve_keys(api_key)
        if not keys:
            raise TavilyConfigError(
                "At least one Tavily API key is required "
                "(TAVILY_API_KEYS or TAVILY_API_KEY)"
            )
        if timeout <= 0 or not math.isfinite(timeout):
            raise TavilyConfigError("timeout must be finite and greater than zero")
        if max_retries < 0:
            raise TavilyConfigError("max_retries cannot be negative")
        if backoff_base < 0 or not math.isfinite(backoff_base):
            raise TavilyConfigError("backoff_base must be finite and non-negative")

        self._keys: list[str] = keys
        self.credential_fingerprint = hashlib.sha256(
            "\0".join(sorted(set(keys))).encode("utf-8")
        ).hexdigest()
        self._key_index: int = 0
        # Per-key rate-limit cooldown: key_str -> epoch_seconds when usable again.
        self._rate_limited_until: dict[str, float] = {}
        # Never persisted or logged: quota-exhausted keys are skipped for the
        # remaining lifetime of this client.
        self._quota_exhausted_keys: set[str] = set()
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self._sleep = sleep
        self._opener = opener or _NO_REDIRECT_OPENER.open
        self.last_operation_requests = 0
        self.last_operation_completed_requests = 0
        self.last_operation_known_credits: int | float = 0

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_keys(api_key: str | Sequence[str] | None) -> list[str]:
        """Collect keys from the constructor argument and/or environment.

        Priority:
        1. ``api_key`` argument (string or sequence of strings).
        2. ``TAVILY_API_KEYS`` env var — comma- or whitespace-separated list.
        3. ``TAVILY_API_KEY`` env var — single key (backward compatible).
        """
        keys: list[str] = []
        if api_key is not None:
            if isinstance(api_key, str):
                keys.append(api_key.strip())
            else:
                for k in api_key:
                    if isinstance(k, str) and k.strip():
                        keys.append(k.strip())
        else:
            multi = os.getenv("TAVILY_API_KEYS", "")
            if multi.strip():
                for part in re.split(r"[\s,]+", multi):
                    part = part.strip()
                    if part:
                        keys.append(part)
            if not keys:
                single = os.getenv("TAVILY_API_KEY", "").strip()
                if single:
                    keys.append(single)

        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for k in keys:
            if not is_placeholder_value(k) and k not in seen:
                seen.add(k)
                unique.append(k)
        return unique

    @property
    def num_keys(self) -> int:
        return len(self._keys)

    def _current_key(self) -> str:
        return self._keys[self._key_index]

    def _advance_key(self) -> bool:
        """Try to advance to the next usable key.

        Returns ``True`` if a usable key was found, ``False`` if all keys
        are rate-limited (or only one key exists).
        """
        now_ts = time.time()
        num = len(self._keys)
        for offset in range(1, num + 1):
            idx = (self._key_index + offset) % num
            key = self._keys[idx]
            if key in self._quota_exhausted_keys:
                continue
            cooldown = self._rate_limited_until.get(key, 0.0)
            if cooldown <= now_ts:
                self._key_index = idx
                return True
        return False

    def _mark_rate_limited(self, key: str, retry_after: float | None) -> None:
        """Mark a key as rate-limited for the given duration."""
        if retry_after is not None:
            delay = min(retry_after + 1.0, MAX_RETRY_DELAY)
        else:
            delay = 60.0  # Default cooldown if no Retry-After header.
        self._rate_limited_until[key] = time.time() + delay

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if path not in {"/search", "/extract"}:
            raise ValueError("unsupported Tavily endpoint")

        rate_limit_round = 0
        while True:
            rate_limits: list[_RateLimitSignal] = []
            last_error: TavilyNetworkError | TavilyResponseError | None = None

            # Rotate immediately through usable keys. A round ends only when
            # every configured key is cooling down after a 429 response.
            for _key_attempt in range(len(self._keys)):
                api_key = self._current_key()
                if api_key in self._quota_exhausted_keys:
                    if self._advance_key():
                        api_key = self._current_key()
                    else:
                        break
                try:
                    return self._post_with_key(path, payload, api_key)
                except _QuotaExhaustedSignal:
                    self._quota_exhausted_keys.add(api_key)
                    if self._advance_key():
                        continue
                    break
                except _RateLimitSignal as exc:
                    self._mark_rate_limited(api_key, exc.retry_after)
                    rate_limits.append(exc)
                    if self._advance_key():
                        continue
                    break
                except TavilyHTTPError:
                    raise
                except (TavilyNetworkError, TavilyResponseError):
                    # The paid request may have reached Tavily. Never replay it
                    # with this or another key when its result is uncertain.
                    raise
            if len(self._quota_exhausted_keys) == len(self._keys):
                raise TavilyQuotaExhaustedError(
                    "All configured Tavily API keys have exhausted their monthly quota"
                )

            if not rate_limits:
                if last_error is not None:
                    raise last_error
                raise TavilyNetworkError("All Tavily API keys exhausted")
            if rate_limit_round >= self.max_retries:
                raise TavilyHTTPError(
                    "Tavily API returned HTTP 429 after bounded retries",
                    status_code=429,
                )

            fallback = self.backoff_base * (2**rate_limit_round)
            delays = [
                signal.retry_after
                if signal.retry_after is not None
                else fallback
                for signal in rate_limits
            ]
            self._sleep(_bounded_retry_delay(min(delays)))

            # Sleeping satisfies the retry delay for this bounded round. Clear
            # cooldowns so a single-key client can retry and a multi-key client
            # can start another rotation without terminating early.
            self._rate_limited_until.clear()
            rate_limit_round += 1

    def _post_with_key(
        self, path: str, payload: Mapping[str, Any], api_key: str
    ) -> dict[str, Any]:
        """Send one paid request; ambiguous failures are never retried."""

        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                f"{API_BASE_URL}{path}",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )

            try:
                with self._opener(request, timeout=self.timeout) as response:
                    length = _content_length(response)
                    if length is not None and length > MAX_RESPONSE_BYTES:
                        raise TavilyResponseError(
                            f"Tavily response exceeds {MAX_RESPONSE_BYTES} bytes"
                        )
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if not isinstance(body, bytes):
                        raise TavilyResponseError("Tavily response body must be bytes")
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise TavilyResponseError(
                            f"Tavily response exceeds {MAX_RESPONSE_BYTES} bytes"
                        )
            except urllib.error.HTTPError as exc:
                if exc.code in {432, 433}:
                    # 432 is plan-limit exhaustion and 433 is pay-as-you-go
                    # exhaustion. Both are monthly-credit terminal states for
                    # this key, unlike the transient 429 request-rate limit.
                    raise _QuotaExhaustedSignal(exc.code) from None

                if exc.code == 429:
                    # Rate limited — signal key rotation instead of retrying.
                    retry_after = _retry_after_seconds(exc)
                    raise _RateLimitSignal(retry_after=retry_after) from None
                raise TavilyHTTPError(
                    f"Tavily API returned HTTP {exc.code}",
                    status_code=exc.code,
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise TavilyNetworkError(
                    "Tavily API request failed; ambiguous paid request was "
                    "not replayed"
                ) from exc

            try:
                decoded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TavilyResponseError("Tavily returned invalid JSON") from exc
            if not isinstance(decoded, dict):
                raise TavilyResponseError("Tavily response must be a JSON object")
            return decoded

        raise TavilyNetworkError("Tavily API request failed after retries")

    def _shortest_cooldown(self) -> float:
        """Return the shortest remaining cooldown across all keys."""
        now = time.time()
        remaining = [
            cooldown - now
            for cooldown in self._rate_limited_until.values()
            if cooldown > now
        ]
        return min(remaining) if remaining else 0.0

    def search_product(
        self, product: Mapping[str, Any], max_results: int = 5
    ) -> dict[str, Any]:
        """Search a product with focused queries and return a deduplicated bundle."""

        return self.search_queries(
            build_queries(product),
            max_results=max_results,
        )

    def search_queries(
        self,
        queries: Sequence[str],
        max_results: int = 5,
    ) -> dict[str, Any]:
        """Run one bounded batch of already locally authorized queries."""

        self.last_operation_requests = 0
        self.last_operation_completed_requests = 0
        self.last_operation_known_credits = 0
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise TypeError("max_results must be an integer")
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20")

        clean_queries = _validated_queries(queries)
        merged: dict[str, dict[str, Any]] = {}
        credits: int | float = 0
        request_ids: list[str] = []

        for query in clean_queries:
            self.last_operation_requests += 1
            response = self._post(
                "/search",
                {
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "topic": "general",
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                    "include_usage": True,
                },
            )
            response_credits = _required_response_credits(response)
            credits += response_credits
            self.last_operation_known_credits = _credit_value(credits)
            self.last_operation_completed_requests += 1
            request_id = response.get("request_id")
            if isinstance(request_id, str) and request_id:
                request_ids.append(request_id)

            results = response.get("results")
            if not isinstance(results, list):
                continue
            for raw_result in results:
                if not isinstance(raw_result, Mapping):
                    continue
                raw_url = raw_result.get("url")
                try:
                    url, dedupe_key = _canonical_url(raw_url, validate_public=True)
                except (TypeError, ValueError):
                    continue

                score = _finite_number(raw_result.get("score"))
                candidate = {
                    "title": _clean_term(raw_result.get("title")),
                    "url": url,
                    "content": str(raw_result.get("content") or ""),
                    "score": score,
                    "source_queries": [query],
                }
                existing = merged.get(dedupe_key)
                if existing is None:
                    merged[dedupe_key] = candidate
                else:
                    if query not in existing["source_queries"]:
                        existing["source_queries"].append(query)
                    if score > existing["score"]:
                        candidate["source_queries"] = existing["source_queries"]
                        merged[dedupe_key] = candidate

        ranked = sorted(
            merged.values(), key=lambda item: (-item["score"], item["url"])
        )[:max_results]
        return {
            "queries": clean_queries,
            "search_depth": "basic",
            "max_results": max_results,
            "results": ranked,
            "usage": {"credits": _credit_value(credits)},
            "request_ids": request_ids,
        }

    def extract_urls(self, urls: Sequence[str], query: str) -> dict[str, Any]:
        """Ask Tavily to extract at most five validated public web URLs."""

        self.last_operation_requests = 0
        self.last_operation_completed_requests = 0
        self.last_operation_known_credits = 0
        if isinstance(urls, (str, bytes)) or not isinstance(urls, Sequence):
            raise TypeError("urls must be a sequence of URL strings")
        clean_query = re.sub(r"\s+", " ", str(query or "")).strip()
        if not clean_query:
            raise ValueError("query must be non-empty")
        if len(clean_query) > 400:
            raise ValueError("query must not exceed 400 characters")

        submitted: list[str] = []
        seen: set[str] = set()
        for raw_url in urls:
            normalized, dedupe_key = _canonical_url(raw_url, validate_public=True)
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                submitted.append(normalized)
        if not submitted:
            raise ValueError("at least one public HTTP(S) URL is required")
        if len(submitted) > MAX_EXTRACT_URLS:
            raise ValueError(f"at most {MAX_EXTRACT_URLS} unique URLs may be extracted")

        self.last_operation_requests = 1
        response = self._post(
            "/extract",
            {
                "urls": submitted,
                "query": clean_query,
                "chunks_per_source": 5,
                "extract_depth": "advanced",
                "format": "markdown",
                "include_images": False,
                "include_usage": True,
            },
        )

        results: list[dict[str, Any]] = []
        raw_results = response.get("results")
        if isinstance(raw_results, list):
            for item in raw_results:
                if not isinstance(item, Mapping):
                    continue
                raw_url = item.get("url")
                try:
                    result_url, _ = _canonical_url(raw_url, validate_public=True)
                except (TypeError, ValueError):
                    continue
                result: dict[str, Any] = {
                    "url": result_url,
                    "raw_content": str(item.get("raw_content") or ""),
                }
                if isinstance(item.get("favicon"), str):
                    result["favicon"] = item["favicon"]
                results.append(result)

        failed_results: list[dict[str, str]] = []
        raw_failed = response.get("failed_results")
        if isinstance(raw_failed, list):
            for item in raw_failed:
                if not isinstance(item, Mapping):
                    continue
                failed_results.append(
                    {
                        "url": str(item.get("url") or ""),
                        "error": str(item.get("error") or "extraction failed"),
                    }
                )

        credit_count = _required_response_credits(response)
        self.last_operation_known_credits = credit_count
        self.last_operation_completed_requests = 1
        request_id = response.get("request_id")
        return {
            "query": clean_query,
            "urls": submitted,
            "extract_depth": "advanced",
            "results": results,
            "failed_results": failed_results,
            "usage": {"credits": credit_count},
            "request_id": request_id if isinstance(request_id, str) else None,
        }


def search_product(
    product: Mapping[str, Any],
    max_results: int = 5,
    *,
    client: TavilyClient | None = None,
) -> dict[str, Any]:
    """Module-level convenience wrapper for :meth:`TavilyClient.search_product`."""

    return (client or TavilyClient()).search_product(product, max_results=max_results)


def search_queries(
    queries: Sequence[str],
    max_results: int = 5,
    *,
    client: TavilyClient | None = None,
) -> dict[str, Any]:
    """Module-level convenience wrapper for incremental research searches."""

    return (client or TavilyClient()).search_queries(
        queries,
        max_results=max_results,
    )


def extract_urls(
    urls: Sequence[str],
    query: str,
    *,
    client: TavilyClient | None = None,
) -> dict[str, Any]:
    """Module-level convenience wrapper for :meth:`TavilyClient.extract_urls`."""

    return (client or TavilyClient()).extract_urls(urls, query)


__all__ = [
    "ADVANCED_EXTRACT_CREDITS_PER_BATCH",
    "BASIC_SEARCH_CREDITS_PER_QUERY",
    "TavilyClient",
    "TavilyConfigError",
    "TavilyError",
    "TavilyHTTPError",
    "TavilyNetworkError",
    "TavilyResponseError",
    "build_queries",
    "extract_urls",
    "search_product",
    "search_queries",
]
