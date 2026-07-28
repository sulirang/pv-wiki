from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wikijs-sync-products" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import ai, cli, documents, state  # noqa: E402


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

    def test_requeue_command_wakes_only_the_requested_cutover_outcome(
        self,
    ) -> None:
        attempted_at = datetime.now(timezone.utc)
        with state.StateStore(self.state_path) as store:
            lease = store.lease_next("old-policy", now=attempted_at)
            store.record_outcome(
                lease,
                "invalid_decision",
                error="old policy",
                now=attempted_at,
            )

        code, payload, error = self.run_cli(
            "requeue",
            "--outcome",
            "invalid_decision",
            "--reason",
            "validation-policy-2026-07-27.2",
            "--attempted-after",
            (attempted_at - timedelta(seconds=1)).isoformat(),
            "--attempted-before",
            (attempted_at + timedelta(seconds=1)).isoformat(),
        )

        self.assertEqual(0, code, error)
        self.assertEqual(1, payload["requeued"])
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertEqual("invalid_decision", current.last_outcome)

    def configure_required_environment(self, *, sslmode: str) -> None:
        os.environ.update(
            {
                "PGHOST": "postgres.example.internal",
                "PGPORT": "5432",
                "PGDATABASE": "products",
                "PGUSER": "products_readonly",
                "PGPASSWORD": "database-secret",
                "PGSSLMODE": sslmode,
                "EXA_API_KEY": "exa-secret",
                "AI_BASE_URL": "https://ai.example.com/v1",
                "AI_API_KEY": "ai-secret",
                "AI_MODEL": "example-model",
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "wiki-secret",
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON":
                    '{"Acme":["acme.example"]}',
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON":
                    '{"Acme":"Acme"}',
            }
        )

    def configure_worker_environment(self) -> None:
        os.environ.update(
            {
                "EXA_API_KEY": "exa-secret",
                "AI_BASE_URL": "https://ai.example.com/v1",
                "AI_API_KEY": "ai-secret",
                "AI_MODEL": "test-model",
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "wiki-secret",
                "WIKIJS_LOCALE": "zh-cn",
                "WIKIJS_PATH_PREFIX": "products",
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON":
                    '{"Acme":["acme.example"]}',
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON":
                    '{"Acme":"Acme"}',
            }
        )

    def test_doctor_lists_missing_sslmode_without_live_connections(self) -> None:
        with mock.patch.object(cli, "ProductReader") as reader:
            code, payload, error = self.run_cli("doctor")

        self.assertEqual(2, code, error)
        self.assertIn("PGSSLMODE", payload["checks"]["environment"]["missing"])
        self.assertFalse(payload["checks"]["postgresql_tls"]["ok"])
        reader.assert_not_called()

    def test_exa_bundle_credits_fail_closed_when_usage_is_invalid(
        self,
    ) -> None:
        for bundle in (
            {},
            {"usage": {}},
            {"usage": {"credits": True}},
            {"usage": {"credits": -1}},
            {"usage": {"credits": float("nan")}},
        ):
            with self.subTest(bundle=bundle), self.assertRaises(
                cli.ExaResponseError
            ):
                cli._credits(bundle)

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

    def test_doctor_validates_exa_without_spending(self) -> None:
        self.configure_required_environment(sslmode="verify-full")
        with (
            mock.patch.object(cli, "ProductReader") as reader,
            mock.patch.object(cli, "ExaClient") as exa_client,
        ):
            code, payload, error = self.run_cli("doctor")

        self.assertEqual(0, code, error)
        self.assertEqual(
            "exa",
            payload["checks"]["search_provider_config"]["provider"],
        )
        reader.assert_not_called()
        exa_client.assert_not_called()

    def test_doctor_rejects_global_budget_below_product_reservation(
        self,
    ) -> None:
        self.configure_required_environment(sslmode="verify-full")
        os.environ["PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT"] = "10"

        code, payload, error = self.run_cli("doctor")

        self.assertEqual(2, code, error)
        self.assertFalse(
            payload["checks"]["global_research_budget_config"]["ok"]
        )
        self.assertIn(
            "at least PV_WIKI_RESEARCH_MAX_CREDITS",
            payload["checks"]["global_research_budget_config"]["error"],
        )

    def test_search_client_factory_always_uses_exa(self) -> None:
        sentinel = mock.Mock()
        with mock.patch.object(
            cli,
            "ExaClient",
            return_value=sentinel,
        ) as factory:
            result = cli._make_search_client(timeout=12.5)

        self.assertIs(sentinel, result)
        factory.assert_called_once_with(timeout=12.5)

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
                "search_quota_exhausted",
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

    def test_sync_catalogue_inherits_brand_from_exact_model_sibling(self) -> None:
        reader = mock.Mock()
        reader.fetch_products.return_value = [
            {
                **product(),
                "product_id": "H1-4K-S2",
                "brand_code": "SAJ",
                "product_name": "4kW inverter ibrido, 2MPPT",
            },
            {
                **product(),
                "product_id": "KIT H1-4K-S2",
                "brand_code": None,
                "family_code": None,
                "product_name": "inv 4 kw + 10 kwh",
            },
        ]
        with mock.patch.object(cli, "ProductReader", return_value=reader):
            payload = cli.sync_catalogue()

        self.assertEqual(1, payload["brands_inferred"])
        with state.StateStore(self.state_path) as store:
            bundle = store.get_product("KIT H1-4K-S2")
            self.assertEqual("SAJ", bundle.payload["brand_code"])

    def test_sync_catalogue_does_not_fuzzy_infer_missing_brand(self) -> None:
        reader = mock.Mock()
        reader.fetch_products.return_value = [
            {
                **product(),
                "product_id": "H1-4K-S20",
                "brand_code": "SAJ",
            },
            {
                **product(),
                "product_id": "KIT H1-4K-S2",
                "brand_code": None,
                "product_name": "inv 4 kw + 10 kwh",
            },
        ]
        with mock.patch.object(cli, "ProductReader", return_value=reader):
            payload = cli.sync_catalogue()

        self.assertEqual(0, payload["brands_inferred"])
        with state.StateStore(self.state_path) as store:
            bundle = store.get_product("KIT H1-4K-S2")
            self.assertIsNone(bundle.payload["brand_code"])

    def test_daily_catalogue_refresh_preserves_quota_wait(self) -> None:
        paused_at = datetime.now(timezone.utc)
        with state.StateStore(self.state_path) as store:
            lease = store.lease_next("quota-worker", now=paused_at)
            outcome = store.record_outcome(
                lease,
                "search_quota_exhausted",
                error="monthly quota exhausted",
                now=paused_at,
            )

        reader = mock.Mock()
        reader.fetch_products.return_value = [product()]
        with mock.patch.object(cli, "ProductReader", return_value=reader):
            payload = cli.refresh_catalogue()

        self.assertEqual(0, payload["quota_resumed"])
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("backoff", current.status)
            self.assertEqual(outcome.next_attempt_at, current.next_run_at)

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
        with mock.patch.object(cli, "ExaClient", return_value=client):
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
        os.environ["PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS"] = "true"

        identity = cli._search_identity(product())

        self.assertNotIn("manufacturer", identity)
        self.assertNotIn("category", identity)
        self.assertNotIn("family_code", identity)

    def test_internal_brand_hint_requires_operator_trust_mapping(self) -> None:
        os.environ["PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS"] = "true"
        os.environ["PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON"] = (
            '{"Acme":["acme.example"]}'
        )
        os.environ["PV_WIKI_PUBLIC_BRAND_ALIASES_JSON"] = (
            '{"Acme":"Acme Solar"}'
        )

        identity = cli._search_identity(product())

        self.assertEqual("Acme Solar", identity["manufacturer"])

    def test_bundled_brand_registry_drives_identity_and_official_domains(
        self,
    ) -> None:
        os.environ["PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS"] = "true"

        identity = cli._search_identity(
            {
                **product(),
                "brand_code": "FOX",
                "product_id": "R125-G2",
                "product_name": (
                    "125000W Three Phase 380V/60HZ,10 MPPT inverter"
                ),
            }
        )

        self.assertEqual("R125-G2", identity["model"])
        self.assertEqual("Fox ESS", identity["manufacturer"])
        self.assertEqual(["fox-ess.com"], identity["_search_domains"])
        self.assertNotIn("125000W", identity.get("model_candidates", []))

    def test_identity_extracts_model_from_space_separated_kit_code(self) -> None:
        os.environ["PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS"] = "true"

        identity = cli._search_identity(
            {
                **product(),
                "brand_code": "",
                "product_id": "KIT HS2-5K-S2",
                "product_name": "Hybrid inverter and battery kit",
            }
        )

        self.assertEqual("HS2-5K-S2", identity["model"])

    def test_identity_keeps_named_series_without_digits(self) -> None:
        os.environ["PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS"] = "true"

        identity = cli._search_identity(
            {
                **product(),
                "product_id": "MIRA BMS",
                "product_name": (
                    "High voltage lithium battery BMS for Mira 2500"
                ),
            }
        )

        self.assertEqual("MIRA BMS", identity["model"])
        self.assertTrue(
            cli.text_contains_catalogue_identity(
                "MIRA BMS",
                "High voltage lithium battery BMS for Mira 2500",
                "FoxESS MIRA BMS datasheet",
            )
        )

    def test_internal_company_identifier_is_not_promoted_as_model(self) -> None:
        os.environ["PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS"] = "true"

        with self.assertRaisesRegex(
            cli.CLIError,
            "catalogue identity is empty",
        ):
            cli._search_identity(
                {
                    **product(),
                    "product_id": "SEARCH4SOLAR BV",
                    "product_name": "46",
                    "brand_code": "",
                }
            )

    def test_company_suffix_id_falls_back_to_public_product_description(
        self,
    ) -> None:
        os.environ["PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS"] = "true"
        public_name = "8000W Three Phase, Dual MPPT hybrid inverter"

        identity = cli._search_identity(
            {
                **product(),
                "product_id": "SEARCH4SOLAR-BV",
                "product_name": public_name,
                "brand_code": "",
            }
        )

        self.assertEqual(public_name, identity["model"])
        self.assertNotIn("SEARCH4SOLAR-BV", identity.values())

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

    def test_catalogue_sync_waits_for_the_final_publish_fence(self) -> None:
        self.configure_worker_environment()
        with state.StateStore(self.state_path) as store:
            lease = store.lease_next("publish-fence-worker")
        proposal = decision(lease.token)
        changed_product = {
            **product(),
            "product_name": "PV-43",
        }

        wiki_entered = threading.Event()
        allow_wiki_finish = threading.Event()
        catalogue_fetched = threading.Event()
        sync_finished = threading.Event()
        publish_result: dict = {}
        sync_result: dict = {}
        errors: list[BaseException] = []

        wiki_client = mock.Mock()

        def upsert_page(*_args, **_kwargs) -> dict:
            wiki_entered.set()
            if not allow_wiki_finish.wait(timeout=5):
                raise TimeoutError("test did not release the Wiki mutation")
            return {"action": "created", "page": {"id": 7}}

        wiki_client.upsert_page.side_effect = upsert_page
        reader = mock.Mock()

        def fetch_products(*, batch_size: int) -> list[dict]:
            self.assertEqual(500, batch_size)
            catalogue_fetched.set()
            return [changed_product]

        reader.fetch_products.side_effect = fetch_products

        def publish() -> None:
            try:
                with state.StateStore(self.state_path) as store:
                    publish_result["value"] = cli._apply_decision(
                        store,
                        lease,
                        proposal,
                        validated_decision=proposal,
                    )
            except BaseException as exc:
                errors.append(exc)

        def sync() -> None:
            try:
                sync_result["value"] = cli.sync_catalogue()
            except BaseException as exc:
                errors.append(exc)
            finally:
                sync_finished.set()

        publish_thread = threading.Thread(target=publish)
        sync_thread = threading.Thread(target=sync)
        with (
            mock.patch.object(
                cli,
                "WikiJSClient",
                return_value=wiki_client,
            ),
            mock.patch.object(
                cli,
                "ProductReader",
                return_value=reader,
            ),
        ):
            publish_thread.start()
            try:
                self.assertTrue(wiki_entered.wait(timeout=2))
                sync_thread.start()
                self.assertTrue(catalogue_fetched.wait(timeout=2))
                self.assertFalse(sync_finished.wait(timeout=0.1))
            finally:
                allow_wiki_finish.set()
                publish_thread.join(timeout=5)
                if sync_thread.ident is not None:
                    sync_thread.join(timeout=5)

        self.assertFalse(publish_thread.is_alive())
        self.assertFalse(sync_thread.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(publish_result["value"]["published"])
        self.assertEqual("synced", publish_result["value"]["outcome"])
        self.assertEqual(1, sync_result["value"]["changed"])
        wiki_client.upsert_page.assert_called_once()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertEqual("source_changed", current.last_outcome)
            self.assertEqual("PV-43", current.payload["product_name"])

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
        with mock.patch.object(cli, "ExaClient", return_value=client):
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

    def test_extract_merges_complementary_direct_pdf_and_exa_text(
        self,
    ) -> None:
        os.environ["PV_WIKI_PDF_DIRECT_FETCH"] = "true"
        token = self.claim()
        url = "https://acme.example/pv-42.pdf"
        self.prepare_search(token, url)
        request = self.write_json(
            "extract-direct-pdf.json",
            {"urls": [url], "query": "PV-42 specifications"},
        )
        client = mock.Mock()
        client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": "PV-42 Exa full text fallback",
                    "content_source": "exa_full_text",
                }
            ],
            "failed_results": [],
            "usage": {"credits": 2},
        }
        direct = documents.PDFEvidence(
            text=(
                "[PDF page 1/2]\n"
                "Rated power 42 W Input voltage 48 V "
                "Efficiency 98.5 percent\n"
                "[End PDF page 1]"
            ),
            requested_url=url,
            final_url="https://cdn.acme.example/pv-42.pdf",
            sha256="a" * 64,
            page_count=2,
            extracted_pages=1,
            truncated=False,
        )
        with (
            mock.patch.object(cli, "ExaClient", return_value=client),
            mock.patch.object(
                cli,
                "extract_pdf_evidence",
                return_value=direct,
            ) as extract_pdf,
        ):
            code, payload, error = self.run_cli(
                "extract",
                "--lease-token",
                token,
                "--request-file",
                str(request),
            )

        self.assertEqual(0, code, error)
        result = payload["extract"]["results"][0]
        self.assertEqual(
            "direct_pdf_text+exa_text",
            result["content_source"],
        )
        self.assertEqual(
            "identity_mismatch",
            result["pdf_direct_status"],
        )
        self.assertEqual(2, result["pdf_page_count"])
        self.assertEqual(1, result["pdf_extracted_pages"])
        self.assertEqual("a" * 64, result["pdf_sha256"])
        self.assertTrue(result["pdf_final_url_changed"])
        self.assertEqual(
            "cdn.acme.example",
            result["pdf_final_hostname"],
        )
        self.assertTrue(result["identity_verified"])
        self.assertIn("[PDF page 1/2]", result["raw_content"])
        self.assertIn("PV-42 Exa full text fallback", result["raw_content"])
        extract_pdf.assert_called_once_with(
            url,
            max_bytes=12_000_000,
            max_pages=80,
            max_chars=30_000,
            download_timeout=20.0,
            parse_timeout=15.0,
        )

    def test_extract_recovers_direct_pdf_when_exa_returns_only_failure(
        self,
    ) -> None:
        os.environ["PV_WIKI_PDF_DIRECT_FETCH"] = "true"
        token = self.claim()
        url = "https://acme.example/pv-42.pdf"
        self.prepare_search(token, url)
        request = self.write_json(
            "extract-direct-only.json",
            {"urls": [url], "query": "PV-42 specifications"},
        )
        client = mock.Mock()
        client.extract_urls.return_value = {
            "results": [],
            "failed_results": [
                {"url": url, "error": "provider could not parse PDF"}
            ],
            "usage": {"credits": 2},
        }
        direct = documents.PDFEvidence(
            text=(
                "[PDF page 1/1]\n"
                "PV-42 Rated power 42 W\n"
                "[End PDF page 1]"
            ),
            requested_url=url,
            final_url=url,
            sha256="b" * 64,
            page_count=1,
            extracted_pages=1,
            truncated=False,
        )
        with (
            mock.patch.object(cli, "ExaClient", return_value=client),
            mock.patch.object(
                cli,
                "extract_pdf_evidence",
                return_value=direct,
            ),
        ):
            code, payload, error = self.run_cli(
                "extract",
                "--lease-token",
                token,
                "--request-file",
                str(request),
            )

        self.assertEqual(0, code, error)
        self.assertEqual(1, len(payload["extract"]["results"]))
        result = payload["extract"]["results"][0]
        self.assertEqual(url, result["url"])
        self.assertEqual("direct_pdf_text", result["content_source"])
        self.assertEqual("used", result["pdf_direct_status"])
        self.assertTrue(result["identity_verified"])
        with state.StateStore(self.state_path) as store:
            self.assertEqual([url], store.allowed_evidence_urls(token))

    def test_extract_keeps_exa_text_when_direct_pdf_fails(self) -> None:
        os.environ["PV_WIKI_PDF_DIRECT_FETCH"] = "true"
        token = self.claim()
        url = "https://acme.example/pv-42.pdf"
        self.prepare_search(token, url)
        request = self.write_json(
            "extract-direct-pdf-fallback.json",
            {"urls": [url], "query": "PV-42 specifications"},
        )
        client = mock.Mock()
        client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": "PV-42 Exa full text fallback",
                    "content_source": "exa_full_text",
                }
            ],
            "failed_results": [],
            "usage": {"credits": 2},
        }
        with (
            mock.patch.object(cli, "ExaClient", return_value=client),
            mock.patch.object(
                cli,
                "extract_pdf_evidence",
                side_effect=documents.PDFDownloadError("blocked safely"),
            ),
        ):
            code, payload, error = self.run_cli(
                "extract",
                "--lease-token",
                token,
                "--request-file",
                str(request),
            )

        self.assertEqual(0, code, error)
        result = payload["extract"]["results"][0]
        self.assertEqual("exa_full_text", result["content_source"])
        self.assertEqual("failed", result["pdf_direct_status"])
        self.assertEqual(
            "PDFDownloadError",
            result["pdf_direct_error_type"],
        )
        self.assertTrue(result["identity_verified"])

    def test_extract_over_limit_prefers_identity_bound_exa_text_and_keeps_pdf_proof(
        self,
    ) -> None:
        os.environ["PV_WIKI_PDF_DIRECT_FETCH"] = "true"
        url = "https://acme.example/pv-42.pdf"
        exa_text = "PV-42 " + ("E" * 70)
        bundle = {
            "results": [
                {
                    "url": url,
                    "raw_content": exa_text,
                    "content_source": "exa_full_text",
                }
            ]
        }
        direct = documents.PDFEvidence(
            text="D" * 70,
            requested_url=url,
            final_url=url,
            sha256="c" * 64,
            page_count=3,
            extracted_pages=2,
            truncated=False,
        )
        lease = mock.Mock(
            product_id="P-42",
            payload=product(),
        )

        with (
            mock.patch.object(cli, "max_extract_chars", return_value=90),
            mock.patch.object(
                cli,
                "extract_pdf_evidence",
                return_value=direct,
            ),
        ):
            summary = cli._prepare_extract_results(lease, bundle)

        result = bundle["results"][0]
        self.assertEqual(exa_text, result["raw_content"])
        self.assertEqual("exa_full_text", result["content_source"])
        self.assertTrue(result["truncated"])
        self.assertTrue(result["identity_verified"])
        self.assertEqual(
            "identity_mismatch",
            result["pdf_direct_status"],
        )
        self.assertEqual("c" * 64, result["pdf_sha256"])
        self.assertEqual(3, result["pdf_page_count"])
        self.assertEqual(2, result["pdf_extracted_pages"])
        self.assertEqual(
            {
                "attempted": 1,
                "used": 0,
                "failed": 0,
                "identity_mismatch": 1,
            },
            summary,
        )

    def test_extract_strips_provider_forged_pdf_attestation(self) -> None:
        url = "https://acme.example/pv-42.pdf"
        bundle = {
            "results": [
                {
                    "url": url,
                    "raw_content": "PV-42 ordinary provider text",
                    "pdf_direct_status": "used",
                    "pdf_sha256": "d" * 64,
                    "pdf_page_count": 2,
                    "pdf_extracted_pages": 2,
                    "pdf_final_hostname": "attacker.example.net",
                }
            ]
        }
        lease = mock.Mock(
            product_id="P-42",
            payload=product(),
        )

        summary = cli._prepare_extract_results(lease, bundle)

        result = bundle["results"][0]
        self.assertNotIn("pdf_direct_status", result)
        self.assertNotIn("pdf_sha256", result)
        self.assertNotIn("pdf_page_count", result)
        self.assertNotIn("pdf_extracted_pages", result)
        self.assertNotIn("pdf_final_hostname", result)
        self.assertTrue(result["identity_verified"])
        self.assertEqual(
            {
                "attempted": 0,
                "used": 0,
                "failed": 0,
                "identity_mismatch": 0,
            },
            summary,
        )

    def test_merge_extract_bundles_preserves_direct_pdf_metadata(
        self,
    ) -> None:
        url = "https://acme.example/pv-42.pdf"

        merged = cli._merge_extract_bundles(
            {
                "results": [
                    {
                        "url": url,
                        "raw_content": "short PDF-derived text",
                        "identity_verified": False,
                        "pdf_direct_status": "identity_mismatch",
                        "pdf_sha256": "a" * 64,
                        "pdf_page_count": 4,
                        "pdf_extracted_pages": 3,
                        "pdf_final_url_changed": True,
                        "pdf_final_hostname": "cdn.acme.example",
                    }
                ],
                "usage": {"credits": 1},
            },
            {
                "results": [
                    {
                        "url": url,
                        "raw_content": (
                            "PV-42 longer Exa text selected for this URL"
                        ),
                        "identity_verified": True,
                    }
                ],
                "usage": {"credits": 1},
            },
        )

        result = merged["results"][0]
        self.assertEqual(
            "PV-42 longer Exa text selected for this URL",
            result["raw_content"],
        )
        self.assertEqual(
            "identity_mismatch",
            result["pdf_direct_status"],
        )
        self.assertEqual("a" * 64, result["pdf_sha256"])
        self.assertEqual(4, result["pdf_page_count"])
        self.assertEqual(3, result["pdf_extracted_pages"])
        self.assertTrue(result["pdf_final_url_changed"])
        self.assertEqual(
            "cdn.acme.example",
            result["pdf_final_hostname"],
        )

    def test_research_evidence_context_rejects_html_and_open_redirect(
        self,
    ) -> None:
        os.environ["PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON"] = (
            '{"Acme":["acme.example"]}'
        )
        pdf_url = "https://docs.acme.example/pv-42.pdf"
        mismatch_pdf_url = "https://acme.example/other-series.pdf"
        open_redirect_url = "https://acme.example/open-redirect.pdf"
        html_url = "https://acme.example/pv-42"
        incomplete_pdf_url = "https://acme.example/incomplete.pdf"
        store = mock.Mock()
        store.allowed_evidence_urls.return_value = [
            pdf_url,
            mismatch_pdf_url,
            open_redirect_url,
            html_url,
            incomplete_pdf_url,
        ]
        lease = mock.Mock(token="lease-token", payload=product())
        metadata = {
            "pdf_direct_status": "used",
            "pdf_sha256": "b" * 64,
            "pdf_page_count": 2,
            "pdf_extracted_pages": 1,
            "pdf_final_hostname": "cdn.acme.example",
        }

        (
            evidence_text,
            classification_urls,
            classification_text,
            verified_pdf_urls,
        ) = cli._research_evidence_context(
            store,
            lease,
            {
                "results": [
                    {
                        "url": pdf_url,
                        "raw_content": "PV-42 verified PDF content",
                        **metadata,
                    },
                    {
                        "url": mismatch_pdf_url,
                        "raw_content": "PV-42 provider text only",
                        **{
                            **metadata,
                            "pdf_direct_status": "identity_mismatch",
                        },
                    },
                    {
                        "url": open_redirect_url,
                        "raw_content": "PV-42 attacker-hosted PDF content",
                        **{
                            **metadata,
                            "pdf_final_hostname": "attacker.example.net",
                        },
                    },
                    {
                        "url": html_url,
                        "raw_content": "PV-42 ordinary HTML content",
                        **metadata,
                    },
                    {
                        "url": incomplete_pdf_url,
                        "raw_content": "PV-42 incomplete metadata",
                        "pdf_direct_status": "used",
                        "pdf_page_count": 1,
                        "pdf_extracted_pages": 1,
                    },
                ]
            },
        )

        self.assertEqual(
            {
                pdf_url,
                mismatch_pdf_url,
                open_redirect_url,
                html_url,
                incomplete_pdf_url,
            },
            set(evidence_text),
        )
        self.assertEqual(set(evidence_text), classification_urls)
        self.assertEqual(evidence_text, classification_text)
        self.assertEqual({pdf_url}, verified_pdf_urls)

    def test_pdf_redirect_policy_handles_common_country_suffixes(
        self,
    ) -> None:
        self.assertTrue(
            cli._pdf_redirect_allows_publication(
                "https://docs.acme.co.uk/pv-42.pdf",
                "cdn.acme.co.uk",
                trusted_domains=frozenset(),
            )
        )
        self.assertFalse(
            cli._pdf_redirect_allows_publication(
                "https://docs.acme.co.uk/pv-42.pdf",
                "files.attacker.co.uk",
                trusted_domains=frozenset(),
            )
        )
        self.assertTrue(
            cli._pdf_redirect_allows_publication(
                "https://legacy.example/pv-42.pdf",
                "downloads.acme-cdn.co.uk",
                trusted_domains=frozenset({"acme-cdn.co.uk"}),
            )
        )

    def test_publish_fact_degradation_drops_duplicate_name_groups(
        self,
    ) -> None:
        pdf_url = "https://acme.example/pv-42.pdf"
        store = mock.Mock()
        store.allowed_evidence_urls.return_value = [pdf_url]
        lease = mock.Mock(
            product_id="P-42",
            token="lease-token",
            payload=product(),
        )
        raw = {
            "outcome": "publish",
            "facts": [
                {"name": "Rated Power", "value": 42},
                {"name": "rated-power", "value": 42},
                {"name": "Efficiency", "value": 98.5},
                {"name": "Weight", "value": 12},
            ],
        }
        observed_facts: list[list[str]] = []
        observed_verified_urls: list[set[str] | None] = []

        def strict_validator(
            candidate: dict,
            **kwargs: object,
        ) -> dict:
            names = [
                str(item["name"])
                for item in candidate["facts"]
            ]
            observed_facts.append(names)
            verified_urls = kwargs["verified_primary_document_urls"]
            observed_verified_urls.append(
                set(verified_urls)
                if isinstance(verified_urls, set)
                else None
            )
            if "Weight" in names:
                raise cli.DecisionError("Weight quote is invalid")
            return {
                **candidate,
                "facts": [dict(item) for item in candidate["facts"]],
            }

        with mock.patch.object(
            cli,
            "validate_decision",
            side_effect=strict_validator,
        ):
            validated = cli._validate_decision_for_lease(
                store,
                lease,
                raw,
                evidence_text_by_url={
                    pdf_url: "PV-42 datasheet",
                },
                verified_primary_document_urls={pdf_url},
            )

        self.assertEqual(
            [["Efficiency"], ["Efficiency", "Weight"]],
            observed_facts[1:],
        )
        self.assertEqual([], observed_facts[0])
        self.assertEqual(
            [{"name": "Efficiency", "value": 98.5}],
            validated["facts"],
        )
        self.assertTrue(
            all(urls == {pdf_url} for urls in observed_verified_urls)
        )

    def test_research_extract_summary_accepts_bounded_direct_pdf_counts(
        self,
    ) -> None:
        serialized = state._research_result_summary_json(
            "extract",
            {
                "submitted_urls": ["https://acme.example/pv-42.pdf"],
                "successful_urls": ["https://acme.example/pv-42.pdf"],
                "provider_requests": 1,
                "direct_pdf": {
                    "attempted": 2,
                    "used": 1,
                    "failed": 1,
                    "identity_mismatch": 0,
                },
            },
            require_successful_urls=True,
        )

        self.assertEqual(
            {
                "attempted": 2,
                "failed": 1,
                "identity_mismatch": 0,
                "used": 1,
            },
            json.loads(serialized)["direct_pdf"],
        )

    def test_research_extract_summary_rejects_inconsistent_direct_pdf_counts(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "outcomes must sum to attempted",
        ):
            state._research_result_summary_json(
                "extract",
                {
                    "submitted_urls": ["https://acme.example/pv-42.pdf"],
                    "successful_urls": [],
                    "direct_pdf": {
                        "attempted": 1,
                        "used": 1,
                        "failed": 1,
                        "identity_mismatch": 0,
                    },
                },
                require_successful_urls=True,
            )

    def test_extract_success_is_separate_from_identity_verification(self) -> None:
        with state.StateStore(self.state_path) as store:
            store.upsert_product(
                {
                    **product(),
                    "product_name": "DC-BAT cable 3000mm",
                }
            )
        token = self.claim()
        url = "https://acme.example/dc-cable.pdf"
        self.prepare_search(token, url)
        request = self.write_json(
            "extract-descriptive-name.json",
            {"urls": [url], "query": "DC-BAT cable 3000mm specifications"},
        )
        client = mock.Mock()
        client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": (
                        "LOW VOLTAGE DC CABLE 3000mm. "
                        "Model: DC.CAB.2700.0300"
                    ),
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        with mock.patch.object(cli, "ExaClient", return_value=client):
            code, payload, error = self.run_cli(
                "extract",
                "--lease-token",
                token,
                "--request-file",
                str(request),
            )

        self.assertEqual(0, code, error)
        self.assertFalse(payload["extract"]["results"][0]["identity_verified"])
        with state.StateStore(self.state_path) as store:
            self.assertEqual([url], store.allowed_evidence_urls(token))

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
        with mock.patch.object(cli, "ExaClient", return_value=client):
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
        with mock.patch.object(cli, "ExaClient", return_value=client):
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

    def test_discovered_manufacturer_publishes_when_catalogue_brand_is_blank(self) -> None:
        with state.StateStore(self.state_path) as store:
            store.upsert_product({**product(), "brand_code": ""})
        token = self.claim()
        official_url = "https://acme.example/pv-42.pdf"
        corroborating_url = "https://certifier.example/products/pv-42"
        with state.StateStore(self.state_path) as store:
            lease = store.get_by_lease(token)
            store.begin_search(lease)
            store.finish_search(
                lease,
                [official_url, corroborating_url],
                {"credits": 3},
            )
            store.begin_extract(lease, [official_url, corroborating_url])
            store.finish_extract(
                lease,
                [official_url, corroborating_url],
                {"credits": 1},
            )
        proposal = decision(token)
        proposal["sources"] = [
            {
                "url": corroborating_url,
                "title": "Independent PV-42 listing",
                "source_type": "authorized",
            }
        ]
        corroborating_rows = []
        for fact in proposal["facts"]:
            quote = fact["evidence_quotes"][0]["quote"]
            fact["evidence_urls"].append(corroborating_url)
            fact["evidence_quotes"].append(
                {"url": corroborating_url, "quote": quote}
            )
            corroborating_rows.append(quote)
        path = self.write_json("publish.json", proposal)
        evidence_path = self.write_json(
            "evidence.json",
            {
                "results": [
                    {
                        "url": official_url,
                        "raw_content": (
                            "Acme official product documentation\n"
                            "PV-42 Rated power 42 W\n"
                            "PV-42 Input voltage 48 V\n"
                            "PV-42 Efficiency 98.5%\n"
                            "PV-42 Ingress protection IP65\n"
                            "PV-42 Weight 12 kg"
                        ),
                    },
                    {
                        "url": corroborating_url,
                        "raw_content": "\n".join(
                            [
                                "Independent listing: Acme model PV-42.",
                                *corroborating_rows,
                            ]
                        ),
                    },
                ]
            },
        )
        os.environ.update(
            {
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "secret-token",
                "WIKIJS_LOCALE": "zh-cn",
                "WIKIJS_PATH_PREFIX": "products",
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

    def test_trusted_primary_datasheet_publishes_without_facts(self) -> None:
        token = self.claim()
        url = "https://acme.example/pv-42.pdf"
        self.prepare_evidence(token, url)
        proposal = decision(token)
        proposal["facts"] = []
        decision_path = self.write_json("publish-no-facts.json", proposal)
        evidence_path = self.write_json(
            "publish-no-facts-evidence.json",
            {
                "results": [
                    {
                        "url": url,
                        "raw_content": (
                            "Acme original datasheet for model PV-42"
                        ),
                    }
                ]
            },
        )
        os.environ.update(
            {
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "secret-token",
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON": (
                    '{"Acme":["acme.example"]}'
                ),
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON": (
                    '{"Acme":"Acme"}'
                ),
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
                str(decision_path),
                "--evidence-file",
                str(evidence_path),
            )

        self.assertEqual(0, code, error)
        self.assertTrue(payload["published"])
        rendered = client.upsert_page.call_args.args[4]
        self.assertNotIn("## 规格参数", rendered)
        self.assertIn(f"]({url})（官方数据表）", rendered)

    def test_invalid_fact_is_omitted_without_blocking_trusted_datasheet(
        self,
    ) -> None:
        token = self.claim()
        url = "https://acme.example/pv-42.pdf"
        self.prepare_evidence(token, url)
        proposal = decision(token)
        proposal["facts"][0]["value"] = 999
        proposal["facts"][0]["evidence_quotes"][0]["quote"] = (
            "PV-42 Rated power 999 W"
        )
        decision_path = self.write_json(
            "publish-skip-invalid-fact.json",
            proposal,
        )
        evidence_path = self.write_json(
            "publish-skip-invalid-fact-evidence.json",
            {
                "results": [
                    {
                        "url": url,
                        "raw_content": (
                            "Acme original datasheet\n"
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
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON": (
                    '{"Acme":["acme.example"]}'
                ),
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON": (
                    '{"Acme":"Acme"}'
                ),
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
                str(decision_path),
                "--evidence-file",
                str(evidence_path),
            )

        self.assertEqual(0, code, error)
        self.assertTrue(payload["published"])
        rendered = client.upsert_page.call_args.args[4]
        self.assertNotIn("999 W", rendered)
        self.assertNotIn("Rated power", rendered)
        self.assertIn("Input voltage", rendered)

    def test_wikijs_edit_conflict_is_nonblocking_and_retried_later(self) -> None:
        token = self.claim()
        url = "https://acme.example/pv-42.pdf"
        self.prepare_evidence(token, url)
        path = self.write_json("conflict-decision.json", decision(token))
        evidence_path = self.write_json(
            "conflict-evidence.json",
            {
                "results": [
                    {
                        "url": url,
                        "raw_content": (
                            "Acme official product documentation\n"
                            "PV-42 Rated power 42 W\n"
                            "PV-42 Input voltage 48 V\n"
                            "PV-42 Efficiency 98.5%\n"
                            "PV-42 Ingress protection IP65\n"
                            "PV-42 Weight 12 kg"
                        ),
                    },
                ]
            },
        )
        os.environ.update(
            {
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "secret-token",
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON": (
                    '{"Acme":["acme.example"]}'
                ),
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON": (
                    '{"Acme":"Acme"}'
                ),
            }
        )
        client = mock.Mock()
        client.upsert_page.side_effect = cli.WikiJSConflictError(
            42, "2026-07-22T00:00:00Z"
        )

        with mock.patch.object(cli, "WikiJSClient", return_value=client):
            code, payload, error = self.run_cli(
                "publish",
                "--decision-file",
                str(path),
                "--evidence-file",
                str(evidence_path),
            )

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("wikijs_conflict", payload["outcome"])
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("backoff", current.status)
            self.assertEqual("wikijs_conflict", current.last_outcome)

    def test_unverified_discovered_manufacturer_is_a_nonblocking_outcome(self) -> None:
        with state.StateStore(self.state_path) as store:
            store.upsert_product({**product(), "brand_code": ""})
        token = self.claim()
        url = "https://acme.example/pv-42.pdf"
        self.prepare_evidence(token, url)
        item = decision(token)
        item["manufacturer"] = "Unknown Manufacturer"
        path = self.write_json("unverified-source.json", item)
        evidence_path = self.write_json(
            "unverified-evidence.json",
            {
                "results": [
                    {
                        "url": url,
                        "raw_content": (
                            "Unknown Manufacturer product documentation\n"
                            "PV-42 Rated power 42 W\n"
                            "PV-42 Input voltage 48 V\n"
                            "PV-42 Efficiency 98.5%\n"
                            "PV-42 Ingress protection IP65\n"
                            "PV-42 Weight 12 kg"
                        ),
                    },
                ]
            },
        )

        with mock.patch.object(cli, "WikiJSClient") as wiki_client:
            code, payload, error = self.run_cli(
                "publish",
                "--decision-file",
                str(path),
                "--evidence-file",
                str(evidence_path),
            )

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("source_unverified", payload["outcome"])
        wiki_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("backoff", current.status)
            self.assertEqual("source_unverified", current.last_outcome)

    def test_internal_brand_override_cannot_replace_ai_manufacturer(self) -> None:
        token = self.claim()
        url = "https://acme.example/pv-42.pdf"
        self.prepare_evidence(token, url)
        item = decision(token)
        item["manufacturer"] = "Contoso"
        path = self.write_json("manufacturer-precedence.json", item)
        evidence_path = self.write_json(
            "manufacturer-precedence-evidence.json",
            {
                "results": [
                    {
                        "url": url,
                        "raw_content": (
                            "Contoso official product documentation\n"
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
        os.environ["PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON"] = (
            '{"Acme":["acme.example"]}'
        )
        os.environ["PV_WIKI_PUBLIC_BRAND_ALIASES_JSON"] = (
            '{"Acme":"Acme"}'
        )

        with mock.patch.object(cli, "WikiJSClient") as wiki_client:
            code, payload, error = self.run_cli(
                "publish",
                "--decision-file",
                str(path),
                "--evidence-file",
                str(evidence_path),
            )

        self.assertEqual(0, code, error)
        self.assertFalse(payload["published"])
        self.assertEqual("source_unverified", payload["outcome"])
        wiki_client.assert_not_called()

    def test_invalid_decision_closes_lease_with_transient_backoff(self) -> None:
        token = self.claim()
        path = self.write_json("invalid.json", decision(token))
        os.environ["PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON"] = (
            '{"Acme":["acme.example"]}'
        )
        os.environ["PV_WIKI_PUBLIC_BRAND_ALIASES_JSON"] = (
            '{"Acme":"Acme"}'
        )

        code, payload, error = self.run_cli(
            "publish", "--decision-file", str(path)
        )

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("invalid_decision", payload["outcome"])
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("backoff", current.status)
            self.assertEqual("invalid_decision", current.last_outcome)

    def test_run_one_no_due_product_is_a_clean_noop(self) -> None:
        with state.StateStore(self.state_path) as store:
            lease = store.lease_next("setup")
            store.record_outcome(lease, "synced")

        with (
            mock.patch.object(cli, "ExaClient") as search_client,
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual("no_due_product", payload["reason"])
        search_client.assert_not_called()
        ai_client.assert_not_called()
        wiki_client.assert_not_called()

    def test_run_one_rejects_a_lease_shorter_than_the_research_tail(self) -> None:
        with self.assertRaisesRegex(cli.CLIError, "lease_seconds"):
            cli.run_one(lease_seconds=1200)
        with state.StateStore(self.state_path) as store:
            self.assertEqual("due", store.get_product("P-42").status)

    def test_research_scopes_change_only_for_relevant_wire_inputs(self) -> None:
        self.configure_worker_environment()
        settings = ai.AISettings.from_env()
        research = cli.ResearchSettings.from_env()
        search_client = mock.Mock()
        search_client.credential_fingerprint = "a" * 64
        baseline = cli._research_scope_fingerprints(
            product(),
            settings,
            search_client,
            max_results=5,
            research_settings=research,
        )

        local_limits = cli._research_scope_fingerprints(
            product(),
            replace(
                settings,
                timeout=settings.timeout + 1,
                max_response_bytes=settings.max_response_bytes + 1,
            ),
            search_client,
            max_results=5,
            research_settings=research,
        )
        self.assertEqual(baseline, local_limits)

        changed_ai_key = cli._research_scope_fingerprints(
            product(),
            replace(settings, api_key="different-ai-key"),
            search_client,
            max_results=5,
            research_settings=research,
        )
        self.assertEqual(baseline["search"], changed_ai_key["search"])
        self.assertEqual(baseline["extract"], changed_ai_key["extract"])
        self.assertNotEqual(baseline["ai"], changed_ai_key["ai"])

        changed_exa = mock.Mock()
        changed_exa.credential_fingerprint = "b" * 64
        changed_exa_scopes = cli._research_scope_fingerprints(
            product(),
            settings,
            changed_exa,
            max_results=5,
            research_settings=research,
        )
        self.assertNotEqual(
            baseline["search"],
            changed_exa_scopes["search"],
        )
        self.assertNotEqual(
            baseline["extract"],
            changed_exa_scopes["extract"],
        )
        self.assertEqual(baseline["ai"], changed_exa_scopes["ai"])

        changed_retrieval = cli._research_scope_fingerprints(
            product(),
            settings,
            search_client,
            max_results=4,
            research_settings=research,
        )
        self.assertNotEqual(baseline["search"], changed_retrieval["search"])
        self.assertEqual(baseline["extract"], changed_retrieval["extract"])
        self.assertNotEqual(baseline["ai"], changed_retrieval["ai"])

        with mock.patch.dict(
            os.environ,
            {"PV_WIKI_PDF_DIRECT_FETCH": "true"},
        ):
            direct_pdf = cli._research_scope_fingerprints(
                product(),
                settings,
                search_client,
                max_results=5,
                research_settings=research,
            )
        self.assertEqual(baseline["search"], direct_pdf["search"])
        self.assertNotEqual(baseline["extract"], direct_pdf["extract"])
        self.assertNotEqual(baseline["ai"], direct_pdf["ai"])

    def test_search_identity_uses_public_model_code_for_descriptive_name(
        self,
    ) -> None:
        os.environ["PV_WIKI_SEARCH_INCLUDE_INTERNAL_HINTS"] = "true"
        foxess = {
            **product(),
            "brand_code": "",
            "product_id": "H3-8.0-E",
            "product_name": (
                "8000W Three Phase, Dual MPPT hybrid inverter"
            ),
        }
        self.assertEqual(
            {
                "model": "H3-8.0-E",
                "product_name": (
                    "8000W Three Phase, Dual MPPT hybrid inverter"
                ),
            },
            cli._search_identity(foxess),
        )
        self.assertEqual(
            {
                "model": "SUN2000-50KTL-M3",
                "product_name": "SUN2000-50KTL-M3 INVERTER",
            },
            cli._search_identity(
                {
                    **product(),
                    "brand_code": "",
                    "product_id": "01073873",
                    "product_name": "SUN2000-50KTL-M3 INVERTER",
                }
            ),
        )

    def test_ai_output_circuit_is_scoped_to_prompt_contract(self) -> None:
        self.configure_worker_environment()
        settings = ai.AISettings.from_env()
        provider = cli._ai_provider_fingerprint(settings)
        output = cli._ai_output_fingerprint(settings)

        self.assertNotEqual(provider, output)
        with mock.patch.object(
            cli,
            "AI_RESEARCH_PROMPT_VERSION",
            "next-contract",
        ):
            self.assertEqual(
                provider,
                cli._ai_provider_fingerprint(settings),
            )
            self.assertNotEqual(
                output,
                cli._ai_output_fingerprint(settings),
            )
        json_settings = ai.AISettings(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            json_response_format=True,
            timeout=settings.timeout,
            max_tokens=settings.max_tokens,
            max_response_bytes=settings.max_response_bytes,
            max_evidence_chars=settings.max_evidence_chars,
        )
        self.assertEqual(
            provider,
            cli._ai_provider_fingerprint(json_settings),
        )
        self.assertNotEqual(
            output,
            cli._ai_output_fingerprint(json_settings),
        )
        self.assertNotEqual(
            output,
            cli._ai_output_fingerprint(
                replace(settings, max_tokens=2048)
            ),
        )
        thinking_settings = replace(settings, thinking_mode="disabled")
        self.assertEqual(
            provider,
            cli._ai_provider_fingerprint(thinking_settings),
        )
        self.assertNotEqual(
            output,
            cli._ai_output_fingerprint(thinking_settings),
        )
        search_client = mock.Mock(credential_fingerprint="a" * 64)
        baseline_scopes = cli._research_scope_fingerprints(
            product(),
            settings,
            search_client,
            max_results=5,
            research_settings=cli.ResearchSettings.from_env(),
        )
        thinking_scopes = cli._research_scope_fingerprints(
            product(),
            thinking_settings,
            search_client,
            max_results=5,
            research_settings=cli.ResearchSettings.from_env(),
        )
        self.assertNotEqual(baseline_scopes["ai"], thinking_scopes["ai"])
        self.assertEqual(baseline_scopes["search"], thinking_scopes["search"])
        self.assertEqual(
            baseline_scopes["extract"],
            thinking_scopes["extract"],
        )
        effort_settings = replace(settings, reasoning_effort="high")
        self.assertEqual(
            provider,
            cli._ai_provider_fingerprint(effort_settings),
        )
        self.assertNotEqual(
            output,
            cli._ai_output_fingerprint(effort_settings),
        )
        effort_scopes = cli._research_scope_fingerprints(
            product(),
            effort_settings,
            search_client,
            max_results=5,
            research_settings=cli.ResearchSettings.from_env(),
        )
        self.assertNotEqual(baseline_scopes["ai"], effort_scopes["ai"])
        self.assertEqual(baseline_scopes["search"], effort_scopes["search"])
        self.assertEqual(
            baseline_scopes["extract"],
            effort_scopes["extract"],
        )

    def test_failure_status_distinguishes_definitive_from_partial_calls(self) -> None:
        search_client = mock.Mock()
        search_client.last_operation_completed_requests = 0
        self.assertEqual(
            "failed",
            cli._search_failure_status(
                cli.ExaQuotaExhaustedError("quota"),
                search_client,
            ),
        )
        search_client.last_operation_completed_requests = 1
        self.assertEqual(
            "failed",
            cli._search_failure_status(
                cli.ExaQuotaExhaustedError("quota after first query"),
                search_client,
            ),
        )

        ai_client = mock.Mock()
        ai_client.last_research_provider_requests = 1
        for status in (401, 402, 429):
            with self.subTest(status=status):
                self.assertEqual(
                    "failed",
                    cli._ai_failure_status(
                        ai.AIHTTPError("definitive rejection", status_code=status),
                        ai_client,
                    ),
                )
        self.assertEqual(
            "uncertain",
            cli._ai_failure_status(
                ai.AIHTTPError("request timeout", status_code=408),
                ai_client,
            ),
        )
        ai_client.last_research_provider_requests = 2
        self.assertEqual(
            "failed",
            cli._ai_failure_status(
                ai.AIHTTPError(
                    "repair unauthorized",
                    status_code=401,
                ),
                ai_client,
            ),
        )
        self.assertEqual(
            "failed",
            cli._ai_failure_status(
                ai.AIInvalidOutputError("bounded repair violated contract"),
                ai_client,
            ),
        )
        self.assertEqual(
            "failed",
            cli._ai_failure_status(
                ai.AIResponseError("oversized provider response"),
                ai_client,
            ),
        )
        self.assertEqual(
            "failed",
            cli._ai_failure_status(
                ai.AILocalExecutionError("spawn unavailable"),
                ai_client,
            ),
        )

    def test_ai_response_audit_keeps_only_numeric_token_metadata(self) -> None:
        client = mock.Mock()
        client.last_response_metadata = [
            ai.AIResponseMetadata(
                finish_reason="length",
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "completion_tokens_details": {
                        "reasoning_tokens": 4,
                        "provider_note": "discard",
                    },
                },
            ),
            ai.AIResponseMetadata(
                finish_reason="stop",
                usage={
                    "prompt_tokens": 110,
                    "completion_tokens": 8,
                    "provider_note": "discard",
                },
            ),
        ]

        self.assertEqual(
            {
                "finish_reasons": ["length", "stop"],
                "usage_totals": {
                    "prompt_tokens": 210,
                    "completion_tokens": 18,
                    "reasoning_tokens": 4,
                },
            },
            cli._ai_response_audit(client),
        )

    def test_ai_provider_rejection_circuit_stops_before_exa(self) -> None:
        self.configure_worker_environment()
        now = datetime.now(timezone.utc)
        settings = ai.AISettings.from_env()
        provider_fingerprint = cli._ai_provider_fingerprint(settings)
        with state.StateStore(self.state_path) as store:
            first = store.lease_next("first-worker", now=now)
            request_fingerprint = state.research_request_fingerprint(
                "ai",
                {"context_sha256": "a" * 64},
            )
            store.begin_research_action(
                first,
                round_number=0,
                action="ai",
                request_fingerprint=request_fingerprint,
                now=now,
            )
            store.finish_research_action(
                first,
                round_number=0,
                action="ai",
                request_fingerprint=request_fingerprint,
                status="uncertain",
                result_summary={
                    "provider_requests": 2,
                    "provider_fingerprint": provider_fingerprint,
                    "http_status": 401,
                },
                error="repair request was unauthorized",
                now=now,
            )
            store.record_outcome(
                first,
                "ai_error",
                error="unauthorized",
                now=now,
            )
            store.upsert_product(
                {
                    **product(),
                    "product_id": "P-SECOND",
                    "product_name": "PV-43",
                },
                now=now,
            )

        with (
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
            ) as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual("ai_provider_circuit_open", payload["reason"])
        self.assertEqual(401, payload["http_status"])
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-SECOND")
            self.assertEqual("due", current.status)
            self.assertIsNone(current.last_outcome)

    def test_global_daily_credit_stop_restores_lease_before_research(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT"] = "20"
        usage = {
            "known_credits": 20.0,
            "unknown_or_uncertain_actions": 1,
        }
        with (
            mock.patch.object(
                state.StateStore,
                "research_usage_between",
                return_value=usage,
            ) as usage_reader,
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual(
            "global_daily_research_budget_exhausted",
            payload["reason"],
        )
        self.assertEqual(20, payload["credit_limit"])
        self.assertEqual(1, payload["unknown_or_uncertain_actions"])
        self.assertEqual(1, usage_reader.call_count)
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertIsNone(current.last_outcome)

    def test_global_budget_fails_closed_for_uncertain_usage(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT"] = "500"
        usage = {
            "known_credits": 1.0,
            "unknown_or_uncertain_actions": 1,
        }
        with (
            mock.patch.object(
                state.StateStore,
                "research_usage_between",
                return_value=usage,
            ),
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual(
            "global_daily_research_usage_uncertain",
            payload["reason"],
        )
        self.assertEqual(20, payload["reserved_credits"])
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertIsNone(current.last_outcome)

    def test_global_budget_reserves_one_full_product_before_research(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT"] = "500"
        usage = {
            "known_credits": 481.0,
            "unknown_or_uncertain_actions": 0,
        }
        with (
            mock.patch.object(
                state.StateStore,
                "research_usage_between",
                return_value=usage,
            ),
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual(
            "global_daily_research_budget_reservation_blocked",
            payload["reason"],
        )
        self.assertEqual(20, payload["reserved_credits"])
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertIsNone(current.last_outcome)

    def test_invalid_global_budget_defers_without_product_failure(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT"] = "invalid"

        with (
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
            ) as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual({}, payload)
        self.assertEqual(2, code)
        self.assertIn("ConfigError", error)
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertIsNone(current.last_outcome)
            self.assertEqual(
                ["system_paused"],
                [
                    attempt.outcome
                    for attempt in store.attempt_history("P-42")
                ],
            )

    def test_global_budget_uses_latest_blocked_window_resume(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT"] = "20"
        os.environ["PV_WIKI_GLOBAL_MONTHLY_CREDIT_LIMIT"] = "20"
        usage = {
            "known_credits": 20.0,
            "unknown_or_uncertain_actions": 0,
        }
        with (
            mock.patch.object(
                state.StateStore,
                "research_usage_between",
                return_value=usage,
            ) as usage_reader,
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
            ) as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual(["daily", "monthly"], payload["blocked_windows"])
        resume_at = datetime.fromisoformat(payload["resume_at"])
        self.assertEqual(1, resume_at.day)
        self.assertEqual(2, usage_reader.call_count)
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()

    def test_repeated_content_failures_are_quarantined_before_research(
        self,
    ) -> None:
        self.configure_worker_environment()
        with (
            mock.patch.object(
                state.StateStore,
                "content_failure_streak",
                return_value=cli.CONTENT_FAILURE_QUARANTINE_THRESHOLD,
            ),
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("content_quarantined", payload["outcome"])
        self.assertEqual("content_failure_quarantine", payload["reason"])
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()

    def test_ai_rate_limit_circuit_stops_cleanly_before_exa(self) -> None:
        self.configure_worker_environment()
        event = mock.Mock()
        event.http_status = 429
        event.expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        with (
            mock.patch.object(
                state.StateStore,
                "recent_ai_provider_rejection_event",
                side_effect=[None, event],
            ),
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
            ) as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual("ai_provider_circuit_open", payload["reason"])
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()

    def test_ai_invalid_output_circuit_stops_before_exa(self) -> None:
        self.configure_worker_environment()
        with (
            mock.patch.object(
                state.StateStore,
                "recent_ai_provider_error_products",
                return_value=3,
            ) as error_products,
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
            ) as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual(
            "ai_invalid_output_circuit_open",
            payload["reason"],
        )
        self.assertEqual(
            cli.AI_INVALID_OUTPUT_CIRCUIT_CATEGORIES,
            error_products.call_args.kwargs["categories"],
        )
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertIsNone(current.last_outcome)

    def test_empty_identity_finishes_without_ai_or_exa_configuration(
        self,
    ) -> None:
        with state.StateStore(self.state_path) as store:
            store.upsert_product({**product(), "product_name": ""})

        with (
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
            ) as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertEqual("insufficient_identity", payload["outcome"])
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()

    def test_missing_exa_configuration_defers_without_product_failure(
        self,
    ) -> None:
        self.configure_worker_environment()
        os.environ.pop("EXA_API_KEY")

        with mock.patch.object(
            cli,
            "OpenAICompatibleClient",
        ) as ai_factory:
            code, payload, error = self.run_cli("run-one")

        self.assertEqual({}, payload)
        self.assertEqual(2, code)
        self.assertIn("ExaConfigError", error)
        ai_factory.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("due", current.status)
            self.assertIsNone(current.last_outcome)
            self.assertEqual(
                ["system_paused"],
                [
                    attempt.outcome
                    for attempt in store.attempt_history("P-42")
                ],
            )

    def test_exa_provider_rejection_defers_next_product(self) -> None:
        self.configure_worker_environment()
        now = datetime.now(timezone.utc)
        search_client = cli.ExaClient(api_key="exa-secret")
        provider_fingerprint = cli._exa_provider_fingerprint(search_client)
        with state.StateStore(self.state_path) as store:
            first = store.lease_next("first-worker", now=now)
            request_fingerprint = state.research_request_fingerprint(
                "search",
                {"queries": ["PV-42 datasheet"]},
            )
            store.begin_research_action(
                first,
                round_number=0,
                action="search",
                request_fingerprint=request_fingerprint,
                now=now,
            )
            store.finish_research_action(
                first,
                round_number=0,
                action="search",
                request_fingerprint=request_fingerprint,
                status="failed",
                result_summary={
                    "provider_fingerprint": provider_fingerprint,
                    "error_type": "ExaHTTPError",
                    "http_status": 401,
                },
                credits=0,
                error="unauthorized",
                now=now,
            )
            store.record_outcome(
                first,
                "search_error",
                error="unauthorized",
                now=now,
            )
            store.upsert_product(
                {
                    **product(),
                    "product_id": "P-SECOND",
                    "product_name": "PV-43",
                },
                now=now,
            )

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ) as exa_factory,
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
            ) as ai_factory,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual("exa_provider_circuit_open", payload["reason"])
        self.assertEqual(401, payload["http_status"])
        self.assertEqual(
            now + cli.EXA_PROVIDER_CIRCUIT_WINDOW,
            datetime.fromisoformat(payload["resume_at"]),
        )
        exa_factory.assert_called_once()
        ai_factory.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-SECOND")
            self.assertEqual("due", current.status)
            self.assertIsNone(current.last_outcome)

    def test_wiki_recovery_runs_without_ai_or_exa_configuration(self) -> None:
        self.configure_worker_environment()
        now = datetime.now(timezone.utc)
        with state.StateStore(self.state_path) as store:
            first = store.lease_next("first-worker", now=now)
            store.record_outcome(
                first,
                "wikijs_error",
                payload={
                    "decision": decision(first.token),
                    "validation_policy_fingerprint": (
                        cli._validation_policy_fingerprint()
                    ),
                },
                error="response lost",
                now=now,
            )
            self.assertEqual(
                1,
                store.requeue_products(
                    {"wikijs_error"},
                    reason="retry-wiki-only",
                    now=now,
                ),
            )
        for name in ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL", "EXA_API_KEY"):
            os.environ.pop(name, None)

        with (
            mock.patch.object(cli, "ExaClient") as exa_factory,
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
            ) as ai_factory,
            mock.patch.object(cli, "WikiJSClient") as wiki_factory,
        ):
            wiki_factory.return_value.upsert_page.return_value = {
                "action": "updated",
                "page": {"id": 42},
            }
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["published"])
        self.assertEqual("synced", payload["outcome"])
        exa_factory.assert_not_called()
        ai_factory.assert_not_called()
        wiki_factory.return_value.upsert_page.assert_called_once()

    def test_wiki_failure_reuses_validated_decision_without_new_research(self) -> None:
        self.configure_worker_environment()
        now = datetime.now(timezone.utc)
        with state.StateStore(self.state_path) as store:
            first = store.lease_next("first-worker", now=now)
            validated = decision(first.token)
            store.record_outcome(
                first,
                "wikijs_error",
                payload={
                    "decision": validated,
                    "validation_policy_fingerprint": (
                        cli._validation_policy_fingerprint()
                    ),
                },
                error="response lost",
                now=now,
            )
            second = store.lease_next(
                "retry-worker",
                now=now + timedelta(hours=2),
            )
            recovered = cli._recover_validated_publish(store, second)
            self.assertIsNotNone(recovered)
            recovered_decision, attempt_id = recovered
            self.assertEqual(first.attempt_id, attempt_id)
            self.assertEqual(second.token, recovered_decision["lease_token"])

            with mock.patch.object(cli, "WikiJSClient") as wiki_client:
                wiki_client.return_value.upsert_page.return_value = {
                    "action": "updated",
                    "page": {"id": 42},
                }
                result = cli._apply_decision(
                    store,
                    second,
                    recovered_decision,
                    validated_decision=recovered_decision,
                    audit={
                        "recovered_publish_attempt_id": attempt_id,
                        "research_reused": True,
                    },
                )

        self.assertTrue(result["published"])
        wiki_client.return_value.upsert_page.assert_called_once()

    def test_wiki_retry_does_not_reuse_decision_after_policy_change(self) -> None:
        self.configure_worker_environment()
        now = datetime.now(timezone.utc)
        with state.StateStore(self.state_path) as store:
            first = store.lease_next("first-worker", now=now)
            store.record_outcome(
                first,
                "wikijs_error",
                payload={
                    "decision": decision(first.token),
                    "validation_policy_fingerprint": (
                        cli._validation_policy_fingerprint()
                    ),
                },
                error="response lost",
                now=now,
            )
            second = store.lease_next(
                "retry-worker",
                now=now + timedelta(hours=2),
            )
            os.environ["PV_WIKI_AUTO_PUBLISH_MIN_CONFIDENCE"] = "0.99"
            self.assertIsNone(
                cli._recover_validated_publish(store, second)
            )

    def test_run_one_stops_batch_after_five_distinct_invalid_decisions(self) -> None:
        with state.StateStore(self.state_path) as store:
            for index in range(5):
                store.upsert_product(
                    {
                        **product(),
                        "product_id": f"CIRCUIT-{index}",
                        "product_name": f"MODEL-{index}",
                    }
                )
            for _ in range(5):
                lease = store.lease_next("circuit-setup")
                self.assertIsNotNone(lease)
                store.record_outcome(
                    lease,
                    "invalid_decision",
                    error=(
                        "facts[0].evidence_quotes[0] is not an exact "
                        "supporting extract span"
                    ),
                )

        with (
            mock.patch.object(cli, "ExaClient") as search_client,
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertEqual("ai_decision_circuit_open", payload["reason"])
        search_client.assert_not_called()
        ai_client.assert_not_called()
        wiki_client.assert_not_called()

    def test_run_one_stops_cleanly_when_all_exa_quotas_are_exhausted(self) -> None:
        self.configure_worker_environment()
        search_client = mock.Mock()
        search_client.search_product.side_effect = (
            cli.ExaQuotaExhaustedError("monthly quota exhausted")
        )

        with (
            mock.patch.object(cli, "ExaClient", return_value=search_client),
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("search_quota_exhausted", payload["reason"])
        self.assertEqual("product_and_provider", payload["pause_scope"])
        resume_at = datetime.fromisoformat(payload["resume_at"])
        self.assertEqual(1, resume_at.day)
        self.assertEqual((0, 0, 0), (resume_at.hour, resume_at.minute, resume_at.second))
        ai_client.assert_called_once()
        wiki_client.assert_not_called()

        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("search_quota_exhausted", current.last_outcome)
            self.assertEqual(0, current.consecutive_failures)
            self.assertEqual(resume_at, current.next_run_at)
            store.upsert_product(
                {
                    **product(),
                    "product_id": "P-SECOND",
                    "product_name": "PV-43",
                }
            )

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
            ) as second_ai_client,
        ):
            code, second_payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(second_payload["processed"])
        self.assertEqual(
            "search_quota_exhausted",
            second_payload["reason"],
        )
        self.assertEqual("system", second_payload["pause_scope"])
        self.assertEqual(payload["resume_at"], second_payload["resume_at"])
        search_client.search_product.assert_called_once()
        second_ai_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            second = store.get_product("P-SECOND")
            self.assertEqual("due", second.status)
            self.assertIsNone(second.last_outcome)

    def test_partial_search_before_quota_is_audited_and_retryable(
        self,
    ) -> None:
        self.configure_worker_environment()
        search_client = mock.Mock()
        search_client.credential_fingerprint = "a" * 64
        search_client.last_operation_requests = 2
        search_client.last_operation_completed_requests = 1
        search_client.last_operation_known_credits = 1
        search_client.search_product.side_effect = (
            cli.ExaQuotaExhaustedError(
                "monthly quota exhausted after one completed query"
            )
        )

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(cli, "OpenAICompatibleClient"),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("search_quota_exhausted", payload["reason"])
        with state.StateStore(self.state_path) as store:
            actions = store.research_action_history(product_id="P-42")
            self.assertEqual(1, len(actions))
            self.assertEqual("search", actions[0].action)
            self.assertEqual("failed", actions[0].status)
            self.assertEqual(1, actions[0].credits)
            self.assertEqual(
                1,
                actions[0].result_summary[
                    "completed_provider_requests"
                ],
            )
            self.assertEqual(
                "ExaQuotaExhaustedError",
                actions[0].result_summary["error_type"],
            )
            self.assertEqual(
                cli._exa_provider_fingerprint(search_client),
                actions[0].result_summary["provider_fingerprint"],
            )

    def test_run_one_missing_brand_uses_ai_manufacturer_and_publishes(self) -> None:
        with state.StateStore(self.state_path) as store:
            store.upsert_product({**product(), "brand_code": ""})
        self.configure_worker_environment()
        os.environ["PV_WIKI_PDF_DIRECT_FETCH"] = "true"
        url = "https://acme.example/pv-42.pdf"
        corroboration_url = "https://lab.example/pv-42-listing"
        official_body = (
            "Acme official product documentation\n"
            "PV-42 Rated power 42 W\n"
            "PV-42 Input voltage 48 V\n"
            "PV-42 Efficiency 98.5%\n"
            "PV-42 Ingress protection IP65\n"
            "PV-42 Weight 12 kg"
        )
        direct = documents.PDFEvidence(
            text=official_body,
            requested_url=url,
            final_url=url,
            sha256="1" * 64,
            page_count=2,
            extracted_pages=2,
            truncated=False,
        )
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [
                {
                    "title": "Official datasheet",
                    "url": url,
                    "content": "official",
                    "score": 0.99,
                },
                {
                    "title": "Independent equipment listing",
                    "url": corroboration_url,
                    "content": "independent",
                    "score": 0.9,
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
                    "raw_content": official_body,
                },
                {
                    "url": corroboration_url,
                    "raw_content": (
                        "Independent equipment registry: Acme PV-42 "
                        "solar inverter."
                    ),
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        search_client.extract_urls.return_value = (
            extract_client.extract_urls.return_value
        )
        ai_client = mock.Mock()
        proposal = decision("forged-token")
        proposal["sources"] = [
            {
                "url": corroboration_url,
                "title": "Independent equipment listing",
                "source_type": "regulatory",
            }
        ]
        ai_client.next_research_action.return_value = ai.FinalAction(
            proposal
        )
        wiki_client = mock.Mock()
        wiki_client.upsert_page.return_value = {
            "action": "created",
            "page": {"id": 42},
        }

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
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
            mock.patch.object(
                cli,
                "extract_pdf_evidence",
                return_value=direct,
            ) as extract_pdf,
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
        extract_pdf.assert_called_once()
        ai_factory.assert_called_once()
        self.assertEqual("test-model", ai_factory.call_args.args[0].model)
        self.assertEqual(
            {url, corroboration_url},
            {
                item["url"]
                for item in ai_client.next_research_action.call_args.kwargs[
                    "extract"
                ]["results"]
            },
        )
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("synced", current.status)
            attempt = store.attempt_history("P-42")[-1]
            self.assertEqual("test-model", attempt.details["payload"]["automation"]["ai"]["model"])

    def test_run_one_executes_ai_requested_supplemental_search_and_persists_it(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_PDF_DIRECT_FETCH"] = "true"
        official_url = "https://acme.example/pv-42.pdf"
        supplemental_url = "https://independent.example.net/pv-42"
        official_body = (
            "PV-42 Rated power 42 W\n"
            "PV-42 Input voltage 48 V\n"
            "PV-42 Efficiency 98.5%\n"
            "PV-42 Ingress protection IP65\n"
            "PV-42 Weight 12 kg"
        )
        direct = documents.PDFEvidence(
            text=official_body,
            requested_url=official_url,
            final_url=official_url,
            sha256="2" * 64,
            page_count=2,
            extracted_pages=2,
            truncated=False,
        )
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "results": [{"url": official_url, "score": 0.99}],
            "usage": {"credits": 3},
            "request_ids": ["initial-search"],
        }
        search_client.search_queries.return_value = {
            "results": [{"url": supplemental_url, "score": 0.91}],
            "usage": {"credits": 1},
            "request_ids": ["supplemental-search"],
        }
        search_client.extract_urls.side_effect = [
            {
                "results": [
                    {"url": official_url, "raw_content": official_body}
                ],
                "failed_results": [],
                "usage": {"credits": 1},
            },
            {
                "results": [
                    {
                        "url": supplemental_url,
                        "raw_content": "PV-42 independent product overview",
                    }
                ],
                "failed_results": [],
                "usage": {"credits": 1},
            },
        ]
        ai_client = mock.Mock()
        ai_client.next_research_action.side_effect = [
            ai.SearchMoreAction(
                ai.ResearchGap.INDEPENDENT_CORROBORATION,
                ('"PV-42" independent specifications',),
            ),
            ai.FinalAction(decision("forged-token")),
        ]
        wiki_client = mock.Mock()
        wiki_client.upsert_page.return_value = {
            "action": "created",
            "page": {"id": 42},
        }

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(
                cli,
                "WikiJSClient",
                return_value=wiki_client,
            ),
            mock.patch.object(
                cli,
                "extract_pdf_evidence",
                return_value=direct,
            ) as extract_pdf,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["published"])
        extract_pdf.assert_called_once()
        search_client.search_queries.assert_called_once_with(
            ['"PV-42" independent specifications'],
            max_results=5,
            exclude_domains=[
                "alldatasheet.com",
                "alldatasheet.net",
                "scribd.com",
                "tekmatic-store.it",
            ],
        )
        self.assertEqual(2, search_client.extract_urls.call_count)
        self.assertEqual(2, ai_client.next_research_action.call_count)
        with state.StateStore(self.state_path) as store:
            actions = store.research_action_history(product_id="P-42")
            self.assertEqual(
                [
                    (0, "search"),
                    (0, "extract"),
                    (0, "ai"),
                    (1, "search"),
                    (1, "extract"),
                    (1, "ai"),
                ],
                [(item.round_number, item.action) for item in actions],
            )
            self.assertTrue(all(item.status == "completed" for item in actions))
            stats = store.research_action_stats(product_id="P-42")
            self.assertEqual(6, stats["actions"])
            attempt = store.attempt_history("P-42")[-1]
            self.assertEqual(
                2,
                attempt.details["payload"]["automation"]["ai"]["calls"],
            )

    def test_local_source_validation_can_request_one_feedback_search(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_PDF_DIRECT_FETCH"] = "true"
        os.environ.pop("PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON")
        official_url = "https://acme.example/pv-42.pdf"
        independent_url = "https://lab.example.net/pv-42"
        lines = [
            ("Rated power", 42, "W"),
            ("Input voltage", 48, "V"),
            ("Efficiency", 98.5, "%"),
            ("Ingress protection", "IP65", ""),
            ("Weight", 12, "kg"),
        ]
        official_body = "\n".join(
            f"Acme PV-42 {name} {value} {unit}".strip()
            for name, value, unit in lines
        )
        direct = documents.PDFEvidence(
            text=official_body,
            requested_url=official_url,
            final_url=official_url,
            sha256="3" * 64,
            page_count=2,
            extracted_pages=2,
            truncated=False,
        )
        independent_body = "\n".join(
            f"Acme PV-42 {name} {value} {unit}".strip()
            for name, value, unit in lines
        )
        final_decision = decision("forged-token")
        final_decision["sources"] = [
            {
                "url": independent_url,
                "title": "Independent PV-42 specifications",
                "source_type": "authorized",
            }
        ]
        for fact in final_decision["facts"]:
            quote = fact["evidence_quotes"][0]["quote"]
            fact["evidence_urls"].append(independent_url)
            fact["evidence_quotes"].append(
                {"url": independent_url, "quote": quote}
            )

        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "results": [{"url": official_url, "score": 0.99}],
            "usage": {"credits": 3},
        }
        search_client.search_queries.return_value = {
            "results": [{"url": independent_url, "score": 0.95}],
            "usage": {"credits": 1},
        }
        search_client.extract_urls.side_effect = [
            {
                "results": [
                    {"url": official_url, "raw_content": official_body}
                ],
                "failed_results": [],
                "usage": {"credits": 1},
            },
            {
                "results": [
                    {"url": independent_url, "raw_content": independent_body}
                ],
                "failed_results": [],
                "usage": {"credits": 1},
            },
        ]
        ai_client = mock.Mock()
        ai_client.next_research_action.side_effect = [
            ai.FinalAction(decision("forged-token")),
            ai.SearchMoreAction(
                ai.ResearchGap.PRIMARY_DATASHEET,
                ('"PV-42" independent manufacturer datasheet',),
            ),
            ai.FinalAction(final_decision),
        ]
        wiki_client = mock.Mock()
        wiki_client.upsert_page.return_value = {
            "action": "created",
            "page": {"id": 42},
        }

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(
                cli,
                "WikiJSClient",
                return_value=wiki_client,
            ),
            mock.patch.object(
                cli,
                "extract_pdf_evidence",
                return_value=direct,
            ) as extract_pdf,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["published"])
        extract_pdf.assert_called_once()
        self.assertEqual(3, ai_client.next_research_action.call_count)
        calls = ai_client.next_research_action.call_args_list
        feedback = calls[1].kwargs["validation_feedback"]
        self.assertIsInstance(feedback, ai.ValidationFeedback)
        self.assertEqual(ai.ResearchGap.PRIMARY_DATASHEET, feedback.gap)
        self.assertIsNone(calls[2].kwargs["validation_feedback"])
        with state.StateStore(self.state_path) as store:
            actions = store.research_action_history(product_id="P-42")
            self.assertEqual(
                [
                    (0, "search"),
                    (0, "extract"),
                    (0, "ai"),
                    (1, "ai"),
                    (2, "search"),
                    (2, "extract"),
                    (2, "ai"),
                ],
                [(item.round_number, item.action) for item in actions],
            )

    def test_three_round_loop_reserves_exactly_five_extract_urls(self) -> None:
        self.configure_worker_environment()
        initial_urls = [
            f"https://initial.example.net/pv-42-{index}"
            for index in range(5)
        ]
        supplemental_urls = [
            "https://supplement-one.example.net/pv-42",
            "https://supplement-two.example.net/pv-42",
        ]
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "results": [
                {"url": url, "score": 1 - index / 10}
                for index, url in enumerate(initial_urls)
            ],
            "usage": {"credits": 3},
        }
        search_client.search_queries.side_effect = [
            {
                "results": [{"url": supplemental_urls[0], "score": 0.9}],
                "usage": {"credits": 2},
            },
            {
                "results": [{"url": supplemental_urls[1], "score": 0.9}],
                "usage": {"credits": 2},
            },
        ]
        search_client.extract_urls.side_effect = [
            {
                "results": [
                    {"url": url, "raw_content": f"PV-42 source {index}"}
                    for index, url in enumerate(initial_urls[:3])
                ],
                "failed_results": [],
                "usage": {"credits": 1},
            },
            {
                "results": [
                    {
                        "url": supplemental_urls[0],
                        "raw_content": "PV-42 supplemental evidence one",
                    }
                ],
                "failed_results": [],
                "usage": {"credits": 1},
            },
            {
                "results": [
                    {
                        "url": supplemental_urls[1],
                        "raw_content": "PV-42 supplemental evidence two",
                    }
                ],
                "failed_results": [],
                "usage": {"credits": 1},
            },
        ]
        ai_client = mock.Mock()
        ai_client.next_research_action.side_effect = [
            ai.SearchMoreAction(
                ai.ResearchGap.PRIMARY_DATASHEET,
                (
                    '"PV-42" official datasheet',
                    '"PV-42" technical manual',
                ),
            ),
            ai.SearchMoreAction(
                ai.ResearchGap.MISSING_EXACT_FACT,
                (
                    '"PV-42" complete specifications',
                    '"PV-42" dimensions weight efficiency',
                ),
            ),
            ai.FinalAction(decision("forged-token", "no_datasheet")),
        ]

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("no_datasheet", payload["outcome"])
        submitted = [
            call.args[0]
            for call in search_client.extract_urls.call_args_list
        ]
        self.assertEqual([3, 1, 1], [len(urls) for urls in submitted])
        self.assertEqual(5, len({url for urls in submitted for url in urls}))
        self.assertEqual(2, search_client.search_queries.call_count)
        final_queries = ai_client.next_research_action.call_args.kwargs[
            "previous_queries"
        ]
        self.assertEqual(7, len(final_queries))
        self.assertTrue(
            ai_client.next_research_action.call_args.kwargs["final_only"]
        )
        with state.StateStore(self.state_path) as store:
            attempt = store.attempt_history("P-42")[-1]
            self.assertEqual(
                7,
                attempt.details["payload"]["automation"]["exa"][
                    "query_count"
                ],
            )

    def test_credit_budget_stops_before_another_exa_action(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_RESEARCH_MAX_CREDITS"] = "3"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "results": [],
            "usage": {"credits": 3},
        }
        ai_client = mock.Mock()
        ai_client.next_research_action.return_value = ai.SearchMoreAction(
            ai.ResearchGap.PRIMARY_DATASHEET,
            ('"PV-42" official datasheet',),
        )

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("no_datasheet", payload["outcome"])
        search_client.search_queries.assert_not_called()
        search_client.extract_urls.assert_not_called()
        with state.StateStore(self.state_path) as store:
            attempt = store.attempt_history("P-42")[-1]
            self.assertEqual(
                "Search budget cannot reserve the next search",
                attempt.details["payload"]["automation"]["research"][
                    "stop_reason"
                ],
            )

    def test_credit_budget_reserves_extract_cost_before_calling_exa(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_RESEARCH_MAX_CREDITS"] = "4"
        url = "https://maker.example/pv-42"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["q1", "q2", "q3"],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        ai_client = mock.Mock()
        ai_client.next_research_action.return_value = ai.FinalAction(
            {
                "outcome": "no_datasheet",
                "confidence": 0.9,
                "manufacturer": "",
                "model": "PV-42",
                "product_category": "",
                "summary": "",
                "review_summary": "",
                "review_evidence_urls": [],
                "classification_evidence_urls": [],
                "classification_evidence_quotes": [],
                "decision_notes": "No extract was admitted by the credit budget.",
                "datasheets": [
                    {
                        "url": url,
                        "title": "Contradictory primary datasheet",
                        "source_type": "manufacturer",
                        "is_primary": True,
                    }
                ],
                "sources": [
                    {
                        "url": "https://community.example/pv-42",
                        "title": "Community listing",
                        "source_type": "community",
                    }
                ],
                "facts": [],
                "conflicts": [],
            }
        )

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("no_datasheet", payload["outcome"])
        search_client.extract_urls.assert_not_called()
        with state.StateStore(self.state_path) as store:
            attempt = store.attempt_history("P-42")[-1]
            recorded = attempt.details["payload"]["decision"]
            self.assertEqual([], recorded["datasheets"])
            self.assertEqual([], recorded["sources"])

    def test_uncertain_prior_action_blocks_all_provider_replay(self) -> None:
        self.configure_worker_environment()
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        with state.StateStore(self.state_path) as store:
            replay_product = {
                **product(),
                "product_id": "P-REPLAY",
            }
            store.upsert_product(replay_product, now=past)
            lease = store.lease_next(
                "crashed-worker",
                lease_seconds=3600,
                now=past,
            )
            fingerprint = state.research_request_fingerprint(
                "ai",
                {"context_sha256": "a" * 64},
            )
            store.begin_research_action(
                lease,
                round_number=0,
                action="ai",
                request_fingerprint=fingerprint,
                now=past,
            )
            store.finish_research_action(
                lease,
                round_number=0,
                action="ai",
                request_fingerprint=fingerprint,
                status="uncertain",
                error="response status unknown",
                now=past + timedelta(seconds=1),
            )
            store.record_outcome(
                lease,
                "ai_error",
                error="connection lost",
                now=past + timedelta(seconds=2),
            )
            # Keep the setUp product leased so run-one reaches the due
            # backoff product carrying the unresolved action.
            blocked = store.lease_next(
                "queue-blocker",
                lease_seconds=3600,
                now=datetime.now(timezone.utc),
            )
            self.assertEqual("P-42", blocked.product_id)

        search_client = mock.Mock()
        ai_client = mock.Mock()
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("research_uncertain", payload["outcome"])
        self.assertEqual("P-REPLAY", payload["product_id"])
        self.assertIn("replay suppressed", payload["reason"])
        search_client.search_product.assert_not_called()
        search_client.search_queries.assert_not_called()
        search_client.extract_urls.assert_not_called()
        ai_client.next_research_action.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-REPLAY")
            self.assertEqual("research_uncertain", current.last_outcome)

    def test_wall_clock_budget_is_rechecked_after_supplemental_search(self) -> None:
        self.configure_worker_environment()
        os.environ["PV_WIKI_RESEARCH_MAX_SECONDS"] = "60"
        new_url = "https://example.net/pv-42"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "results": [],
            "usage": {"credits": 3},
        }
        search_client.search_queries.return_value = {
            "results": [{"url": new_url, "score": 0.9}],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()
        ai_client.next_research_action.return_value = ai.SearchMoreAction(
            ai.ResearchGap.PRIMARY_DATASHEET,
            ('"PV-42" official datasheet',),
        )

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(
                cli.time,
                "monotonic",
                side_effect=[0.0, 0.0, 0.0, 61.0],
            ),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("no_datasheet", payload["outcome"])
        search_client.search_queries.assert_called_once()
        search_client.extract_urls.assert_not_called()
        with state.StateStore(self.state_path) as store:
            attempt = store.attempt_history("P-42")[-1]
            self.assertEqual(
                "research wall-clock budget exhausted after search",
                attempt.details["payload"]["automation"]["research"][
                    "stop_reason"
                ],
            )

    def test_extract_response_cannot_swap_in_an_unsubmitted_search_candidate(self) -> None:
        self.configure_worker_environment()
        candidates = [
            f"https://example.net/pv-42-{index}"
            for index in range(4)
        ]
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "results": [
                {"url": url, "score": 1 - index / 10}
                for index, url in enumerate(candidates)
            ],
            "usage": {"credits": 3},
        }
        search_client.extract_urls.return_value = {
            "results": [
                {
                    "url": candidates[3],
                    "raw_content": "PV-42 unexpected substituted response",
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, _, error = self.run_cli("run-one")

        self.assertEqual(2, code)
        self.assertIn("outside this action", error)
        ai_client.return_value.next_research_action.assert_not_called()
        wiki_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("search_error", current.last_outcome)
            attempt = store.attempt_history("P-42")[-1]
            self.assertEqual("search_error", attempt.outcome)
            actions = store.research_action_history(product_id="P-42")
            self.assertEqual("uncertain", actions[-1].status)
            self.assertEqual("extract", actions[-1].action)

    def test_supplemental_search_with_no_new_url_stops_as_normal_outcome(self) -> None:
        self.configure_worker_environment()
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "results": [],
            "usage": {"credits": 3},
        }
        search_client.search_queries.return_value = {
            "results": [],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()
        ai_client.next_research_action.return_value = ai.SearchMoreAction(
            ai.ResearchGap.PRIMARY_DATASHEET,
            ('"PV-42" official technical datasheet',),
        )

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("no_datasheet", payload["outcome"])
        self.assertTrue(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual(1, ai_client.next_research_action.call_count)
        search_client.extract_urls.assert_not_called()
        wiki_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            attempt = store.attempt_history("P-42")[-1]
            research = attempt.details["payload"]["automation"]["research"]
            self.assertEqual(
                "supplemental search returned no new URL",
                research["stop_reason"],
            )
            self.assertEqual(
                [(0, "search"), (0, "ai"), (1, "search")],
                [
                    (item.round_number, item.action)
                    for item in store.research_action_history(product_id="P-42")
                ],
            )

    def test_run_one_blank_name_never_calls_external_services(self) -> None:
        blank = {**product(), "product_name": ""}
        with state.StateStore(self.state_path) as store:
            store.upsert_product(blank)
        self.configure_worker_environment()

        with (
            mock.patch.object(cli, "ExaClient") as search_client,
            mock.patch.object(cli, "OpenAICompatibleClient") as ai_client,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("insufficient_identity", payload["outcome"])
        search_client.assert_not_called()
        ai_client.assert_not_called()
        wiki_client.assert_not_called()

    def test_run_one_empty_search_allows_ai_to_propose_the_final_backoff(self) -> None:
        self.configure_worker_environment()
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [],
            "usage": {"credits": 3},
        }
        ai_instance = mock.Mock()
        ai_instance.next_research_action.return_value = ai.FinalAction(
            decision("forged-token", "no_datasheet")
        )
        with (
            mock.patch.object(cli, "ExaClient", return_value=search_client),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_instance,
            ),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("no_datasheet", payload["outcome"])
        ai_instance.next_research_action.assert_called_once()

    def test_run_one_uses_exa_and_records_provider_cost_in_audit(self) -> None:
        self.configure_worker_environment()
        os.environ["EXA_API_KEY"] = "exa-secret"
        search_client = mock.Mock()
        search_client.provider_name = "exa"
        search_client.api_base_url = cli.EXA_API_BASE_URL
        search_client.search_contract_version = cli.EXA_SEARCH_CONTRACT_VERSION
        search_client.extract_contract_version = cli.EXA_EXTRACT_CONTRACT_VERSION
        search_client.credential_fingerprint = "e" * 64
        search_client.search_product.return_value = {
            "provider": "exa",
            "queries": ["PV-42 datasheet"],
            "results": [],
            "usage": {"credits": 3, "cost_dollars": 0.021},
        }
        ai_instance = mock.Mock()
        ai_instance.next_research_action.return_value = ai.FinalAction(
            decision("forged-token", "no_datasheet")
        )

        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_instance,
            ),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("no_datasheet", payload["outcome"])
        with state.StateStore(self.state_path) as store:
            attempt = store.attempt_history("P-42")[-1]
        self.assertEqual(
            0.021,
            attempt.details["payload"]["automation"]["exa"][
                "search_usage"
            ]["cost_dollars"],
        )

    def test_run_one_empty_extract_records_nonblocking_no_datasheet(self) -> None:
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
            "results": [],
            "failed_results": [{"url": url, "error": "unavailable"}],
            "usage": {"credits": 1},
        }
        search_client.extract_urls.return_value = (
            extract_client.extract_urls.return_value
        )
        ai_instance = mock.Mock()
        ai_instance.next_research_action.return_value = ai.FinalAction(
            decision("forged-token", "no_datasheet")
        )
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_instance,
            ),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("no_datasheet", payload["outcome"])
        ai_instance.next_research_action.assert_called_once()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("no_datasheet", current.last_outcome)

    def test_run_one_sends_off_model_extract_to_ai_but_cannot_publish_it(self) -> None:
        self.configure_worker_environment()
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
        search_client.extract_urls.return_value = (
            extract_client.extract_urls.return_value
        )
        ai_client = mock.Mock()
        ai_client.next_research_action.return_value = ai.FinalAction(
            decision("forged-token", "insufficient_identity")
        )
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertEqual("insufficient_identity", payload["outcome"])
        ai_client.next_research_action.assert_called_once()
        classified = ai_client.next_research_action.call_args.kwargs[
            "extract"
        ]["results"]
        self.assertEqual([url], [item["url"] for item in classified])
        self.assertFalse(classified[0]["identity_verified"])
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("insufficient_identity", current.last_outcome)
            extract_actions = [
                item
                for item in store.research_action_history(product_id="P-42")
                if item.action == "extract"
            ]
            self.assertEqual(
                [url],
                extract_actions[0].result_summary["successful_urls"],
            )

    def test_run_one_uses_ai_to_skip_matching_generic_hardware(self) -> None:
        item = {
            **product(),
            "brand_code": "",
            "family_code": "INTERNAL-DO-NOT-ROUTE",
            "product_name": "flange nut m8",
        }
        with state.StateStore(self.state_path) as store:
            store.upsert_product(item)
        os.environ.update(
            {
                "AI_BASE_URL": "https://ai.example.com/v1",
                "AI_API_KEY": "ai-secret",
                "AI_MODEL": "test-model",
            }
        )
        url = "https://hardware.example/flange-nut-m8"
        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ['"flange nut m8" manufacturer product type official'],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        extract_client = mock.Mock()
        extract_client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": (
                        "Product flange nut M8. Category: metric fastener."
                    ),
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        search_client.extract_urls.return_value = (
            extract_client.extract_urls.return_value
        )
        ai_client = mock.Mock()
        ai_client.next_research_action.return_value = ai.FinalAction({
            "outcome": "out_of_scope",
            "confidence": 0.95,
            "manufacturer": "",
            "model": "flange nut m8",
            "product_category": "紧固件",
            "summary": "A generic M8 flange nut, not an energy device.",
            "review_summary": "",
            "review_evidence_urls": [],
            "classification_evidence_urls": [url],
            "classification_evidence_quotes": [
                {
                    "url": url,
                    "quote": (
                        "Product flange nut M8. Category: metric fastener."
                    ),
                }
            ],
            "decision_notes": "Matching evidence identifies a commodity fastener.",
            "datasheets": [],
            "sources": [],
            "facts": [],
            "conflicts": [],
        })
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("out_of_scope", payload["outcome"])
        wiki_client.assert_not_called()
        classified = ai_client.next_research_action.call_args.kwargs[
            "extract"
        ]["results"]
        self.assertTrue(classified[0]["identity_verified"])
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("out_of_scope", current.last_outcome)
            self.assertEqual("backoff", current.status)

    def test_run_one_never_publishes_hardware_even_if_ai_requests_it(
        self,
    ) -> None:
        self.configure_worker_environment()
        item = {
            **product(),
            "brand_code": "",
            "family_code": "INTERNAL-DO-NOT-ROUTE",
            "product_name": "flange nut m8",
        }
        with state.StateStore(self.state_path) as store:
            store.upsert_product(item)
        url = "https://hardware.example/flange-nut-m8"
        proposal = decision("forged-token")
        proposal.update(
            {
                "manufacturer": "Acme",
                "model": "flange nut m8",
                "product_category": "Fastener",
                "summary": "A documented M8 flange nut.",
            }
        )
        proposal["datasheets"][0]["url"] = url
        evidence_lines: list[str] = []
        for fact in proposal["facts"]:
            fact["evidence_urls"] = [url]
            quote = fact["evidence_quotes"][0]
            quote["url"] = url
            quote["quote"] = quote["quote"].replace(
                "PV-42",
                "flange nut m8",
            )
            evidence_lines.append(quote["quote"])

        search_client = mock.Mock()
        search_client.search_product.return_value = {
            "queries": ['"flange nut m8" official datasheet'],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        search_client.extract_urls.return_value = {
            "results": [
                {
                    "url": url,
                    "raw_content": "\n".join(evidence_lines),
                }
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()
        ai_client.next_research_action.return_value = ai.FinalAction(
            proposal
        )
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertFalse(payload["published"])
        self.assertEqual("insufficient_identity", payload["outcome"])
        self.assertEqual(2, ai_client.next_research_action.call_count)
        wiki_client.assert_not_called()

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
        search_client.extract_urls.return_value = (
            extract_client.extract_urls.return_value
        )
        ai_client = mock.Mock()
        ai_client.next_research_action.return_value = ai.FinalAction(
            decision("forged-token", "ambiguous")
        )
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
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
        ai_client.next_research_action.assert_called_once()
        self.assertEqual(
            [url],
            [
                item["url"]
                for item in ai_client.next_research_action.call_args.kwargs[
                    "extract"
                ]["results"]
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
        search_client.extract_urls.return_value = (
            extract_client.extract_urls.return_value
        )
        ai_client = mock.Mock()
        ai_client.next_research_action.return_value = ai.FinalAction(
            decision("forged-token", "ambiguous")
        )
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
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
        ai_client.next_research_action.assert_called_once()
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
        search_client.extract_urls.return_value = (
            extract_client.extract_urls.return_value
        )
        ai_client = mock.Mock()
        ai_client.next_research_action.side_effect = ai.AIResponseError(
            "invalid AI response"
        )
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
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
            action = store.research_action_history(
                product_id="P-42",
            )[-1]
            self.assertEqual(
                cli._ai_output_fingerprint(ai.AISettings.from_env()),
                action.result_summary["provider_fingerprint"],
            )
            self.assertEqual(
                ai.AIOutputErrorCategory.PROVIDER_ENVELOPE.value,
                action.result_summary["error_category"],
            )

    def test_run_one_ai_timeout_is_audited_without_stopping_batch(self) -> None:
        self.configure_worker_environment()
        url = "https://acme.example/pv-42.pdf"
        search_client = mock.Mock()
        search_client.credential_fingerprint = "a" * 64
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        search_client.extract_urls.return_value = {
            "results": [
                {"url": url, "raw_content": "PV-42 specifications"}
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()
        ai_client.next_research_action.side_effect = ai.AITimeoutError(
            "AI endpoint exceeded the configured wall-clock timeout"
        )
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("ai_error", payload["outcome"])
        self.assertEqual("ai_timeout", payload["reason"])
        self.assertIn("next_attempt_at", payload)
        policy = ai_client.next_research_action.call_args.kwargs[
            "trusted_source_policy"
        ]
        self.assertEqual("Acme", policy.manufacturer)
        self.assertEqual(("acme.example",), policy.domains)
        wiki_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("backoff", current.status)
            self.assertEqual("ai_error", current.last_outcome)
            action = store.research_action_history(
                product_id="P-42",
            )[-1]
            self.assertEqual("ai", action.action)
            self.assertEqual("uncertain", action.status)
            self.assertEqual(
                "AITimeoutError",
                action.result_summary["error_type"],
            )

    def test_run_one_incomplete_ai_output_does_not_stop_batch(self) -> None:
        self.configure_worker_environment()
        url = "https://acme.example/pv-42.pdf"
        search_client = mock.Mock()
        search_client.credential_fingerprint = "a" * 64
        search_client.search_product.return_value = {
            "queries": ["PV-42 datasheet"],
            "results": [{"url": url, "score": 0.9}],
            "usage": {"credits": 3},
        }
        search_client.extract_urls.return_value = {
            "results": [
                {"url": url, "raw_content": "PV-42 specifications"}
            ],
            "failed_results": [],
            "usage": {"credits": 1},
        }
        ai_client = mock.Mock()
        ai_client.next_research_action.side_effect = ai.AIInvalidOutputError(
            "AI endpoint response did not finish normally",
            category=ai.AIOutputErrorCategory.INCOMPLETE_RESPONSE,
        )
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
            ),
            mock.patch.object(
                cli,
                "OpenAICompatibleClient",
                return_value=ai_client,
            ),
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("run-one")

        self.assertEqual(0, code, error)
        self.assertTrue(payload["processed"])
        self.assertFalse(payload["published"])
        self.assertEqual("ai_error", payload["outcome"])
        self.assertEqual("ai_incomplete_response", payload["reason"])
        self.assertIn("next_attempt_at", payload)
        wiki_client.assert_not_called()
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("backoff", current.status)
            self.assertEqual("ai_error", current.last_outcome)
            action = store.research_action_history(
                product_id="P-42",
            )[-1]
            self.assertEqual("ai", action.action)
            self.assertEqual("failed", action.status)
            self.assertEqual(
                "AIInvalidOutputError",
                action.result_summary["error_type"],
            )
            self.assertEqual(
                ai.AIOutputErrorCategory.INCOMPLETE_RESPONSE.value,
                action.result_summary["error_category"],
            )

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
        search_client.extract_urls.return_value = (
            extract_client.extract_urls.return_value
        )
        ai_client = mock.Mock()

        def change_source(**_kwargs: object) -> ai.FinalAction:
            with state.StateStore(self.state_path) as store:
                store.upsert_product(
                    {**product(), "product_name": "PV-42 revised"}
                )
            return ai.FinalAction(decision("forged-token"))

        ai_client.next_research_action.side_effect = change_source
        with (
            mock.patch.object(
                cli,
                "ExaClient",
                return_value=search_client,
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
