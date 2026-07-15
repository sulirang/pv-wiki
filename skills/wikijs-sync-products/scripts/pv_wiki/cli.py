"""Command-line orchestration for one bounded PV wiki maintenance cycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    ConfigError,
    WikiSettings,
    allow_mirrors,
    include_internal_search_hints,
    max_extract_chars,
    min_fact_confidence,
    min_publish_confidence,
    missing_environment,
    state_path,
)
from .db import DatabaseConfigurationError, ProductReader, validate_postgres_sslmode
from .decision import DecisionError, validate_decision
from .render import render_product_page, stable_path, stable_slug
from .state import Lease, LeaseLostError, StateError, StateStore
from .tavily import TavilyClient, TavilyError
from .wikijs import WikiJSClient, WikiJSConflictError, WikiJSError


REQUIRED_ENVIRONMENT = (
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSSLMODE",
    "TAVILY_API_KEY",
    "WIKIJS_URL",
    "WIKIJS_TOKEN",
)
SECRET_ENVIRONMENT = ("PGPASSWORD", "TAVILY_API_KEY", "WIKIJS_TOKEN")
MAX_JSON_INPUT_BYTES = 1_000_000


class CLIError(RuntimeError):
    """Raised for an operator-correctable CLI workflow error."""


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _emit(value: Any, *, stream: Any = None) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default),
        file=stream or sys.stdout,
    )


def _safe_error(error: BaseException) -> str:
    message = str(error) or error.__class__.__name__
    for name in SECRET_ENVIRONMENT:
        secret = os.getenv(name, "")
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:2000]


def _read_json(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CLIError(f"cannot read JSON file: {path}") from exc
    if size > MAX_JSON_INPUT_BYTES:
        raise CLIError(f"JSON file exceeds {MAX_JSON_INPUT_BYTES} bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise CLIError("JSON input must be an object")
    return value


def _store() -> StateStore:
    return StateStore(state_path())


def _require_lease(store: StateStore, token: str) -> Lease:
    lease = store.get_by_lease(token)
    if lease is None:
        raise LeaseLostError("lease is missing, expired, or already completed")
    check = store.precheck(lease)
    if not check.ready:
        raise LeaseLostError(f"lease cannot continue: {check.reason}")
    return lease


def _search_identity(product: dict[str, Any]) -> dict[str, Any]:
    """Map the known catalogue columns to Tavily's generic identity aliases."""

    public_name = str(product.get("product_name") or "").strip()
    if not public_name:
        raise CLIError(
            "product_name is empty; record insufficient_identity without Tavily"
        )
    identity: dict[str, Any] = {"model": public_name, "product_name": public_name}
    if include_internal_search_hints():
        brand = str(product.get("brand_code") or "").strip()
        family = str(product.get("family_code") or "").strip()
        if brand:
            identity["manufacturer"] = brand
        if family:
            identity["category"] = family
    return identity


def _worker_id(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()[:200]
    session = os.getenv("HERMES_SESSION_ID", "").strip()
    suffix = session[:80] if session else str(os.getpid())
    return f"hermes:{socket.gethostname()[:80]}:{suffix}"


def _tag_slug(prefix: str, value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return f"{prefix}-{stable_slug(text, max_length=64)}"
    except ValueError:
        return None


def _wiki_tags(product: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    tags = {"product", "datasheet-found", "managed-by-hermes"}
    for item in decision["datasheets"] + decision["sources"]:
        tags.add(f"source-{item['source_type']}")
    for prefix, key in (("brand", "brand_code"), ("family", "family_code")):
        tag = _tag_slug(prefix, product.get(key))
        if tag:
            tags.add(tag)
    return sorted(tags)


def _cmd_doctor(args: argparse.Namespace) -> int:
    missing = missing_environment(REQUIRED_ENVIRONMENT)
    state = _store()
    checks: dict[str, Any] = {
        "state": {
            "ok": True,
            "path": str(Path(state.path)),
            "schema_version": state.schema_version,
        },
        "environment": {"ok": not missing, "missing": missing},
        "live": bool(args.live),
    }
    state.close()

    wiki_settings: WikiSettings | None = None
    try:
        wiki_settings = WikiSettings.from_env()
        checks["wikijs_config"] = {"ok": True}
    except ConfigError as exc:
        checks["wikijs_config"] = {"ok": False, "error": _safe_error(exc)}

    pg_sslmode: str | None = None
    try:
        pg_sslmode = validate_postgres_sslmode()
        checks["postgresql_tls"] = {
            "ok": True,
            "sslmode": pg_sslmode,
            "minimum": "require",
        }
    except DatabaseConfigurationError as exc:
        checks["postgresql_tls"] = {"ok": False, "error": _safe_error(exc)}

    if args.live and not missing and wiki_settings is not None and pg_sslmode:
        generator = ProductReader().iter_products(batch_size=1)
        try:
            first = next(generator, None)
        finally:
            generator.close()
        checks["postgresql"] = {
            "ok": True,
            "read_only_transaction_enforced": True,
            "sslmode": pg_sslmode,
            "sample_product": first is not None,
        }
        client = WikiJSClient(
            wiki_settings.base_url,
            wiki_settings.token,
            timeout=wiki_settings.timeout,
            new_page_private=wiki_settings.new_page_private,
            new_page_published=wiki_settings.new_page_published,
        )
        probe_path = f"{wiki_settings.path_prefix}/__pv-wiki-doctor__"
        client.get_page(probe_path, wiki_settings.locale)
        checks["wikijs"] = {"ok": True, "read_probe": probe_path}
        checks["tavily"] = {
            "ok": True,
            "note": "key configured; no credits spent by doctor",
        }

    ok = (
        checks["environment"]["ok"]
        and checks["wikijs_config"]["ok"]
        and all(
            value.get("ok", True)
            for value in checks.values()
            if isinstance(value, dict)
        )
    )
    _emit({"ok": ok, "checks": checks})
    return 0 if ok else 2


def _cmd_sync_db(_args: argparse.Namespace) -> int:
    products = ProductReader().fetch_products(batch_size=500)
    with _store() as store:
        results = store.upsert_products(products)
        counts = store.status_counts()
    _emit(
        {
            "ok": True,
            "source_records": len(products),
            "created": sum(item.created for item in results),
            "changed": sum(item.changed and not item.created for item in results),
            "rescheduled": sum(item.rescheduled for item in results),
            "queue": counts,
        }
    )
    return 0


def _cmd_precheck(_args: argparse.Namespace) -> int:
    with _store() as store:
        due = store.list_due(limit=1_000_000)
        counts = store.status_counts()
    _emit(
        {
            "ok": True,
            "due": len(due),
            "wakeAgent": bool(due),
            "counts": counts,
        }
    )
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    with _store() as store:
        lease = store.lease_next(
            _worker_id(args.worker_id), lease_seconds=args.lease_seconds
        )
    if lease is None:
        _emit({"ok": True, "claimed": False})
        return 0
    _emit(
        {
            "ok": True,
            "claimed": True,
            "product": lease.payload,
            "product_id": lease.product_id,
            "source_hash": lease.source_hash,
            "lease_token": lease.token,
            "leased_until": lease.leased_until,
        }
    )
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    with _store() as store:
        lease = _require_lease(store, args.lease_token)
        store.begin_search(lease)
        try:
            client = TavilyClient(timeout=args.timeout)
            bundle = client.search_product(
                _search_identity(lease.payload), max_results=args.max_results
            )
            store.finish_search(
                lease,
                [item["url"] for item in bundle.get("results", [])],
                bundle.get("usage", {}),
            )
        except Exception as exc:
            try:
                store.record_outcome(
                    lease,
                    "tavily_error",
                    error=_safe_error(exc),
                )
            except StateError:
                pass
            raise
        # Refuse to present evidence for a product changed while Tavily ran.
        final_check = store.precheck(lease)
        if not final_check.ready:
            raise LeaseLostError(f"source changed during search: {final_check.reason}")
    _emit(
        {
            "ok": True,
            "product_id": lease.product_id,
            "lease_token": lease.token,
            "product": lease.payload,
            "search": bundle,
        }
    )
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    request = _read_json(args.request_file)
    urls = request.get("urls")
    query = request.get("query")
    if not isinstance(urls, list) or not isinstance(query, str):
        raise CLIError("extract request needs string query and array urls")
    with _store() as store:
        lease = _require_lease(store, args.lease_token)
        submitted = store.begin_extract(lease, urls)
        try:
            client = TavilyClient(timeout=args.timeout)
            bundle = client.extract_urls(submitted, query)
            store.finish_extract(lease, bundle.get("usage", {}))
            limit = max_extract_chars()
            for result in bundle.get("results", []):
                content = result.get("raw_content")
                if isinstance(content, str) and len(content) > limit:
                    result["raw_content_sha256"] = hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest()
                    result["raw_content"] = content[:limit]
                    result["truncated"] = True
        except Exception as exc:
            try:
                store.record_outcome(
                    lease,
                    "tavily_error",
                    error=_safe_error(exc),
                )
            except StateError:
                pass
            raise
        final_check = store.precheck(lease)
        if not final_check.ready:
            raise LeaseLostError(f"source changed during extract: {final_check.reason}")
    _emit(
        {
            "ok": True,
            "product_id": lease.product_id,
            "lease_token": lease.token,
            "extract": bundle,
        }
    )
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    raw = _read_json(args.decision_file)
    with _store() as store:
        lease = _require_lease(store, str(raw.get("lease_token") or ""))
        try:
            allowed_urls = set(store.allowed_evidence_urls(lease.token))
            decision = validate_decision(
                raw,
                expected_product_id=lease.product_id,
                expected_lease_token=lease.token,
                minimum_confidence=min_publish_confidence(),
                minimum_fact_confidence=min_fact_confidence(),
                mirrors_allowed=allow_mirrors(),
                allowed_evidence_urls=allowed_urls,
            )
        except DecisionError as exc:
            store.record_outcome(
                lease,
                "invalid_decision",
                error=_safe_error(exc),
            )
            raise

        if decision["outcome"] != "publish":
            outcome = store.record_outcome(
                lease,
                decision["outcome"],
                payload={"decision": decision},
            )
            _emit(
                {
                    "ok": True,
                    "published": False,
                    "product_id": lease.product_id,
                    "outcome": outcome.outcome,
                    "next_attempt_at": outcome.next_attempt_at,
                }
            )
            return 0

        path: str | None = None
        try:
            settings = WikiSettings.from_env()
            path = stable_path(lease.payload, prefix=settings.path_prefix)
            product = {
                **lease.payload,
                "manufacturer": decision.get("manufacturer")
                or lease.payload.get("brand_code"),
                "model": decision.get("model") or lease.payload.get("product_name"),
            }
            title = " ".join(
                str(
                    decision.get("model")
                    or lease.payload.get("product_name")
                    or lease.product_id
                ).split()
            )
            summary = " ".join(str(decision.get("summary") or "").split())
            description = (
                summary or f"Datasheet and cited specifications for {title}."
            )[:500]
            managed = render_product_page(
                product, decision, datetime.now(timezone.utc)
            )
            tags = _wiki_tags(lease.payload, decision)

            # The source may have changed after AI evaluation; check once more
            # immediately before the only external mutation in the workflow.
            check = store.precheck(lease)
            if not check.ready:
                raise LeaseLostError(f"lease cannot publish: {check.reason}")

            client = WikiJSClient(
                settings.base_url,
                settings.token,
                timeout=settings.timeout,
                new_page_private=settings.new_page_private,
                new_page_published=settings.new_page_published,
            )
            result = client.upsert_page(
                path,
                settings.locale,
                title,
                description,
                managed,
                tags,
            )
        except LeaseLostError:
            raise
        except Exception as exc:
            failure = (
                "wikijs_conflict"
                if isinstance(exc, WikiJSConflictError)
                else "wikijs_error"
                if isinstance(exc, WikiJSError)
                else "publish_error"
            )
            try:
                store.record_outcome(
                    lease,
                    failure,
                    payload={"decision": decision},
                    wiki_path=path,
                    error=_safe_error(exc),
                )
            except StateError:
                pass
            raise

        outcome = store.record_outcome(
            lease,
            "synced",
            payload={
                "decision": decision,
                "wiki_action": result.get("action"),
            },
            wiki_path=path,
        )

    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    _emit(
        {
            "ok": True,
            "published": True,
            "product_id": lease.product_id,
            "wiki": {
                "action": result.get("action"),
                "id": page.get("id"),
                "path": path,
                "locale": settings.locale,
            },
            "sources": len(decision["datasheets"]) + len(decision["sources"]),
            "next_attempt_at": outcome.next_attempt_at,
        }
    )
    return 0


def _cmd_fail(args: argparse.Namespace) -> int:
    with _store() as store:
        lease = _require_lease(store, args.lease_token)
        outcome = store.record_outcome(
            lease,
            "error",
            error=args.reason.strip()[:1000],
        )
    _emit(
        {
            "ok": True,
            "product_id": lease.product_id,
            "outcome": outcome.outcome,
            "next_attempt_at": outcome.next_attempt_at,
        }
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    with _store() as store:
        result: dict[str, Any] = {
            "ok": True,
            "state_path": str(Path(store.path)),
            "schema_version": store.schema_version,
            "counts": store.status_counts(),
            "due_now": len(store.list_due(limit=1_000_000)),
        }
        if args.product_id:
            product = store.get_product(args.product_id)
            result["product"] = product
            result["attempts"] = store.attempt_history(args.product_id)[-20:]
    _emit(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pv-wiki",
        description="Research one PostgreSQL product and safely maintain Wiki.js.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="validate configuration and optional live reads")
    doctor.add_argument("--live", action="store_true", help="read PostgreSQL and Wiki.js")
    doctor.set_defaults(func=_cmd_doctor)

    sync_db = subparsers.add_parser("sync-db", help="read PostgreSQL and refresh the local queue")
    sync_db.set_defaults(func=_cmd_sync_db)

    precheck = subparsers.add_parser("precheck", help="report whether any product is due")
    precheck.set_defaults(func=_cmd_precheck)

    claim = subparsers.add_parser("claim", help="atomically claim one due product")
    claim.add_argument("--worker-id")
    claim.add_argument("--lease-seconds", type=int, default=3600)
    claim.set_defaults(func=_cmd_claim)

    search = subparsers.add_parser("search", help="perform bounded Tavily searches")
    search.add_argument("--lease-token", required=True)
    search.add_argument("--max-results", type=int, default=5)
    search.add_argument("--timeout", type=float, default=30.0)
    search.set_defaults(func=_cmd_search)

    extract = subparsers.add_parser("extract", help="extract selected URLs through Tavily")
    extract.add_argument("--lease-token", required=True)
    extract.add_argument("--request-file", required=True)
    extract.add_argument("--timeout", type=float, default=30.0)
    extract.set_defaults(func=_cmd_extract)

    publish = subparsers.add_parser("publish", help="validate a decision and apply its outcome")
    publish.add_argument("--decision-file", required=True)
    publish.set_defaults(func=_cmd_publish)

    fail = subparsers.add_parser("fail", help="record a bounded failure and release a lease")
    fail.add_argument("--lease-token", required=True)
    fail.add_argument("--reason", required=True)
    fail.set_defaults(func=_cmd_fail)

    status = subparsers.add_parser("status", help="show queue counts and recent product attempts")
    status.add_argument("--product-id")
    status.set_defaults(func=_cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (
        CLIError,
        ConfigError,
        DecisionError,
        LeaseLostError,
        StateError,
        TavilyError,
        WikiJSError,
        ValueError,
    ) as exc:
        _emit(
            {
                "ok": False,
                "error_type": exc.__class__.__name__,
                "error": _safe_error(exc),
            },
            stream=sys.stderr,
        )
        return 2
    except Exception as exc:  # Last-resort JSON boundary for cron/CLI operation.
        _emit(
            {
                "ok": False,
                "error_type": exc.__class__.__name__,
                "error": _safe_error(exc),
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
