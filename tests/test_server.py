from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
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


class WorkerHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_STATE_PATH": str(
                    Path(self.tempdir.name) / "state.sqlite3"
                )
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
