"""Validate the bounded AI-to-runtime decision contract."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .render import validate_public_http_url


OUTCOMES = frozenset(
    {"publish", "no_datasheet", "ambiguous", "insufficient_identity"}
)
SOURCE_TYPES = frozenset(
    {"manufacturer", "regulatory", "authorized", "mirror", "community"}
)
TRUSTED_TYPES = frozenset({"manufacturer", "regulatory", "authorized"})
_KNOWN_VARIANT_PREFIXES = ("hc",)
_EXPLICIT_REVISION_SUFFIX_PATTERN = (
    r"(?:"
    r"(?:[^\w\r\n]|_)+"
    r"(?:rev(?:ision)?|ver(?:sion)?)(?![^\W_])"
    r"(?:[^\w\r\n]|_)*"
    r"(?:"
    r"[^\W\d_]{1,3}\d{0,3}"
    r"|\d{1,4}[^\W\d_]{1,3}"
    r"|\d{1,4}(?:\.\d{1,3})*"
    r")"
    r"(?![^\W_])"
    r")?"
)
_PRODUCT_CATEGORY_ALIASES = {
    "heat pump": "热泵",
    "heat pumps": "热泵",
    "air source heat pump": "热泵",
    "air-to-water heat pump": "热泵",
    "ground source heat pump": "热泵",
    "water source heat pump": "热泵",
    "空气源热泵": "热泵",
    "空气能热泵": "热泵",
    "地源热泵": "热泵",
    "水源热泵": "热泵",
    "solar inverter": "光伏逆变器",
    "photovoltaic inverter": "光伏逆变器",
    "pv inverter": "光伏逆变器",
    "太阳能逆变器": "光伏逆变器",
}
_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "product_id",
        "lease_token",
        "outcome",
        "confidence",
        "manufacturer",
        "model",
        "product_category",
        "summary",
        "review_summary",
        "review_evidence_urls",
        "decision_notes",
        "datasheets",
        "sources",
        "facts",
        "conflicts",
    }
)


class DecisionError(ValueError):
    """Raised when an AI decision proposal is incomplete or unsafe to apply."""


def canonical_product_category(value: str) -> str:
    """Normalize common source-language aliases into broad reader categories."""

    cleaned = " ".join(value.split())
    return _PRODUCT_CATEGORY_ALIASES.get(cleaned.casefold(), cleaned)


def _text(value: Any, field: str, *, required: bool = False, limit: int = 2000) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise DecisionError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise DecisionError(f"{field} is required")
    if len(value) > limit:
        raise DecisionError(f"{field} exceeds {limit} characters")
    return value


def _confidence(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionError(f"{field} must be a number")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise DecisionError(f"{field} must be between 0 and 1")
    return number


def _scalar(value: Any, field: str) -> Any:
    if isinstance(value, (dict, list)) or value is None:
        raise DecisionError(f"{field} must be scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise DecisionError(f"{field} must be finite")
    return value


def identity_key(value: str) -> str:
    """Normalize a model identity for exact punctuation-insensitive matching."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def text_contains_exact_identity(expected: str, body: str) -> bool:
    """Match a complete model with flexible internal punctuation, not variants."""

    if not isinstance(expected, str) or not isinstance(body, str):
        return False
    expected_text = unicodedata.normalize("NFKC", expected).casefold()
    body_text = unicodedata.normalize("NFKC", body).casefold()
    tokens = re.findall(r"[^\W_]+", expected_text, flags=re.UNICODE)
    if not tokens:
        return False
    pattern = (
        r"(?<![^\W_])(?<![^\W_][.\-_/])"
        + r"[\W_]*".join(re.escape(token) for token in tokens)
        + r"(?![^\W_])(?![.\-_/][^\W_])"
    )
    return re.search(pattern, body_text, flags=re.UNICODE) is not None


def _variable_model_token_pattern(token: str) -> str:
    if token.isdecimal():
        # Consume a directly attached alphabetic revision as part of the
        # candidate (for example, PV-42A must not collapse to PV-42).
        return r"\d+[^\W_]*"
    if any(character.isdecimal() for character in token):
        return r"[^\W_]*\d[^\W_]*"
    return r"[^\W\d_]+"


def _contains_compact_affix(expected: str, body: str) -> bool:
    """Detect a compact prefix/suffix split from a model by OCR or layout."""

    expected_text = unicodedata.normalize("NFKC", expected)
    body_text = unicodedata.normalize("NFKC", body)
    tokens = re.findall(r"[^\W_]+", expected_text, flags=re.UNICODE)
    if not tokens:
        return False
    expected_core = r"[\W_]*".join(re.escape(token) for token in tokens)
    expected_pattern = (
        r"(?<![^\W_])(?<![^\W_][.\-_/])"
        + expected_core
        + r"(?![^\W_])(?![.\-_/][^\W_])"
    )
    # Case is intentionally preserved for the suffix. Technical suffixes are
    # conventionally uppercase; requiring that avoids treating the English
    # article in "PV-42 a documented product" as a model variant.
    bare_suffix = (
        r"(?:[A-Z]|[A-Z]{1,3}\d{1,3}|\d{1,3}[A-Z]{1,3})"
    )
    parenthesized_suffix = bare_suffix
    suffix_pattern = (
        r"(?i:"
        + expected_pattern
        + r")"
        + r"(?:"
        + r"[^\S\r\n]+"
        + bare_suffix
        + r"(?![A-Za-z0-9_])"
        + r"|[^\S\r\n]*\([^\S\r\n]*"
        + parenthesized_suffix
        + r"[^\S\r\n]*\)"
        + r")"
    )
    known_prefixes = "|".join(
        re.escape(prefix) for prefix in _KNOWN_VARIANT_PREFIXES
    )
    known_prefix_pattern = (
        r"(?<![A-Za-z0-9_])"
        + r"(?i:(?:"
        + known_prefixes
        + r"))"
        + r"(?:"
        + r"(?i:"
        + expected_core
        + r")"
        + r"|(?:[^\w\r\n]|_)+"
        + r"(?i:"
        + expected_core
        + r")"
        + r")"
        + r"(?![^\W_])(?![.\-_/][^\W_])"
    )
    return any(
        re.search(candidate, body_text, flags=re.UNICODE) is not None
        for candidate in (
            suffix_pattern,
            known_prefix_pattern,
        )
    )


def text_contains_competing_identity(expected: str, body: str) -> bool:
    """Detect sibling model/revision identifiers sharing the expected stem."""

    if not isinstance(expected, str) or not isinstance(body, str):
        return False
    expected_text = unicodedata.normalize("NFKC", expected).casefold()
    body_text = unicodedata.normalize("NFKC", body).casefold()
    tokens = re.findall(r"[^\W_]+", expected_text, flags=re.UNICODE)
    if not tokens:
        return False
    if len(tokens) == 1:
        prefix_match = re.match(r"[^\W\d_]+", tokens[0], flags=re.UNICODE)
        if (
            prefix_match is None
            or prefix_match.end() == len(tokens[0])
            or len(prefix_match.group()) < 2
        ):
            return False
        pattern = (
            r"(?<![^\W_])"
            + re.escape(prefix_match.group())
            + r"[^\W_]*\d[^\W_]*"
            + r"(?:[.\-_/][^\W_]+)*"
        )
    else:
        pattern = (
            r"(?<![^\W_])"
            + re.escape(tokens[0])
            + "".join(
                r"[\W_]*" + _variable_model_token_pattern(token)
                for token in tokens[1:]
            )
            + r"(?:[.\-_/][^\W_]+)*"
        )
    # Also treat an explicit, immediately following compact revision label as
    # part of the model candidate. The bounded value grammar catches forms
    # such as "PV-42 Rev.B" and "PV-42 Version 2.1" without interpreting
    # ordinary prose such as "PV-42 revision history" as a sibling model.
    pattern += _EXPLICIT_REVISION_SUFFIX_PATTERN
    expected_key = identity_key(expected)
    if any(
        identity_key(match.group()) != expected_key
        for match in re.finditer(pattern, body_text, flags=re.UNICODE)
    ):
        return True
    return _contains_compact_affix(expected, body)


def _hostname_matches_domain(hostname: str, domain: str) -> bool:
    host = hostname.rstrip(".").casefold()
    trusted = domain.rstrip(".").casefold()
    return host == trusted or host.endswith(f".{trusted}")


def _url_has_trusted_domain(url: str, trusted_domains: frozenset[str]) -> bool:
    hostname = urlsplit(url).hostname or ""
    return any(
        _hostname_matches_domain(hostname, domain)
        for domain in trusted_domains
    )


def _fact_value_present(value: Any, unit: str, normalized_body: str) -> bool:
    value_key = identity_key(str(value))
    unit_key = identity_key(unit)
    if not value_key:
        return False
    target = f"{value_key}{unit_key}" if unit_key else value_key
    return target in normalized_body


def _quote_supports_fact(
    quote: str,
    *,
    name: str,
    value: Any,
    unit: str,
    expected_product_name: str,
    normalized_body: str,
) -> bool:
    quote_key = identity_key(quote)
    name_key = identity_key(name)
    return (
        len(quote_key) >= 8
        and len(name_key) >= 2
        and quote_key in normalized_body
        and name_key in quote_key
        and _fact_value_present(value, unit, quote_key)
        and text_contains_exact_identity(expected_product_name, quote)
        and not text_contains_competing_identity(expected_product_name, quote)
    )


def validate_public_url(value: Any, field: str) -> str:
    url = _text(value, field, required=True, limit=2048)
    try:
        safe_url = validate_public_http_url(url)
        parsed = urlsplit(safe_url)
        port = parsed.port
    except ValueError as exc:
        raise DecisionError(f"{field} is not a valid public URL: {exc}") from exc
    if port not in {None, 80, 443}:
        raise DecisionError(f"{field} uses a non-standard port")
    return safe_url


def _require_list(decision: Mapping[str, Any], name: str, limit: int) -> list[Any]:
    value = decision.get(name)
    if not isinstance(value, list):
        raise DecisionError(f"{name} must be an array")
    if len(value) > limit:
        raise DecisionError(f"{name} may contain at most {limit} entries")
    return value


def validate_decision(
    raw: Mapping[str, Any],
    *,
    expected_product_id: str,
    expected_lease_token: str,
    minimum_confidence: float = 0.85,
    minimum_fact_confidence: float = 0.8,
    mirrors_allowed: bool = False,
    allowed_evidence_urls: set[str] | None = None,
    trusted_source_domains: set[str] | frozenset[str] | None = None,
    expected_product_name: str | None = None,
    evidence_text_by_url: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a normalized decision or reject it before any Wiki mutation."""

    if not isinstance(raw, Mapping):
        raise DecisionError("decision must be a JSON object")
    unknown = set(raw) - _TOP_LEVEL
    if unknown:
        raise DecisionError(f"decision has unknown fields: {', '.join(sorted(unknown))}")
    if raw.get("schema_version") != "1":
        raise DecisionError("schema_version must be '1'")

    product_id = _text(raw.get("product_id"), "product_id", required=True, limit=200)
    lease_token = _text(raw.get("lease_token"), "lease_token", required=True, limit=200)
    if product_id != expected_product_id:
        raise DecisionError("product_id does not match the active lease")
    if lease_token != expected_lease_token:
        raise DecisionError("lease_token does not match the active lease")

    outcome = raw.get("outcome")
    if outcome not in OUTCOMES:
        raise DecisionError("outcome is invalid")
    confidence = _confidence(raw.get("confidence"), "confidence")

    trusted_domains = frozenset(
        domain.strip().rstrip(".").casefold()
        for domain in (trusted_source_domains or set())
        if isinstance(domain, str) and domain.strip()
    )

    datasheets: list[dict[str, Any]] = []
    for index, item in enumerate(_require_list(raw, "datasheets", 10)):
        if not isinstance(item, Mapping):
            raise DecisionError(f"datasheets[{index}] must be an object")
        unknown_item = set(item) - {"url", "title", "source_type", "is_primary"}
        if unknown_item:
            raise DecisionError(f"datasheets[{index}] has unknown fields")
        source_type = item.get("source_type")
        if source_type not in SOURCE_TYPES:
            raise DecisionError(f"datasheets[{index}].source_type is invalid")
        if not isinstance(item.get("is_primary"), bool):
            raise DecisionError(f"datasheets[{index}].is_primary must be boolean")
        url = validate_public_url(item.get("url"), f"datasheets[{index}].url")
        if (
            outcome == "publish"
            and source_type in TRUSTED_TYPES
            and not _url_has_trusted_domain(url, trusted_domains)
        ):
            raise DecisionError(
                f"datasheets[{index}] claims a trusted source type for "
                "a domain not approved by the operator"
            )
        datasheets.append(
            {
                "url": url,
                "title": _text(
                    item.get("title"), f"datasheets[{index}].title", required=True, limit=500
                ),
                "source_type": source_type,
                "is_primary": item["is_primary"],
            }
        )

    sources: list[dict[str, Any]] = []
    for index, item in enumerate(_require_list(raw, "sources", 20)):
        if not isinstance(item, Mapping):
            raise DecisionError(f"sources[{index}] must be an object")
        unknown_item = set(item) - {"url", "title", "source_type"}
        if unknown_item:
            raise DecisionError(f"sources[{index}] has unknown fields")
        source_type = item.get("source_type")
        if source_type not in SOURCE_TYPES:
            raise DecisionError(f"sources[{index}].source_type is invalid")
        url = validate_public_url(item.get("url"), f"sources[{index}].url")
        if (
            outcome == "publish"
            and source_type in TRUSTED_TYPES
            and not _url_has_trusted_domain(url, trusted_domains)
        ):
            raise DecisionError(
                f"sources[{index}] claims a trusted source type for "
                "a domain not approved by the operator"
            )
        sources.append(
            {
                "url": url,
                "title": _text(
                    item.get("title"), f"sources[{index}].title", required=True, limit=500
                ),
                "source_type": source_type,
            }
        )

    declared_urls = {item["url"] for item in datasheets + sources}
    if allowed_evidence_urls is not None:
        normalized_allowed = {
            validate_public_url(url, "allowed_evidence_urls")
            for url in allowed_evidence_urls
        }
        undeclared = declared_urls - normalized_allowed
        if undeclared:
            raise DecisionError("decision cites URLs not extracted for this lease")

    normalized_evidence_text: dict[str, str] = {}
    if evidence_text_by_url is not None:
        if not isinstance(evidence_text_by_url, Mapping) or len(evidence_text_by_url) > 5:
            raise DecisionError(
                "evidence_text_by_url must map at most 5 URLs to extracted text"
            )
        for raw_url, body in evidence_text_by_url.items():
            url = validate_public_url(raw_url, "evidence_text_by_url")
            if not isinstance(body, str) or not body.strip() or len(body) > 200_000:
                raise DecisionError(
                    "evidence_text_by_url values must be bounded non-empty strings"
                )
            if outcome == "publish" and (
                not text_contains_exact_identity(
                    expected_product_name or "",
                    body,
                )
                or text_contains_competing_identity(
                    expected_product_name or "",
                    body,
                )
            ):
                raise DecisionError(
                    "publish evidence must contain the matching model identity "
                    "and no detected sibling model or revision"
                )
            normalized_evidence_text[url] = identity_key(body)

    summary = _text(raw.get("summary"), "summary", limit=2000)
    product_category = canonical_product_category(
        _text(raw.get("product_category"), "product_category", limit=100)
    )
    review_summary = _text(
        raw.get("review_summary"), "review_summary", limit=1500
    )
    review_evidence_raw = raw.get("review_evidence_urls", [])
    if not isinstance(review_evidence_raw, list) or len(review_evidence_raw) > 5:
        raise DecisionError("review_evidence_urls must be an array of at most 5 URLs")
    review_evidence_urls = [
        validate_public_url(url, "review_evidence_urls")
        for url in review_evidence_raw
    ]
    if review_summary and len(set(review_evidence_urls)) < 2:
        raise DecisionError(
            "review_summary requires at least two review evidence URLs"
        )
    if review_evidence_urls and not review_summary:
        raise DecisionError("review_evidence_urls require review_summary")
    if not set(review_evidence_urls) <= declared_urls:
        raise DecisionError(
            "review_evidence_urls must reference declared sources"
        )

    source_types_by_url = {
        item["url"]: item["source_type"] for item in datasheets + sources
    }
    community_urls = {
        url for url, source_type in source_types_by_url.items()
        if source_type == "community"
    }
    if any(item["source_type"] == "community" for item in datasheets):
        raise DecisionError("community sources cannot be datasheets")
    if not community_urls <= set(review_evidence_urls):
        raise DecisionError(
            "community sources may be cited only as review evidence"
        )

    facts: list[dict[str, Any]] = []
    fact_name_keys: set[str] = set()
    for index, item in enumerate(_require_list(raw, "facts", 100)):
        if not isinstance(item, Mapping):
            raise DecisionError(f"facts[{index}] must be an object")
        unknown_item = set(item) - {
            "name",
            "category",
            "value",
            "unit",
            "confidence",
            "evidence_urls",
            "evidence_quotes",
        }
        if unknown_item:
            raise DecisionError(f"facts[{index}] has unknown fields")
        value = _scalar(item.get("value"), f"facts[{index}].value")
        evidence_raw = item.get("evidence_urls")
        if not isinstance(evidence_raw, list) or not evidence_raw:
            raise DecisionError(f"facts[{index}].evidence_urls must be non-empty")
        evidence = [
            validate_public_url(url, f"facts[{index}].evidence_urls")
            for url in evidence_raw
        ]
        fact_confidence = _confidence(
            item.get("confidence"), f"facts[{index}].confidence"
        )
        if fact_confidence < minimum_fact_confidence:
            raise DecisionError(
                f"facts[{index}].confidence is below the configured threshold"
            )
        if not set(evidence) <= declared_urls:
            raise DecisionError(
                f"facts[{index}].evidence_urls must reference declared sources"
            )
        if set(evidence) & community_urls:
            raise DecisionError(
                f"facts[{index}] cannot use community review evidence"
            )
        name = _text(
            item.get("name"),
            f"facts[{index}].name",
            required=True,
            limit=200,
        )
        name_key = identity_key(name)
        if not name_key or name_key in fact_name_keys:
            raise DecisionError("fact names must be non-empty and unique")
        fact_name_keys.add(name_key)
        unit = (
            _text(item.get("unit"), f"facts[{index}].unit", limit=80)
            if "unit" in item
            else ""
        )
        quotes_raw = item.get("evidence_quotes")
        if quotes_raw is None and outcome != "publish":
            quotes_raw = []
        if not isinstance(quotes_raw, list) or len(quotes_raw) > 5:
            raise DecisionError(
                f"facts[{index}].evidence_quotes must be an array of at most 5 entries"
            )
        if outcome == "publish" and not quotes_raw:
            raise DecisionError(
                f"facts[{index}].evidence_quotes must contain 1-5 entries"
            )
        evidence_quotes: list[dict[str, str]] = []
        for quote_index, quote_item in enumerate(quotes_raw):
            if not isinstance(quote_item, Mapping):
                raise DecisionError(
                    f"facts[{index}].evidence_quotes[{quote_index}] "
                    "must be an object"
                )
            if set(quote_item) != {"url", "quote"}:
                raise DecisionError(
                    f"facts[{index}].evidence_quotes[{quote_index}] "
                    "must contain only url and quote"
                )
            quote_url = validate_public_url(
                quote_item.get("url"),
                f"facts[{index}].evidence_quotes[{quote_index}].url",
            )
            quote = _text(
                quote_item.get("quote"),
                f"facts[{index}].evidence_quotes[{quote_index}].quote",
                required=True,
                limit=500,
            )
            if quote_url not in evidence:
                raise DecisionError(
                    f"facts[{index}].evidence_quotes URLs must also be "
                    "listed in evidence_urls"
                )
            if outcome == "publish" and not _quote_supports_fact(
                quote,
                name=name,
                value=value,
                unit=unit,
                expected_product_name=expected_product_name or "",
                normalized_body=normalized_evidence_text.get(quote_url, ""),
            ):
                raise DecisionError(
                    f"facts[{index}].evidence_quotes[{quote_index}] is not "
                    "an exact supporting extract span"
                )
            evidence_quotes.append({"url": quote_url, "quote": quote})
        fact = {
            "name": name,
            "value": value,
            "confidence": fact_confidence,
            "evidence_urls": evidence,
            "evidence_quotes": evidence_quotes,
        }
        if "unit" in item:
            fact["unit"] = unit
        if "category" in item:
            fact["category"] = _text(
                item.get("category"), f"facts[{index}].category", limit=100
            )
        facts.append(fact)

    conflicts = _require_list(raw, "conflicts", 100)
    normalized_conflicts: list[dict[str, Any]] = []
    conflict_fields: set[str] = set()
    for index, item in enumerate(conflicts):
        if not isinstance(item, Mapping):
            raise DecisionError(f"conflicts[{index}] must be an object")
        unknown_item = set(item) - {"field", "values", "source_urls"}
        if unknown_item:
            raise DecisionError(f"conflicts[{index}] has unknown fields")
        field = _text(
            item.get("field"), f"conflicts[{index}].field", required=True, limit=200
        )
        conflict_fields.add(identity_key(field))
        if not isinstance(item.get("values"), list) or len(item["values"]) < 2:
            raise DecisionError(f"conflicts[{index}].values needs at least two values")
        values = [
            _scalar(value, f"conflicts[{index}].values")
            for value in item["values"]
        ]
        urls = item.get("source_urls")
        if not isinstance(urls, list) or not urls:
            raise DecisionError(f"conflicts[{index}].source_urls must be non-empty")
        normalized_urls = [
            validate_public_url(url, f"conflicts[{index}].source_urls") for url in urls
        ]
        if not set(normalized_urls) <= declared_urls:
            raise DecisionError(
                f"conflicts[{index}].source_urls must reference declared sources"
            )
        normalized_conflicts.append(
            {"field": field, "values": values, "source_urls": normalized_urls}
        )

    disputed_facts = {
        identity_key(item["name"]) for item in facts
    } & conflict_fields
    if disputed_facts:
        raise DecisionError("conflicted fields must not also appear as verified facts")

    if outcome == "publish":
        if confidence < minimum_confidence:
            raise DecisionError("publish confidence is below the configured threshold")
        if not product_category:
            raise DecisionError(
                "publish requires a public product_category; internal family codes "
                "must not be used"
            )
        if not summary:
            raise DecisionError("publish requires a user-facing product summary")
        expected_identity = identity_key(expected_product_name or "")
        proposed_identity = identity_key(
            _text(raw.get("model"), "model", required=True, limit=300)
        )
        if not expected_identity or proposed_identity != expected_identity:
            raise DecisionError(
                "publish model does not exactly match the catalogue product name"
            )
        if len(facts) < 5:
            raise DecisionError(
                "publish requires at least 5 cited specification facts"
            )
        primary = [item for item in datasheets if item["is_primary"]]
        if not primary:
            raise DecisionError("publish requires a primary datasheet")
        trusted_primary = [
            item
            for item in primary
            if item["source_type"] in TRUSTED_TYPES
            and _url_has_trusted_domain(item["url"], trusted_domains)
        ]
        if not trusted_primary:
            mirror_domains = {
                ".".join((urlsplit(item["url"]).hostname or "").split(".")[-2:])
                for item in primary
                if item["source_type"] == "mirror"
            }
            if (
                not mirrors_allowed
                or len(primary) < 2
                or len(mirror_domains) < 2
                or any(item["source_type"] != "mirror" for item in primary)
            ):
                raise DecisionError(
                    "primary datasheet needs a trusted source or two enabled independent mirrors"
                )
        trusted_fact_urls = {
            item["url"]
            for item in datasheets + sources
            if item["source_type"] in TRUSTED_TYPES
            and _url_has_trusted_domain(item["url"], trusted_domains)
        }
        for index, fact in enumerate(facts):
            quoted_urls = {
                item["url"] for item in fact["evidence_quotes"]
            }
            if trusted_primary:
                trusted_evidence = quoted_urls & trusted_fact_urls
                if not trusted_evidence:
                    raise DecisionError(
                        f"facts[{index}] needs evidence from an "
                        "operator-approved trusted domain"
                    )
            else:
                matching_mirror_domains = {
                    ".".join((urlsplit(url).hostname or "").split(".")[-2:])
                    for url in quoted_urls
                    if source_types_by_url.get(url) == "mirror"
                }
                if len(matching_mirror_domains) < 2:
                    raise DecisionError(
                        f"facts[{index}] needs its value in two independent "
                        "mirror extracts"
                    )

    return {
        "schema_version": "1",
        "product_id": product_id,
        "lease_token": lease_token,
        "outcome": outcome,
        "confidence": confidence,
        "manufacturer": _text(raw.get("manufacturer"), "manufacturer", limit=300),
        "model": _text(raw.get("model"), "model", limit=300),
        "product_category": product_category,
        "summary": summary,
        "review_summary": review_summary,
        "review_evidence_urls": review_evidence_urls,
        "decision_notes": _text(
            raw.get("decision_notes"), "decision_notes", required=True, limit=1000
        ),
        "datasheets": datasheets,
        "sources": sources,
        "facts": facts,
        "conflicts": normalized_conflicts,
    }


__all__ = [
    "DecisionError",
    "canonical_product_category",
    "identity_key",
    "text_contains_competing_identity",
    "text_contains_exact_identity",
    "validate_decision",
    "validate_public_url",
]
