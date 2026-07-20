from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wikijs-sync-products" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import cli, state  # noqa: E402


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
            store.finish_extract(lease, {"credits": 1})

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
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "wiki-secret",
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

    def test_live_doctor_refuses_weak_sslmode_before_connections(self) -> None:
        self.configure_required_environment(sslmode="prefer")
        with (
            mock.patch.object(cli, "ProductReader") as reader,
            mock.patch.object(cli, "WikiJSClient") as wiki_client,
        ):
            code, payload, error = self.run_cli("doctor", "--live")

        self.assertEqual(2, code, error)
        self.assertFalse(payload["checks"]["postgresql_tls"]["ok"])
        reader.assert_not_called()
        wiki_client.assert_not_called()

    def test_sync_db_refuses_weak_sslmode_without_leaking_password(self) -> None:
        secret = "database-password-that-must-not-leak"
        os.environ.update({"PGSSLMODE": "allow", "PGPASSWORD": secret})

        code, _, error = self.run_cli("sync-db")

        self.assertEqual(2, code)
        self.assertIn("DatabaseConfigurationError", error)
        self.assertNotIn(secret, error)

    def test_precheck_claim_and_status_are_resumable(self) -> None:
        code, before, _ = self.run_cli("precheck")
        self.assertEqual(0, code)
        self.assertEqual(1, before["due"])
        self.assertTrue(before["wakeAgent"])

        self.claim()
        code, after, _ = self.run_cli("precheck")
        self.assertEqual(0, code)
        self.assertEqual(0, after["due"])
        self.assertFalse(after["wakeAgent"])

    def test_publish_home_upserts_managed_landing_page(self) -> None:
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
        self.assertEqual(1, payload["counts"]["due"])
        arguments = client.upsert_page.call_args.args
        self.assertEqual("home", arguments[0])
        self.assertEqual("PV Wiki", arguments[2])
        self.assertIn("| Total catalogue products | 1 |", arguments[4])

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
            "results": [{"url": url, "raw_content": "x" * 40_000}],
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

    def test_official_decision_upserts_wiki_and_marks_synced(self) -> None:
        token = self.claim()
        self.prepare_evidence(token, "https://acme.example/pv-42.pdf")
        path = self.write_json("publish.json", decision(token))
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
            code, payload, error = self.run_cli("publish", "--decision-file", str(path))

        self.assertEqual(0, code, error)
        self.assertTrue(payload["published"])
        self.assertEqual("products/p-42-c8d5a4d2d3", payload["wiki"]["path"])
        self.assertEqual("created", payload["wiki"]["action"])
        tags = client.upsert_page.call_args.args[5]
        self.assertIn("managed-by-hermes", tags)
        self.assertNotIn("family-pv", tags)
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("synced", current.status)

    def test_invalid_decision_closes_lease_with_transient_backoff(self) -> None:
        token = self.claim()
        path = self.write_json("invalid.json", decision(token))

        code, _, error = self.run_cli("publish", "--decision-file", str(path))

        self.assertEqual(2, code)
        self.assertIn("DecisionError", error)
        with state.StateStore(self.state_path) as store:
            current = store.get_product("P-42")
            self.assertEqual("backoff", current.status)
            self.assertEqual("invalid_decision", current.last_outcome)


if __name__ == "__main__":
    unittest.main()
