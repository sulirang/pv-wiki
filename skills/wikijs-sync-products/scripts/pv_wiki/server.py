"""Small authenticated HTTP boundary for n8n-to-PV-Wiki operations."""

from __future__ import annotations

import hmac
import json
import os
import threading
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
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
    state_path,
)
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

    with StateStore(state_path()) as store:
        schema_version = store.schema_version
    return {
        "ok": True,
        "service": "pv-wiki-worker",
        "version": __version__,
        "state_schema_version": schema_version,
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
            if path != "/healthz":
                self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                return
            try:
                self._send(HTTPStatus.OK, health())
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
            except (AIError, ConfigError, WorkerConfigError) as exc:
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
    "serve",
]
