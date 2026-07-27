"""Validate the bounded AI-to-runtime decision contract."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .config import is_shared_source_hostname
from .render import validate_public_http_url


OUTCOMES = frozenset(
    {
        "publish",
        "no_datasheet",
        "ambiguous",
        "insufficient_identity",
        "out_of_scope",
    }
)
SOURCE_TYPES = frozenset(
    {"manufacturer", "regulatory", "authorized", "mirror", "community"}
)
TRUSTED_TYPES = frozenset({"manufacturer", "regulatory", "authorized"})
_GENERIC_MANUFACTURER_TOKENS = frozenset(
    {
        "ag",
        "advanced",
        "bv",
        "clean",
        "co",
        "company",
        "corp",
        "corporation",
        "electric",
        "electronics",
        "energy",
        "general",
        "gmbh",
        "global",
        "green",
        "group",
        "inc",
        "industrial",
        "industries",
        "international",
        "limited",
        "llc",
        "ltd",
        "manufacturing",
        "marketplace",
        "new",
        "official",
        "oy",
        "power",
        "products",
        "renewable",
        "sa",
        "services",
        "smart",
        "solar",
        "solutions",
        "spa",
        "srl",
        "systems",
        "technologies",
        "technology",
        "the",
        "via",
        "world",
    }
)
_COMMON_COUNTRY_SECOND_LEVELS = frozenset(
    {"ac", "co", "com", "edu", "gov", "net", "org"}
)
_LEGAL_ENTITY_TOKENS = frozenset(
    {
        "ag",
        "bv",
        "co",
        "company",
        "corp",
        "corporation",
        "gmbh",
        "group",
        "inc",
        "limited",
        "llc",
        "ltd",
        "oy",
        "sa",
        "spa",
        "srl",
    }
)
_KNOWN_VARIANT_PREFIXES = ("hc",)
_OUT_OF_SCOPE_ENGLISH_TYPE_RE = re.compile(
    r"(?<!\w)(?:"
    r"fasteners?|screws?|bolts?|nuts?|washers?|rivets?|"
    r"anchors?|nails?|cotter\s+pins?|retaining\s+rings?|"
    r"hose\s+clamps?|cable\s+ties?|zip\s+ties?|"
    r"o[\s-]?rings?|gaskets?|consumables?"
    r")(?!\w)",
    flags=re.IGNORECASE,
)
_OUT_OF_SCOPE_CJK_TYPE_TERMS = (
    "螺丝",
    "螺钉",
    "螺栓",
    "螺母",
    "垫圈",
    "紧固件",
    "铆钉",
    "卡箍",
    "开口销",
    "挡圈",
    "扎带",
    "密封圈",
    "垫片",
    "耗材",
)
_IN_SCOPE_ENGLISH_TYPE_RE = re.compile(
    r"(?<!\w)(?:"
    r"solar|photovoltaic|pv\s+module|solar\s+module|solar\s+panel|"
    r"inverters?|batter(?:y|ies)|energy\s+storage|heat\s+pumps?|"
    r"compressors?|chargers?|converters?|power\s+suppl(?:y|ies)"
    r")(?!\w)",
    flags=re.IGNORECASE,
)
_IN_SCOPE_CJK_TYPE_TERMS = (
    "光伏",
    "太阳能组件",
    "太阳能板",
    "逆变器",
    "电池",
    "储能",
    "热泵",
    "压缩机",
    "充电器",
    "变流器",
    "电源",
)
_OUT_OF_SCOPE_NEGATION_RE = re.compile(
    r"(?<!\w)(?:"
    r"not|isn['’]?t|aren['’]?t"
    r")(?!\w)",
    flags=re.IGNORECASE,
)
_OUT_OF_SCOPE_ACCESSORY_RE = re.compile(
    r"(?<!\w)(?:"
    r"includes?|contains?|uses?|with|supplied\s+with|mounting"
    r")(?!\w)",
    flags=re.IGNORECASE,
)
_OUT_OF_SCOPE_RELATION_RE = re.compile(
    r"(?<!\w)(?:"
    r"product\s*type|item\s*type|category|type|description"
    r")\s*[:=\-]\s*.{0,80}(?:"
    r"fasteners?|screws?|bolts?|nuts?|washers?|rivets?|"
    r"anchors?|nails?|cotter\s+pins?|retaining\s+rings?|"
    r"hose\s+clamps?|cable\s+ties?|zip\s+ties?|"
    r"o[\s-]?rings?|gaskets?|consumables?"
    r")(?!\w)",
    flags=re.IGNORECASE,
)
_OUT_OF_SCOPE_IS_A_RELATION_RE = re.compile(
    r"(?<!\w)(?:is|are)\s+(?:an?\s+)?(?:"
    r"fasteners?|screws?|bolts?|nuts?|washers?|rivets?|"
    r"anchors?|nails?|cotter\s+pins?|retaining\s+rings?|"
    r"hose\s+clamps?|cable\s+ties?|zip\s+ties?|"
    r"o[\s-]?rings?|gaskets?|consumables?"
    r")(?!\w)",
    flags=re.IGNORECASE,
)
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
_MODEL_FRAGMENT_RE = re.compile(
    r"(?<!\w)[^\W_]+(?:[./_-][^\W_]+)*(?!\w)",
    flags=re.UNICODE,
)
_MEASUREMENT_FRAGMENT_RE = re.compile(
    r"\d+(?:[.,]\d+)?(?:"
    r"w|kw|mw|v|kv|a|ma|ah|wh|kwh|va|kva|"
    r"mm|cm|m|g|kg|hz|khz|mhz"
    r")",
    flags=re.IGNORECASE,
)
_ELLIPSIS_RE = re.compile(r"\s*(?:\.{3,}|…+)\s*")
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
        "classification_evidence_urls",
        "classification_evidence_quotes",
        "decision_notes",
        "datasheets",
        "sources",
        "facts",
        "conflicts",
    }
)


class DecisionError(ValueError):
    """Raised when an AI decision proposal is incomplete or unsafe to apply."""


class SourceVerificationError(DecisionError):
    """Raised when publication evidence cannot verify its claimed authority."""


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


def _is_distinctive_model_identity(value: str) -> bool:
    key = identity_key(value)
    return (
        len(key) >= 3
        and any(character.isalpha() for character in key)
        and any(character.isdecimal() for character in key)
        and _MEASUREMENT_FRAGMENT_RE.fullmatch(key) is None
    )


def _distinctive_model_fragments(value: str) -> tuple[str, ...]:
    fragments: list[str] = []
    seen: set[str] = set()
    for match in _MODEL_FRAGMENT_RE.finditer(value):
        fragment = match.group(0)
        key = identity_key(fragment)
        if (
            key not in seen
            and _is_distinctive_model_identity(fragment)
        ):
            seen.add(key)
            fragments.append(fragment)
    return tuple(fragments)


def _looks_like_organization_name(value: str) -> bool:
    tokens = [
        identity_key(token)
        for token in re.findall(r"\S+", value, flags=re.UNICODE)
    ]
    return bool(tokens) and tokens[-1] in _LEGAL_ENTITY_TOKENS


def _looks_like_product_description(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return (
        _IN_SCOPE_ENGLISH_TYPE_RE.search(normalized) is not None
        or _OUT_OF_SCOPE_ENGLISH_TYPE_RE.search(normalized) is not None
        or any(term in normalized for term in _IN_SCOPE_CJK_TYPE_TERMS)
        or any(term in normalized for term in _OUT_OF_SCOPE_CJK_TYPE_TERMS)
    )


def preferred_catalogue_model(
    product_id: Any,
    product_name: Any,
    *,
    allow_product_id: bool = False,
) -> str:
    """Choose a public model hint, promoting the stable key only by opt-in."""

    name = " ".join(str(product_name or "").split())
    stable_id = " ".join(str(product_id or "").split())
    if not name:
        return ""
    name_key = identity_key(name)
    if (
        not name_key
        or name_key.isdecimal()
        or _looks_like_organization_name(name)
    ):
        return ""
    if _distinctive_model_fragments(name):
        return name
    if not _looks_like_product_description(name):
        return ""
    if allow_product_id and _is_distinctive_model_identity(stable_id):
        return stable_id
    return name


def model_matches_catalogue_identity(
    model: str,
    *,
    product_id: str,
    product_name: str,
) -> bool:
    """Require a proposed public model to be anchored in catalogue identity."""

    model_key = identity_key(model)
    name_key = identity_key(product_name)
    product_id_key = identity_key(product_id)
    if not model_key:
        return False
    if name_key and model_key == name_key:
        return True
    if not _is_distinctive_model_identity(model):
        return False
    if (
        product_id_key
        and model_key == product_id_key
        and _is_distinctive_model_identity(product_id)
    ):
        return True
    fragments = _distinctive_model_fragments(product_name)
    return (
        bool(fragments)
        and model_key in name_key
        and all(identity_key(fragment) in model_key for fragment in fragments)
    )


def text_contains_catalogue_identity(
    product_id: Any,
    product_name: Any,
    body: str,
) -> bool:
    """Recognize a catalogue-bound model in an extracted public document."""

    if not isinstance(body, str) or not body.strip():
        return False
    name = " ".join(str(product_name or "").split())
    stable_id = " ".join(str(product_id or "").split())
    fragments = _distinctive_model_fragments(name)
    return (
        bool(name)
        and text_contains_exact_identity(name, body)
    ) or (
        bool(fragments)
        and all(text_contains_exact_identity(fragment, body) for fragment in fragments)
    ) or (
        _is_distinctive_model_identity(stable_id)
        and text_contains_exact_identity(stable_id, body)
    )


def _flexible_whitespace_pattern(value: str) -> str:
    return r"\s+".join(
        re.escape(part)
        for part in re.split(r"\s+", value.strip())
        if part
    )


def _ground_extract_quote(
    proposed: str,
    body: str,
    *,
    maximum: int = 500,
) -> str | None:
    """Return an actual bounded source span for a model-proposed quote."""

    if not proposed or not body:
        return None
    exact_index = body.find(proposed)
    if exact_index >= 0:
        return proposed

    direct_pattern = _flexible_whitespace_pattern(proposed)
    if direct_pattern:
        direct = re.search(direct_pattern, body, flags=re.IGNORECASE)
        if direct is not None:
            span = body[direct.start():direct.end()].strip()
            return span if 0 < len(span) <= maximum else None

    normalized_body: list[str] = []
    raw_offsets: list[tuple[int, int]] = []
    for raw_index, character in enumerate(body):
        normalized_character = unicodedata.normalize(
            "NFKC",
            character,
        ).casefold()
        for normalized in normalized_character:
            if normalized.isalnum():
                normalized_body.append(normalized)
                raw_offsets.append((raw_index, raw_index + 1))
    normalized_proposed = identity_key(proposed)
    normalized_index = "".join(normalized_body).find(normalized_proposed)
    if normalized_proposed and normalized_index >= 0:
        raw_start = raw_offsets[normalized_index][0]
        raw_end = raw_offsets[
            normalized_index + len(normalized_proposed) - 1
        ][1]
        while (
            raw_end < len(body)
            and not body[raw_end].isalnum()
            and body[raw_end] not in "\r\n"
        ):
            raw_end += 1
        span = body[raw_start:raw_end].strip()
        if 0 < len(span) <= maximum:
            return span

    anchors = [
        anchor.strip()
        for anchor in _ELLIPSIS_RE.split(proposed)
        if len(identity_key(anchor)) >= 3
    ]
    if len(anchors) < 2:
        return None
    start: int | None = None
    end = 0
    for anchor in anchors:
        pattern = _flexible_whitespace_pattern(anchor)
        match = re.search(pattern, body[end:], flags=re.IGNORECASE)
        if match is None:
            return None
        absolute_start = end + match.start()
        absolute_end = end + match.end()
        if start is None:
            start = absolute_start
        end = absolute_end
        if start is not None and end - start > maximum:
            return None
    if start is None:
        return None
    span = body[start:end].strip()
    return span if 0 < len(span) <= maximum else None


def _mixed_model_token_pattern(token: str) -> str | None:
    """Vary digit runs while preserving a mixed model token's letter skeleton."""

    runs = re.findall(r"\d+|[^\W\d_]+", token, flags=re.UNICODE)
    if not any(run.isdecimal() for run in runs) or not any(
        not run.isdecimal() for run in runs
    ):
        return None
    return "".join(r"\d+" if run.isdecimal() else re.escape(run) for run in runs)


def _variable_model_token_pattern(token: str) -> str:
    if token.isdecimal():
        # Consume a directly attached alphabetic revision as part of the
        # candidate (for example, PV-42A must not collapse to PV-42).
        return r"\d+[^\W_]*"
    mixed_pattern = _mixed_model_token_pattern(token)
    if mixed_pattern is not None:
        # A complete candidate has enough surrounding model context to allow
        # both digit and letter revisions to vary. Require at least one of
        # each so decimal specification values cannot impersonate a token.
        return (
            r"(?:"
            r"[^\W\d_]+[^\W_]*\d[^\W_]*"
            r"|\d+[^\W_]*[^\W\d_][^\W_]*"
            r")"
        )
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


def _contains_abbreviated_sibling(tokens: list[str], body_text: str) -> bool:
    """Detect shortened sibling tokens such as bare ``6KTL`` beside ``8KTL``."""

    for token in tokens:
        variable_pattern = _mixed_model_token_pattern(token)
        if variable_pattern is None:
            continue
        letter_skeleton = "".join(
            run
            for run in re.findall(r"\d+|[^\W\d_]+", token, flags=re.UNICODE)
            if not run.isdecimal()
        )
        if len(letter_skeleton) < 2:
            continue
        pattern = r"(?<![^\W_])" + variable_pattern + r"(?![^\W_])"
        if any(
            identity_key(match.group()) != identity_key(token)
            for match in re.finditer(pattern, body_text, flags=re.UNICODE)
        ):
            return True
    return False


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
    return _contains_abbreviated_sibling(tokens, body_text) or _contains_compact_affix(
        expected,
        body,
    )


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


def _manufacturer_tokens(manufacturer: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", manufacturer).casefold()
    tokens = [
        identity_key(token)
        for token in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    ]
    meaningful = [
        token
        for token in tokens
        if len(token) >= 2 and token not in _GENERIC_MANUFACTURER_TOKENS
    ]
    return tuple(dict.fromkeys(meaningful))


def _registrable_domain_label(hostname: str) -> str:
    if is_shared_source_hostname(hostname):
        return ""
    labels = [
        label
        for label in hostname.rstrip(".").casefold().split(".")
        if label
    ]
    if len(labels) < 2:
        return ""
    if (
        len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in _COMMON_COUNTRY_SECOND_LEVELS
    ):
        return labels[-3]
    return labels[-2]


def _manufacturer_matches_domain(manufacturer: str, url: str) -> bool:
    hostname = urlsplit(url).hostname or ""
    domain_label = _registrable_domain_label(hostname)
    normalized = unicodedata.normalize("NFKC", manufacturer).casefold()
    domain_tokens: list[str] = []
    for raw_token in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE):
        token = identity_key(raw_token)
        if len(token) >= 2 and token not in _LEGAL_ENTITY_TOKENS:
            domain_tokens.append(token)
    meaningful_tokens = set(_manufacturer_tokens(manufacturer))
    if not domain_label or not domain_tokens or not meaningful_tokens:
        return False
    prefix_candidates = {
        "".join(domain_tokens[:index])
        for index in range(1, len(domain_tokens) + 1)
        if any(
            token in meaningful_tokens for token in domain_tokens[:index]
        )
    }
    hyphenated_prefix_candidates = {
        "-".join(domain_tokens[:index])
        for index in range(1, len(domain_tokens) + 1)
        if any(
            token in meaningful_tokens for token in domain_tokens[:index]
        )
    }
    return domain_label in (
        prefix_candidates | hyphenated_prefix_candidates | meaningful_tokens
    )


def _body_supports_manufacturer_identity(
    body: str,
    *,
    manufacturer_tokens: tuple[str, ...],
    expected_product_name: str,
) -> bool:
    body_tokens = {
        identity_key(token)
        for token in re.findall(
            r"[^\W_]+",
            unicodedata.normalize("NFKC", body).casefold(),
            flags=re.UNICODE,
        )
    }
    return text_contains_exact_identity(expected_product_name, body) and all(
        token in body_tokens for token in manufacturer_tokens
    )


def _auto_verified_manufacturer_urls(
    items: list[dict[str, Any]],
    *,
    manufacturer: str,
    expected_product_name: str,
    evidence_body_by_url: Mapping[str, str],
) -> set[str]:
    """Verify obvious official manufacturer hosts without a maintained map.

    The model proposes source types, but it cannot authorize an arbitrary host:
    the candidate must use HTTPS on a non-shared organization host, its
    registrable label must exactly match a manufacturer-derived label, its
    extract must contain every meaningful manufacturer token and the complete
    catalogue model identity, and a second extracted organization must
    independently corroborate that same identity.
    """

    manufacturer_tokens = _manufacturer_tokens(manufacturer)
    if not manufacturer_tokens:
        return set()
    declared_noncommunity_urls = {
        item["url"]
        for item in items
        if item["source_type"] != "community"
    }
    corroborating_labels = {
        _registrable_domain_label(urlsplit(url).hostname or "")
        for url, body in evidence_body_by_url.items()
        if (
            url in declared_noncommunity_urls
            and urlsplit(url).scheme.casefold() == "https"
            and _body_supports_manufacturer_identity(
                body,
                manufacturer_tokens=manufacturer_tokens,
                expected_product_name=expected_product_name,
            )
        )
    }
    corroborating_labels.discard("")
    verified: set[str] = set()
    for item in items:
        url = item["url"]
        body = evidence_body_by_url.get(url, "")
        parsed = urlsplit(url)
        candidate_label = _registrable_domain_label(parsed.hostname or "")
        if (
            item["source_type"] == "manufacturer"
            and parsed.scheme.casefold() == "https"
            and _manufacturer_matches_domain(manufacturer, url)
            and _body_supports_manufacturer_identity(
                body,
                manufacturer_tokens=manufacturer_tokens,
                expected_product_name=expected_product_name,
            )
            and any(
                label != candidate_label for label in corroborating_labels
            )
        ):
            verified.add(url)
    return verified


def _numeric_value_pattern(value: int | float) -> str:
    number = format(float(value), ".15g") if isinstance(value, float) else str(value)
    if "e" in number.casefold():
        mantissa, exponent = re.split(r"[eE]", number, maxsplit=1)
        mantissa_pattern = re.escape(mantissa).replace(r"\.", r"[.,]")
        return mantissa_pattern + r"[eE]" + re.escape(exponent)
    if "." in number:
        whole, fraction = number.split(".", maxsplit=1)
        return re.escape(whole) + r"[.,]" + re.escape(fraction) + r"0*"
    return re.escape(number) + (
        r"(?:[.,]0+)?"
        if isinstance(value, float)
        else ""
    )


def _bounded_literal_pattern(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    parts = [part for part in re.split(r"\s+", normalized) if part]
    return r"\s+".join(re.escape(part) for part in parts)


def _fact_value_present(value: Any, unit: str, body: str) -> bool:
    """Match a complete value/unit token, never a numeric substring."""

    if not isinstance(body, str) or not body:
        return False
    if isinstance(value, bool):
        value_pattern = re.escape(str(value))
        numeric = False
    elif isinstance(value, (int, float)):
        value_pattern = _numeric_value_pattern(value)
        numeric = True
    else:
        value_pattern = _bounded_literal_pattern(str(value))
        numeric = False
    if not value_pattern:
        return False

    unit_pattern = _bounded_literal_pattern(unit) if unit else ""
    prefix = r"(?<![\w.,+\-])"
    if numeric:
        value_suffix = r"(?![\d.,])"
    else:
        value_suffix = r"(?!\w)" if str(value)[-1:].isalnum() else ""
    if unit_pattern:
        unit_suffix = (
            r"(?![\w/])"
            if unit[-1:].isalnum()
            else r"(?![%\w/])"
        )
        suffix = rf"{value_suffix}\s*{unit_pattern}{unit_suffix}"
    else:
        literal_suffix = r"(?!/)" if str(value)[-1:].isalnum() else ""
        suffix = value_suffix + literal_suffix
    suffix += r"(?!\s*(?:/|;|\bor\b))"
    return re.search(
        prefix + value_pattern + suffix,
        unicodedata.normalize("NFKC", body),
        flags=re.IGNORECASE | re.UNICODE,
    ) is not None


def _fact_label_tail(quote: str, name: str) -> str | None:
    pattern = _flexible_whitespace_pattern(
        unicodedata.normalize("NFKC", name)
    )
    if not pattern:
        return None
    normalized_quote = unicodedata.normalize("NFKC", quote)
    match = re.search(pattern, normalized_quote, flags=re.IGNORECASE)
    return normalized_quote[match.end():] if match is not None else None


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
    value_region = _fact_label_tail(quote, name)
    return (
        len(quote_key) >= 8
        and len(name_key) >= 2
        and quote_key in normalized_body
        and name_key in quote_key
        and value_region is not None
        and _fact_value_present(value, unit, value_region)
        and text_contains_exact_identity(expected_product_name, quote)
        and not text_contains_competing_identity(expected_product_name, quote)
    )


def _table_row_cells(row: str) -> list[str] | None:
    """Parse one explicit Markdown/TSV row; prose spacing is not structural."""

    lines = [line.strip() for line in row.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    line = lines[0]
    if "|" in line:
        cells = [cell.strip() for cell in line.split("|")]
        if cells and not cells[0]:
            cells.pop(0)
        if cells and not cells[-1]:
            cells.pop()
    elif "\t" in line:
        cells = [cell.strip() for cell in line.split("\t")]
    else:
        return None
    return cells if len(cells) >= 2 and all(cells) else None


def _cell_has_unambiguous_fact_value(
    cell: str,
    *,
    value: Any,
    unit: str,
) -> bool:
    if not _fact_value_present(value, unit, cell):
        return False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return re.search(r"\s(?:/|;|\bor\b)\s", cell, flags=re.IGNORECASE) is None

    expected = float(value)
    numeric_tokens = re.findall(
        r"(?<![\w.,])[+\-]?\d+(?:[.,]\d+)?(?![\w.,])",
        unicodedata.normalize("NFKC", cell),
        flags=re.UNICODE,
    )
    for token in numeric_tokens:
        try:
            candidate = float(token.replace(",", "."))
        except ValueError:
            return False
        if not math.isclose(candidate, expected, rel_tol=1e-12, abs_tol=1e-12):
            return False
    return bool(numeric_tokens)


def _structured_table_quote_supports_fact(
    *,
    model_quote: str,
    fact_quote: str,
    name: str,
    value: Any,
    unit: str,
    expected_product_name: str,
) -> bool:
    """Bind a target model header cell to the same column in a fact row."""

    header_cells = _table_row_cells(model_quote)
    fact_cells = _table_row_cells(fact_quote)
    if (
        header_cells is None
        or fact_cells is None
        or len(header_cells) != len(fact_cells)
    ):
        return False
    target_columns = [
        index
        for index, cell in enumerate(header_cells)
        if text_contains_exact_identity(expected_product_name, cell)
        and not text_contains_competing_identity(expected_product_name, cell)
    ]
    if len(target_columns) != 1:
        return False
    target_column = target_columns[0]
    label_cells = [
        index
        for index, cell in enumerate(fact_cells)
        if (
            index != target_column
            and identity_key(name) == identity_key(cell)
        )
    ]
    return (
        len(label_cells) == 1
        and _cell_has_unambiguous_fact_value(
            fact_cells[target_column],
            value=value,
            unit=unit,
        )
    )


def _quote_supports_out_of_scope(
    quote: str,
    *,
    expected_product_name: str,
    body: str,
) -> bool:
    normalized_quote = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", quote).casefold(),
    ).strip()
    normalized_body = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", body).casefold(),
    ).strip()
    has_explicit_type = (
        _OUT_OF_SCOPE_ENGLISH_TYPE_RE.search(normalized_quote) is not None
        or any(
            term in normalized_quote
            for term in _OUT_OF_SCOPE_CJK_TYPE_TERMS
        )
    )
    expected_type_is_explicit = (
        _OUT_OF_SCOPE_ENGLISH_TYPE_RE.search(expected_product_name) is not None
        or any(
            term in expected_product_name
            for term in _OUT_OF_SCOPE_CJK_TYPE_TERMS
        )
    )
    has_in_scope_type = (
        _IN_SCOPE_ENGLISH_TYPE_RE.search(normalized_quote) is not None
        or any(term in normalized_quote for term in _IN_SCOPE_CJK_TYPE_TERMS)
    )
    has_negation_language = (
        _OUT_OF_SCOPE_NEGATION_RE.search(normalized_quote) is not None
        or "不是" in normalized_quote
        or "并非" in normalized_quote
    )
    has_accessory_language = (
        _OUT_OF_SCOPE_ACCESSORY_RE.search(normalized_quote) is not None
        or "包含" in normalized_quote
        or "配有" in normalized_quote
        or "使用" in normalized_quote
    )
    has_quoted_relation = (
        _OUT_OF_SCOPE_RELATION_RE.search(normalized_quote) is not None
        or _OUT_OF_SCOPE_IS_A_RELATION_RE.search(normalized_quote) is not None
        or (
            any(
                marker in normalized_quote
                for marker in ("类型：", "类型:", "类别：", "类别:", "属于")
            )
            and any(
                term in normalized_quote
                for term in _OUT_OF_SCOPE_CJK_TYPE_TERMS
            )
        )
    )
    has_explicit_relation = expected_type_is_explicit or has_quoted_relation
    return (
        len(normalized_quote) >= 8
        and normalized_quote in normalized_body
        and has_explicit_type
        and has_explicit_relation
        and (has_quoted_relation or not has_in_scope_type)
        and (has_quoted_relation or not has_accessory_language)
        and not has_negation_language
        and text_contains_exact_identity(expected_product_name, quote)
        and not text_contains_competing_identity(expected_product_name, quote)
    )


def _decision_identifies_generic_hardware(
    *,
    model: str,
    product_category: str,
    summary: str,
) -> bool:
    """Return whether identity-bearing fields describe commodity hardware."""

    if any(
        _OUT_OF_SCOPE_ENGLISH_TYPE_RE.search(value) is not None
        or any(term in value for term in _OUT_OF_SCOPE_CJK_TYPE_TERMS)
        for value in (model, product_category)
    ):
        return True
    normalized_summary = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", summary).casefold(),
    ).strip()
    return (
        _OUT_OF_SCOPE_RELATION_RE.search(normalized_summary) is not None
        and _OUT_OF_SCOPE_ACCESSORY_RE.search(normalized_summary) is None
        and _OUT_OF_SCOPE_NEGATION_RE.search(normalized_summary) is None
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
    allowed_classification_urls: set[str] | None = None,
    trusted_source_domains: set[str] | frozenset[str] | None = None,
    expected_product_name: str | None = None,
    operator_manufacturer_identity: str | None = None,
    evidence_text_by_url: Mapping[str, str] | None = None,
    classification_text_by_url: Mapping[str, str] | None = None,
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
    summary = _text(raw.get("summary"), "summary", limit=2000)
    manufacturer = _text(raw.get("manufacturer"), "manufacturer", limit=300)
    operator_manufacturer = (
        _text(
            operator_manufacturer_identity,
            "operator_manufacturer_identity",
            required=True,
            limit=300,
        )
        if operator_manufacturer_identity is not None
        else ""
    )
    if (
        outcome == "publish"
        and operator_manufacturer
        and identity_key(manufacturer) != identity_key(operator_manufacturer)
    ):
        raise SourceVerificationError(
            "publish manufacturer conflicts with the operator-approved "
            "catalogue brand identity"
        )
    model = _text(raw.get("model"), "model", limit=300)
    product_category = canonical_product_category(
        _text(raw.get("product_category"), "product_category", limit=100)
    )
    catalogue_name = expected_product_name or ""
    model_is_catalogue_bound = model_matches_catalogue_identity(
        model,
        product_id=expected_product_id,
        product_name=catalogue_name,
    )
    if outcome == "publish" and not (
        model_is_catalogue_bound
    ):
        raise DecisionError(
            "publish model is not bound to the catalogue identity"
        )
    decision_identity = (
        model
        if model_is_catalogue_bound
        else preferred_catalogue_model(expected_product_id, catalogue_name)
    )

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

    classification_evidence_raw = raw.get("classification_evidence_urls", [])
    if (
        not isinstance(classification_evidence_raw, list)
        or len(classification_evidence_raw) > 5
    ):
        raise DecisionError(
            "classification_evidence_urls must be an array of at most 5 URLs"
        )
    classification_evidence_urls = [
        validate_public_url(url, "classification_evidence_urls")
        for url in classification_evidence_raw
    ]
    if len(set(classification_evidence_urls)) != len(classification_evidence_urls):
        raise DecisionError("classification_evidence_urls must be unique")
    if allowed_classification_urls is not None:
        normalized_classification_urls = {
            validate_public_url(url, "allowed_classification_urls")
            for url in allowed_classification_urls
        }
        if not set(classification_evidence_urls) <= normalized_classification_urls:
            raise DecisionError(
                "classification evidence must come from this lease's extracts"
            )

    normalized_classification_text: dict[str, str] = {}
    if classification_text_by_url is not None:
        if (
            not isinstance(classification_text_by_url, Mapping)
            or len(classification_text_by_url) > 5
        ):
            raise DecisionError(
                "classification_text_by_url must map at most 5 URLs to extracted text"
            )
        for raw_url, body in classification_text_by_url.items():
            url = validate_public_url(raw_url, "classification_text_by_url")
            if not isinstance(body, str) or not body.strip() or len(body) > 200_000:
                raise DecisionError(
                    "classification_text_by_url values must be bounded non-empty strings"
                )
            normalized_classification_text[url] = body

    classification_quotes_raw = raw.get(
        "classification_evidence_quotes",
        [],
    )
    if (
        not isinstance(classification_quotes_raw, list)
        or len(classification_quotes_raw) > 5
    ):
        raise DecisionError(
            "classification_evidence_quotes must be an array of at most 5 entries"
        )
    classification_evidence_quotes: list[dict[str, str]] = []
    for index, quote_item in enumerate(classification_quotes_raw):
        if not isinstance(quote_item, Mapping):
            raise DecisionError(
                f"classification_evidence_quotes[{index}] must be an object"
            )
        if set(quote_item) != {"url", "quote"}:
            raise DecisionError(
                f"classification_evidence_quotes[{index}] must contain only "
                "url and quote"
            )
        quote_url = validate_public_url(
            quote_item.get("url"),
            f"classification_evidence_quotes[{index}].url",
        )
        quote = _text(
            quote_item.get("quote"),
            f"classification_evidence_quotes[{index}].quote",
            required=True,
            limit=500,
        )
        if quote_url not in classification_evidence_urls:
            raise DecisionError(
                "classification evidence quote URLs must also appear in "
                "classification_evidence_urls"
            )
        classification_evidence_quotes.append(
            {"url": quote_url, "quote": quote}
        )

    evidence_body_by_url: dict[str, str] = {}
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
            evidence_body_by_url[url] = body
            normalized_evidence_text[url] = identity_key(body)

    if outcome == "publish" and any(
        url in evidence_body_by_url
        and not text_contains_exact_identity(
            model,
            evidence_body_by_url[url],
        )
        for url in declared_urls
    ):
        raise DecisionError(
            "cited publish evidence must contain the catalogue-bound model identity"
        )
    auto_verified_urls = _auto_verified_manufacturer_urls(
        datasheets + sources,
        manufacturer=operator_manufacturer or manufacturer,
        expected_product_name=model,
        evidence_body_by_url=evidence_body_by_url,
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
            quote_fields = set(quote_item)
            structured_table_quote = "model_quote" in quote_fields
            if quote_fields not in (
                {"url", "quote"},
                {"url", "quote", "model_quote"},
            ):
                raise DecisionError(
                    f"facts[{index}].evidence_quotes[{quote_index}] "
                    "must contain url and quote, with only an optional "
                    "model_quote table-header span"
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
            if outcome == "publish":
                grounded_quote = _ground_extract_quote(
                    quote,
                    evidence_body_by_url.get(quote_url, ""),
                )
                grounded_model_quote: str | None = None
                supports_fact = False
                if grounded_quote is not None and structured_table_quote:
                    model_quote = _text(
                        quote_item.get("model_quote"),
                        (
                            f"facts[{index}].evidence_quotes[{quote_index}]"
                            ".model_quote"
                        ),
                        required=True,
                        limit=500,
                    )
                    grounded_model_quote = _ground_extract_quote(
                        model_quote,
                        evidence_body_by_url.get(quote_url, ""),
                    )
                    supports_fact = (
                        grounded_model_quote is not None
                        and _structured_table_quote_supports_fact(
                            model_quote=grounded_model_quote,
                            fact_quote=grounded_quote,
                            name=name,
                            value=value,
                            unit=unit,
                            expected_product_name=model,
                        )
                    )
                elif grounded_quote is not None:
                    supports_fact = _quote_supports_fact(
                        grounded_quote,
                        name=name,
                        value=value,
                        unit=unit,
                        expected_product_name=model,
                        normalized_body=normalized_evidence_text.get(quote_url, ""),
                    )
                if not supports_fact:
                    raise DecisionError(
                        f"facts[{index}].evidence_quotes[{quote_index}] is not "
                        "an exact supporting extract span"
                    )
                quote = grounded_quote
                normalized_quote = {"url": quote_url, "quote": quote}
                if grounded_model_quote is not None:
                    normalized_quote["model_quote"] = grounded_model_quote
                evidence_quotes.append(normalized_quote)
            else:
                normalized_quote = {"url": quote_url, "quote": quote}
                if structured_table_quote:
                    normalized_quote["model_quote"] = _text(
                        quote_item.get("model_quote"),
                        (
                            f"facts[{index}].evidence_quotes[{quote_index}]"
                            ".model_quote"
                        ),
                        required=True,
                        limit=500,
                    )
                evidence_quotes.append(normalized_quote)
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

    if outcome in {"no_datasheet", "ambiguous", "insufficient_identity"}:
        if facts:
            raise DecisionError(
                f"{outcome} must not contain verified specification facts"
            )
        if any(item["is_primary"] for item in datasheets):
            raise DecisionError(
                f"{outcome} must not declare a primary datasheet"
            )
        if review_summary or review_evidence_urls:
            raise DecisionError(
                f"{outcome} must not contain publication review evidence"
            )
        if classification_evidence_urls or classification_evidence_quotes:
            raise DecisionError(
                f"{outcome} must not contain out_of_scope classification evidence"
            )
        if outcome == "no_datasheet" and datasheets:
            raise DecisionError(
                "no_datasheet must not declare datasheet candidates"
            )
        if outcome == "insufficient_identity" and normalized_conflicts:
            raise DecisionError(
                "insufficient_identity must not assert verified conflicts; "
                "use ambiguous"
            )
    if outcome == "out_of_scope":
        if confidence < minimum_confidence:
            raise DecisionError(
                "out_of_scope confidence is below the configured threshold"
            )
        if not product_category:
            raise DecisionError("out_of_scope requires a public product_category")
        if not summary:
            raise DecisionError("out_of_scope requires a short classification summary")
        if not classification_evidence_urls:
            raise DecisionError(
                "out_of_scope requires extracted classification evidence"
            )
        if not classification_evidence_quotes:
            raise DecisionError(
                "out_of_scope requires an exact hardware-type classification quote"
            )
        if any(
            (
                datasheets,
                sources,
                facts,
                normalized_conflicts,
                review_summary,
                review_evidence_urls,
            )
        ):
            raise DecisionError(
                "out_of_scope must not contain publication or review evidence"
            )
        if not any(
            url in normalized_classification_text
            and text_contains_exact_identity(
                decision_identity,
                normalized_classification_text[url],
            )
            for url in classification_evidence_urls
        ):
            raise DecisionError(
                "out_of_scope evidence must contain the complete catalogue identity"
            )
        for index, item in enumerate(classification_evidence_quotes):
            grounded_quote = _ground_extract_quote(
                item["quote"],
                normalized_classification_text.get(item["url"], ""),
            )
            if not _quote_supports_out_of_scope(
                grounded_quote or "",
                expected_product_name=decision_identity,
                body=normalized_classification_text.get(item["url"], ""),
            ):
                raise DecisionError(
                    f"classification_evidence_quotes[{index}] is not a "
                    "contiguous target-model hardware-type quote from the extract"
                )
            item["quote"] = grounded_quote

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
        if not manufacturer:
            raise DecisionError("publish requires a discovered manufacturer")
        if _decision_identifies_generic_hardware(
            model=model,
            product_category=product_category,
            summary=summary,
        ):
            raise DecisionError(
                "publish cannot classify the catalogue product as generic "
                "hardware; return out_of_scope with exact classification "
                "evidence"
            )
        if len(facts) < 5:
            raise DecisionError(
                "publish requires at least 5 cited specification facts"
            )
        primary = [item for item in datasheets if item["is_primary"]]
        if not primary:
            raise DecisionError("publish requires a primary datasheet")
        configured_primary = [
            item
            for item in primary
            if item["source_type"] in TRUSTED_TYPES
            and _url_has_trusted_domain(item["url"], trusted_domains)
        ]
        auto_primary = [
            item
            for item in primary
            if item["source_type"] == "manufacturer"
            and item["url"] in auto_verified_urls
        ]
        trusted_primary = configured_primary + auto_primary
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
                raise SourceVerificationError(
                    "primary datasheet needs an automatically verified manufacturer "
                    "source, a configured domain override, or two enabled independent "
                    "mirrors"
                )
        configured_primary_urls = {
            item["url"] for item in configured_primary
        }
        auto_primary_urls = {
            item["url"] for item in auto_primary
        }
        for index, fact in enumerate(facts):
            quoted_urls = {
                item["url"] for item in fact["evidence_quotes"]
            }
            if configured_primary:
                if not quoted_urls & configured_primary_urls:
                    raise SourceVerificationError(
                        f"facts[{index}] needs evidence from the configured "
                        "primary manufacturer datasheet"
                    )
            elif auto_primary:
                if not quoted_urls & auto_primary_urls:
                    raise SourceVerificationError(
                        f"facts[{index}] needs evidence from the automatically "
                        "verified primary manufacturer datasheet"
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
        "manufacturer": manufacturer,
        "model": model,
        "product_category": product_category,
        "summary": summary,
        "review_summary": review_summary,
        "review_evidence_urls": review_evidence_urls,
        "classification_evidence_urls": classification_evidence_urls,
        "classification_evidence_quotes": classification_evidence_quotes,
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
    "SourceVerificationError",
    "canonical_product_category",
    "identity_key",
    "model_matches_catalogue_identity",
    "preferred_catalogue_model",
    "text_contains_competing_identity",
    "text_contains_catalogue_identity",
    "text_contains_exact_identity",
    "validate_decision",
    "validate_public_url",
]
