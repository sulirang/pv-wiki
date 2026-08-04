from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import server  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class WorkerSettingsTests(unittest.TestCase):
    def test_requires_a_strong_token_and_valid_port(self) -> None:
        with self.assertRaises(server.WorkerConfigError):
            server.WorkerSettings(token="short")
        with self.assertRaises(server.WorkerConfigError):
            server.WorkerSettings(token="x" * 32, port=0)
        with self.assertRaises(server.WorkerConfigError):
            server.WorkerSettings(
                token="replace-with-at-least-32-random-characters"
            )

        with mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_WORKER_TOKEN": "t" * 40,
                "PV_WIKI_WORKER_HOST": "127.0.0.1",
                "PV_WIKI_WORKER_PORT": "9090",
            },
            clear=True,
        ):
            settings = server.WorkerSettings.from_env()
        self.assertEqual("127.0.0.1", settings.host)
        self.assertEqual(9090, settings.port)
        self.assertNotIn("t" * 40, repr(settings))

    def test_safe_error_redacts_each_rotated_exa_key(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"EXA_API_KEYS": "exa-key-one,exa-key-two"},
            clear=True,
        ):
            message = server._safe_error(
                RuntimeError("failed exa-key-one then exa-key-two")
            )
        self.assertEqual("failed [REDACTED] then [REDACTED]", message)


class WorkerStatusTests(unittest.TestCase):
    def test_open_circuits_make_readiness_false_without_exposing_config(
        self,
    ) -> None:
        store = mock.MagicMock()
        store.schema_version = 9
        store.status_counts.return_value = {
            "due": 2,
            "leased": 0,
            "backoff": 3,
            "synced": 4,
        }
        store.due_count.return_value = 3
        store.recent_distinct_outcome_streak.return_value = 5
        rate_limit_event = SimpleNamespace(
            http_status=429,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ),
        )
        store.recent_ai_provider_rejection_event.side_effect = [
            None,
            rate_limit_event,
        ]
        store.recent_ai_provider_error_products.return_value = 3
        store.recent_exa_provider_rejection.return_value = None
        store.recent_exa_provider_error.return_value = None
        context = mock.MagicMock()
        context.__enter__.return_value = store

        api_key = "sk-" + ("z" * 40)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PV_WIKI_STATE_PATH": "/tmp/not-opened.sqlite3",
                    "AI_BASE_URL": "https://api.deepseek.com",
                    "AI_API_KEY": api_key,
                    "AI_MODEL": "deepseek-chat",
                    "EXA_API_KEY": "exa-test-key",
                },
                clear=True,
            ),
            mock.patch.object(server, "StateStore", return_value=context),
        ):
            payload = server.status()

        self.assertFalse(payload["ready"])
        self.assertEqual(3, payload["queue"]["due_now"])
        self.assertTrue(payload["circuits"]["decision"]["open"])
        self.assertTrue(payload["circuits"]["ai_provider"]["open"])
        self.assertEqual(
            "rate_limit",
            payload["circuits"]["ai_provider"]["reason"],
        )
        self.assertTrue(payload["circuits"]["ai_output"]["open"])
        self.assertEqual({"paused": False}, payload["research_budget"])
        self.assertEqual(
            {
                "decision_circuit_open",
                "ai_provider_circuit_open",
                "ai_output_circuit_open",
            },
            {
                issue["type"]
                for issue in payload["readiness_issues"]
            },
        )
        self.assertNotIn(api_key, json.dumps(payload))


class WorkerHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_STATE_PATH": str(
                    Path(self.tempdir.name) / "state.sqlite3"
                ),
                "AI_BASE_URL": "https://api.deepseek.com",
                "AI_API_KEY": "sk-" + ("a" * 40),
                "AI_MODEL": "deepseek-chat",
                "EXA_API_KEY": "exa-test-key",
            },
            clear=True,
        )
        self.environment.start()
        self.token = "worker-token-" + ("x" * 32)
        self.calls = 0
        self.sync_calls = 0
        self.refresh_calls = 0
        self.home_calls = 0

        def run() -> dict:
            self.calls += 1
            return {"ok": True, "processed": False}

        def sync() -> dict:
            self.sync_calls += 1
            return {"ok": True, "source_records": 0}

        def publish_home() -> dict:
            self.home_calls += 1
            return {"ok": True, "published": True}

        def refresh() -> dict:
            self.refresh_calls += 1
            return {"ok": True, "source_records": 0}

        self.port = free_port()
        self.httpd = server.build_server(
            server.WorkerSettings(
                token=self.token,
                host="127.0.0.1",
                port=self.port,
            ),
            operations={
                "/run-one": run,
                "/sync-catalogue": sync,
                "/refresh-catalogue": refresh,
                "/publish-home": publish_home,
            },
        )
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.environment.stop()
        self.tempdir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        authorized: bool = False,
        authorization: str | None = None,
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.port,
            timeout=2,
        )
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {self.token}"
        if authorization is not None:
            headers["Authorization"] = authorization
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_health_is_public_but_fixed_operations_require_auth(self) -> None:
        status, payload = self.request("GET", "/healthz")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual("pv-wiki-worker", payload["service"])

        status, payload = self.request("POST", "/run-one", body="{}")
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", payload["error"])
        self.assertEqual(0, self.calls)

        status, payload = self.request(
            "POST",
            "/run-one",
            body="{}",
            authorized=True,
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, self.calls)

    def test_status_requires_auth_and_exposes_redacted_readiness(self) -> None:
        status_code, payload = self.request("GET", "/status")
        self.assertEqual(401, status_code)
        self.assertEqual("unauthorized", payload["error"])

        status_code, payload = self.request(
            "GET",
            "/status",
            authorized=True,
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["ready"])
        self.assertEqual(
            {
                "due": 0,
                "leased": 0,
                "backoff": 0,
                "synced": 0,
                "retired": 0,
            },
            payload["queue"]["counts"],
        )
        self.assertEqual(0, payload["queue"]["due_now"])
        self.assertEqual(
            {"decision", "ai_provider", "ai_output", "exa_provider"},
            set(payload["circuits"]),
        )
        self.assertEqual({"paused": False}, payload["research_budget"])
        self.assertEqual(
            {
                "run_one_busy": False,
                "catalogue_busy": False,
                "homepage_busy": False,
            },
            payload["operations"],
        )
        self.assertNotIn(
            os.environ["AI_API_KEY"],
            json.dumps(payload),
        )

    def test_status_reports_ai_configuration_failure_without_a_secret(
        self,
    ) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "AI_API_KEY": "secret-that-must-not-be-returned",
                "AI_MODEL": "",
            },
        ):
            status_code, payload = self.request(
                "GET",
                "/status",
                authorized=True,
            )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["ready"])
        self.assertIsNone(payload["circuits"]["ai_provider"]["open"])
        self.assertEqual(
            "ai_configuration_invalid",
            payload["readiness_issues"][0]["type"],
        )
        self.assertNotIn(
            "secret-that-must-not-be-returned",
            json.dumps(payload),
        )

    def test_status_reports_invalid_global_budget_configuration(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT": "invalid"},
        ):
            status_code, payload = self.request(
                "GET",
                "/status",
                authorized=True,
            )

        self.assertEqual(200, status_code)
        self.assertFalse(payload["ready"])
        self.assertIsNone(payload["research_budget"]["paused"])
        self.assertIn(
            "global_research_budget_configuration_invalid",
            {
                issue["type"]
                for issue in payload["readiness_issues"]
            },
        )

    def test_unknown_paths_and_nonempty_inputs_fail_closed(self) -> None:
        status, _ = self.request(
            "POST",
            "/anything",
            body="{}",
            authorized=True,
        )
        self.assertEqual(404, status)

        status, payload = self.request(
            "POST",
            "/run-one",
            body='{"command":"whoami"}',
            authorized=True,
        )
        self.assertEqual(400, status)
        self.assertIn("empty JSON object", payload["error"])
        self.assertEqual(0, self.calls)

    def test_non_ascii_bearer_is_rejected_without_dropping_connection(self) -> None:
        status, payload = self.request(
            "POST",
            "/run-one",
            body="{}",
            authorization="Bearer " + ("é" * 40),
        )
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", payload["error"])

    def test_overlapping_run_one_stops_cleanly_without_a_false_failure(
        self,
    ) -> None:
        self.assertTrue(server._RUN_LOCK.acquire(blocking=False))
        try:
            status, payload = self.request(
                "POST",
                "/run-one",
                body="{}",
                authorized=True,
            )
        finally:
            server._RUN_LOCK.release()

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["processed"])
        self.assertEqual("worker_busy", payload["reason"])
        self.assertEqual(0, self.calls)

    def test_catalogue_sync_can_run_while_product_research_is_active(
        self,
    ) -> None:
        self.assertTrue(server._RUN_LOCK.acquire(blocking=False))
        try:
            status, payload = self.request(
                "POST",
                "/sync-catalogue",
                body="{}",
                authorized=True,
            )
        finally:
            server._RUN_LOCK.release()

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, self.sync_calls)

    def test_daily_catalogue_refresh_uses_the_catalogue_lane(self) -> None:
        status, payload = self.request(
            "POST",
            "/refresh-catalogue",
            body="{}",
            authorized=True,
        )

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, self.refresh_calls)

    def test_homepage_refresh_can_run_while_product_research_is_active(
        self,
    ) -> None:
        self.assertTrue(server._RUN_LOCK.acquire(blocking=False))
        try:
            status, payload = self.request(
                "POST",
                "/publish-home",
                body="{}",
                authorized=True,
            )
        finally:
            server._RUN_LOCK.release()

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, self.home_calls)


if __name__ == "__main__":
    unittest.main()
