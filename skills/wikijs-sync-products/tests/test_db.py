from __future__ import annotations

import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import db  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.executions = []
        self.closed = False

    def execute(self, statement, parameters=None):
        self.executions.append((statement, parameters))
        return self

    def fetchmany(self, size):
        batch = self._rows[:size]
        del self._rows[:size]
        return batch

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows):
        self.fake_cursor = FakeCursor(rows)
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class ProductReaderTests(unittest.TestCase):
    def setUp(self):
        self.pg_env = mock.patch.dict(os.environ, {"PGSSLMODE": "require"})
        self.pg_env.start()
        self.created = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.updated = datetime(2025, 2, 1, tzinfo=timezone.utc)

    def tearDown(self):
        self.pg_env.stop()

    def test_streams_fixed_product_shape_and_closes_read_transaction(self):
        connection = FakeConnection(
            [
                (
                    "P-1",
                    "ACME",
                    "PANEL",
                    "Panel 1",
                    "EA",
                    self.created,
                    self.updated,
                ),
                (
                    "P-2",
                    None,
                    None,
                    "Panel 2",
                    None,
                    self.created,
                    None,
                ),
            ]
        )
        products = db.ProductReader(lambda: connection).fetch_products(batch_size=1)

        self.assertEqual([item.product_id for item in products], ["P-1", "P-2"])
        self.assertEqual(products[0].to_dict(), products[0].as_dict())
        self.assertEqual(products[0].asdict(), products[0].as_dict())
        self.assertEqual(
            connection.fake_cursor.executions[0],
            ("SET TRANSACTION READ ONLY", None),
        )
        query, parameters = connection.fake_cursor.executions[1]
        self.assertIn("FROM public.products", query)
        self.assertNotIn("WHERE", query)
        self.assertIsNone(parameters)
        self.assertTrue(connection.fake_cursor.closed)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_since_is_a_parameter_and_never_interpolated(self):
        connection = FakeConnection([])
        since = datetime(2025, 3, 4, 5, 6, tzinfo=timezone.utc)

        list(
            db.iter_products(
                since=since,
                connect_factory=lambda: connection,
            )
        )

        query, parameters = connection.fake_cursor.executions[1]
        self.assertIn("COALESCE(updated_at, created_at) >= %s", query)
        self.assertNotIn(since.isoformat(), query)
        self.assertEqual(parameters, (since,))

    def test_mapping_rows_are_supported(self):
        row = {
            "product_id": "P-3",
            "brand_code": "B",
            "family_code": "F",
            "product_name": "Name",
            "unit_of_measure": "EA",
            "created_at": self.created,
            "updated_at": None,
        }
        connection = FakeConnection([row])
        product = db.ProductReader(lambda: connection).fetch_products()[0]
        self.assertEqual(product.product_name, "Name")

    def test_default_connector_is_lazy_and_uses_libpq_environment(self):
        sentinel = object()
        connect = mock.Mock(return_value=sentinel)
        fake_psycopg = types.SimpleNamespace(connect=connect)

        with mock.patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            result = db._connect_from_environment()

        self.assertIs(result, sentinel)
        connect.assert_called_once_with(sslmode="require")

    def test_accepts_and_normalizes_explicit_sslmodes(self):
        for raw, normalized in (
            ("disable", "disable"),
            (" ALLOW ", "allow"),
            ("Prefer", "prefer"),
            ("require", "require"),
            (" VERIFY-CA ", "verify-ca"),
            ("Verify-Full", "verify-full"),
        ):
            with self.subTest(raw=raw):
                sentinel = object()
                connect = mock.Mock(return_value=sentinel)
                fake_psycopg = types.SimpleNamespace(connect=connect)
                with (
                    mock.patch.dict(os.environ, {"PGSSLMODE": raw}),
                    mock.patch.dict(sys.modules, {"psycopg": fake_psycopg}),
                ):
                    result = db._connect_from_environment()

                self.assertIs(result, sentinel)
                connect.assert_called_once_with(sslmode=normalized)

    def test_rejects_missing_and_unknown_sslmodes_before_connecting(self):
        for raw in (None, "", "   ", "unknown"):
            with self.subTest(raw=raw):
                values = {} if raw is None else {"PGSSLMODE": raw}
                connect = mock.Mock()
                fake_psycopg = types.SimpleNamespace(connect=connect)
                with (
                    mock.patch.dict(os.environ, values, clear=True),
                    mock.patch.dict(sys.modules, {"psycopg": fake_psycopg}),
                    self.assertRaises(db.DatabaseConfigurationError) as raised,
                ):
                    db._connect_from_environment()

                self.assertIn("PGSSLMODE", str(raised.exception))
                connect.assert_not_called()

    def test_sslmode_configuration_error_does_not_expose_password(self):
        secret = "database-password-that-must-not-leak"
        with (
            mock.patch.dict(
                os.environ,
                {"PGSSLMODE": "unknown", "PGPASSWORD": secret},
                clear=True,
            ),
            self.assertRaises(db.DatabaseConfigurationError) as raised,
        ):
            db._connect_from_environment()

        self.assertNotIn(secret, str(raised.exception))

    def test_rejects_invalid_batch_size_before_connecting(self):
        connector = mock.Mock()
        reader = db.ProductReader(connector)
        with self.assertRaises(ValueError):
            list(reader.iter_products(batch_size=0))
        connector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
