"""Environment configuration helpers without secret persistence."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when runtime configuration is missing or unsafe."""


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
    raw = os.getenv("PV_WIKI_STATE_PATH", "~/.hermes/data/pv-wiki/state.sqlite3")
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


def min_publish_confidence() -> float:
    return _float_env("PV_WIKI_AUTO_PUBLISH_MIN_CONFIDENCE", 0.85, 0.5, 1.0)


def min_fact_confidence() -> float:
    return _float_env("PV_WIKI_MIN_FACT_CONFIDENCE", 0.8, 0.5, 1.0)


def allow_mirrors() -> bool:
    return _bool_env("PV_WIKI_ALLOW_MIRRORS", False)


def include_internal_search_hints() -> bool:
    return _bool_env("PV_WIKI_TAVILY_INCLUDE_INTERNAL_HINTS", False)


def max_extract_chars() -> int:
    return _int_env("PV_WIKI_MAX_EXTRACT_CHARS", 30_000, 1_000, 200_000)


def missing_environment(names: list[str] | tuple[str, ...]) -> list[str]:
    return [name for name in names if not os.getenv(name, "").strip()]


__all__ = [
    "ConfigError",
    "WikiSettings",
    "allow_mirrors",
    "include_internal_search_hints",
    "max_extract_chars",
    "min_fact_confidence",
    "min_publish_confidence",
    "missing_environment",
    "state_path",
]
