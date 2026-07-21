from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wikijs-sync-products" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki.decision import (  # noqa: E402
    DecisionError,
    text_contains_competing_identity,
    text_contains_exact_identity,
    validate_decision as _validate_decision,
)


def validate_decision(*args: object, **kwargs: object) -> dict:
    kwargs.setdefault("trusted_source_domains", {"acme.example"})
    kwargs.setdefault("expected_product_name", "PV-42")
    kwargs.setdefault(
        "evidence_text_by_url",
        {
            "https://acme.example/PV-42.pdf": (
                "PV-42 Power 42 W\nPV-42 Input voltage 48 V\n"
                "PV-42 Efficiency 98.5%\n"
                "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
            )
        },
    )
    return _validate_decision(*args, **kwargs)


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
                "evidence_quotes": [
                    {
                        "url": "https://acme.example/PV-42.pdf",
                        "quote": f"PV-42 {name} {value} {unit}".strip(),
                    }
                ],
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
    def test_exact_identity_match_rejects_prefixes_suffixes_and_substrings(self) -> None:
        self.assertTrue(
            text_contains_exact_identity(
                "SUN2000-8KTL-M1",
                "Model: SUN2000 / 8KTL - M1 specifications",
            )
        )
        for expected, body in (
            ("PV-42", "PV-420 full specifications"),
            ("PV-42", "PV-42.1 full specifications"),
            ("AB12", "cab 123"),
            ("SUN2000-8KTL-M1", "SUN2000-8KTL-M1-HC"),
            ("SUN2000-8KTL-M1", "HC-SUN2000-8KTL-M1"),
        ):
            with self.subTest(expected=expected, body=body):
                self.assertFalse(text_contains_exact_identity(expected, body))
        self.assertTrue(
            text_contains_competing_identity(
                "SUN2000-8KTL-M1",
                "SUN2000-8KTL-M1 and SUN2000-10KTL-M1",
            )
        )
        self.assertTrue(
            text_contains_competing_identity(
                "SUN2000-8KTL-M1",
                "SUN2000-8KTL-M1 and SUN2000-6KTL",
            )
        )
        self.assertTrue(
            text_contains_competing_identity(
                "SUN2000-8KTL-M1",
                "SUN2000-8KTL-M1 output powers: 6KTL 6000 W",
            )
        )
        self.assertTrue(
            text_contains_competing_identity(
                "SUN2000-8KTL-M1",
                "SUN2000-8KTL-M1 and SUN2000-8KTL-L1",
            )
        )
        self.assertTrue(
            text_contains_competing_identity(
                "ACME-PRO-100",
                "ACME-PRO-100 and ACME-MAX-100",
            )
        )
        self.assertTrue(
            text_contains_competing_identity("PV-42", "PV-42 and PV-43")
        )
        for body in (
            "PV-42 and PV-42A",
            "PV42 and PV42A",
            "PV-42 A",
            "PV-42 (A)",
            "PV-42 R2",
            "PV-42 V2",
            "PV-42 2B",
            "PV-42 rev B",
            "PV-42 Rev.B",
            "PV-42 Revision B",
            "PV-42 Rev AB",
            "PV-42 Revision 2B",
            "PV-42 (Rev B)",
            "PV-42 Version 2.1",
            "PV-42 and HC-PV-42",
            "PV-42 and HC PV-42",
            "PV-42 and HCPV-42",
            "PV-42 and hc-PV-42",
            "PV-42 and hc PV-42",
            "PV-42 and hcpv-42",
            "PV-42 and Hc-PV-42",
            "PV-42 and hc.PV-42",
            "PV-42 and hc:PV-42",
            "PV-42 and hc–PV-42",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    text_contains_competing_identity("PV-42", body)
                )
        self.assertFalse(
            text_contains_competing_identity(
                "PV-42",
                "PV-42 photovoltaic power specifications",
            )
        )
        self.assertFalse(
            text_contains_competing_identity(
                "PV-42",
                "PV-42 review summary",
            )
        )
        self.assertFalse(
            text_contains_competing_identity(
                "PV-42",
                "PV-42 revised specifications and revision history",
            )
        )
        self.assertFalse(
            text_contains_competing_identity(
                "PV-42",
                "PV-42 a documented product",
            )
        )
        self.assertFalse(
            text_contains_competing_identity(
                "PV-42 Rev B",
                "PV-42 Rev B specifications",
            )
        )
        self.assertFalse(
            text_contains_competing_identity(
                "SUN2000-8KTL-M1",
                "SUN2000-8KTL-M1 Maximum efficiency SUN2000 98.6 percent",
            )
        )
        self.assertFalse(
            text_contains_competing_identity(
                "Samsung Galaxy S24",
                "Samsung Galaxy S24 Weight 168 g Samsung Electronics",
            )
        )
        for area_unit in ("m2", "m²"):
            with self.subTest(area_unit=area_unit):
                self.assertFalse(
                    text_contains_competing_identity(
                        "SUN2000-8KTL-M1",
                        f"SUN2000-8KTL-M1 Installation footprint 0.5 {area_unit}",
                    )
                )
        for body in (
            "PV-42 AC output specifications",
            "PV-42 DC input voltage",
            "PV-42 MAX. efficiency",
            "PV-42 IP rating",
            "PV-42 LED status",
            "PV-42. A documented product",
            "PV-42, A documented product",
            "PV-42 (AC) output",
            "PV-42\nA output",
            "PV-42\nRevision B",
            "ID: PV-42",
            "NO. PV-42",
            "SKU PV-42",
            "MPN PV-42",
            "REF PV-42",
            "FOR PV-42",
            "ID-PV-42",
            "EN/PV-42",
            "NO_PV-42",
            "AC/PV-42",
            "LG-PV-42",
            "HP/PV-42",
            "GE_PV-42",
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    text_contains_competing_identity("PV-42", body)
                )

    def test_accepts_cited_official_publish(self) -> None:
        result = validate_decision(
            valid_decision(),
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
        )
        self.assertEqual("publish", result["outcome"])
        self.assertEqual("光伏逆变器", result["product_category"])

    def test_common_uppercase_spec_labels_are_not_sibling_models(self) -> None:
        item = valid_decision()
        rows = []
        specifications = (
            ("AC output", "Output", 42, "W"),
            ("DC input voltage", "Input", 48, "V"),
            ("MAX. efficiency", "Efficiency", 98.5, "%"),
            ("IP rating", "General", "IP65", ""),
            ("LED status", "General", "Green", ""),
        )
        for fact, (name, category, value, unit) in zip(
            item["facts"], specifications, strict=True
        ):
            fact.update(
                {
                    "name": name,
                    "category": category,
                    "value": value,
                    "unit": unit,
                }
            )
            quote = f"PV-42 {name} {value} {unit}".strip()
            fact["evidence_quotes"][0]["quote"] = quote
            rows.append(quote)

        result = validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            evidence_text_by_url={
                "https://acme.example/PV-42.pdf": "\n".join(rows)
            },
        )

        self.assertEqual("publish", result["outcome"])

    def test_series_datasheet_can_publish_target_specific_fact_spans(self) -> None:
        item = valid_decision()
        url = "https://solar.huawei.com/sun2000-m1-datasheet.pdf"
        target = "SUN2000-8KTL-M1"
        item.update(
            {
                "product_id": "SUN8",
                "manufacturer": "Huawei",
                "model": target,
                "datasheets": [
                    {
                        "url": url,
                        "title": "SUN2000-3-10KTL-M1 datasheet",
                        "source_type": "manufacturer",
                        "is_primary": True,
                    }
                ],
            }
        )
        specifications = (
            ("Rated output power", "Output", 8000, "W"),
            ("Maximum apparent power", "Output", 8800, "VA"),
            ("Maximum efficiency", "Efficiency", 98.6, "%"),
            ("Protection degree", "General", "IP65", ""),
            ("Weight", "General", 17, "kg"),
        )
        target_rows = []
        for fact, (name, category, value, unit) in zip(
            item["facts"], specifications, strict=True
        ):
            quote = f"{target} {name} {value} {unit}".strip()
            fact.update(
                {
                    "name": name,
                    "category": category,
                    "value": value,
                    "unit": unit,
                    "evidence_urls": [url],
                    "evidence_quotes": [{"url": url, "quote": quote}],
                }
            )
            target_rows.append(quote)
        body = "\n".join(
            [
                "Models: SUN2000-5KTL-M1 SUN2000-6KTL-M1 "
                "SUN2000-8KTL-M1 SUN2000-10KTL-M1",
                *target_rows,
            ]
        )

        result = _validate_decision(
            item,
            expected_product_id="SUN8",
            expected_lease_token="1234567890abcdef",
            allowed_evidence_urls={url},
            trusted_source_domains={"solar.huawei.com"},
            expected_product_name=target,
            evidence_text_by_url={url: body},
        )

        self.assertEqual("publish", result["outcome"])

        ambiguous_row = (
            "SUN2000-8KTL-M1 6KTL Rated output power 8000 W 6000 W"
        )
        item["facts"][0].update(
            {
                "value": 6000,
                "evidence_quotes": [{"url": url, "quote": ambiguous_row}],
            }
        )
        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            _validate_decision(
                item,
                expected_product_id="SUN8",
                expected_lease_token="1234567890abcdef",
                allowed_evidence_urls={url},
                trusted_source_domains={"solar.huawei.com"},
                expected_product_name=target,
                evidence_text_by_url={url: f"{body}\n{ambiguous_row}"},
            )

        variant_row = (
            "SUN2000-8KTL-M1 SUN2000-8KTL-L1 "
            "Rated output power 8000 W 7000 W"
        )
        item["facts"][0].update(
            {
                "value": 7000,
                "evidence_quotes": [{"url": url, "quote": variant_row}],
            }
        )
        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            _validate_decision(
                item,
                expected_product_id="SUN8",
                expected_lease_token="1234567890abcdef",
                allowed_evidence_urls={url},
                trusted_source_domains={"solar.huawei.com"},
                expected_product_name=target,
                evidence_text_by_url={url: f"{body}\n{variant_row}"},
            )

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

        item = valid_decision()
        item["facts"][1]["name"] = "Power!"
        item["facts"][1]["evidence_quotes"][0]["quote"] = "PV-42 Power! 48 V"
        with self.assertRaisesRegex(DecisionError, "unique"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

        item = valid_decision()
        item["conflicts"] = [
            {
                "field": "Input-voltage",
                "values": [48, 50],
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

    def test_model_cannot_grant_trust_or_change_catalogue_identity(self) -> None:
        with self.assertRaisesRegex(DecisionError, "not approved"):
            _validate_decision(
                valid_decision(),
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                trusted_source_domains={"manufacturer.example"},
                expected_product_name="PV-42",
                evidence_text_by_url={
                    "https://acme.example/PV-42.pdf": (
                        "PV-42 Power 42 W\nPV-42 Input voltage 48 V\n"
                        "PV-42 Efficiency 98.5%\n"
                        "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
                    )
                },
            )

        item = valid_decision()
        item["model"] = "PV-43"
        with self.assertRaisesRegex(DecisionError, "catalogue product name"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_every_published_fact_needs_trusted_or_dual_mirror_evidence(self) -> None:
        item = valid_decision()
        mirror_url = "https://mirror.example/PV-42.pdf"
        item["sources"] = [
            {
                "url": mirror_url,
                "title": "Untrusted mirror",
                "source_type": "mirror",
            }
        ]
        for fact in item["facts"]:
            fact["evidence_urls"] = [mirror_url]
            fact["evidence_quotes"] = [
                {
                    "url": mirror_url,
                    "quote": (
                        f"PV-42 {fact['name']} {fact['value']} "
                        f"{fact.get('unit', '')}"
                    ).strip(),
                }
            ]
        with self.assertRaisesRegex(DecisionError, "trusted domain"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                evidence_text_by_url={
                    mirror_url: (
                        "PV-42 Power 42 W\nPV-42 Input voltage 48 V\n"
                        "PV-42 Efficiency 98.5%\n"
                        "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
                    )
                },
            )

        item = valid_decision()
        mirror_a = "https://mirror-a.example/PV-42.pdf"
        mirror_b = "https://mirror-b.test/PV-42.pdf"
        item["datasheets"] = [
            {
                "url": mirror_a,
                "title": "Mirror A",
                "source_type": "mirror",
                "is_primary": True,
            },
            {
                "url": mirror_b,
                "title": "Mirror B",
                "source_type": "mirror",
                "is_primary": True,
            },
        ]
        for fact in item["facts"]:
            fact["evidence_urls"] = [mirror_a]
            fact["evidence_quotes"] = [
                {
                    "url": mirror_a,
                    "quote": (
                        f"PV-42 {fact['name']} {fact['value']} "
                        f"{fact.get('unit', '')}"
                    ).strip(),
                }
            ]
        with self.assertRaisesRegex(DecisionError, "two independent"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                mirrors_allowed=True,
                evidence_text_by_url={
                    mirror_a: (
                        "PV-42 Power 42 W\nPV-42 Input voltage 48 V\n"
                        "PV-42 Efficiency 98.5%\n"
                        "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
                    )
                },
            )

    def test_fact_quotes_bind_model_label_and_value_to_the_extract(self) -> None:
        item = valid_decision()
        item["facts"][0]["evidence_quotes"][0]["quote"] = "Power 42 W"
        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

        item = valid_decision()
        body = (
            "PV-42 PV-43 Rated power 4.2 kW 4.3 kW\n"
            "PV-42 Input voltage 48 V\nPV-42 Efficiency 98.5%\n"
            "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
        )
        item["facts"][0].update(
            {
                "name": "Rated power",
                "value": 4.3,
                "unit": "kW",
                "evidence_quotes": [
                    {
                        "url": "https://acme.example/PV-42.pdf",
                        "quote": "PV-42 PV-43 Rated power 4.2 kW 4.3 kW",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                evidence_text_by_url={
                    "https://acme.example/PV-42.pdf": body
                },
            )

        item["facts"][0]["evidence_quotes"][0]["quote"] = (
            "PV-42 Rated power 4.3 kW"
        )
        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                evidence_text_by_url={
                    "https://acme.example/PV-42.pdf": body
                },
            )

        wrong_values = (420, 480, 99.9, "IP68", 20)
        wrong_units = ("W", "V", "%", "", "kg")
        for sibling in (
            "PV-42A",
            "PV-42 A",
            "HC-PV-42",
            "HC PV-42",
            "hc-PV-42",
            "hc PV-42",
            "hcpv-42",
            "Hc-PV-42",
            "hc.PV-42",
            "hc:PV-42",
            "hc–PV-42",
        ):
            item = valid_decision()
            wrong_rows = []
            for fact, value, unit in zip(
                item["facts"], wrong_values, wrong_units, strict=True
            ):
                fact["value"] = value
                fact["unit"] = unit
                quote = (
                    f"PV-42 {sibling} {fact['name']} {value} {unit}"
                ).strip()
                fact["evidence_quotes"][0]["quote"] = quote
                wrong_rows.append(quote)
            with self.subTest(sibling=sibling):
                with self.assertRaisesRegex(
                    DecisionError, "exact supporting"
                ):
                    validate_decision(
                        item,
                        expected_product_id="P-42",
                        expected_lease_token="1234567890abcdef",
                        evidence_text_by_url={
                            "https://acme.example/PV-42.pdf":
                                "\n".join(wrong_rows)
                        },
                    )

        for revision in ("rev B", "Rev.B", "Revision B"):
            item = valid_decision()
            body = "\n".join(
                (
                    f"PV-42 {revision} {fact['name']} "
                    f"{fact['value']} {fact.get('unit', '')}"
                ).strip()
                for fact in item["facts"]
            )
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(
                    DecisionError, "exact supporting"
                ):
                    validate_decision(
                        item,
                        expected_product_id="P-42",
                        expected_lease_token="1234567890abcdef",
                        evidence_text_by_url={
                            "https://acme.example/PV-42.pdf": body
                        },
                    )

    def test_nonpublish_can_retain_official_candidate_without_authorizing_it(self) -> None:
        item = valid_decision()
        item["outcome"] = "ambiguous"
        item["confidence"] = 0.6

        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            trusted_source_domains=set(),
            expected_product_name="PV-42",
            evidence_text_by_url=None,
        )

        self.assertEqual("ambiguous", result["outcome"])

    def test_nonfinite_fact_and_conflict_values_are_rejected(self) -> None:
        item = valid_decision()
        item["facts"][0]["value"] = float("nan")
        with self.assertRaisesRegex(DecisionError, "finite"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

        item = valid_decision()
        item["conflicts"] = [
            {
                "field": "New conflict",
                "values": [42, float("inf")],
                "source_urls": ["https://acme.example/PV-42.pdf"],
            }
        ]
        with self.assertRaisesRegex(DecisionError, "finite"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

if __name__ == "__main__":
    unittest.main()
