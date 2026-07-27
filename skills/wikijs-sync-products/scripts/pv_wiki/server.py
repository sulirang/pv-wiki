"""Small authenticated HTTP boundary for n8n-to-PV-Wiki operations."""

from __future__ import annotations

import hmac
import json
import os
import threading
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import __version__
from .ai import AIError
from .config import (
    ConfigError,
    is_placeholder_value,
    redact_environment_secrets,
    state_target,
)
from .exa import ExaConfigError
from .state import StateStore


MAX_REQUEST_BYTES = 8 * 1024
MAX_TOKEN_LENGTH = 512
_RUN_LOCK = threading.Lock()
_CATALOGUE_LOCK = threading.Lock()
_HOMEPAGE_LOCK = threading.Lock()


class WorkerConfigError(ValueError):
    """Raised when the worker HTTP boundary is configured unsafely."""


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8080

    def __post_init__(self) -> None:
        if (
            not isinstance(self.token, str)
            or not 32 <= len(self.token) <= MAX_TOKEN_LENGTH
            or is_placeholder_value(self.token)
            or any(ord(character) < 33 or ord(character) > 126 for character in self.token)
        ):
            raise WorkerConfigError(
                "PV_WIKI_WORKER_TOKEN must contain 32-512 printable non-space characters"
            )
        if (
            not isinstance(self.host, str)
            or not self.host.strip()
            or any(ord(character) < 32 for character in self.host)
        ):
            raise WorkerConfigError("PV_WIKI_WORKER_HOST is invalid")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise WorkerConfigError("PV_WIKI_WORKER_PORT must be between 1 and 65535")

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        token = os.getenv("PV_WIKI_WORKER_TOKEN", "")
        host = os.getenv("PV_WIKI_WORKER_HOST", "127.0.0.1").strip()
        raw_port = os.getenv("PV_WIKI_WORKER_PORT", "8080").strip()
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise WorkerConfigError(
                "PV_WIKI_WORKER_PORT must be an integer"
            ) from exc
        return cls(token=token, host=host, port=port)


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _safe_error(error: BaseException) -> str:
    message = str(error) or error.__class__.__name__
    return redact_environment_secrets(
        message,
        (
            "PV_WIKI_WORKER_TOKEN",
            "PGPASSWORD",
            "EXA_API_KEY",
            "EXA_API_KEYS",
            "AI_API_KEY",
            "LLM_API_KEY",
            "OPENAI_API_KEY",
            "WIKIJS_TOKEN",
        ),
        limit=1000,
    )


def health() -> dict[str, Any]:
    """Return local process/state health without probing paid dependencies."""

    with StateStore(state_target()) as store:
        schema_version = store.schema_version
    return {
        "ok": True,
        "service": "pv-wiki-worker",
        "version": __version__,
        "state_schema_version": schema_version,
    }


def status() -> dict[str, Any]:
    """Return authenticated queue and circuit readiness without paid probes."""

    # Importing the CLI constants lazily avoids the server/CLI import cycle at
    # module load time. These helpers calculate opaque fingerprints; neither
    # credentials nor fingerprints are returned by this endpoint.
    from .ai import AISettings
    from .cli import (
        AI_INVALID_OUTPUT_CIRCUIT_CATEGORIES,
        AI_INVALID_OUTPUT_CIRCUIT_ERROR_TYPES,
        AI_INVALID_OUTPUT_CIRCUIT_THRESHOLD,
        AI_INVALID_OUTPUT_CIRCUIT_WINDOW,
        AI_PROVIDER_CIRCUIT_HTTP_STATUSES,
        AI_PROVIDER_CIRCUIT_WINDOW,
        AI_RATE_LIMIT_CIRCUIT_HTTP_STATUSES,
        AI_RATE_LIMIT_CIRCUIT_WINDOW,
        EXA_PROVIDER_CIRCUIT_HTTP_STATUSES,
        EXA_PROVIDER_CIRCUIT_WINDOW,
        EXA_RATE_LIMIT_CIRCUIT_HTTP_STATUSES,
        EXA_RATE_LIMIT_CIRCUIT_WINDOW,
        INVALID_DECISION_CIRCUIT_THRESHOLD,
        INVALID_DECISION_CIRCUIT_WINDOW,
        _ai_output_fingerprint,
        _ai_provider_fingerprint,
        _exa_provider_fingerprint,
        _global_research_budget_pause,
        _make_search_client,
    )
    from .exa import ExaError
    from .state import next_month_start

    readiness_issues: list[dict[str, Any]] = []
    with StateStore(state_target()) as store:
        counts = store.status_counts()
        due_now = store.due_count()
        invalid_decisions = store.recent_distinct_outcome_streak(
            "invalid_decision",
            limit=INVALID_DECISION_CIRCUIT_THRESHOLD,
            within=INVALID_DECISION_CIRCUIT_WINDOW,
        )
        decision_open = (
            invalid_decisions >= INVALID_DECISION_CIRCUIT_THRESHOLD
        )
        circuits: dict[str, dict[str, Any]] = {
            "decision": {
                "open": decision_open,
                "distinct_products": invalid_decisions,
                "threshold": INVALID_DECISION_CIRCUIT_THRESHOLD,
                "window_seconds": int(
                    INVALID_DECISION_CIRCUIT_WINDOW.total_seconds()
                ),
            }
        }
        if decision_open:
            readiness_issues.append({"type": "decision_circuit_open"})

        try:
            ai_settings = AISettings.from_env()
        except AIError as exc:
            safe_configuration_error = {
                "open": None,
                "reason": "configuration_invalid",
            }
            circuits["ai_provider"] = dict(safe_configuration_error)
            circuits["ai_output"] = dict(safe_configuration_error)
            readiness_issues.append(
                {
                    "type": "ai_configuration_invalid",
                    "error": _safe_error(exc),
                }
            )
        else:
            provider_fingerprint = _ai_provider_fingerprint(ai_settings)
            provider_event = store.recent_ai_provider_rejection_event(
                provider_fingerprint,
                http_statuses=AI_PROVIDER_CIRCUIT_HTTP_STATUSES,
                within=AI_PROVIDER_CIRCUIT_WINDOW,
            )
            provider_window = AI_PROVIDER_CIRCUIT_WINDOW
            provider_reason = "definitive_rejection"
            if provider_event is None:
                provider_event = store.recent_ai_provider_rejection_event(
                    provider_fingerprint,
                    http_statuses=AI_RATE_LIMIT_CIRCUIT_HTTP_STATUSES,
                    within=AI_RATE_LIMIT_CIRCUIT_WINDOW,
                )
                provider_window = AI_RATE_LIMIT_CIRCUIT_WINDOW
                provider_reason = "rate_limit"
            provider_open = provider_event is not None
            circuits["ai_provider"] = {
                "open": provider_open,
                "reason": provider_reason if provider_open else None,
                "http_status": (
                    provider_event.http_status
                    if provider_event is not None
                    else None
                ),
                "resume_at": (
                    provider_event.expires_at.isoformat()
                    if provider_event is not None
                    else None
                ),
                "window_seconds": int(provider_window.total_seconds()),
            }
            if provider_open:
                readiness_issues.append(
                    {
                        "type": "ai_provider_circuit_open",
                        "http_status": provider_event.http_status,
                        "resume_at": provider_event.expires_at.isoformat(),
                    }
                )

            invalid_output_products = (
                store.recent_ai_provider_error_products(
                    _ai_output_fingerprint(ai_settings),
                    error_types=AI_INVALID_OUTPUT_CIRCUIT_ERROR_TYPES,
                    categories=AI_INVALID_OUTPUT_CIRCUIT_CATEGORIES,
                    within=AI_INVALID_OUTPUT_CIRCUIT_WINDOW,
                )
            )
            output_open = (
                invalid_output_products
                >= AI_INVALID_OUTPUT_CIRCUIT_THRESHOLD
            )
            circuits["ai_output"] = {
                "open": output_open,
                "distinct_products": invalid_output_products,
                "threshold": AI_INVALID_OUTPUT_CIRCUIT_THRESHOLD,
                "window_seconds": int(
                    AI_INVALID_OUTPUT_CIRCUIT_WINDOW.total_seconds()
                ),
            }
            if output_open:
                readiness_issues.append(
                    {"type": "ai_output_circuit_open"}
                )

        try:
            exa_client = _make_search_client(timeout=1.0)
        except ExaError as exc:
            circuits["exa_provider"] = {
                "open": None,
                "reason": "configuration_invalid",
            }
            readiness_issues.append(
                {
                    "type": "exa_configuration_invalid",
                    "error": _safe_error(exc),
                }
            )
        else:
            exa_fingerprint = _exa_provider_fingerprint(exa_client)
            exa_event = store.recent_exa_provider_rejection(
                exa_fingerprint,
                http_statuses=EXA_PROVIDER_CIRCUIT_HTTP_STATUSES,
                within=EXA_PROVIDER_CIRCUIT_WINDOW,
            )
            exa_window = EXA_PROVIDER_CIRCUIT_WINDOW
            exa_reason = "definitive_rejection"
            if exa_event is None:
                exa_event = store.recent_exa_provider_rejection(
                    exa_fingerprint,
                    http_statuses=EXA_RATE_LIMIT_CIRCUIT_HTTP_STATUSES,
                    within=EXA_RATE_LIMIT_CIRCUIT_WINDOW,
                )
                exa_window = EXA_RATE_LIMIT_CIRCUIT_WINDOW
                exa_reason = "rate_limit"

            now = datetime.now(timezone.utc)
            month_start = datetime(
                now.year,
                now.month,
                1,
                tzinfo=timezone.utc,
            )
            quota_event = store.recent_exa_provider_error(
                exa_fingerprint,
                error_types={"ExaQuotaExhaustedError"},
                within=max(now - month_start, timedelta(microseconds=1)),
                now=now,
            )
            quota_open = (
                quota_event is not None
                and quota_event.finished_at >= month_start
            )
            exa_open = exa_event is not None or quota_open
            if quota_open:
                exa_reason = "monthly_quota_exhausted"
                exa_resume_at = next_month_start(now).isoformat()
            elif exa_event is not None:
                exa_resume_at = exa_event.expires_at.isoformat()
            else:
                exa_resume_at = None
            circuits["exa_provider"] = {
                "open": exa_open,
                "reason": exa_reason if exa_open else None,
                "http_status": (
                    exa_event.http_status
                    if exa_event is not None
                    else None
                ),
                "resume_at": exa_resume_at,
                "window_seconds": (
                    int(exa_window.total_seconds())
                    if not quota_open
                    else None
                ),
            }
            if exa_open:
                readiness_issues.append(
                    {
                        "type": (
                            "exa_quota_exhausted"
                            if quota_open
                            else "exa_provider_circuit_open"
                        ),
                        "http_status": (
                            exa_event.http_status
                            if exa_event is not None
                            else None
                        ),
                        "resume_at": exa_resume_at,
                    }
                )

        try:
            budget_pause = _global_research_budget_pause(store)
        except ConfigError as exc:
            research_budget = {
                "paused": None,
                "reason": "configuration_invalid",
            }
            readiness_issues.append(
                {
                    "type": "global_research_budget_configuration_invalid",
                    "error": _safe_error(exc),
                }
            )
        else:
            research_budget = {
                "paused": budget_pause is not None,
            }
            if budget_pause is not None:
                research_budget.update(
                    {
                        key: value
                        for key, value in budget_pause.items()
                        if key not in {"ok", "processed", "published"}
                    }
                )
                readiness_issues.append(
                    {
                        "type": str(budget_pause["reason"]),
                        "resume_at": budget_pause["resume_at"],
                    }
                )

        schema_version = store.schema_version

    return {
        "ok": True,
        "ready": not readiness_issues,
        "service": "pv-wiki-worker",
        "version": __version__,
        "state_schema_version": schema_version,
        "queue": {
            "counts": counts,
            "due_now": due_now,
        },
        "circuits": circuits,
        "research_budget": research_budget,
        "operations": {
            "run_one_busy": _RUN_LOCK.locked(),
            "catalogue_busy": _CATALOGUE_LOCK.locked(),
            "homepage_busy": _HOMEPAGE_LOCK.locked(),
        },
        "readiness_issues": readiness_issues,
    }


def _read_empty_json_object(handler: BaseHTTPRequestHandler) -> None:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise WorkerConfigError("invalid Content-Length") from exc
    if length < 0 or length > MAX_REQUEST_BYTES:
        raise WorkerConfigError("request body is too large")
    body = handler.rfile.read(length)
    if not body:
        return
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerConfigError("request body must be a JSON object") from exc
    if value != {}:
        raise WorkerConfigError("this endpoint accepts only an empty JSON object")


def _handler(
    settings: WorkerSettings,
    operations: dict[str, Callable[[], dict[str, Any]]],
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "PVWikiWorker"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            # BaseHTTPRequestHandler logs only method/path/status here. Request
            # headers and bodies, which may contain secrets/evidence, are never logged.
            super().log_message(format, *args)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=_json_default,
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not supplied.startswith(prefix):
                return False
            candidate = supplied[len(prefix) :]
            if not candidate or len(candidate) > MAX_TOKEN_LENGTH:
                return False
            try:
                candidate_bytes = candidate.encode("ascii")
                expected_bytes = settings.token.encode("ascii")
            except UnicodeEncodeError:
                return False
            return hmac.compare_digest(candidate_bytes, expected_bytes)

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/healthz":
                try:
                    self._send(HTTPStatus.OK, health())
                except Exception as exc:
                    self._send(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"ok": False, "error": _safe_error(exc)},
                    )
                return
            if path != "/status":
                self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                return
            if not self._authorized():
                self._send(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "unauthorized"},
                )
                return
            try:
                self._send(HTTPStatus.OK, status())
            except Exception as exc:
                self._send(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": _safe_error(exc)},
                )

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            operation = operations.get(path)
            if operation is None:
                self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                return
            if not self._authorized():
                self._send(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "unauthorized"},
                )
                return
            try:
                _read_empty_json_object(self)
            except WorkerConfigError as exc:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": _safe_error(exc)},
                )
                return
            operation_lock = {
                "/sync-catalogue": _CATALOGUE_LOCK,
                "/refresh-catalogue": _CATALOGUE_LOCK,
                "/publish-home": _HOMEPAGE_LOCK,
            }.get(path, _RUN_LOCK)
            if not operation_lock.acquire(blocking=False):
                if path == "/run-one":
                    self._send(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "processed": False,
                            "reason": "worker_busy",
                        },
                    )
                else:
                    self._send(
                        HTTPStatus.CONFLICT,
                        {
                            "ok": False,
                            "error": "another PV Wiki operation is running",
                        },
                    )
                return
            try:
                result = operation()
            except (
                AIError,
                ConfigError,
                ExaConfigError,
                WorkerConfigError,
            ) as exc:
                self._send(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "ok": False,
                        "error_type": exc.__class__.__name__,
                        "error": _safe_error(exc),
                    },
                )
            except Exception as exc:
                self._send(
                    HTTPStatus.BAD_GATEWAY,
                    {
                        "ok": False,
                        "error_type": exc.__class__.__name__,
                        "error": _safe_error(exc),
                    },
                )
            else:
                self._send(HTTPStatus.OK, result)
            finally:
                operation_lock.release()

    return Handler


def build_server(
    settings: WorkerSettings | None = None,
    *,
    operations: dict[str, Callable[[], dict[str, Any]]] | None = None,
) -> ThreadingHTTPServer:
    """Build the server, permitting injected operations for offline tests."""

    from .cli import (
        publish_home,
        refresh_catalogue,
        run_one,
        sync_catalogue,
    )

    active_settings = settings or WorkerSettings.from_env()
    active_operations = operations or {
        "/sync-catalogue": sync_catalogue,
        "/refresh-catalogue": refresh_catalogue,
        "/run-one": run_one,
        "/publish-home": publish_home,
    }
    return ThreadingHTTPServer(
        (active_settings.host, active_settings.port),
        _handler(active_settings, active_operations),
    )


def serve(settings: WorkerSettings | None = None) -> None:
    """Serve until interrupted; lifecycle is owned by Docker/systemd."""

    with build_server(settings) as server:
        server.serve_forever(poll_interval=0.5)


__all__ = [
    "MAX_REQUEST_BYTES",
    "WorkerConfigError",
    "WorkerSettings",
    "build_server",
    "health",
    "status",
    "serve",
]
