"""Dependency-free Exa Search/Contents client for bounded product research.

The client exposes the worker's bounded Search/Contents interface while
normalizing provider billing into per-action budget units. Exa's
provider-reported dollar cost is retained separately in every successful
bundle.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .config import (
    is_placeholder_value,
    max_extract_chars,
    supplier_search_excluded_domains,
)
from .search import (
    EXTRACT_BUDGET_UNITS_PER_BATCH,
    MAX_EXTRACT_URLS,
    MAX_RESPONSE_BYTES,
    SEARCH_BUDGET_UNITS_PER_QUERY,
    _NO_REDIRECT_OPENER,
    _bounded_retry_delay,
    _canonical_url,
    _clean_term,
    _content_length,
    _finite_number,
    _retry_after_seconds,
    _validated_queries,
    build_queries,
)


API_BASE_URL = "https://api.exa.ai"
DEFAULT_TIMEOUT = 20.0
SEARCH_CONTRACT_VERSION = "2026-07-27.5"
EXTRACT_CONTRACT_VERSION = "2026-07-28.1"
MAX_HIGHLIGHT_CHARACTERS = 12_000


class ExaError(RuntimeError):
    """Base class for Exa client errors."""


class ExaConfigError(ExaError):
    """Raised when required Exa configuration is missing or invalid."""


class ExaHTTPError(ExaError):
    """Raised for a non-retryable or exhausted Exa HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(message)


class ExaQuotaExhaustedError(ExaError):
    """Raised after every configured key has exhausted its spend budget."""


class ExaNetworkError(ExaError):
    """Raised when an Exa paid request has an ambiguous network result."""


class ExaResponseError(ExaError):
    """Raised when Exa returns malformed or unsafe response data."""


class _RateLimitSignal(Exception):
    def __init__(self, retry_after: float | None) -> None:
        self.retry_after = retry_after
        super().__init__("rate limited")


class _QuotaExhaustedSignal(Exception):
    pass


def _provider_cost(response: Mapping[str, Any]) -> float:
    cost = response.get("costDollars")
    if not isinstance(cost, Mapping):
        return 0.0
    total = cost.get("total")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return 0.0
    value = float(total)
    return value if math.isfinite(value) and value >= 0 else 0.0


def _clean_query(value: Any) -> str:
    query = re.sub(r"\s+", " ", str(value or "")).strip()
    if not query:
        raise ValueError("query must be non-empty")
    if len(query) > 400:
        raise ValueError("query must not exceed 400 characters")
    return query


def _validated_search_domains(
    values: Sequence[str] | None,
    *,
    name: str,
) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a sequence of hostnames")
    if len(values) > 50:
        raise ValueError(f"{name} cannot contain more than 50 hostnames")
    domains: list[str] = []
    seen: set[str] = set()
    for raw_domain in values:
        if not isinstance(raw_domain, str):
            raise TypeError(f"{name} must contain only hostname strings")
        domain = raw_domain.strip().rstrip(".").casefold()
        if (
            not domain
            or len(domain) > 253
            or "://" in domain
            or "/" in domain
            or "*" in domain
            or not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                domain,
            )
        ):
            raise ValueError(f"{name} must contain only narrow hostnames")
        if domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


def _identity_key(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )


def _product_model_keys(product: Mapping[str, Any]) -> tuple[str, ...]:
    raw_values: list[Any] = [product.get("model")]
    candidates = product.get("model_candidates")
    if (
        isinstance(candidates, Sequence)
        and not isinstance(candidates, (str, bytes, bytearray))
    ):
        raw_values.extend(candidates)
    keys: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        key = _identity_key(raw_value)
        if len(key) >= 3 and key not in seen:
            seen.add(key)
            keys.append(key)
    return tuple(keys)


def _result_model_relevance(
    result: Mapping[str, Any],
    model_keys: Sequence[str],
) -> int:
    searchable = _identity_key(
        " ".join(
            (
                str(result.get("title") or ""),
                urllib.parse.unquote(str(result.get("url") or "")),
                str(result.get("content") or ""),
            )
        )
    )
    return max(
        (len(key) for key in model_keys if key in searchable),
        default=0,
    )


def _is_pdf_result(result: Mapping[str, Any]) -> bool:
    try:
        path = urllib.parse.urlsplit(str(result.get("url") or "")).path
    except ValueError:
        return False
    return path.casefold().endswith(".pdf")


def _rank_product_results(
    results: Any,
    model_keys: Sequence[str],
) -> list[dict[str, Any]]:
    if not isinstance(results, list):
        return []
    valid = [dict(item) for item in results if isinstance(item, Mapping)]
    return sorted(
        valid,
        key=lambda item: (
            -_result_model_relevance(item, model_keys),
            -int(_is_pdf_result(item)),
            -_finite_number(item.get("score")),
            str(item.get("url") or ""),
        ),
    )


class ExaClient:
    """Exa REST client with bounded retries and no credential persistence."""

    provider_name = "exa"
    api_base_url = API_BASE_URL
    search_contract_version = SEARCH_CONTRACT_VERSION
    extract_contract_version = EXTRACT_CONTRACT_VERSION

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
            raise ExaConfigError(
                "At least one Exa API key is required "
                "(EXA_API_KEYS or EXA_API_KEY)"
            )
        if timeout <= 0 or not math.isfinite(timeout):
            raise ExaConfigError("timeout must be finite and greater than zero")
        if max_retries < 0:
            raise ExaConfigError("max_retries cannot be negative")
        if backoff_base < 0 or not math.isfinite(backoff_base):
            raise ExaConfigError("backoff_base must be finite and non-negative")

        self._keys = keys
        self.credential_fingerprint = hashlib.sha256(
            # Key order controls which credential receives the first request
            # and therefore belongs to the provider/circuit scope.
            "\0".join(keys).encode("utf-8")
        ).hexdigest()
        self._key_index = 0
        self._quota_exhausted_keys: set[str] = set()
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self._sleep = sleep
        self._opener = opener or _NO_REDIRECT_OPENER.open
        self.last_operation_requests = 0
        self.last_operation_completed_requests = 0
        self.last_operation_known_credits: int | float = 0

    @staticmethod
    def _resolve_keys(api_key: str | Sequence[str] | None) -> list[str]:
        keys: list[str] = []
        if api_key is not None:
            if isinstance(api_key, str):
                keys.append(api_key.strip())
            else:
                for item in api_key:
                    if isinstance(item, str) and item.strip():
                        keys.append(item.strip())
        else:
            multi = os.getenv("EXA_API_KEYS", "")
            if multi.strip():
                keys.extend(
                    part.strip()
                    for part in re.split(r"[\s,]+", multi)
                    if part.strip()
                )
            if not keys:
                single = os.getenv("EXA_API_KEY", "").strip()
                if single:
                    keys.append(single)

        unique: list[str] = []
        seen: set[str] = set()
        for key in keys:
            if key and not is_placeholder_value(key) and key not in seen:
                seen.add(key)
                unique.append(key)
        return unique

    @property
    def num_keys(self) -> int:
        return len(self._keys)

    def _advance_key(self) -> bool:
        count = len(self._keys)
        for offset in range(1, count + 1):
            candidate = (self._key_index + offset) % count
            if self._keys[candidate] not in self._quota_exhausted_keys:
                self._key_index = candidate
                return True
        return False

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if path not in {"/search", "/contents"}:
            raise ValueError("unsupported Exa endpoint")

        rate_limit_round = 0
        while True:
            rate_limits: list[_RateLimitSignal] = []
            for _ in range(len(self._keys)):
                api_key = self._keys[self._key_index]
                if api_key in self._quota_exhausted_keys:
                    if self._advance_key():
                        api_key = self._keys[self._key_index]
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
                    rate_limits.append(exc)
                    if self._advance_key():
                        continue
                    break

            if len(self._quota_exhausted_keys) == len(self._keys):
                raise ExaQuotaExhaustedError(
                    "All configured Exa API keys have exhausted their spend budget"
                )
            if not rate_limits:
                raise ExaNetworkError("All Exa API keys are unavailable")
            if rate_limit_round >= self.max_retries:
                raise ExaHTTPError(
                    "Exa API returned HTTP 429 after bounded retries",
                    status_code=429,
                )
            fallback = self.backoff_base * (2**rate_limit_round)
            delays = [
                item.retry_after
                if item.retry_after is not None
                else fallback
                for item in rate_limits
            ]
            self._sleep(_bounded_retry_delay(min(delays)))
            rate_limit_round += 1

    def _post_with_key(
        self,
        path: str,
        payload: Mapping[str, Any],
        api_key: str,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{API_BASE_URL}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                length = _content_length(response)
                if length is not None and length > MAX_RESPONSE_BYTES:
                    raise ExaResponseError(
                        f"Exa response exceeds {MAX_RESPONSE_BYTES} bytes"
                    )
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if not isinstance(body, bytes):
                    raise ExaResponseError("Exa response body must be bytes")
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ExaResponseError(
                        f"Exa response exceeds {MAX_RESPONSE_BYTES} bytes"
                    )
        except urllib.error.HTTPError as exc:
            if exc.code == 402:
                raise _QuotaExhaustedSignal() from None
            if exc.code == 429:
                raise _RateLimitSignal(_retry_after_seconds(exc)) from None
            raise ExaHTTPError(
                f"Exa API returned HTTP {exc.code}",
                status_code=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExaNetworkError(
                "Exa API request failed; ambiguous paid request was not replayed"
            ) from exc

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExaResponseError("Exa returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ExaResponseError("Exa response must be a JSON object")
        return decoded

    def search_product(
        self,
        product: Mapping[str, Any],
        max_results: int = 5,
    ) -> dict[str, Any]:
        queries = build_queries(product)
        model_keys = _product_model_keys(product)
        include_domains = _validated_search_domains(
            product.get("_search_domains"),
            name="product._search_domains",
        )
        exclude_domains = _validated_search_domains(
            (
                product.get("_exclude_domains")
                or sorted(supplier_search_excluded_domains())
            ),
            name="product._exclude_domains",
        )
        if not include_domains:
            bundle = self.search_queries(
                queries,
                max_results=max_results,
                exclude_domains=exclude_domains,
            )
            bundle["results"] = _rank_product_results(
                bundle.get("results"),
                model_keys,
            )
            bundle["search_mode"] = "open_web"
            return bundle

        official_queries = queries[:2]
        fallback_queries = (
            queries[1:2]
            if len(model_keys) > 1
            else queries[2:]
        )
        official = self.search_queries(
            official_queries,
            max_results=max_results,
            include_domains=include_domains,
        )
        official_requests = self.last_operation_requests
        official_completed = self.last_operation_completed_requests
        official_credits = self.last_operation_known_credits
        official["planned_queries"] = queries
        official["official_domains"] = include_domains
        official_results = [
            item
            for item in _rank_product_results(
                official.get("results"),
                model_keys,
            )
            if _result_model_relevance(item, model_keys) > 0
        ]
        official["results"] = official_results
        official["official_results_found"] = bool(official_results)
        if official["official_results_found"] or not fallback_queries:
            official["search_mode"] = "official_only"
            return official

        fallback = self.search_queries(
            fallback_queries,
            max_results=max_results,
            exclude_domains=exclude_domains,
        )
        self.last_operation_requests += official_requests
        self.last_operation_completed_requests += official_completed
        self.last_operation_known_credits += official_credits
        fallback_results = _rank_product_results(
            fallback.get("results"),
            model_keys,
        )
        return {
            **fallback,
            "queries": [
                *official.get("queries", []),
                *fallback.get("queries", []),
            ],
            "planned_queries": queries,
            "search_mode": "official_then_open_web",
            "official_domains": include_domains,
            "official_results_found": False,
            "results": fallback_results,
            "usage": {
                "credits": (
                    float(official.get("usage", {}).get("credits", 0))
                    + float(fallback.get("usage", {}).get("credits", 0))
                ),
                "cost_dollars": round(
                    float(
                        official.get("usage", {}).get("cost_dollars", 0)
                    )
                    + float(
                        fallback.get("usage", {}).get("cost_dollars", 0)
                    ),
                    9,
                ),
            },
            "request_ids": [
                *official.get("request_ids", []),
                *fallback.get("request_ids", []),
            ],
        }

    def search_queries(
        self,
        queries: Sequence[str],
        max_results: int = 5,
        *,
        include_domains: Sequence[str] | None = None,
        exclude_domains: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        self.last_operation_requests = 0
        self.last_operation_completed_requests = 0
        self.last_operation_known_credits = 0
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise TypeError("max_results must be an integer")
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20")

        clean_queries = _validated_queries(queries)
        clean_include_domains = _validated_search_domains(
            include_domains,
            name="include_domains",
        )
        clean_exclude_domains = _validated_search_domains(
            exclude_domains,
            name="exclude_domains",
        )
        merged: dict[str, dict[str, Any]] = {}
        request_ids: list[str] = []
        cost_dollars = 0.0
        credits = 0

        for query in clean_queries:
            self.last_operation_requests += 1
            payload: dict[str, Any] = {
                "query": query,
                "type": "auto",
                "numResults": max_results,
                "contents": {
                    "highlights": {
                        "query": (
                            "official manufacturer datasheet and complete "
                            "model identity"
                        ),
                        "maxCharacters": 1_200,
                    }
                },
            }
            if clean_include_domains:
                payload["includeDomains"] = clean_include_domains
            if clean_exclude_domains:
                payload["excludeDomains"] = clean_exclude_domains
            response = self._post(
                "/search",
                payload,
            )
            results = response.get("results")
            if not isinstance(results, list):
                raise ExaResponseError("Exa search results must be a list")

            credits += SEARCH_BUDGET_UNITS_PER_QUERY
            cost_dollars += _provider_cost(response)
            self.last_operation_known_credits = credits
            self.last_operation_completed_requests += 1
            request_id = response.get("requestId")
            if isinstance(request_id, str) and request_id:
                request_ids.append(request_id)

            result_count = max(len(results), 1)
            for index, raw_result in enumerate(results[:max_results]):
                if not isinstance(raw_result, Mapping):
                    continue
                try:
                    url, dedupe_key = _canonical_url(
                        raw_result.get("url"),
                        validate_public=True,
                    )
                except (TypeError, ValueError):
                    continue
                highlights = raw_result.get("highlights")
                content = (
                    "\n\n[...]\n\n".join(
                        item for item in highlights if isinstance(item, str)
                    )
                    if isinstance(highlights, list)
                    else ""
                )
                score = 1.0 - (index / result_count)
                candidate = {
                    "title": _clean_term(raw_result.get("title")),
                    "url": url,
                    "content": content,
                    "score": score,
                    "provider_rank": index + 1,
                    "source_queries": [query],
                }
                existing = merged.get(dedupe_key)
                if existing is None:
                    merged[dedupe_key] = candidate
                    continue
                if query not in existing["source_queries"]:
                    existing["source_queries"].append(query)
                if score > _finite_number(existing.get("score")):
                    candidate["source_queries"] = existing["source_queries"]
                    merged[dedupe_key] = candidate

        ranked = sorted(
            merged.values(),
            key=lambda item: (-_finite_number(item.get("score")), item["url"]),
        )[:max_results]
        return {
            "provider": self.provider_name,
            "queries": clean_queries,
            "search_depth": "auto",
            "max_results": max_results,
            "include_domains": clean_include_domains,
            "exclude_domains": clean_exclude_domains,
            "results": ranked,
            "usage": {
                "credits": credits,
                "cost_dollars": round(cost_dollars, 9),
            },
            "request_ids": request_ids,
        }

    def extract_urls(
        self,
        urls: Sequence[str],
        query: str,
    ) -> dict[str, Any]:
        self.last_operation_requests = 0
        self.last_operation_completed_requests = 0
        self.last_operation_known_credits = 0
        if isinstance(urls, (str, bytes)) or not isinstance(urls, Sequence):
            raise TypeError("urls must be a sequence of URL strings")
        clean_query = _clean_query(query)

        submitted: list[str] = []
        submitted_by_key: dict[str, str] = {}
        seen: set[str] = set()
        for raw_url in urls:
            normalized, dedupe_key = _canonical_url(raw_url, validate_public=True)
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                submitted.append(normalized)
                submitted_by_key[dedupe_key] = normalized
        if not submitted:
            raise ValueError("at least one public HTTP(S) URL is required")
        if len(submitted) > MAX_EXTRACT_URLS:
            raise ValueError(f"at most {MAX_EXTRACT_URLS} unique URLs may be extracted")

        self.last_operation_requests = 1
        response = self._post(
            "/contents",
            {
                "urls": submitted,
                "highlights": {
                    "query": clean_query,
                    "maxCharacters": MAX_HIGHLIGHT_CHARACTERS,
                },
                "text": {
                    "maxCharacters": max_extract_chars(),
                },
                "livecrawlTimeout": min(15_000, max(1_000, int(self.timeout * 500))),
            },
        )
        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise ExaResponseError("Exa contents results must be a list")

        results: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, Mapping):
                continue
            result_url: str | None = None
            resolved_url: str | None = None
            for raw_candidate in (item.get("id"), item.get("url")):
                try:
                    normalized, dedupe_key = _canonical_url(
                        raw_candidate,
                        validate_public=True,
                    )
                except (TypeError, ValueError):
                    continue
                if raw_candidate == item.get("url"):
                    resolved_url = normalized
                if dedupe_key in submitted_by_key and result_url is None:
                    result_url = submitted_by_key[dedupe_key]
            if result_url is None:
                continue
            highlights = item.get("highlights")
            highlight_content = (
                "\n\n[...]\n\n".join(
                    part for part in highlights if isinstance(part, str)
                )
                if isinstance(highlights, list)
                else ""
            )
            full_text = item.get("text")
            content = (
                full_text
                if isinstance(full_text, str) and full_text.strip()
                else highlight_content
            )
            result: dict[str, Any] = {
                "url": result_url,
                "raw_content": content,
                "content_source": (
                    "exa_full_text"
                    if isinstance(full_text, str) and full_text.strip()
                    else "exa_highlights"
                ),
            }
            if resolved_url is not None and resolved_url != result_url:
                result["resolved_url"] = resolved_url
            if isinstance(item.get("title"), str):
                result["title"] = _clean_term(item["title"])
            if isinstance(item.get("favicon"), str):
                result["favicon"] = item["favicon"]
            results.append(result)

        failed_results: list[dict[str, str]] = []
        statuses = response.get("statuses")
        if isinstance(statuses, list):
            for item in statuses:
                if not isinstance(item, Mapping) or item.get("status") != "error":
                    continue
                raw_error = item.get("error")
                failed_results.append(
                    {
                        "url": str(item.get("id") or ""),
                        "error": (
                            str(raw_error.get("tag") or "extraction failed")
                            if isinstance(raw_error, Mapping)
                            else "extraction failed"
                        ),
                    }
                )

        credits = EXTRACT_BUDGET_UNITS_PER_BATCH
        self.last_operation_known_credits = credits
        self.last_operation_completed_requests = 1
        request_id = response.get("requestId")
        return {
            "provider": self.provider_name,
            "query": clean_query,
            "urls": submitted,
            "extract_depth": "full_text_with_highlights_fallback",
            "results": results,
            "failed_results": failed_results,
            "usage": {
                "credits": credits,
                "cost_dollars": round(_provider_cost(response), 9),
            },
            "request_id": request_id if isinstance(request_id, str) else None,
        }


__all__ = [
    "API_BASE_URL",
    "EXTRACT_CONTRACT_VERSION",
    "ExaClient",
    "ExaConfigError",
    "ExaError",
    "ExaHTTPError",
    "ExaNetworkError",
    "ExaQuotaExhaustedError",
    "ExaResponseError",
    "SEARCH_CONTRACT_VERSION",
]
