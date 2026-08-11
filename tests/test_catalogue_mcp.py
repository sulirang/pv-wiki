from __future__ import annotations

import json
import os
import sys
import types
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

from pv_wiki import catalogue_mcp  # noqa: E402
from pv_wiki.catalogue_mcp import (  # noqa: E402
    CatalogueRefreshError,
    refresh_catalogue_once,
)
from pv_wiki.db import CatalogueEndpoint  # noqa: E402


SECRET = "one-shot-database-password"


class FakeCursor:
    def __init__(self) -> None:
        self.rows = [
            (
                "P-1",
                "ACME",
                "PV",
                "PV-1",
                "EA",
                None,
                None,
            )
        ]
        self.executed: list[tuple[str, object]] = []
        self.closed = False

    def execute(self, statement: str, parameters: object = None) -> None:
        self.executed.append((statement, parameters))

    def fetchmany(self, _size: int) -> list[tuple[object, ...]]:
        rows, self.rows = self.rows, []
        return rows

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class CatalogueMCPTests(unittest.TestCase):
    def endpoint(self) -> CatalogueEndpoint:
        return CatalogueEndpoint(
            host="catalogue.internal",
            port=5432,
            database="catalogue",
            sslmode="verify-full",
            sslrootcert="/run/secrets/catalogue-ca.pem",
        )

    def test_explicit_connection_uses_kwargs_without_mutating_environment(self) -> None:
        captured: dict[str, object] = {}
        sentinel = object()

        class Psycopg:
            @staticmethod
            def connect(**kwargs: object) -> object:
                captured.update(kwargs)
                return sentinel

        before = dict(os.environ)
        with mock.patch("pv_wiki.db._psycopg_module", return_value=Psycopg):
            connection = self.endpoint().connect(
                username="temporary_reader",
                password=SECRET,
            )

        self.assertIs(sentinel, connection)
        self.assertEqual("temporary_reader", captured["user"])
        self.assertEqual(SECRET, captured["password"])
        self.assertEqual("catalogue.internal", captured["host"])
        self.assertEqual("verify-full", captured["sslmode"])
        self.assertEqual(before, dict(os.environ))

    def test_one_shot_refresh_closes_source_and_never_returns_credentials(self) -> None:
        connection = FakeConnection()
        credentials: list[tuple[str, str]] = []

        class Endpoint:
            def connect(self, *, username: str, password: str) -> FakeConnection:
                credentials.append((username, password))
                return connection

        def syncer(reader: object) -> dict[str, object]:
            products = reader.fetch_products(batch_size=500)  # type: ignore[attr-defined]
            return {
                "ok": True,
                "source_records": len(products),
                "snapshot": {
                    "generation": 1,
                    "checksum": "a" * 64,
                    "refreshed_at": "2026-08-10T00:00:00+00:00",
                    "added": 1,
                    "changed": 0,
                    "removed": 0,
                    "unchanged": 0,
                },
                "internal_debug": f"{credentials[-1][0]}:{credentials[-1][1]}",
            }

        payload = refresh_catalogue_once(
            username="temporary_reader",
            password=SECRET,
            endpoint=Endpoint(),  # type: ignore[arg-type]
            syncer=syncer,
        )

        self.assertEqual([("temporary_reader", SECRET)], credentials)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.cursor_instance.closed)
        self.assertFalse(payload["credentials_stored_by_pv_wiki"])
        self.assertTrue(payload["history_retention_possible"])
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertNotIn("temporary_reader", json.dumps(payload))

    def test_dynamic_database_error_is_reduced_to_nonsecret_code(self) -> None:
        class Endpoint:
            def connect(self, *, username: str, password: str) -> object:
                raise RuntimeError(f"authentication failed for {username}: {password}")

        def syncer(reader: object) -> dict[str, object]:
            reader.fetch_products()  # type: ignore[attr-defined]
            return {"ok": True}

        with self.assertRaises(CatalogueRefreshError) as raised:
            refresh_catalogue_once(
                username="temporary_reader",
                password=SECRET,
                endpoint=Endpoint(),  # type: ignore[arg-type]
                syncer=syncer,
            )
        self.assertEqual("source_unavailable", raised.exception.code)
        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn("temporary_reader", str(raised.exception))

    def test_admin_mcp_exposes_only_manual_refresh_tool(self) -> None:
        registered: dict[str, tuple[object, dict[str, object]]] = {}

        class FakeAnnotations:
            @classmethod
            def model_validate(cls, value: dict[str, object]) -> dict[str, object]:
                return value

        class FakeTextContent:
            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class FakeCallToolResult:
            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class FakeServer:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def tool(self, **metadata: object):
                def decorate(function: object) -> object:
                    registered[str(metadata["name"])] = (function, metadata)
                    return function

                return decorate

        mcp_package = types.ModuleType("mcp")
        mcp_package.__path__ = []  # type: ignore[attr-defined]
        mcp_types = types.ModuleType("mcp.types")
        mcp_types.ToolAnnotations = FakeAnnotations
        mcp_types.TextContent = FakeTextContent
        mcp_types.CallToolResult = FakeCallToolResult
        mcp_server = types.ModuleType("mcp.server")
        mcp_server.MCPServer = FakeServer
        with mock.patch.dict(
            sys.modules,
            {
                "mcp": mcp_package,
                "mcp.types": mcp_types,
                "mcp.server": mcp_server,
            },
        ):
            catalogue_mcp.build_server(
                endpoint_factory=self.endpoint,
                syncer=lambda _reader: {
                    "ok": True,
                    "source_records": 7,
                    "snapshot": {
                        "generation": 3,
                        "checksum": "b" * 64,
                        "refreshed_at": "2026-08-10T00:00:00+00:00",
                        "added": 2,
                        "changed": 1,
                        "removed": 4,
                        "unchanged": 0,
                    },
                },
            )

        self.assertEqual({"pv_refresh_catalogue"}, set(registered))
        tool, metadata = registered["pv_refresh_catalogue"]
        self.assertTrue(metadata["annotations"]["destructiveHint"])
        self.assertFalse(metadata["annotations"]["idempotentHint"])
        response = tool("temporary_reader", SECRET)  # type: ignore[operator]
        payload = json.loads(response.content[0].text)
        self.assertTrue(payload["ok"])
        self.assertEqual(7, payload["source_records"])
        self.assertEqual(3, payload["generation"])
        self.assertNotIn(SECRET, response.content[0].text)
        self.assertNotIn("temporary_reader", response.content[0].text)

    def test_endpoint_factory_failure_is_returned_as_a_safe_error(self) -> None:
        registered: dict[str, object] = {}

        class FakeAnnotations:
            @classmethod
            def model_validate(cls, value: dict[str, object]) -> dict[str, object]:
                return value

        class FakeTextContent:
            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class FakeCallToolResult:
            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class FakeServer:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def tool(self, **metadata: object):
                def decorate(function: object) -> object:
                    registered[str(metadata["name"])] = function
                    return function

                return decorate

        def failing_factory() -> CatalogueEndpoint:
            raise RuntimeError(f"unexpected endpoint failure: {SECRET}")

        mcp_package = types.ModuleType("mcp")
        mcp_package.__path__ = []  # type: ignore[attr-defined]
        mcp_types = types.ModuleType("mcp.types")
        mcp_types.ToolAnnotations = FakeAnnotations
        mcp_types.TextContent = FakeTextContent
        mcp_types.CallToolResult = FakeCallToolResult
        mcp_server = types.ModuleType("mcp.server")
        mcp_server.MCPServer = FakeServer
        with mock.patch.dict(
            sys.modules,
            {
                "mcp": mcp_package,
                "mcp.types": mcp_types,
                "mcp.server": mcp_server,
            },
        ):
            catalogue_mcp.build_server(endpoint_factory=failing_factory)

        tool = registered["pv_refresh_catalogue"]
        response = tool("temporary_reader", SECRET)  # type: ignore[operator]
        payload = json.loads(response.content[0].text)
        self.assertTrue(response.is_error)
        self.assertEqual("source_unavailable", payload["error_code"])
        self.assertNotIn(SECRET, response.content[0].text)
        self.assertNotIn("temporary_reader", response.content[0].text)


if __name__ == "__main__":
    unittest.main()
