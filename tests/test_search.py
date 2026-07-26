from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import search  # noqa: E402


class BuildQueriesTests(unittest.TestCase):
    def test_build_queries_is_official_focused_unique_and_bounded(self) -> None:
        queries = search.build_queries(
            {
                "manufacturer": "Acme",
                "model": "PV-42",
                "category": "solar inverter",
            }
        )

        self.assertEqual(3, len(queries))
        self.assertTrue(all(len(query) <= 400 for query in queries))
        self.assertTrue(all("official" in query for query in queries))
        self.assertEqual(len(queries), len(set(queries)))
        self.assertIn('"Acme" "PV-42"', queries[0])

    def test_build_queries_requires_meaningful_identity(self) -> None:
        with self.assertRaises(TypeError):
            search.build_queries([])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            search.build_queries({"id": 123})


if __name__ == "__main__":
    unittest.main()
