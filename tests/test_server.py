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

    def test_safe_error_redacts_each_rotated_tavily_key(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"TAVILY_API_KEYS": "key-one-long,key-two-long"},
            clear=True,
        ):
            message = server._safe_error(
                RuntimeError("failed key-one-long then key-two-long")
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

        def run() -> dict:
            self.calls += 1
            return {"ok": True, "processed": False}

        self.port = free_port()
        self.httpd = server.build_server(
            server.WorkerSettings(
                token=self.token,
                host="127.0.0.1",
                port=self.port,
            ),
            operations={"/run-one": run},
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


if __name__ == "__main__":
    unittest.main()
