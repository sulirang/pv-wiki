"""Environment configuration helpers without secret persistence."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when runtime configuration is missing or unsafe."""


class TrustedSourceNotConfigured(LookupError):
    """Raised only by the legacy strict single-brand lookup helper."""


_PLACEHOLDER_PREFIXES = ("replace-me", "replace-with-", "<replace-")
_COMMON_COUNTRY_PUBLIC_SUFFIX_LABELS = frozenset(
    {"ac", "co", "com", "edu", "gov", "net", "org"}
)
_KNOWN_SHARED_HOST_SUFFIXES = frozenset(
    {
        "amazonaws.com",
        "appspot.com",
        "azurewebsites.net",
        "azureedge.net",
        "backblazeb2.com",
        "blob.core.windows.net",
        "blogspot.com",
        "box.com",
        "canva.site",
        "carrd.co",
        "cloudfront.net",
        "cloudfunctions.net",
        "digitaloceanspaces.com",
        "docs.google.com",
        "drive.google.com",
        "dropbox.com",
        "facebook.com",
        "firebaseapp.com",
        "ghost.io",
        "github.io",
        "githubusercontent.com",
        "gitlab.io",
        "groups.google.com",
        "hashnode.dev",
        "herokuapp.com",
        "instagram.com",
        "issuu.com",
        "linkedin.com",
        "linodeobjects.com",
        "medium.com",
        "myshopify.com",
        "netlify.app",
        "notion.site",
        "notion.so",
        "onrender.com",
        "onedrive.live.com",
        "pages.dev",
        "r2.cloudflarestorage.com",
        "r2.dev",
        "readthedocs.io",
        "reddit.com",
        "run.app",
        "scribd.com",
        "sharepoint.com",
        "sites.google.com",
        "slideshare.net",
        "storage.googleapis.com",
        "substack.com",
        "surge.sh",
        "tumblr.com",
        "twitter.com",
        "vercel.app",
        "wasabisys.com",
        "web.app",
        "weebly.com",
        "wixsite.com",
        "workers.dev",
        "wordpress.com",
        "x.com",
        "youtube.com",
        "youtu.be",
    }
)


def is_shared_source_hostname(hostname: str) -> bool:
    """Reject IP literals and hosts controlled by unrelated public tenants."""

    if not isinstance(hostname, str):
        return False
    host = hostname.strip().rstrip(".").casefold()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _KNOWN_SHARED_HOST_SUFFIXES
    )


def is_placeholder_value(value: str) -> bool:
    """Return whether an example value was left in a live configuration."""

    return (
        isinstance(value, str)
        and value.strip().casefold().startswith(_PLACEHOLDER_PREFIXES)
    )


def redact_environment_secrets(
    message: str,
    names: Iterable[str],
    *,
    limit: int,
) -> str:
    """Redact configured secrets, including every key in a rotation list."""

    candidates: set[str] = set()
    for name in names:
        value = os.getenv(name, "")
        if not value:
            continue
        candidates.add(value)
        if name == "EXA_API_KEYS":
            candidates.update(
                part for part in re.split(r"[\s,]+", value) if part
            )
    for secret in sorted(candidates, key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    return message[:limit]


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def state_path() -> Path:
    raw = os.getenv(
        "PV_WIKI_STATE_PATH",
        "~/.local/state/pv-wiki/state.sqlite3",
    )
    path = Path(raw).expanduser()
    if not path.name:
        raise ConfigError("PV_WIKI_STATE_PATH must name a file")
    return path


@dataclass(frozen=True, slots=True)
class WikiSettings:
    base_url: str
    token: str
    locale: str
    path_prefix: str
    home_path: str
    home_title: str
    timeout: float
    new_page_private: bool
    new_page_published: bool

    @classmethod
    def from_env(cls) -> "WikiSettings":
        base_url = os.getenv("WIKIJS_URL", "").strip().rstrip("/")
        token = os.getenv("WIKIJS_TOKEN", "").strip()
        locale = os.getenv("WIKIJS_LOCALE", "en").strip()
        prefix = os.getenv("WIKIJS_PATH_PREFIX", "products").strip().strip("/")
        home_path = os.getenv("WIKIJS_HOME_PATH", "home").strip().strip("/")
        home_title = os.getenv("WIKIJS_HOME_TITLE", "PV Wiki").strip()
        if not base_url or not token:
            raise ConfigError("WIKIJS_URL and WIKIJS_TOKEN are required")
        if is_placeholder_value(token):
            raise ConfigError("WIKIJS_TOKEN still contains an example placeholder")
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ConfigError("WIKIJS_URL must be an HTTPS origin without a path")
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,16}", locale):
            raise ConfigError("WIKIJS_LOCALE is invalid")
        if not prefix or not re.fullmatch(r"[A-Za-z0-9/_-]+", prefix):
            raise ConfigError("WIKIJS_PATH_PREFIX contains unsafe characters")
        if (
            not home_path
            or not re.fullmatch(r"[A-Za-z0-9/_-]+", home_path)
            or "//" in home_path
        ):
            raise ConfigError("WIKIJS_HOME_PATH contains unsafe characters")
        if (
            not home_title
            or len(home_title) > 200
            or any(ord(character) < 32 for character in home_title)
        ):
            raise ConfigError("WIKIJS_HOME_TITLE is invalid")
        return cls(
            base_url=base_url,
            token=token,
            locale=locale,
            path_prefix=prefix,
            home_path=home_path,
            home_title=home_title,
            timeout=_float_env("PV_WIKI_HTTP_TIMEOUT", 30.0, 1.0, 120.0),
            new_page_private=_bool_env("WIKIJS_NEW_PAGE_PRIVATE", True),
            new_page_published=_bool_env("WIKIJS_NEW_PAGE_PUBLISHED", False),
        )


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    """Hard-bounded budgets for one product's inner research loop."""

    max_rounds: int
    max_queries: int
    max_credits: int
    max_seconds: float

    @classmethod
    def from_env(cls) -> "ResearchSettings":
        return cls(
            max_rounds=_int_env(
                "PV_WIKI_RESEARCH_MAX_ROUNDS",
                3,
                1,
                3,
            ),
            max_queries=_int_env(
                "PV_WIKI_RESEARCH_MAX_QUERIES",
                7,
                3,
                7,
            ),
            max_credits=_int_env(
                "PV_WIKI_RESEARCH_MAX_CREDITS",
                20,
                3,
                100,
            ),
            max_seconds=_float_env(
                "PV_WIKI_RESEARCH_MAX_SECONDS",
                600.0,
                60.0,
                1200.0,
            ),
        )


@dataclass(frozen=True, slots=True)
class GlobalResearchBudgetSettings:
    """UTC stop-loss limits across all products; zero disables one window."""

    daily_credit_limit: int
    monthly_credit_limit: int

    @classmethod
    def from_env(cls) -> "GlobalResearchBudgetSettings":
        return cls(
            daily_credit_limit=_int_env(
                "PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT",
                0,
                0,
                1_000_000,
            ),
            monthly_credit_limit=_int_env(
                "PV_WIKI_GLOBAL_MONTHLY_CREDIT_LIMIT",
                0,
                0,
                10_000_000,
            ),
        )


def min_publish_confidence() -> float:
    return _float_env("PV_WIKI_AUTO_PUBLISH_MIN_CONFIDENCE", 0.85, 0.5, 1.0)


def min_fact_confidence() -> float:
    return _float_env("PV_WIKI_MIN_FACT_CONFIDENCE", 0.8, 0.5, 1.0)


def allow_mirrors() -> bool:
    return _bool_env("PV_WIKI_ALLOW_MIRRORS", False)


def include_internal_search_hints() -> bool:
    return _bool_env("PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS", False)


def max_extract_chars() -> int:
    return _int_env("PV_WIKI_MAX_EXTRACT_CHARS", 30_000, 1_000, 200_000)


def _validated_source_domain(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON values must be hostnames"
        )
    domain = value.strip().rstrip(".").casefold()
    labels = domain.split(".")
    is_public_suffix = is_shared_source_hostname(domain) or (
        len(labels) == 2
        and len(labels[-1]) == 2
        and labels[0] in _COMMON_COUNTRY_PUBLIC_SUFFIX_LABELS
    )
    if (
        not domain
        or len(domain) > 253
        or "://" in domain
        or "/" in domain
        or "*" in domain
        or is_public_suffix
        or not re.fullmatch(
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            domain,
        )
    ):
        raise ConfigError(
            "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON values must be narrow hostnames, "
            "not public or shared hosting suffixes"
        )
    return domain


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON has duplicate keys"
            )
        result[key] = value
    return result


def _unique_public_alias_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON has duplicate keys"
            )
        result[key] = value
    return result


def public_brand_alias_map() -> dict[str, str]:
    """Return operator-approved public manufacturer names by catalogue brand.

    Catalogue brand codes are internal authority boundaries.  The configured
    value is the public manufacturer identity that may be sent to providers
    and used for automatic manufacturer-domain verification.
    """

    raw = os.getenv("PV_WIKI_PUBLIC_BRAND_ALIASES_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_public_alias_object,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ConfigError(
            "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON must be a JSON object"
        ) from exc
    if not isinstance(value, dict) or len(value) > 500:
        raise ConfigError(
            "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON must be a JSON object "
            "with at most 500 entries"
        )

    mapping: dict[str, str] = {}
    for raw_brand, raw_alias in value.items():
        if (
            not isinstance(raw_brand, str)
            or not raw_brand.strip()
            or len(raw_brand.strip()) > 200
            or any(ord(character) < 32 for character in raw_brand)
        ):
            raise ConfigError(
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON keys must be non-empty "
                "catalogue brand strings"
            )
        brand = raw_brand.strip().casefold()
        if brand in mapping:
            raise ConfigError(
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON has duplicate "
                "case-insensitive catalogue brands"
            )
        if (
            not isinstance(raw_alias, str)
            or not raw_alias.strip()
            or len(raw_alias.strip()) > 200
            or any(ord(character) < 32 for character in raw_alias)
            or not any(character.isalpha() for character in raw_alias)
            or len(
                "".join(
                    character
                    for character in raw_alias.casefold()
                    if character.isalnum()
                )
            ) < 2
            or "://" in raw_alias
            or any(character in raw_alias for character in "{}[]<>")
        ):
            raise ConfigError(
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON values must be bounded "
                "public manufacturer names"
            )
        mapping[brand] = " ".join(raw_alias.split())
    return mapping


def public_brand_alias(brand_code: str | None) -> str:
    """Resolve an explicitly approved public name for one catalogue brand."""

    brand = brand_code.strip().casefold() if isinstance(brand_code, str) else ""
    return public_brand_alias_map().get(brand, "") if brand else ""


def trusted_source_domain_map() -> dict[str, frozenset[str]]:
    """Return optional overrides keyed by public manufacturer or legacy brand.

    The map is a fast path for official hosts whose domain name does not
    resemble the public manufacturer name.  It is deliberately optional:
    ordinary manufacturer hosts can be verified from the current bounded
    extract by the decision validator.
    """

    raw = os.getenv("PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ConfigError(
            "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON must be a JSON object"
        ) from exc
    if not isinstance(value, dict):
        raise ConfigError(
            "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON must be a JSON object"
        )
    mapping: dict[str, frozenset[str]] = {}
    for raw_brand, raw_domains in value.items():
        if (
            not isinstance(raw_brand, str)
            or not raw_brand.strip()
            or len(raw_brand.strip()) > 200
            or any(ord(character) < 32 for character in raw_brand)
        ):
            raise ConfigError(
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON keys must be non-empty "
                "brand code or manufacturer alias strings"
            )
        brand = raw_brand.strip().casefold()
        if brand in mapping:
            raise ConfigError(
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON has duplicate identities"
            )
        if (
            not isinstance(raw_domains, list)
            or not raw_domains
            or len(raw_domains) > 50
        ):
            raise ConfigError(
                "each trusted source identity must have 1-50 domain strings"
            )
        mapping[brand] = frozenset(
            _validated_source_domain(domain) for domain in raw_domains
        )
    return mapping


def trusted_source_domains(brand_code: str) -> frozenset[str]:
    """Resolve trusted domains for one exact catalogue brand code."""

    if not isinstance(brand_code, str) or not brand_code.strip():
        raise TrustedSourceNotConfigured(
            "a non-empty catalogue brand_code is required for trusted publishing"
        )
    domains = trusted_source_domain_map().get(brand_code.strip().casefold())
    if not domains:
        raise TrustedSourceNotConfigured(
            "no trusted source domains are configured for this catalogue brand_code"
        )
    return domains


def trusted_source_domains_for_product(
    brand_code: str | None,
    discovered_manufacturer: str | None,
) -> frozenset[str]:
    """Return the override bound to the operator-owned catalogue identity.

    ``discovered_manufacturer`` remains in the signature for compatibility,
    but model output is never allowed to select or replace a trusted-domain
    entry.  Operators can map legacy brand codes directly in the trusted
    domain map and publish a separate public name via the explicit alias map.
    """

    mapping = trusted_source_domain_map()
    brand = brand_code.strip().casefold() if isinstance(brand_code, str) else ""
    if not brand:
        return frozenset()
    alias = public_brand_alias_map().get(brand, "").casefold()
    brand_domains = mapping.get(brand, frozenset())
    alias_domains = mapping.get(alias, frozenset()) if alias else frozenset()
    if brand_domains and alias_domains and brand_domains != alias_domains:
        raise ConfigError(
            "trusted source domains conflict between the catalogue brand "
            "and its explicit public alias"
        )
    return brand_domains or alias_domains


def missing_environment(names: list[str] | tuple[str, ...]) -> list[str]:
    return [
        name
        for name in names
        if not os.getenv(name, "").strip()
        or is_placeholder_value(os.getenv(name, ""))
    ]


__all__ = [
    "ConfigError",
    "GlobalResearchBudgetSettings",
    "ResearchSettings",
    "TrustedSourceNotConfigured",
    "WikiSettings",
    "allow_mirrors",
    "include_internal_search_hints",
    "is_shared_source_hostname",
    "is_placeholder_value",
    "max_extract_chars",
    "min_fact_confidence",
    "min_publish_confidence",
    "missing_environment",
    "public_brand_alias",
    "public_brand_alias_map",
    "redact_environment_secrets",
    "state_path",
    "trusted_source_domain_map",
    "trusted_source_domains",
    "trusted_source_domains_for_product",
]
