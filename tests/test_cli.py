from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wikijs-sync-products" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import ai, cli, state  # noqa: E402


def product() -> dict:
    return {
        "product_id": "P-42",
        "brand_code": "Acme",
        "family_code": "PV",
        "product_name": "PV-42",
        "unit_of_measure": "EA",
        "created_at": None,
        "updated_at": None,
    }


def decision(token: str, outcome: str = "publish") -> dict:
    value = {
        "schema_version": "1",
        "product_id": "P-42",
        "lease_token": token,
        "outcome": outcome,
        "confidence": 0.95 if outcome == "publish" else 0.6,
        "manufacturer": "Acme",
        "model": "PV-42",
        "product_category": "Solar Inverter",
        "summary": "Exact product match.",
        "decision_notes": "Official manufacturer model and document agree.",
        "datasheets": [],
        "sources": [],
        "facts": [],
        "conflicts": [],
    }
    if outcome == "publish":
        value["datasheets"] = [
            {
                "url": "https://acme.example/pv-42.pdf",
                "title": "PV-42 datasheet",
                "source_type": "manufacturer",
                "is_primary": True,
            }
        ]
        value["facts"] = [
            {
                "name": name,
                "category": category,
                "value": fact_value,
                "unit": unit,
                "confidence": 0.95,
                "evidence_urls": ["https://acme.example/pv-42.pdf"],
                "evidence_quotes": [
                    {
                        "url": "https://acme.example/pv-42.pdf",
                        "quote": (
                            f"PV-42 {name} {fact_value} {unit}"
                        ).strip(),
                    }
                ],
            }
            for name, category, fact_value, unit in (
                ("Rated power", "Output", 42, "W"),
                ("Input voltage", "Input", 48, "V"),
                ("Efficiency", "Efficiency", 98.5, "%"),
                ("Ingress protection", "General", "IP65", ""),
                ("Weight", "General", 12, "kg"),
            )
        ]
    return value


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "state.sqlite3"
        self.env = mock.patch.dict(
            os.environ,
            {"PV_WIKI_STATE_PATH": str(self.state_path)},
            clear=True,
        )
        self.env.start()
        with state.StateStore(self.state_path) as store:
            store.upsert_product(product())

    def tearDown(self) -> None:
        self.env.stop()
        self.tempdir.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(arguments))
        output = stdout.getvalue().strip()
        payload = json.loads(output) if output else {}
        return code, payload, stderr.getvalue()

    def claim(self) -> str:
        code, payload, error = self.run_cli(
            "claim", "--worker-id", "test-worker", "--lease-seconds", "3600"
        )
        self.assertEqual(0, code, error)
        self.assertTrue(payload["claimed"])
        return payload["lease_token"]

    def write_json(self, name: str, payload: dict) -> Path:
        path = Path(self.tempdir.name) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def prepare_evidence(self, token: str, url: str) -> None:
        with state.StateStore(self.state_path) as store:
            lease = store.get_by_lease(token)
            store.begin_search(lease)
            store.finish_search(lease, [url], {"credits": 3})
            store.begin_extract(lease, [url])
            store.finish_extract(lease, [url], {"credits": 1})

    def prepare_search(self, token: str, url: str) -> None:
        with state.StateStore(self.state_path) as store:
            lease = store.get_by_lease(token)
            store.begin_search(lease)
            store.finish_search(lease, [url], {"credits": 3})

    def configure_required_environment(self, *, sslmode: str) -> None:
        os.environ.update(
            {
                "PGHOST": "postgres.example.internal",
                "PGPORT": "5432",
                "PGDATABASE": "products",
                "PGUSER": "products_readonly",
                "PGPASSWORD": "database-secret",
                "PGSSLMODE": sslmode,
                "TAVILY_API_KEY": "tavily-secret",
                "AI_BASE_URL": "https://ai.example.com/v1",
                "AI_API_KEY": "ai-secret",
                "AI_MODEL": "example-model",
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "wiki-secret",
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON":
                    '{"Acme":["acme.example"]}',
            }
        )

    def configure_worker_environment(self) -> None:
        os.environ.update(
            {
                "AI_BASE_URL": "https://ai.example.com/v1",
                "AI_API_KEY": "ai-secret",
                "AI_MODEL": "test-model",
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "wiki-secret",
                "WIKIJS_LOCALE": "zh-cn",
                "WIKIJS_PATH_PREFIX": "products",
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON":
                    '{"Acme":["acme.example"]}',
            }
        )

    def test_doctor_lists_missing_sslmode_without_live_connections(self) -> None:
        with mock.patch.object(cli, "ProductReader") as reader:
            code, payload, error = self.run_cli("doctor")

        self.assertEqual(2, code, error)
        self.assertIn("PGSSLMODE", payload["checks"]["environment"]["missing"])
        self.assertFalse(payload["checks"]["postgresql_tls"]["ok"])
        reader.assert_not_called()

    def test_non_live_doctor_normalizes_secure_sslmode(self) -> None:
        self.configure_required_environment(sslmode=" VERIFY-FULL ")
        with mock.patch.object(cli, "ProductReader") as reader:
            code, payload, error = self.run_cli("doctor")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            "verify-full", payload["checks"]["postgresql_tls"]["sslmode"]
        )
        reader.assert_not_called()

    def test_live_doctor_accepts_explicit_prefer_mode_with_warning(self) -> None:
        self.configure_required_environment(sslmode="prefer")
        with (
            mock.patch.object(cli, "ProductReader") as reader,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("doctor", "--live")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["checks"]["postgresql_tls"]["ok"])
        self.assertEqual(
            "prefer", payload["checks"]["postgresql_tls"]["sslmode"]
        )
        self.assertIn("warning", payload["checks"]["postgresql_tls"])
        reader.assert_called_once()
        wiki_client.assert_called_once()

    def test_sync_db_refuses_unknown_sslmode_without_leaking_password(self) -> None:
        secret = "database-password-that-must-not-leak"
        os.environ.update({"PGSSLMODE": "unknown", "PGPASSWORD": secret})

        code, _, error = self.run_cli("sync-db")

        self.assertEqual(2, code)
        self.assertIn("DatabaseConfigurationError", error)
        self.assertNotIn(secret, error)

    def test_precheck_claim_and_status_are_resumable(self) -> None:
        code, before, _ = self.run_cli("precheck")
        self.assertEqual(0, code)
        self.assertEqual(1, before["due"])
        self.assertTrue(before["workAvailable"])

        self.claim()
        code, after, _ = self.run_cli("precheck")
        self.assertEqual(0, code)
        self.assertEqual(0, after["due"])
        self.assertFalse(after["workAvailable"])

    def test_sync_catalogue_resumes_quota_paused_product(self) -> None:
        paused_at = datetime.now(timezone.utc)
        with state.StateStore(self.state_path) as store:
            lease = store.lease_next("quota-worker", now=paused_at)
            store.record_outcome(
                lease,
                "tavily_quota_exhausted",
                error="monthly quota exhausted",
                now=paused_at,
            )

        reader = mock.Mock()
        reader.fetch_products.return_value = [product()]
        with mock.patch.object(cli, "ProductReader", return_value=reader):
            payload = cli.sync_catalogue()

        self.assertEqual(1, payload["quota_resumed"])
        self.assertEqual(1, payload["queue"]["due"])
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertEqual(0, current.consecutive_failures)

    def test_publish_home_upserts_managed_landing_page(self) -> None:
        with state.StateStore(self.state_path) as store:
            lease = store.lease_next("homepage-test")
            store.record_outcome(
                lease,
                "synced",
                payload={
                    "decision": {
                        "manufacturer": "Acme",
                        "model": "HeatPro 42",
                        "product_category": "热泵",
                    }
                },
                wiki_path="products/p-42-c8d5a4d2d3",
                now=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
        os.environ.update(
            {
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "wiki-secret",
                "WIKIJS_HOME_PATH": "home",
                "WIKIJS_HOME_TITLE": "PV Wiki",
            }
        )

        client = mock.Mock()
        client.upsert_page.return_value = {
            "action": "created",
            "page": {"id": 7},
        }
        with mock.patch.object(cli, "WikiJSClient", return_value=client):
            code, payload, error = self.run_cli("publish-home")

        self.assertEqual(0, code, error)
        self.assertEqual(
            {
                "action": "created",
                "id": 7,
                "locale": "en",
                "path": "home",
            },
            payload["home"],
        )
        self.assertEqual(1, payload["updated_products"])
        arguments = client.upsert_page.call_args.args
        self.assertEqual("home", arguments[0])
        self.assertEqual("PV Wiki", arguments[2])
        self.assertIn("当前已更新 **1** 款产品", arguments[4])
        self.assertIn("[热泵](/t/category-%E7%83%AD%E6%B3%B5)", arguments[4])
        self.assertIn("[Acme](/t/brand-acme)", arguments[4])
        self.assertIn("HeatPro 42", arguments[4])

    def test_search_uses_leased_snapshot_and_bounded_client(self) -> None:
        token = self.claim()
        client = mock.Mock()
        client.search_product.return_value = {
            "queries": ["q"],
            "results": [],
            "usage": {"credits": 3},
        }
        with mock.patch.object(cli, "TavilyClient", return_value=client):
            code, payload, error = self.run_cli(
                "search", "--lease-token", token, "--max-results", "5"
            )

        self.assertEqual(0, code, error)
        self.assertEqual("P-42", payload["product_id"])
        submitted = client.search_product.call_args.args[0]
        self.assertEqual("PV-42", submitted["model"])
        self.assertNotIn("manufacturer", submitted)
        self.assertNotIn("product_id", submitted)
        self.assertEqual(5, client.search_product.call_args.kwargs["max_results"])

        code, _, error = self.run_cli(
            "search", "--lease-token", token, "--max-results", "5"
        )
        self.assertEqual(2, code)
        self.assertIn("AttemptBudgetError", error)

    def test_internal_search_hints_never_treat_family_code_as_category(self) -> None:
        os.environ["PV_WIKI_TAVILY_INCLUDE_INTERNAL_HINTS"] = "true"

        identity = cli._search_identity(product())

        self.assertEqual("Acme", identity["manufacturer"])
        self.assertNotIn("category", identity)
        self.assertNotIn("family_code", identity)

    def test_legacy_public_category_fact_is_normalized_for_homepage(self) -> None:
        category = cli._decision_product_category(
            {
                "facts": [
                    {"name": "Product Category", "value": "Solar Inverter"}
                ],
                "family_code": "SO003",
            }
        )

        self.assertEqual("光伏逆变器", category)

    def test_unresolved_decision_records_backoff_without_wiki_credentials(self) -> None:
        token = self.claim()
        path = self.write_json("no-datasheet.json", decision(token, "no_datasheet"))

        code, payload, error = self.run_cli("publish", "--decision-file", str(path))

        self.assertEqual(0, code, error)
        self.assertFalse(payload["published"])
        self.assertEqual("no_datasheet", payload["outcome"])
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("backoff", current.status)

    def test_extract_is_bound_to_search_and_truncates_terminal_content(self) -> None:
        token = self.claim()
        url = "https://acme.example/pv-42.pdf"
        self.prepare_search(token, url)
        request = self.write_json(
            "extract.json", {"urls": [url], "query": "PV-42 specifications"}
        )
        client = mock.Mock()
        client.extract_urls.return_value = {
            "results": [
                {"url": url, "raw_content": "PV-42 " + ("x" * 40_000)}
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        with mock.patch.object(cli, "TavilyClient", return_value=client):
            code, payload, error = self.run_cli(
                "extract",
                "--lease-token",
                token,
                "--request-file",
                str(request),
            )

        self.assertEqual(0, code, error)
        result = payload["extract"]["results"][0]
        self.assertEqual(30_000, len(result["raw_content"]))
        self.assertTrue(result["truncated"])
        self.assertEqual(64, len(result["raw_content_sha256"]))
        with state.StateStore(self.state_path) as store:
            self.assertEqual([url], store.allowed_evidence_urls(token))

    def test_extract_identity_must_match_inside_the_bounded_ai_text(self) -> None:
        token = self.claim()
        url = "https://acme.example/wrong-variant.pdf"
        self.prepare_search(token, url)
        request = self.write_json(
            "extract-wrong.json",
            {"urls": [url], "query": "PV-42 specifications"},
        )
        client = mock.Mock()
        client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": (
                        "PV-420 wrong variant "
                        + ("x" * 31_000)
                        + " PV-42 related products"
                    ),
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        with mock.patch.object(cli, "TavilyClient", return_value=client):
            code, payload, error = self.run_cli(
                "extract",
                "--lease-token",
                token,
                "--request-file",
                str(request),
            )

        self.assertEqual(0, code, error)
        self.assertNotIn(
            "PV-42 related products",
            payload["extract"]["results"][0]["raw_content"],
        )
        with state.StateStore(self.state_path) as store:
            self.assertEqual([], store.allowed_evidence_urls(token))

    def test_extract_allows_series_datasheet_with_target_and_siblings(self) -> None:
        target = "SUN2000-8KTL-M1"
        with state.StateStore(self.state_path) as store:
            store.upsert_product({**product(), "product_name": target})
        token = self.claim()
        url = "https://solar.huawei.com/sun2000-m1-datasheet.pdf"
        self.prepare_search(token, url)
        request = self.write_json(
            "extract-series.json",
            {"urls": [url], "query": f"{target} specifications"},
        )
        client = mock.Mock()
        client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": (
                        "SUN2000-5KTL-M1 SUN2000-6KTL-M1 "
                        "SUN2000-8KTL-M1 SUN2000-10KTL-M1\n"
                        "Series datasheet specifications"
                    ),
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        with mock.patch.object(cli, "TavilyClient", return_value=client):
            code, _, error = self.run_cli(
                "extract",
                "--lease-token",
                token,
                "--request-file",
                str(request),
            )

        self.assertEqual(0, code, error)
        with state.StateStore(self.state_path) as store:
            self.assertEqual([url], store.allowed_evidence_urls(token))

    def test_extract_allows_candidate_with_prefixed_sibling_in_same_body(self) -> None:
        token = self.claim()
        url = "https://acme.example/hc-pv-42.pdf"
        self.prepare_search(token, url)
        request = self.write_json(
            "extract-prefixed-sibling.json",
            {"urls": [url], "query": "PV-42 specifications"},
        )
        client = mock.Mock()
        client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": (
                        "PV-42 hc-PV-42 Rated power 420 W\n"
                        "PV-42 hc-PV-42 Input voltage 480 V"
                    ),
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        with mock.patch.object(cli, "TavilyClient", return_value=client):
            code, _, error = self.run_cli(
                "extract",
                "--lease-token",
                token,
                "--request-file",
                str(request),
            )

        self.assertEqual(0, code, error)
        with state.StateStore(self.state_path) as store:
            self.assertEqual([url], store.allowed_evidence_urls(token))

    def test_official_decision_upserts_wiki_and_marks_synced(self) -> None:
        token = self.claim()
        self.prepare_evidence(token, "https://acme.example/pv-42.pdf")
        path = self.write_json("publish.json", decision(token))
        evidence_path = self.write_json(
            "evidence.json",
            {
                "results": [
                    {
                        "url": "https://acme.example/pv-42.pdf",
                        "raw_content": (
                            "PV-42 Rated power 42 W\n"
                            "PV-42 Input voltage 48 V\n"
                            "PV-42 Efficiency 98.5%\n"
                            "PV-42 Ingress protection IP65\n"
                            "PV-42 Weight 12 kg"
                        ),
                    }
                ]
            },
        )
        os.environ.update(
            {
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "secret-token",
                "WIKIJS_LOCALE": "zh-cn",
                "WIKIJS_PATH_PREFIX": "products",
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON":
                    '{"Acme":["acme.example"]}',
            }
        )
        client = mock.Mock()
        client.upsert_page.return_value = {
            "action": "created",
            "page": {"id": 42},
        }
        with mock.patch.object(cli, "WikiJSClient", return_value=client):
            code, payload, error = self.run_cli(
                "publish",
                "--decision-file",
                str(path),
                "--evidence-file",
                str(evidence_path),
            )

        self.assertEqual(0, code, error)
        self.assertTrue(payload["published"])
        self.assertEqual("products/p-42-c8d5a4d2d3", payload["wiki"]["path"])
        self.assertEqual("created", payload["wiki"]["action"])
        tags = client.upsert_page.call_args.args[5]
        self.assertIn("managed-by-pv-wiki", tags)
        self.assertNotIn("managed-by-hermes", tags)
        self.assertNotIn("family-pv", tags)
        self.assertIn("category-光伏逆变器", tags)
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("synced", current.status)

    def test_invalid_decision_closes_lease_with_transient_backoff(self) -> None:
        token = self.claim()
        path = self.write_json("invalid.json", decision(token))
        os.environ["PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON"] = (
            '{"Acme":["acme.example"]}'
        )

        code, _, error = self.run_cli("publish", "--decision-file", str(path))

        self.assertEqual(2, code)
        self.assertIn("DecisionError", error)
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("backoff", current.status)
            self.assertEqual("invalid_decision", current.last_outcome)

    def test_run_one_no_due_product_is_a_clean_noop(self) -> None:
        with state.StateStore(self.state_path) as store:
            lease = store.lease_next("setup")
            store.record_outcome(lease, "synced")

        with (
            mock.patch.object(cli, "TavilyClient") as tavily_client,
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual("no_due_product", payload["reason"])
        tavily_client.assert_not_called()
        ai_client.assert_not_called()
        wiki_client.assert_not_called()

    def test_run_one_stops_cleanly_when_all_tavily_quotas_are_exhausted(self) -> None:
        search_client = mock.Mock()
        search_client.search_product.side_effect = (
            cli.TavilyQuotaExhaustedError("monthly quota exhausted")
        )

        with (
            mock.patch.object(cli, "TavilyClient", return_value=search_client),
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("tavily_quota_exhausted", payload["reason"])
        resume_at = datetime.fromisoformat(payload["resume_at"])
        self.assertEqual(1, resume_at.day)
        self.assertEqual((0, 0, 0), (resume_at.hour, resume_at.minute, resume_at.second))
        ai_client.assert_not_called()
        wiki_client.assert_not_called()

        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("tavily_quota_exhausted", current.last_outcome)
            self.assertEqual(0, current.consecutive_failures)
            self.assertEqual(resume_at, current.next_run_at)

    def test_run_one_happy_path_uses_configured_ai_and_publishes(self) -> None:
        self.configure_worker_environment()
        url = "https://acme.example/pv-42.pdf"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [
                {
                    "title": "Official datasheet",
                    "url": url,
                    "content": "official",
                    "score": 0.99,
                }
            ],
            "usage": {"credits": 3},
        }
        extract_client = mock.Mock()
        extract_client.extract_urls.return_value = {
            "query": "PV-42 specifications",
            "results": [
                {
                    "url": url,
                    "raw_content": (
                        "PV-42 Rated power 42 W\n"
                        "PV-42 Input voltage 48 V\n"
                        "PV-42 Efficiency 98.5%\n"
                        "PV-42 Ingress protection IP65\n"
                        "PV-42 Weight 12 kg"
                    ),
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()
        ai_client.decide.return_value = decision("forged-token")
        wiki_client = mock.Mock()
        wiki_client.upsert_page.return_value = {
            "action": "created",
            "page": {"id": 42},
        }

        with (
            mock.patch.object(
                cli,
                "TavilyClient",
                side_effect=[search_client, extract_client],
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ) as ai_factory,
            mock.patch.object(
                cli,
                "WikiJSClient",
                return_value=wiki_client,
            ),
        ):
            code, payload, error = self.run_cli(
                "run-one",
                "--worker-id",
                "n8n-test",
            )

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertTrue(payload["published"])
        self.assertEqual("synced", payload["outcome"])
        ai_factory.assert_called_once()
        self.assertEqual("test-model", ai_factory.call_args.args[0].model)
        self.assertEqual(
            [url],
            [
                item["url"]
                for item in ai_client.decide.call_args.kwargs["extract"]["results"]
            ],
        )
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("synced", current.status)
            attempt = store.attempt_history("P-42")[-1]
            self.assertEqual("test-model", attempt.details["payload"]["automation"]["ai"]["model"])

    def test_run_one_blank_name_never_calls_external_services(self) -> None:
        blank = {**product(), "product_name": ""}
        with state.StateStore(self.state_path) as store:
            store.upsert_product(blank)

        with (
            mock.patch.object(cli, "TavilyClient") as tavily_client,
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("insufficient_identity", payload["outcome"])
        tavily_client.assert_not_called()
        ai_client.assert_not_called()
        wiki_client.assert_not_called()

    def test_run_one_empty_search_records_content_backoff_without_ai(self) -> None:
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [],
            "usage": {"credits": 3},
        }
        with (
            mock.patch.object(cli, "TavilyClient", return_value=search_client),
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("no_datasheet", payload["outcome"])
        ai_client.assert_not_called()

    def test_run_one_empty_extract_records_transient_tavily_error(self) -> None:
        url = "https://acme.example/pv-42.pdf"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        extract_client = mock.Mock()
        extract_client.extract_urls.return_value = {
            "results": [],
            "failed_results": [{"url": url, "error": "unavailable"}],
            "usage": {"credits": 1},
        }
        with (
            mock.patch.object(
                cli,
                "TavilyClient",
                side_effect=[search_client, extract_client],
            ),
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
        ):
            code, _, error = self.run_cli("run-one")

        self.assertEqual(2, code)
        self.assertIn("no successfully extracted evidence", error)
        ai_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("tavily_error", current.last_outcome)

    def test_run_one_rejects_extract_without_complete_catalogue_model(self) -> None:
        url = "https://acme.example/pv-43.pdf"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        extract_client = mock.Mock()
        extract_client.extract_urls.return_value = {
            "results": [
                {"url": url, "raw_content": "PV-43 specifications only"}
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        with (
            mock.patch.object(
                cli,
                "TavilyClient",
                side_effect=[search_client, extract_client],
            ),
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("insufficient_identity", payload["outcome"])
        ai_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("insufficient_identity", current.last_outcome)

    def test_run_one_passes_series_datasheet_to_ai(self) -> None:
        self.configure_worker_environment()
        url = "https://acme.example/pv-series.pdf"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        extract_client = mock.Mock()
        extract_client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": (
                        "PV-41 PV-42 PV-43 series datasheet specifications"
                    ),
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()
        ai_client.decide.return_value = decision("forged-token", "ambiguous")
        with (
            mock.patch.object(
                cli,
                "TavilyClient",
                side_effect=[search_client, extract_client],
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ) as ai_factory,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("ambiguous", payload["outcome"])
        ai_factory.assert_called_once()
        ai_client.decide.assert_called_once()
        self.assertEqual(
            [url],
            [
                item["url"]
                for item in ai_client.decide.call_args.kwargs["extract"]["results"]
            ],
        )
        wiki_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("ambiguous", current.last_outcome)

    def test_run_one_passes_prefixed_sibling_candidate_to_ai(self) -> None:
        self.configure_worker_environment()
        url = "https://acme.example/hc-pv-42.pdf"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        extract_client = mock.Mock()
        extract_client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": "PV-42 hc PV-42 specifications only",
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()
        ai_client.decide.return_value = decision("forged-token", "ambiguous")
        with (
            mock.patch.object(
                cli,
                "TavilyClient",
                side_effect=[search_client, extract_client],
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ) as ai_factory,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("ambiguous", payload["outcome"])
        ai_factory.assert_called_once()
        ai_client.decide.assert_called_once()
        wiki_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("ambiguous", current.last_outcome)

    def test_run_one_ai_failure_records_short_transient_backoff(self) -> None:
        self.configure_worker_environment()
        url = "https://acme.example/pv-42.pdf"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        extract_client = mock.Mock()
        extract_client.extract_urls.return_value = {
            "results": [
                {"url": url, "raw_content": "PV-42 specifications"}
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()
        ai_client.decide.side_effect = ai.AIResponseError("invalid AI response")
        with (
            mock.patch.object(
                cli,
                "TavilyClient",
                side_effect=[search_client, extract_client],
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, _, error = self.run_cli("run-one")

        self.assertEqual(2, code)
        self.assertIn("AIResponseError", error)
        wiki_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("ai_error", current.last_outcome)

    def test_run_one_source_change_during_ai_releases_stale_lease(self) -> None:
        self.configure_worker_environment()
        url = "https://acme.example/pv-42.pdf"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        extract_client = mock.Mock()
        extract_client.extract_urls.return_value = {
            "results": [
                {"url": url, "raw_content": "PV-42 specifications"}
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()

        def change_source(**_kwargs: object) -> dict:
            with state.StateStore(self.state_path) as store:
                store.upsert_product(
                    {**product(), "product_name": "PV-42 revised"}
                )
            return decision("forged-token")

        ai_client.decide.side_effect = change_source
        with (
            mock.patch.object(
                cli,
                "TavilyClient",
                side_effect=[search_client, extract_client],
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, _, error = self.run_cli("run-one")

        self.assertEqual(2, code)
        self.assertIn("source changed during AI analysis", error)
        wiki_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertEqual("stale_source", current.last_outcome)


if __name__ == "__main__":
    unittest.main()
