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
from .ai import AIError, AISettings, OpenAICompatibleClient
from .config import (
    ConfigError,
    WikiSettings,
    allow_mirrors,
    include_internal_search_hints,
    max_extract_chars,
    min_fact_confidence,
    min_publish_confidence,
    missing_environment,
    redact_environment_secrets,
    state_path,
    trusted_source_domain_map,
    trusted_source_domains,
)
from .db import DatabaseConfigurationError, ProductReader, validate_postgres_sslmode
from .decision import (
    DecisionError,
    canonical_product_category,
    text_contains_exact_identity,
    validate_decision,
)
from .render import render_home_page, render_product_page, stable_path, stable_slug
from .server import WorkerConfigError, WorkerSettings, serve
from .state import (
    Lease,
    LeaseLostError,
    StateError,
    StateStore,
    next_month_start,
)
from .tavily import TavilyClient, TavilyError, TavilyQuotaExhaustedError
from .wikijs import WikiJSClient, WikiJSConflictError, WikiJSError


REQUIRED_ENVIRONMENT = (
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSSLMODE",
    "WIKIJS_URL",
    "WIKIJS_TOKEN",
    "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON",
)
SECRET_ENVIRONMENT = (
    "PGPASSWORD",
    "TAVILY_API_KEY",
    "TAVILY_API_KEYS",
    "WIKIJS_TOKEN",
    "AI_API_KEY",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "PV_WIKI_WORKER_TOKEN",
)
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
    return redact_environment_secrets(
        message,
        SECRET_ENVIRONMENT,
        limit=2000,
    )


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


def _read_evidence_texts(path_value: str) -> dict[str, str]:
    payload = _read_json(path_value)
    container = payload.get("extract", payload)
    if not isinstance(container, dict):
        raise CLIError("evidence file must contain an extract object")
    results = container.get("results")
    if not isinstance(results, list) or len(results) > 5:
        raise CLIError("evidence file must contain at most 5 extract results")
    evidence: dict[str, str] = {}
    for item in results:
        if not isinstance(item, dict):
            raise CLIError("evidence extract results must be objects")
        url = item.get("url")
        body = item.get("raw_content")
        if not isinstance(url, str) or not isinstance(body, str) or not body.strip():
            continue
        if url in evidence and evidence[url] != body:
            raise CLIError("evidence file has duplicate URLs with different content")
        evidence[url] = body
    return evidence


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
        if brand:
            identity["manufacturer"] = brand
    return identity


def _worker_id(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()[:200]
    return f"pv-wiki:{socket.gethostname()[:80]}:{os.getpid()}"


def _tag_slug(prefix: str, value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return f"{prefix}-{stable_slug(text, max_length=64)}"
    except ValueError:
        return None


def _wiki_tags(product: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    tags = {"product", "datasheet-found", "managed-by-pv-wiki"}
    for item in decision["datasheets"] + decision["sources"]:
        tags.add(f"source-{item['source_type']}")
    brand_tag = _tag_slug(
        "brand", decision.get("manufacturer") or product.get("brand_code")
    )
    if brand_tag:
        tags.add(brand_tag)
    category_tag = _tag_slug(
        "category", _decision_product_category(decision)
    )
    if category_tag:
        tags.add(category_tag)
    return sorted(tags)


def _decision_product_category(decision: dict[str, Any]) -> str:
    """Read the public category, including a narrow legacy-fact fallback."""

    value = decision.get("product_category")
    if isinstance(value, str) and value.strip():
        return canonical_product_category(value)
    for fact in decision.get("facts", []):
        if not isinstance(fact, dict):
            continue
        name = str(fact.get("name") or "").strip().casefold()
        if name not in {
            "product category",
            "product type",
            "产品类别",
            "产品类型",
        }:
            continue
        legacy_value = fact.get("value")
        if isinstance(legacy_value, str) and legacy_value.strip():
            return canonical_product_category(legacy_value)
    return ""


def _home_catalogue_product(item: Any, *, path_prefix: str) -> dict[str, Any]:
    payload = item.payload
    decision = item.decision
    return {
        "product_id": item.product_id,
        "product_name": payload.get("product_name"),
        "model": decision.get("model") or payload.get("product_name"),
        "manufacturer": (
            decision.get("manufacturer") or payload.get("brand_code")
        ),
        "product_category": _decision_product_category(decision),
        "wiki_path": item.wiki_path or stable_path(payload, prefix=path_prefix),
        "published_at": item.published_at,
    }


def _cmd_doctor(args: argparse.Namespace) -> int:
    missing = missing_environment(REQUIRED_ENVIRONMENT)
    # Tavily keys: accept either TAVILY_API_KEYS (multi) or TAVILY_API_KEY (single).
    tavily_keys = os.getenv("TAVILY_API_KEYS", "").strip()
    tavily_single = os.getenv("TAVILY_API_KEY", "").strip()
    if not tavily_keys and not tavily_single:
        missing.append("TAVILY_API_KEYS or TAVILY_API_KEY")
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

    trusted_domain_mapping: dict[str, frozenset[str]] | None = None
    try:
        trusted_domain_mapping = trusted_source_domain_map()
        checks["trusted_sources_config"] = {
            "ok": True,
            "brand_mappings": len(trusted_domain_mapping),
            "domains": sum(
                len(domains) for domains in trusted_domain_mapping.values()
            ),
        }
    except ConfigError as exc:
        checks["trusted_sources_config"] = {
            "ok": False,
            "error": _safe_error(exc),
        }

    ai_settings: AISettings | None = None
    try:
        ai_settings = AISettings.from_env()
        checks["ai_config"] = {
            "ok": True,
            "base_url": ai_settings.base_url,
            "model": ai_settings.model,
            "note": "configuration validated; doctor does not spend AI tokens",
        }
    except AIError as exc:
        checks["ai_config"] = {"ok": False, "error": _safe_error(exc)}

    pg_sslmode: str | None = None
    try:
        pg_sslmode = validate_postgres_sslmode()
        checks["postgresql_tls"] = {
            "ok": True,
            "sslmode": pg_sslmode,
            "recommended": "verify-full",
        }
        if pg_sslmode in {"disable", "allow", "prefer"}:
            checks["postgresql_tls"]["warning"] = (
                "catalogue transport may be unencrypted; use this mode only "
                "after the operator accepts the network risk"
            )
    except DatabaseConfigurationError as exc:
        checks["postgresql_tls"] = {"ok": False, "error": _safe_error(exc)}

    if (
        args.live
        and not missing
        and wiki_settings is not None
        and ai_settings is not None
        and trusted_domain_mapping is not None
        and pg_sslmode
    ):
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
        try:
            client.get_page(probe_path, wiki_settings.locale)
        except WikiJSError as exc:
            if "does not exist" in str(exc).casefold():
                pass  # Page not found is expected — API is reachable.
            else:
                raise
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
    _emit(sync_catalogue())
    return 0


def sync_catalogue() -> dict[str, Any]:
    """Refresh the durable queue from the read-only product catalogue."""

    products = ProductReader().fetch_products(batch_size=500)
    with _store() as store:
        results = store.upsert_products(products)
        quota_resumed = store.resume_tavily_quota_waits()
        counts = store.status_counts()
    return {
        "ok": True,
        "source_records": len(products),
        "created": sum(item.created for item in results),
        "changed": sum(item.changed and not item.created for item in results),
        "rescheduled": sum(item.rescheduled for item in results),
        "quota_resumed": quota_resumed,
        "queue": counts,
    }


def _cmd_precheck(_args: argparse.Namespace) -> int:
    with _store() as store:
        due = store.list_due(limit=1_000_000)
        counts = store.status_counts()
    _emit(
        {
            "ok": True,
            "due": len(due),
            "workAvailable": bool(due),
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


def _run_search(
    store: StateStore,
    lease: Lease,
    *,
    max_results: int,
    timeout: float,
) -> dict[str, Any]:
    store.begin_search(lease)
    try:
        client = TavilyClient(timeout=timeout)
        bundle = client.search_product(
            _search_identity(lease.payload), max_results=max_results
        )
        store.finish_search(
            lease,
            [
                item["url"]
                for item in bundle.get("results", [])
                if isinstance(item, dict) and isinstance(item.get("url"), str)
            ],
            bundle.get("usage", {}),
        )
    except Exception as exc:
        try:
            store.record_outcome(
                lease,
                (
                    "tavily_quota_exhausted"
                    if isinstance(exc, TavilyQuotaExhaustedError)
                    else "tavily_error"
                ),
                error=_safe_error(exc),
            )
        except StateError:
            pass
        raise
    final_check = store.precheck(lease)
    if not final_check.ready:
        raise LeaseLostError(f"source changed during search: {final_check.reason}")
    return bundle


def _cmd_search(args: argparse.Namespace) -> int:
    with _store() as store:
        lease = _require_lease(store, args.lease_token)
        bundle = _run_search(
            store,
            lease,
            max_results=args.max_results,
            timeout=args.timeout,
        )
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


def _run_extract(
    store: StateStore,
    lease: Lease,
    *,
    urls: list[str],
    query: str,
    timeout: float,
) -> dict[str, Any]:
    submitted = store.begin_extract(lease, urls)
    try:
        client = TavilyClient(timeout=timeout)
        bundle = client.extract_urls(submitted, query)
        limit = max_extract_chars()
        for result in bundle.get("results", []):
            content = result.get("raw_content")
            if isinstance(content, str) and len(content) > limit:
                result["raw_content_sha256"] = hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest()
                result["raw_content"] = content[:limit]
                result["truncated"] = True
        expected_identity = str(lease.payload.get("product_name") or "")
        successful_urls = [
            item["url"]
            for item in bundle.get("results", [])
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and isinstance(item.get("raw_content"), str)
            and item["raw_content"].strip()
            and text_contains_exact_identity(
                expected_identity,
                item["raw_content"],
            )
        ]
        store.finish_extract(
            lease,
            successful_urls,
            bundle.get("usage", {}),
        )
    except Exception as exc:
        try:
            store.record_outcome(
                lease,
                (
                    "tavily_quota_exhausted"
                    if isinstance(exc, TavilyQuotaExhaustedError)
                    else "tavily_error"
                ),
                error=_safe_error(exc),
            )
        except StateError:
            pass
        raise
    final_check = store.precheck(lease)
    if not final_check.ready:
        raise LeaseLostError(f"source changed during extract: {final_check.reason}")
    return bundle


def _cmd_extract(args: argparse.Namespace) -> int:
    request = _read_json(args.request_file)
    urls = request.get("urls")
    query = request.get("query")
    if not isinstance(urls, list) or not isinstance(query, str):
        raise CLIError("extract request needs string query and array urls")
    with _store() as store:
        lease = _require_lease(store, args.lease_token)
        bundle = _run_extract(
            store,
            lease,
            urls=urls,
            query=query,
            timeout=args.timeout,
        )
    _emit(
        {
            "ok": True,
            "product_id": lease.product_id,
            "lease_token": lease.token,
            "extract": bundle,
        }
    )
    return 0


def _apply_decision(
    store: StateStore,
    lease: Lease,
    raw: dict[str, Any],
    *,
    audit: dict[str, Any] | None = None,
    evidence_text_by_url: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        allowed_urls = set(store.allowed_evidence_urls(lease.token))
        trusted_domains = (
            trusted_source_domains(
                str(lease.payload.get("brand_code") or "")
            )
            if raw.get("outcome") == "publish"
            else frozenset()
        )
        decision = validate_decision(
            raw,
            expected_product_id=lease.product_id,
            expected_lease_token=lease.token,
            minimum_confidence=min_publish_confidence(),
            minimum_fact_confidence=min_fact_confidence(),
            mirrors_allowed=allow_mirrors(),
            allowed_evidence_urls=allowed_urls,
            trusted_source_domains=trusted_domains,
            expected_product_name=str(
                lease.payload.get("product_name") or ""
            ),
            evidence_text_by_url=evidence_text_by_url,
        )
    except DecisionError as exc:
        store.record_outcome(
            lease,
            "invalid_decision",
            error=_safe_error(exc),
        )
        raise
    except ConfigError as exc:
        store.record_outcome(
            lease,
            "configuration_error",
            error=_safe_error(exc),
        )
        raise

    audit_payload: dict[str, Any] = {"decision": decision}
    if audit:
        audit_payload["automation"] = audit

    if decision["outcome"] != "publish":
        outcome = store.record_outcome(
            lease,
            decision["outcome"],
            payload=audit_payload,
        )
        return {
            "ok": True,
            "processed": True,
            "published": False,
            "product_id": lease.product_id,
            "outcome": outcome.outcome,
            "next_attempt_at": outcome.next_attempt_at,
        }

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
                payload=audit_payload,
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
            **audit_payload,
            "wiki_action": result.get("action"),
        },
        wiki_path=path,
    )
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    return {
        "ok": True,
        "processed": True,
        "published": True,
        "product_id": lease.product_id,
        "outcome": outcome.outcome,
        "wiki": {
            "action": result.get("action"),
            "id": page.get("id"),
            "path": path,
            "locale": settings.locale,
        },
        "sources": len(decision["datasheets"]) + len(decision["sources"]),
        "next_attempt_at": outcome.next_attempt_at,
    }


def _cmd_publish(args: argparse.Namespace) -> int:
    raw = _read_json(args.decision_file)
    evidence_text_by_url = (
        _read_evidence_texts(args.evidence_file)
        if args.evidence_file
        else None
    )
    with _store() as store:
        lease = _require_lease(store, str(raw.get("lease_token") or ""))
        result = _apply_decision(
            store,
            lease,
            raw,
            evidence_text_by_url=evidence_text_by_url,
        )
    _emit(result)
    return 0


def _nonpublish_decision(
    lease: Lease,
    outcome: str,
    note: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "product_id": lease.product_id,
        "lease_token": lease.token,
        "outcome": outcome,
        "confidence": 1.0,
        "manufacturer": "",
        "model": str(lease.payload.get("product_name") or "").strip(),
        "product_category": "",
        "summary": "",
        "review_summary": "",
        "review_evidence_urls": [],
        "decision_notes": note,
        "datasheets": [],
        "sources": [],
        "facts": [],
        "conflicts": [],
    }


def _record_active_failure(
    store: StateStore,
    lease: Lease,
    outcome: str,
    error: BaseException | str,
) -> None:
    try:
        active = store.get_by_lease(lease.token)
        if active is not None:
            safe_exception = (
                error if isinstance(error, BaseException) else Exception(error)
            )
            store.record_outcome(
                active,
                outcome,
                error=_safe_error(safe_exception),
            )
    except StateError:
        pass


def run_one(
    *,
    worker_id: str | None = None,
    lease_seconds: int = 3600,
    max_results: int = 5,
    tavily_timeout: float = 30.0,
) -> dict[str, Any]:
    """Run one bounded Search → Extract → AI → Wiki.js product cycle."""

    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= 5
    ):
        raise CLIError("run-one max_results must be between 1 and 5")
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or not 300 <= lease_seconds <= 7200
    ):
        raise CLIError("run-one lease_seconds must be between 300 and 7200")
    if (
        isinstance(tavily_timeout, bool)
        or not isinstance(tavily_timeout, (int, float))
        or not 1 <= float(tavily_timeout) <= 120
    ):
        raise CLIError("run-one tavily_timeout must be between 1 and 120 seconds")

    with _store() as store:
        lease = store.lease_next(
            _worker_id(worker_id),
            lease_seconds=lease_seconds,
        )
        if lease is None:
            return {
                "ok": True,
                "processed": False,
                "reason": "no_due_product",
            }

        try:
            product_name = " ".join(
                str(lease.payload.get("product_name") or "").split()
            )
            if not product_name:
                return _apply_decision(
                    store,
                    lease,
                    _nonpublish_decision(
                        lease,
                        "insufficient_identity",
                        "The catalogue record has no public product name.",
                    ),
                )

            search = _run_search(
                store,
                lease,
                max_results=max_results,
                timeout=tavily_timeout,
            )
            urls = [
                item["url"]
                for item in search.get("results", [])[:5]
                if isinstance(item, dict) and isinstance(item.get("url"), str)
            ]
            if not urls:
                return _apply_decision(
                    store,
                    lease,
                    _nonpublish_decision(
                        lease,
                        "no_datasheet",
                        "The bounded search returned no public candidates.",
                    ),
                    audit={
                        "tavily": {
                            "search_usage": search.get("usage", {}),
                            "candidate_count": 0,
                        }
                    },
                )

            query = (
                f"{product_name[:180]} official datasheet complete specifications "
                "input output efficiency protection communication dimensions "
                "weight review reliability"
            )
            extract = _run_extract(
                store,
                lease,
                urls=urls,
                query=query[:400],
                timeout=tavily_timeout,
            )
            extracted = [
                item
                for item in extract.get("results", [])
                if isinstance(item, dict)
                and isinstance(item.get("url"), str)
                and isinstance(item.get("raw_content"), str)
                and item["raw_content"].strip()
            ]
            if not extracted:
                error = CLIError(
                    "Tavily returned no successfully extracted evidence"
                )
                _record_active_failure(store, lease, "tavily_error", error)
                raise error
            identity_urls = set(store.allowed_evidence_urls(lease.token))
            evidence = [
                item for item in extracted if item["url"] in identity_urls
            ]
            if not evidence:
                return _apply_decision(
                    store,
                    lease,
                    _nonpublish_decision(
                        lease,
                        "insufficient_identity",
                        "Extracted candidates did not contain the complete "
                        "catalogue product name.",
                    ),
                    audit={
                        "tavily": {
                            "search_usage": search.get("usage", {}),
                            "extract_usage": extract.get("usage", {}),
                            "candidate_count": len(urls),
                            "evidence_count": 0,
                        }
                    },
                )

            try:
                trusted_source_domains(
                    str(lease.payload.get("brand_code") or "")
                )
            except ConfigError as exc:
                _record_active_failure(
                    store,
                    lease,
                    "configuration_error",
                    exc,
                )
                raise

            check = store.precheck(lease)
            if not check.ready:
                raise LeaseLostError(
                    f"lease cannot call AI: {check.reason}"
                )

            try:
                ai_settings = AISettings.from_env()
                proposal = OpenAICompatibleClient(ai_settings).decide(
                    product=lease.payload,
                    search=search,
                    extract={**extract, "results": evidence},
                )
            except AIError as exc:
                _record_active_failure(store, lease, "ai_error", exc)
                raise

            check = store.precheck(lease)
            if not check.ready:
                raise LeaseLostError(
                    f"source changed during AI analysis: {check.reason}"
                )

            raw = {
                **proposal,
                "schema_version": "1",
                "product_id": lease.product_id,
                "lease_token": lease.token,
            }
            return _apply_decision(
                store,
                lease,
                raw,
                evidence_text_by_url={
                    item["url"]: item["raw_content"]
                    for item in evidence
                },
                audit={
                    "ai": {"model": ai_settings.model},
                    "tavily": {
                        "search_usage": search.get("usage", {}),
                        "extract_usage": extract.get("usage", {}),
                        "candidate_count": len(urls),
                        "evidence_count": len(evidence),
                    },
                },
            )
        except TavilyQuotaExhaustedError:
            return {
                "ok": True,
                "processed": False,
                "published": False,
                "reason": "tavily_quota_exhausted",
                "resume_at": next_month_start().isoformat(),
            }

        except LeaseLostError as exc:
            _record_active_failure(store, lease, "error", exc)
            raise
        except (
            AIError,
            ConfigError,
            DecisionError,
            StateError,
            TavilyError,
            WikiJSError,
            CLIError,
        ):
            raise
        except Exception as exc:
            _record_active_failure(store, lease, "error", exc)
            raise


def _cmd_run_one(args: argparse.Namespace) -> int:
    _emit(
        run_one(
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            max_results=args.max_results,
            tavily_timeout=args.tavily_timeout,
        )
    )
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    configured = WorkerSettings.from_env()
    settings = WorkerSettings(
        token=configured.token,
        host=args.host or configured.host,
        port=args.port or configured.port,
    )
    serve(settings)
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


def _cmd_publish_home(_args: argparse.Namespace) -> int:
    _emit(publish_home())
    return 0


def publish_home() -> dict[str, Any]:
    """Create or update the reader-facing catalogue landing page."""

    settings = WikiSettings.from_env()
    with _store() as store:
        counts = store.status_counts()
        due_now = len(store.list_due(limit=1_000_000))
        published = store.published_products()

    managed = render_home_page(
        [
            _home_catalogue_product(item, path_prefix=settings.path_prefix)
            for item in published
        ],
        title=settings.home_title,
    )
    client = WikiJSClient(
        settings.base_url,
        settings.token,
        timeout=settings.timeout,
        new_page_private=settings.new_page_private,
        new_page_published=settings.new_page_published,
    )
    result = client.upsert_page(
        settings.home_path,
        settings.locale,
        settings.home_title,
        "按品牌、产品类别和最近更新浏览经过资料核验的产品百科。",
        managed,
        ["homepage", "managed-by-pv-wiki", "product-catalogue"],
    )
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    return {
        "ok": True,
        "home": {
            "action": result.get("action"),
            "id": page.get("id"),
            "path": settings.home_path,
            "locale": settings.locale,
        },
        "updated_products": len(published),
        "counts": counts,
        "due_now": due_now,
    }


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
    publish.add_argument(
        "--evidence-file",
        help="Tavily extract JSON required when the decision outcome is publish",
    )
    publish.set_defaults(func=_cmd_publish)

    run_one_parser = subparsers.add_parser(
        "run-one",
        help="process one due product with Tavily, configured AI, and Wiki.js",
    )
    run_one_parser.add_argument("--worker-id")
    run_one_parser.add_argument("--lease-seconds", type=int, default=3600)
    run_one_parser.add_argument("--max-results", type=int, default=5)
    run_one_parser.add_argument("--tavily-timeout", type=float, default=30.0)
    run_one_parser.set_defaults(func=_cmd_run_one)

    serve_parser = subparsers.add_parser(
        "serve",
        help="serve fixed authenticated operations for an internal n8n workflow",
    )
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)
    serve_parser.set_defaults(func=_cmd_serve)

    fail = subparsers.add_parser("fail", help="record a bounded failure and release a lease")
    fail.add_argument("--lease-token", required=True)
    fail.add_argument("--reason", required=True)
    fail.set_defaults(func=_cmd_fail)

    status = subparsers.add_parser("status", help="show queue counts and recent product attempts")
    status.add_argument("--product-id")
    status.set_defaults(func=_cmd_status)

    publish_home = subparsers.add_parser(
        "publish-home",
        help="create or update the managed Wiki.js landing page",
    )
    publish_home.set_defaults(func=_cmd_publish_home)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (
        CLIError,
        AIError,
        ConfigError,
        DecisionError,
        LeaseLostError,
        StateError,
        TavilyError,
        WorkerConfigError,
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
    except Exception as exc:  # Last-resort JSON boundary for scheduler/CLI use.
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
