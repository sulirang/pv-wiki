from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki.catalogue_snapshot import (  # noqa: E402
    CATALOGUE_TABLE,
    catalogue_snapshot_status,
    initialize_catalogue_snapshot,
    replace_catalogue_snapshot,
)
from pv_wiki.state import StateError, StateStore  # noqa: E402


NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


def product(product_id: str, name: str | None = None) -> dict[str, object]:
    return {
        "product_id": product_id,
        "brand_code": "ACME",
        "family_code": "PV",
        "product_name": name or product_id,
        "unit_of_measure": "EA",
        "created_at": NOW,
        "updated_at": NOW,
    }


class CatalogueSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.state = StateStore(self.path)

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def snapshot_ids(self) -> list[str]:
        with self.state._connection() as connection:
            rows = connection.execute(
                f"SELECT product_id FROM {CATALOGUE_TABLE} ORDER BY product_id"
            ).fetchall()
        return [str(row["product_id"]) for row in rows]

    def test_initialization_backfills_legacy_products_once(self) -> None:
        self.state.upsert_products([product("P-1"), product("P-2")], now=NOW)
        status = initialize_catalogue_snapshot(self.state)

        self.assertTrue(status.initialized)
        self.assertEqual(0, status.generation)
        self.assertEqual(2, status.source_records)
        self.assertEqual("legacy-backfill", status.source)
        self.assertEqual(["P-1", "P-2"], self.snapshot_ids())

        self.state.upsert_product(product("P-3"), now=NOW)
        reopened = initialize_catalogue_snapshot(self.state)
        self.assertEqual(2, reopened.source_records)
        self.assertEqual(["P-1", "P-2"], self.snapshot_ids())

    def test_full_refresh_atomically_adds_changes_and_removes(self) -> None:
        self.state.upsert_products([product("P-1"), product("P-2")], now=NOW)
        initialize_catalogue_snapshot(self.state)

        result = replace_catalogue_snapshot(
            self.state,
            [product("P-2", "changed"), product("P-3")],
            source="manual-hermes",
            now=NOW,
        )

        self.assertEqual(1, result.added)
        self.assertEqual(1, result.changed)
        self.assertEqual(1, result.removed)
        self.assertEqual(0, result.unchanged)
        self.assertEqual(1, result.status.generation)
        self.assertEqual("manual-hermes", result.status.source)
        self.assertEqual(["P-2", "P-3"], self.snapshot_ids())

        # Snapshot removal must not cascade into the legacy products/attempts
        # tables used for rollback and historical completion backfill.
        self.assertIsNotNone(self.state.get_product("P-1"))

    def test_empty_or_duplicate_scan_preserves_active_snapshot(self) -> None:
        self.state.upsert_product(product("P-1"), now=NOW)
        initialize_catalogue_snapshot(self.state)
        before = catalogue_snapshot_status(self.state)

        with self.assertRaisesRegex(StateError, "no products"):
            replace_catalogue_snapshot(self.state, [], now=NOW)
        with self.assertRaisesRegex(StateError, "duplicate product_id"):
            replace_catalogue_snapshot(
                self.state,
                [product("P-2"), product("P-2", "duplicate")],
                now=NOW,
            )

        after = catalogue_snapshot_status(self.state)
        self.assertEqual(before, after)
        self.assertEqual(["P-1"], self.snapshot_ids())

    def test_empty_database_is_distinct_from_completed_queue(self) -> None:
        status = initialize_catalogue_snapshot(self.state)
        self.assertFalse(status.initialized)
        self.assertEqual(0, status.source_records)
        self.assertEqual("uninitialized", status.source)


if __name__ == "__main__":
    unittest.main()
