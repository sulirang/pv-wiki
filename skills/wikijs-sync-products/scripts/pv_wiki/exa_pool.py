"""Health-aware Exa API-key pool for the Hermes MCP integration.

The pool deliberately contains no product-research policy.  It only selects a
credential, sends a request to one of Exa's two read-only endpoints, and reacts
to credential-specific failures.  Network failures and ambiguous provider
errors are never replayed automatically.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .config import is_placeholder_value


API_BASE_URL = "https://api.exa.ai"
DEFAULT_TIMEOUT = 60.0
DEFAULT_RATE_LIMIT_COOLDOWN = 60.0
MAX_RATE_LIMIT_COOLDOWN = 3_600.0
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 64 * 1024
MAX_KEYS_FILE_BYTES = 64 * 1024
_ALLOWED_PATHS = frozenset({"/search", "/contents"})
_KEY_BUDGET_ERROR = "API_KEY_BUDGET_EXCEEDED"
_GLOBAL_BUDGET_ERRORS = frozenset(
    {"NO_MORE_CREDITS", "TEAM_BUDGET_EXCEEDED"}
)


class ExaPoolError(RuntimeError):
    """Base class for safe, credential-free Exa pool errors."""


class ExaPoolConfigError(ExaPoolError):
    """Raised when the key pool or transport configuration is invalid."""


class ExaPoolHTTPError(ExaPoolError):
    """Raised for an HTTP response that must not be replayed with another key."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after = retry_after
        super().__init__(message)


class ExaPoolUnavailableError(ExaPoolError):
    """Raised when no configured key is currently eligible."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class ExaPoolNetworkError(ExaPoolError):
    """Raised when a paid request has an ambiguous network result."""


class ExaPoolResponseError(ExaPoolError):
    """Raised when Exa returns a malformed or unbounded response."""


@dataclass(slots=True)
class _KeyState:
    secret: str = field(repr=False)
    fingerprint: str
    disabled_reason: str | None = None
    cooldown_until: float = 0.0


@dataclass(frozen=True, slots=True)
class _KeySelection:
    secret: str = field(repr=False)
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _RejectedRequest(Exception):
    status_code: int
    error_code: str | None
    retry_after: float | None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never replay an Exa credential to a redirect target."""

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


def _split_keys(value: str) -> list[str]:
    return [part for part in re.split(r"[\s,]+", value) if part]


def _read_keys_file(path_value: str) -> list[str]:
    path = Path(path_value).expanduser()
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise ExaPoolConfigError("EXA_API_KEYS_FILE cannot be read") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ExaPoolConfigError("EXA_API_KEYS_FILE must be a regular file")
    if os.name == "posix" and file_stat.st_mode & 0o077:
        raise ExaPoolConfigError(
            "EXA_API_KEYS_FILE must not grant group or world permissions"
        )
    if file_stat.st_size > MAX_KEYS_FILE_BYTES:
        raise ExaPoolConfigError(
            f"EXA_API_KEYS_FILE exceeds {MAX_KEYS_FILE_BYTES} bytes"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExaPoolConfigError("EXA_API_KEYS_FILE cannot be read") from exc
    return _split_keys(text)


def resolve_api_keys(
    api_keys: str | Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Resolve unique keys without ever placing their values in an error."""

    values: list[str] = []
    active_environ = os.environ if environ is None else environ
    if api_keys is not None:
        if isinstance(api_keys, str):
            values.append(api_keys.strip())
        elif isinstance(api_keys, Sequence):
            for item in api_keys:
                if isinstance(item, str):
                    values.append(item.strip())
        else:
            raise ExaPoolConfigError("api_keys must be a string or sequence")
    else:
        keys_file = active_environ.get("EXA_API_KEYS_FILE", "").strip()
        if keys_file:
            values.extend(_read_keys_file(keys_file))
        else:
            multi = active_environ.get("EXA_API_KEYS", "")
            if multi.strip():
                values.extend(_split_keys(multi))
            else:
                single = active_environ.get("EXA_API_KEY", "").strip()
                if single:
                    values.append(single)

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.strip()
        if not key or is_placeholder_value(key) or key in seen:
            continue
        seen.add(key)
        unique.append(key)
    if not unique:
        raise ExaPoolConfigError(
            "At least one Exa key is required via EXA_API_KEYS_FILE, "
            "EXA_API_KEYS, or EXA_API_KEY"
        )
    return unique


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


class ExaKeyPool:
    """Thread-safe, in-memory health state with true round-robin selection."""

    def __init__(
        self,
        api_keys: str | Sequence[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        default_cooldown: float = DEFAULT_RATE_LIMIT_COOLDOWN,
    ) -> None:
        keys = resolve_api_keys(api_keys, environ=environ)
        if (
            isinstance(default_cooldown, bool)
            or not isinstance(default_cooldown, (int, float))
            or not math.isfinite(float(default_cooldown))
            or float(default_cooldown) < 0
        ):
            raise ExaPoolConfigError(
                "default_cooldown must be a finite non-negative number"
            )
        self._states = [
            _KeyState(secret=key, fingerprint=_fingerprint(key)) for key in keys
        ]
        self._clock = clock
        self._default_cooldown = float(default_cooldown)
        self._cursor = 0
        self._global_error_code: str | None = None
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._states)

    def acquire(self, *, excluded: frozenset[str] = frozenset()) -> _KeySelection:
        """Select the next healthy key and advance the cursor immediately."""

        with self._lock:
            if self._global_error_code:
                raise ExaPoolUnavailableError(
                    "The Exa account or team budget is unavailable "
                    f"({self._global_error_code})"
                )
            now = self._clock()
            count = len(self._states)
            for offset in range(count):
                index = (self._cursor + offset) % count
                state = self._states[index]
                if (
                    state.fingerprint in excluded
                    or state.disabled_reason is not None
                    or state.cooldown_until > now
                ):
                    continue
                self._cursor = (index + 1) % count
                return _KeySelection(
                    secret=state.secret,
                    fingerprint=state.fingerprint,
                )

            cooldowns = [
                state.cooldown_until - now
                for state in self._states
                if state.disabled_reason is None and state.cooldown_until > now
            ]
            retry_after = max(0.0, min(cooldowns)) if cooldowns else None
            raise ExaPoolUnavailableError(
                "No healthy Exa API key is currently available",
                retry_after=retry_after,
            )

    def disable(self, selection: _KeySelection, reason: str) -> None:
        with self._lock:
            for state in self._states:
                if state.fingerprint == selection.fingerprint:
                    state.disabled_reason = reason
                    state.cooldown_until = 0.0
                    return

    def cool_down(
        self,
        selection: _KeySelection,
        retry_after: float | None,
    ) -> float:
        delay = self._default_cooldown if retry_after is None else retry_after
        if not math.isfinite(delay):
            delay = MAX_RATE_LIMIT_COOLDOWN
        delay = min(MAX_RATE_LIMIT_COOLDOWN, max(0.0, float(delay)))
        with self._lock:
            now = self._clock()
            for state in self._states:
                if state.fingerprint == selection.fingerprint:
                    state.cooldown_until = max(
                        state.cooldown_until,
                        now + delay,
                    )
                    break
        return delay

    def block_globally(self, error_code: str) -> None:
        with self._lock:
            self._global_error_code = error_code

    def snapshot(self) -> list[dict[str, Any]]:
        """Return redacted state for tests and local health diagnostics."""

        with self._lock:
            now = self._clock()
            return [
                {
                    "fingerprint": state.fingerprint,
                    "disabled_reason": state.disabled_reason,
                    "cooldown_remaining": max(0.0, state.cooldown_until - now),
                }
                for state in self._states
            ]


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


def _retry_after_seconds(
    error: urllib.error.HTTPError,
    *,
    now: float,
) -> float | None:
    raw = error.headers.get("Retry-After") if error.headers is not None else None
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if math.isfinite(seconds):
        return min(MAX_RATE_LIMIT_COOLDOWN, max(0.0, seconds))
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return min(
        MAX_RATE_LIMIT_COOLDOWN,
        max(0.0, retry_at.timestamp() - now),
    )


def _normalize_error_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")
    if "API_KEY_BUDGET_EXCEEDED" in normalized:
        return _KEY_BUDGET_ERROR
    if "TEAM_BUDGET_EXCEEDED" in normalized:
        return "TEAM_BUDGET_EXCEEDED"
    if "NO_MORE_CREDITS" in normalized:
        return "NO_MORE_CREDITS"
    return normalized[:80] or None


def _error_code_from_body(body: bytes) -> str | None:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    candidates: list[Any] = [decoded.get("code"), decoded.get("error")]
    raw_error = decoded.get("error")
    if isinstance(raw_error, Mapping):
        candidates.extend(
            (raw_error.get("code"), raw_error.get("tag"), raw_error.get("type"))
        )
    candidates.append(decoded.get("message"))
    for candidate in candidates:
        code = _normalize_error_code(candidate)
        if code in _GLOBAL_BUDGET_ERRORS or code == _KEY_BUDGET_ERROR:
            return code
    return _normalize_error_code(candidates[0])


class ExaPoolClient:
    """Dependency-free Exa Search/Contents client backed by ``ExaKeyPool``."""

    def __init__(
        self,
        api_keys: str | Sequence[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        pool: ExaKeyPool | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
        default_cooldown: float = DEFAULT_RATE_LIMIT_COOLDOWN,
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise ExaPoolConfigError("timeout must be finite and greater than zero")
        self.pool = pool or ExaKeyPool(
            api_keys,
            environ=environ,
            clock=clock,
            default_cooldown=default_cooldown,
        )
        self.timeout = float(timeout)
        self._opener = opener or _NO_REDIRECT_OPENER.open
        self._clock = clock

    def post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if path not in _ALLOWED_PATHS:
            raise ValueError("unsupported Exa endpoint")
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        active_timeout = self.timeout if timeout is None else float(timeout)
        if not math.isfinite(active_timeout) or active_timeout <= 0:
            raise ValueError("timeout must be finite and greater than zero")

        attempted: set[str] = set()
        rate_limit_delays: list[float] = []
        while len(attempted) < len(self.pool):
            selection = self.pool.acquire(excluded=frozenset(attempted))
            attempted.add(selection.fingerprint)
            try:
                return self._post_once(
                    path,
                    payload,
                    selection.secret,
                    timeout=active_timeout,
                )
            except _RejectedRequest as error:
                if error.status_code == 401:
                    self.pool.disable(selection, "invalid_api_key")
                    continue
                if error.status_code == 402:
                    if error.error_code == _KEY_BUDGET_ERROR:
                        self.pool.disable(selection, "api_key_budget_exceeded")
                        continue
                    if error.error_code in _GLOBAL_BUDGET_ERRORS:
                        self.pool.block_globally(error.error_code)
                    code_suffix = (
                        f" ({error.error_code})" if error.error_code else ""
                    )
                    raise ExaPoolHTTPError(
                        f"Exa API returned HTTP 402{code_suffix}",
                        status_code=402,
                        error_code=error.error_code,
                    ) from None
                if error.status_code == 429:
                    delay = self.pool.cool_down(selection, error.retry_after)
                    rate_limit_delays.append(delay)
                    continue
                code_suffix = (
                    f" ({error.error_code})" if error.error_code else ""
                )
                raise ExaPoolHTTPError(
                    f"Exa API returned HTTP {error.status_code}{code_suffix}",
                    status_code=error.status_code,
                    error_code=error.error_code,
                    retry_after=error.retry_after,
                ) from None

        if rate_limit_delays:
            raise ExaPoolUnavailableError(
                "All healthy Exa API keys are rate limited",
                retry_after=min(rate_limit_delays),
            )
        raise ExaPoolUnavailableError("No healthy Exa API key is available")

    def _post_once(
        self,
        path: str,
        payload: Mapping[str, Any],
        api_key: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{API_BASE_URL}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "pv-wiki-exa-pool-mcp/0.1",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                length = _content_length(response)
                if length is not None and length > MAX_RESPONSE_BYTES:
                    raise ExaPoolResponseError(
                        f"Exa response exceeds {MAX_RESPONSE_BYTES} bytes"
                    )
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            retry_after = _retry_after_seconds(error, now=self._clock())
            try:
                error_body = error.read(MAX_ERROR_RESPONSE_BYTES + 1)
            except OSError:
                error_body = b""
            finally:
                error.close()
            raise _RejectedRequest(
                status_code=error.code,
                error_code=_error_code_from_body(error_body),
                retry_after=retry_after,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExaPoolNetworkError(
                "Exa API request failed; ambiguous request was not replayed"
            ) from exc

        if not isinstance(body, bytes):
            raise ExaPoolResponseError("Exa response body must be bytes")
        if len(body) > MAX_RESPONSE_BYTES:
            raise ExaPoolResponseError(
                f"Exa response exceeds {MAX_RESPONSE_BYTES} bytes"
            )
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExaPoolResponseError("Exa returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ExaPoolResponseError("Exa response must be a JSON object")
        return decoded


__all__ = [
    "API_BASE_URL",
    "DEFAULT_RATE_LIMIT_COOLDOWN",
    "DEFAULT_TIMEOUT",
    "ExaKeyPool",
    "ExaPoolClient",
    "ExaPoolConfigError",
    "ExaPoolError",
    "ExaPoolHTTPError",
    "ExaPoolNetworkError",
    "ExaPoolResponseError",
    "ExaPoolUnavailableError",
    "resolve_api_keys",
]
