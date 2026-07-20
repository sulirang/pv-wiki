from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wikijs-sync-products" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki.decision import DecisionError, validate_decision  # noqa: E402


def valid_decision() -> dict:
    return {
        "schema_version": "1",
        "product_id": "P-42",
        "lease_token": "1234567890abcdef",
        "outcome": "publish",
        "confidence": 0.95,
        "manufacturer": "Acme",
        "model": "PV-42",
        "product_category": "Solar Inverter",
        "summary": "A documented product.",
        "decision_notes": "Exact model and official document match.",
        "datasheets": [
            {
                "url": "https://acme.example/PV-42.pdf",
                "title": "PV-42 datasheet",
                "source_type": "manufacturer",
                "is_primary": True,
            }
        ],
        "sources": [],
        "facts": [
            {
                "name": name,
                "category": category,
                "value": value,
                "unit": unit,
                "confidence": 0.95,
                "evidence_urls": ["https://acme.example/PV-42.pdf"],
            }
            for name, category, value, unit in (
                ("Power", "Output", 42, "W"),
                ("Input voltage", "Input", 48, "V"),
                ("Efficiency", "Efficiency", 98.5, "%"),
                ("Ingress protection", "General", "IP65", ""),
                ("Weight", "General", 12, "kg"),
            )
        ],
        "conflicts": [],
    }


class DecisionTests(unittest.TestCase):
    def test_accepts_cited_official_publish(self) -> None:
        result = validate_decision(
            valid_decision(),
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
        )
        self.assertEqual("publish", result["outcome"])
        self.assertEqual("光伏逆变器", result["product_category"])

    def test_rejects_lease_mismatch_and_low_confidence(self) -> None:
        with self.assertRaisesRegex(DecisionError, "lease_token"):
            validate_decision(
                valid_decision(),
                expected_product_id="P-42",
                expected_lease_token="different-token",
            )
        item = valid_decision()
        item["confidence"] = 0.7
        with self.assertRaisesRegex(DecisionError, "threshold"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_rejects_private_url_and_untrusted_only_source(self) -> None:
        item = valid_decision()
        item["datasheets"][0]["url"] = "http://127.0.0.1/data.pdf"
        with self.assertRaisesRegex(DecisionError, "non-public"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_rejects_numeric_loopback_spellings(self) -> None:
        for url in ("http://127.1/data.pdf", "http://2130706433/data.pdf", "http://0x7f000001/data.pdf"):
            item = valid_decision()
            item["datasheets"][0]["url"] = url
            item["facts"][0]["evidence_urls"] = [url]
            with self.subTest(url=url), self.assertRaises(DecisionError):
                validate_decision(
                    item,
                    expected_product_id="P-42",
                    expected_lease_token="1234567890abcdef",
                )

    def test_primary_itself_must_be_trusted_and_community_is_forbidden(self) -> None:
        item = valid_decision()
        item["datasheets"][0]["source_type"] = "community"
        item["sources"] = [
            {
                "url": "https://acme.example/product/PV-42",
                "title": "Manufacturer page",
                "source_type": "manufacturer",
            }
        ]
        with self.assertRaisesRegex(DecisionError, "community"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_community_sources_are_limited_to_cited_review_summary(self) -> None:
        item = valid_decision()
        item["sources"] = [
            {
                "url": "https://reviews.example.com/pv-42-a",
                "title": "Owner review A",
                "source_type": "community",
            },
            {
                "url": "https://reviews.example.com/pv-42-b",
                "title": "Owner review B",
                "source_type": "community",
            },
        ]
        item["review_summary"] = "Owners report straightforward commissioning."
        item["review_evidence_urls"] = [
            "https://reviews.example.com/pv-42-a",
            "https://reviews.example.com/pv-42-b",
        ]

        result = validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
        )

        self.assertEqual(item["review_summary"], result["review_summary"])

        item["facts"][0]["evidence_urls"].append(
            "https://reviews.example.com/pv-42-a"
        )
        with self.assertRaisesRegex(DecisionError, "community review"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_rejects_low_confidence_or_conflicted_facts(self) -> None:
        item = valid_decision()
        item["facts"][0]["confidence"] = 0.4
        with self.assertRaisesRegex(DecisionError, "configured threshold"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

        item = valid_decision()
        item["facts"] = item["facts"][:4]
        with self.assertRaisesRegex(DecisionError, "at least 5"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

        item = valid_decision()
        item["conflicts"] = [
            {
                "field": "Power",
                "values": [42, 50],
                "source_urls": ["https://acme.example/PV-42.pdf"],
            }
        ]
        with self.assertRaisesRegex(DecisionError, "conflicted fields"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_publish_requires_a_public_product_category(self) -> None:
        item = valid_decision()
        item.pop("product_category")

        with self.assertRaisesRegex(DecisionError, "product_category"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_restricts_decision_to_extracted_urls_when_provided(self) -> None:
        with self.assertRaisesRegex(DecisionError, "not extracted"):
            validate_decision(
                valid_decision(),
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                allowed_evidence_urls={"https://other.example/other.pdf"},
            )

if __name__ == "__main__":
    unittest.main()
