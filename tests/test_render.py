from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import render  # noqa: E402


class StablePathTests(unittest.TestCase):
    def test_database_id_makes_path_stable_across_title_changes(self) -> None:
        first = {
            "product_id": "PV / 42",
            "brand_code": "ACME Corp.",
            "product_name": "Old title",
        }
        renamed = {**first, "product_name": "A completely new title"}

        self.assertEqual(render.stable_path(first), render.stable_path(renamed))
        self.assertTrue(render.stable_path(first).startswith("products/pv-42-"))
        self.assertTrue(
            render.stable_path(first, prefix="catalogue/2026").startswith(
                "catalogue/2026/pv-42-"
            )
        )

    def test_model_is_a_valid_fallback_when_no_database_id_exists(self) -> None:
        self.assertTrue(
            render.stable_path(
                {"manufacturer": "Acme", "model": "Model 100"}
            ).startswith("products/model-100-")
        )

    def test_slug_collisions_and_brand_changes_do_not_collide_or_move(self) -> None:
        paths = {
            render.stable_path({"product_id": value, "brand_code": "A"})
            for value in ("A+B", "A/B", "A B", "A--B")
        }
        self.assertEqual(4, len(paths))
        self.assertEqual(
            render.stable_path({"product_id": "P-42", "brand_code": "A"}),
            render.stable_path({"product_id": "P-42", "brand_code": "B"}),
        )


class UrlAndEscapingTests(unittest.TestCase):
    def test_table_and_markdown_link_are_escaped(self) -> None:
        self.assertEqual(
            r"PV\|42 &lt;script&gt;",
            render.escape_table_cell("PV|42 <script>"),
        )
        link = render.markdown_link(
            "Official ](bad)",
            "https://EXAMPLE.com/a(b).pdf?q=x)y",
        )
        self.assertEqual(
            r"[Official \]\(bad\)](https://example.com/a%28b%29.pdf?q=x%29y)",
            link,
        )

    def test_non_http_and_local_ip_urls_are_rejected(self) -> None:
        invalid = (
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://127.0.0.1/private",
            "http://10.1.2.3/private",
            "http://[::1]/private",
            "http://127.1/private",
            "https://service.local/doc.pdf",
            "https://user:pass@example.com/doc.pdf",
            "https://exa mple.com/doc.pdf",
        )
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                render.validate_public_http_url(url)

    def test_public_url_is_normalized_without_network_access(self) -> None:
        self.assertEqual(
            "https://example.com:8443/a%20b.pdf",
            render.validate_public_http_url("HTTPS://Example.COM:8443/a b.pdf"),
        )


class RenderTests(unittest.TestCase):
    def test_render_is_deterministic_and_sorts_unordered_content(self) -> None:
        product = {
            "product_id": "42",
            "brand_code": "ACME",
            "product_name": "PV|42 <module>",
            "unit_of_measure": "piece",
        }
        first = {
            "datasheets": [
                {"title": "B sheet", "url": "https://example.com/b.pdf"},
                {"title": "A sheet", "url": "https://example.com/a.pdf"},
            ],
            "specifications": {"Voltage|input": "48 < 60 V", "Current": "2 A"},
            "summary": "Matched [manufacturer] evidence.",
            "confidence": 0.875,
        }
        second = {
            "confidence": 0.875,
            "summary": "Matched [manufacturer] evidence.",
            "specifications": {"Current": "2 A", "Voltage|input": "48 < 60 V"},
            "datasheets": list(reversed(first["datasheets"])),
        }
        checked = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

        left = render.render_product_page(product, first, checked)
        right = render.render_product_page(product, second, checked)

        self.assertEqual(left, right)
        self.assertTrue(left.startswith(render.AUTO_BEGIN + "\n"))
        self.assertTrue(left.endswith(render.AUTO_END + "\n"))
        self.assertIn(r"PV\|42 &lt;module&gt;", left)
        self.assertIn(r"Voltage\|input", left)
        self.assertLess(left.index("a.pdf"), left.index("b.pdf"))
        self.assertIn("2026-07-14T12:00:00+00:00", left)

    def test_render_fails_closed_on_invalid_search_result_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "public|local|HTTP"):
            render.render_product_page(
                {"product_id": "42", "product_name": "PV-42"},
                {"datasheet_url": "http://192.168.1.20/datasheet.pdf"},
            )

    def test_normalized_decision_facts_and_evidence_are_rendered(self) -> None:
        rendered = render.render_product_page(
            {"product_id": "42", "product_name": "PV-42"},
            {
                "outcome": "publish",
                "confidence": 0.93,
                "summary": "Official source matched.",
                "datasheets": [
                    {
                        "title": "Official datasheet",
                        "url": "https://example.com/pv-42.pdf",
                        "source_type": "manufacturer",
                        "is_primary": True,
                    }
                ],
                "facts": [
                    {
                        "name": "Input voltage",
                        "value": 48,
                        "unit": "V",
                        "confidence": 0.95,
                        "evidence_urls": ["https://example.com/manual.html"],
                    }
                ],
                "conflicts": [
                    {
                        "field": "Ingress rating",
                        "values": ["IP65", "IP67"],
                        "source_urls": ["https://example.com/ip-rating.html"],
                    }
                ],
            },
        )

        self.assertIn("| Input voltage | 48 V |", rendered)
        self.assertIn("[Input voltage（证据）](https://example.com/manual.html)", rendered)
        self.assertIn("- 判定：publish", rendered)
        self.assertIn("## 未解决的来源冲突", rendered)
        self.assertIn("IP65 / IP67", rendered)


class MergeTests(unittest.TestCase):
    def test_replaces_only_auto_block_and_preserves_human_bytes(self) -> None:
        prefix = "Manual **before**\r\n\r\n"
        suffix = "\n\nManual after  \n"
        existing = (
            prefix
            + render.AUTO_BEGIN
            + "\nold generated text\n"
            + render.AUTO_END
            + suffix
        )
        managed = render.AUTO_BEGIN + "\nnew generated text\n" + render.AUTO_END + "\n"

        merged = render.merge_auto_block(existing, managed)

        self.assertEqual(
            prefix + render.AUTO_BEGIN + "\nnew generated text\n" + render.AUTO_END + suffix,
            merged,
        )

    def test_adds_a_single_block_to_a_human_only_page(self) -> None:
        merged = render.merge_auto_block("Human notes\n", "generated")
        self.assertEqual(1, merged.count(render.AUTO_BEGIN))
        self.assertEqual(1, merged.count(render.AUTO_END))
        self.assertTrue(merged.startswith("Human notes\n\n"))

    def test_malformed_existing_or_managed_markers_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render.merge_auto_block(render.AUTO_BEGIN + "\nbroken", "new")
        with self.assertRaises(ValueError):
            render.merge_auto_block(
                render.AUTO_BEGIN + render.AUTO_END + render.AUTO_BEGIN + render.AUTO_END,
                "new",
            )
        with self.assertRaises(ValueError):
            render.merge_auto_block("", f"outside\n{render.AUTO_BEGIN}\nx\n{render.AUTO_END}")


if __name__ == "__main__":
    unittest.main()
