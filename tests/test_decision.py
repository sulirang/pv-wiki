from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wikijs-sync-products" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki.decision import (  # noqa: E402
    DecisionError,
    PRODUCT_CATEGORY_LABELS,
    SourceVerificationError,
    _TOP_LEVEL,
    _cell_has_unambiguous_fact_value,
    _fact_value_present,
    catalogue_model_candidates,
    canonical_product_category_code,
    model_matches_catalogue_identity,
    preferred_catalogue_model,
    text_contains_catalogue_identity,
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
        "manufacturer_zh": "Acme（制造商）",
        "model": "PV-42",
        "display_title_zh": "PV-42 光伏逆变器",
        "product_category": "光伏逆变器",
        "summary": "PV-42 是一款资料完整的光伏逆变器。",
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
    def test_catalogue_candidates_combine_stock_code_and_public_model(self) -> None:
        self.assertEqual(
            (
                "JA460W",
                "JAM72S20",
                "JAM72S20 460W HALFCELL MODULE 166MM 2112X1052X35MM",
            ),
            catalogue_model_candidates(
                "JA460W",
                "JAM72S20 460W HALFCELL MODULE 166MM 2112X1052X35MM",
                allow_product_id=True,
            ),
        )
        self.assertEqual(
            (
                "AHB16VR3HP",
                "Pompa di Calore 16 kW Monoblocco Inverter R32-M",
            ),
            catalogue_model_candidates(
                "AHB16VR3HP",
                "Pompa di Calore 16 kW Monoblocco Inverter R32-M",
                allow_product_id=True,
            ),
        )
        self.assertEqual(
            "R125-G2",
            preferred_catalogue_model(
                "R125-G2",
                "125000W Three Phase 380V/60HZ,10 MPPT inverter",
                allow_product_id=True,
            ),
        )

    def test_catalogue_candidates_keep_named_series_without_digits(self) -> None:
        self.assertEqual(
            (
                "MIRA BMS",
                "High voltage lithium battery BMS for Mira 2500",
            ),
            catalogue_model_candidates(
                "MIRA BMS",
                "High voltage lithium battery BMS for Mira 2500",
                allow_product_id=True,
            ),
        )
        self.assertTrue(
            text_contains_catalogue_identity(
                "MIRA BMS",
                "High voltage lithium battery BMS for Mira 2500",
                "FoxESS MIRA BMS installation and datasheet",
            )
        )
        for generic_identity in (
            "CONTROL UNIT",
            "POWER MODULE",
            "SERVICE MANUAL",
        ):
            with self.subTest(generic_identity=generic_identity):
                self.assertEqual(
                    (),
                    catalogue_model_candidates(
                        generic_identity,
                        "",
                        allow_product_id=True,
                    ),
                )

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

    def test_catalogue_identity_distinguishes_model_codes_from_descriptions(
        self,
    ) -> None:
        description = "8000W Three Phase, Dual MPPT hybrid inverter"
        self.assertEqual(
            "H3-8.0-E",
            preferred_catalogue_model(
                "H3-8.0-E",
                description,
                allow_product_id=True,
            ),
        )
        self.assertTrue(
            model_matches_catalogue_identity(
                "H3-8.0-E",
                product_id="H3-8.0-E",
                product_name=description,
            )
        )
        self.assertFalse(
            model_matches_catalogue_identity(
                "HYD 8KTL-3PH",
                product_id="H3-8.0-E",
                product_name=description,
            )
        )
        self.assertTrue(
            text_contains_catalogue_identity(
                "H3-8.0-E",
                description,
                "FOXESS model H3-8.0-E hybrid inverter",
            )
        )
        self.assertEqual(
            "SUN2000-50KTL-M3",
            preferred_catalogue_model(
                "01073873",
                "SUN2000-50KTL-M3 INVERTER",
            ),
        )

    def test_public_model_hint_rejects_company_and_numeric_catalogue_names(
        self,
    ) -> None:
        for name in (
            "AREA 46 SRL",
            "SEARCH4SOLAR BV",
            "2020",
            "Acme Holdings",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    "",
                    preferred_catalogue_model("INTERNAL-42", name),
                )
        self.assertEqual(
            "PV-42",
            preferred_catalogue_model("INTERNAL-42", "PV-42 solar inverter"),
        )
        self.assertEqual(
            "H3-8.0-E",
            preferred_catalogue_model(
                "H3-8.0-E",
                "8000W Three Phase hybrid inverter",
                allow_product_id=True,
            ),
        )

    def test_accepts_cited_official_publish(self) -> None:
        result = validate_decision(
            valid_decision(),
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
        )
        self.assertEqual("publish", result["outcome"])
        self.assertEqual("inverter", result["product_category_code"])
        self.assertEqual("逆变器", result["product_category"])
        self.assertEqual("光伏逆变器", result["product_type"])
        self.assertEqual(
            "PV-42 光伏逆变器",
            result["product_description_zh"],
        )

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

    def test_multimodel_table_uses_explicit_same_column_binding(self) -> None:
        item = valid_decision()
        url = "https://foxess.example/h1-series.pdf"
        target = "H1-3.7-E"
        model_row = (
            "| Parameter | H1-3.7-E | H1-5.0-E |"
        )
        specifications = (
            ("Rated output power", "Output", 3700, "W", "5000 W"),
            ("Maximum apparent power", "Output", 4070, "VA", "5500 VA"),
            ("Maximum efficiency", "Efficiency", 97.8, "%", "98.0 %"),
            ("Protection degree", "General", "IP65", "", "IP65"),
            ("Weight", "General", 21, "kg", "22 kg"),
        )
        fact_rows = []
        item.update(
            {
                "product_id": target,
                "manufacturer": "Fox ESS",
                "model": target,
                "datasheets": [
                    {
                        "url": url,
                        "title": "H1 series datasheet",
                        "source_type": "manufacturer",
                        "is_primary": True,
                    }
                ],
            }
        )
        for fact, (name, category, value, unit, sibling_value) in zip(
            item["facts"],
            specifications,
            strict=True,
        ):
            target_value = f"{value} {unit}".strip()
            fact_row = f"| {name} | {target_value} | {sibling_value} |"
            fact.update(
                {
                    "name": name,
                    "category": category,
                    "value": value,
                    "unit": unit,
                    "evidence_urls": [url],
                    "evidence_quotes": [
                        {
                            "url": url,
                            "model_quote": model_row,
                            "quote": fact_row,
                        }
                    ],
                }
            )
            fact_rows.append(fact_row)
        body = "\n".join(
            (
                "[Derived PDF layout table 1]",
                model_row,
                *fact_rows,
                "[End derived PDF layout table 1]",
            )
        )

        result = _validate_decision(
            item,
            expected_product_id=target,
            expected_lease_token="1234567890abcdef",
            expected_product_name=target,
            trusted_source_domains={"foxess.example"},
            evidence_text_by_url={url: body},
        )
        self.assertEqual("publish", result["outcome"])
        self.assertEqual(
            model_row,
            result["facts"][0]["evidence_quotes"][0]["model_quote"],
        )

        sibling_value = copy.deepcopy(item)
        sibling_value["facts"][0]["value"] = 5000
        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            _validate_decision(
                sibling_value,
                expected_product_id=target,
                expected_lease_token="1234567890abcdef",
                expected_product_name=target,
                trusted_source_domains={"foxess.example"},
                evidence_text_by_url={url: body},
            )

        ambiguous_target_cell = copy.deepcopy(item)
        ambiguous_row = "| Rated output power | 3700 W / 5000 W | 5000 W |"
        ambiguous_target_cell["facts"][0]["evidence_quotes"][0]["quote"] = (
            ambiguous_row
        )
        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            _validate_decision(
                ambiguous_target_cell,
                expected_product_id=target,
                expected_lease_token="1234567890abcdef",
                expected_product_name=target,
                trusted_source_domains={"foxess.example"},
                evidence_text_by_url={url: f"{body}\n{ambiguous_row}"},
            )

    def test_multimodel_table_rejects_a_row_from_a_reordered_table(
        self,
    ) -> None:
        item = valid_decision()
        url = "https://acme.example/PV-42-series.pdf"
        model_row = "Parameter\tPV-42\tPV-43"
        reordered_model_row = "Parameter\tPV-43\tPV-42"
        reordered_fact_row = "Rated power\t43 W\t42 W"
        body = "\n".join(
            (
                "[Derived PDF layout table 1]",
                model_row,
                "[End derived PDF layout table 1]",
                "[Derived PDF layout table 2]",
                reordered_model_row,
                reordered_fact_row,
                "[End derived PDF layout table 2]",
            )
        )
        item["facts"] = [item["facts"][0]]
        item["facts"][0].update(
            {
                "name": "Rated power",
                "value": 43,
                "unit": "W",
                "evidence_quotes": [
                    {
                        "url": url,
                        "model_quote": model_row,
                        "quote": reordered_fact_row,
                    }
                ],
            }
        )
        item["datasheets"][0]["url"] = url
        item["facts"][0]["evidence_urls"] = [url]

        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
                trusted_source_domains={"acme.example"},
                evidence_text_by_url={url: body},
            )

        abbreviated_reordered_header = "Parameter\t43\t42"
        abbreviated_body = "\n".join(
            (
                model_row,
                abbreviated_reordered_header,
                reordered_fact_row,
            )
        )
        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
                trusted_source_domains={"acme.example"},
                evidence_text_by_url={url: abbreviated_body},
            )

        renamed_abbreviated_header = "Product\t43\t42"
        renamed_abbreviated_body = "\n".join(
            (
                model_row,
                renamed_abbreviated_header,
                reordered_fact_row,
            )
        )
        with self.assertRaisesRegex(DecisionError, "exact supporting"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
                trusted_source_domains={"acme.example"},
                evidence_text_by_url={url: renamed_abbreviated_body},
            )

        same_table = copy.deepcopy(item)
        same_table["facts"][0]["value"] = 42
        same_table["facts"][0]["evidence_quotes"][0][
            "model_quote"
        ] = reordered_model_row
        result = _validate_decision(
            same_table,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            expected_product_name="PV-42",
            trusted_source_domains={"acme.example"},
            evidence_text_by_url={url: body},
        )
        self.assertEqual(42, result["facts"][0]["value"])

    def test_fixed_width_series_pdf_can_publish_four_target_column_facts(
        self,
    ) -> None:
        item = valid_decision()
        url = "https://solar.huawei.com/sun2000l-series.pdf"
        target = "SUN2000L-4.6KTL"
        model_row = (
            "Technical Specification  SUN2000L-2KTL  SUN2000L-3KTL  "
            "SUN2000L-4KTL  SUN2000L-4.6KTL  SUN2000L-5KTL"
        )
        specifications = (
            (
                "Max. efficiency",
                "Efficiency",
                98.6,
                "%",
                "98.4 %  98.5 %  98.6 %  98.6 %  98.6 %",
            ),
            (
                "Recommended max. PV power",
                "Input",
                6210,
                "Wp",
                "2660 Wp  3990 Wp  5400 Wp  6210 Wp  6750 Wp",
            ),
            (
                "Rated output power",
                "Output",
                4600,
                "W",
                "2,000 W  3,000 W  4,000 W  4,600 W  5,000 W",
            ),
            (
                "Maximum output current",
                "Output",
                23,
                "A",
                "10 A  15 A  20 A  23 A  25 A",
            ),
        )
        item.update(
            {
                "product_id": target,
                "manufacturer": "Huawei",
                "model": target,
                "datasheets": [
                    {
                        "url": url,
                        "title": "SUN2000L-2/3/4/4.6/5KTL datasheet",
                        "source_type": "manufacturer",
                        "is_primary": True,
                    }
                ],
                "facts": item["facts"][:4],
            }
        )
        fact_rows = []
        for fact, (name, category, value, unit, values) in zip(
            item["facts"],
            specifications,
            strict=True,
        ):
            fact_row = f"{name}  {values}"
            fact.update(
                {
                    "name": name,
                    "category": category,
                    "value": value,
                    "unit": unit,
                    "evidence_urls": [url],
                    "evidence_quotes": [
                        {
                            "url": url,
                            "model_quote": model_row,
                            "quote": fact_row,
                        }
                    ],
                }
            )
            fact_rows.append(fact_row)
        body = "\n".join(
            (
                "[Derived PDF layout table 1]",
                model_row,
                *fact_rows,
                "[End derived PDF layout table 1]",
            )
        )

        result = _validate_decision(
            item,
            expected_product_id=target,
            expected_lease_token="1234567890abcdef",
            expected_product_name=target,
            trusted_source_domains={"solar.huawei.com"},
            evidence_text_by_url={url: body},
        )

        self.assertEqual("publish", result["outcome"])
        self.assertEqual(4, len(result["facts"]))
        self.assertEqual(
            6210,
            result["facts"][1]["value"],
        )

    def test_numeric_grouping_has_one_deterministic_interpretation(self) -> None:
        for grouped in ("4,600 W", "4 600 W", "4\u00a0600 W"):
            with self.subTest(grouped=grouped):
                self.assertTrue(
                    _cell_has_unambiguous_fact_value(
                        grouped,
                        value=4600,
                        unit="W",
                    )
                )
                self.assertFalse(
                    _cell_has_unambiguous_fact_value(
                        grouped,
                        value=4.6,
                        unit="W",
                    )
                )
                self.assertTrue(
                    _fact_value_present(4600, "W", grouped)
                )
                self.assertFalse(
                    _fact_value_present(4.6, "W", grouped)
                )
        self.assertTrue(
            _cell_has_unambiguous_fact_value(
                "4.600 W",
                value=4.6,
                unit="W",
            )
        )
        self.assertFalse(
            _cell_has_unambiguous_fact_value(
                "4.600 W",
                value=4600,
                unit="W",
            )
        )
        self.assertTrue(_fact_value_present(4.6, "W", "4.600 W"))
        self.assertFalse(_fact_value_present(4600, "W", "4.600 W"))

    def test_trusted_primary_datasheet_can_publish_without_facts(self) -> None:
        item = valid_decision()
        item["facts"] = []

        result = validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
        )

        self.assertEqual("publish", result["outcome"])
        self.assertEqual([], result["facts"])

    def test_automatic_publish_requires_a_verified_primary_document(
        self,
    ) -> None:
        item = valid_decision()
        item["facts"] = []
        primary_url = item["datasheets"][0]["url"]

        result = validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            verified_primary_document_urls={primary_url},
        )
        self.assertEqual("publish", result["outcome"])

        for verified_urls in (
            set(),
            {"https://acme.example/other-document.pdf"},
        ):
            with self.subTest(verified_urls=verified_urls):
                with self.assertRaisesRegex(
                    SourceVerificationError,
                    "directly verified.*primary datasheet",
                ):
                    validate_decision(
                        item,
                        expected_product_id="P-42",
                        expected_lease_token="1234567890abcdef",
                        verified_primary_document_urls=verified_urls,
                    )

    def test_automatic_facts_use_the_directly_verified_primary(
        self,
    ) -> None:
        item = valid_decision()
        item["facts"] = [item["facts"][0]]
        verified_url = item["datasheets"][0]["url"]
        other_primary_url = "https://acme.example/PV-42-secondary.pdf"
        item["datasheets"].append(
            {
                "url": other_primary_url,
                "title": "PV-42 secondary datasheet",
                "source_type": "manufacturer",
                "is_primary": True,
            }
        )
        fact_quote = item["facts"][0]["evidence_quotes"][0]["quote"]
        item["facts"][0]["evidence_urls"] = [other_primary_url]
        item["facts"][0]["evidence_quotes"] = [
            {"url": other_primary_url, "quote": fact_quote}
        ]
        evidence = {
            verified_url: "PV-42 directly verified primary datasheet",
            other_primary_url: fact_quote,
        }

        with self.assertRaisesRegex(
            SourceVerificationError,
            "configured primary manufacturer datasheet",
        ):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
                trusted_source_domains={"acme.example"},
                verified_primary_document_urls={verified_url},
                evidence_text_by_url=evidence,
            )

        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            expected_product_name="PV-42",
            trusted_source_domains={"acme.example"},
            verified_primary_document_urls={other_primary_url},
            evidence_text_by_url=evidence,
        )
        self.assertEqual(42, result["facts"][0]["value"])

    def test_only_manufacturer_sources_can_authorize_primary_publication(
        self,
    ) -> None:
        for source_type in ("regulatory", "authorized", "mirror"):
            item = valid_decision()
            item["facts"] = []
            item["datasheets"][0]["source_type"] = source_type
            with self.subTest(source_type=source_type):
                with self.assertRaisesRegex(
                    SourceVerificationError,
                    "original manufacturer source",
                ):
                    validate_decision(
                        item,
                        expected_product_id="P-42",
                        expected_lease_token="1234567890abcdef",
                        mirrors_allowed=True,
                    )

    def test_target_model_must_be_in_the_trusted_primary_datasheet(
        self,
    ) -> None:
        item = valid_decision()
        item["facts"] = []
        supporting_url = "https://catalog.example/PV-42"
        item["sources"] = [
            {
                "url": supporting_url,
                "title": "PV-42 product listing",
                "source_type": "authorized",
            }
        ]

        with self.assertRaisesRegex(
            SourceVerificationError,
            "trusted primary datasheet",
        ):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
                trusted_source_domains={"acme.example"},
                evidence_text_by_url={
                    "https://acme.example/PV-42.pdf": (
                        "Acme unrelated product datasheet"
                    ),
                    supporting_url: "Acme PV-42 product listing",
                },
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
        result = validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
        )
        self.assertEqual(4, len(result["facts"]))

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

    def test_publish_requires_safe_chinese_display_copy(self) -> None:
        invalid_values = (
            ("display_title_zh", "", "product_description_zh"),
            ("manufacturer_zh", "Acme", "Simplified Chinese"),
            ("product_category", "unknown category", "documented broad"),
            ("summary", "A documented product.", "Simplified Chinese"),
        )
        for field, value, message in invalid_values:
            with self.subTest(field=field, value=value):
                item = valid_decision()
                item[field] = value
                with self.assertRaisesRegex(DecisionError, message):
                    validate_decision(
                        item,
                        expected_product_id="P-42",
                        expected_lease_token="1234567890abcdef",
                    )

    def test_closed_category_code_matches_label_and_keeps_specific_type(self) -> None:
        item = valid_decision()
        item.update(
            {
                "product_description_zh": "10kW 三相太阳能逆变器，双 MPPT",
                "product_category_code": "inverter",
                "product_category": "逆变器",
                "product_type": "三相太阳能并网逆变器",
                "datasheet_parameters": [],
                "derived_insights": [],
            }
        )

        result = validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
        )
        self.assertEqual("inverter", result["product_category_code"])
        self.assertEqual("逆变器", result["product_category"])
        self.assertEqual("三相太阳能并网逆变器", result["product_type"])

        item["product_category_code"] = "pv_module"
        with self.assertRaisesRegex(DecisionError, "does not match"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_historical_detailed_categories_map_to_closed_broad_classes(self) -> None:
        expected = {
            "Single-phase hybrid inverter": "inverter",
            "三相并网逆变器": "inverter",
            "单相太阳能逆变器": "inverter",
            "单相光伏逆变器": "inverter",
            "太阳能光伏组件": "pv_module",
        }

        self.assertEqual(
            expected,
            {
                value: canonical_product_category_code(value)
                for value in expected
            },
        )

    def test_datasheet_parameters_and_insights_are_grounded_and_ordered(self) -> None:
        item = valid_decision()
        item["facts"] = []
        item["datasheet_parameters"] = [
            {
                **{key: value for key, value in fact.items() if key != "category"},
                "section": section,
            }
            for fact, section in zip(
                valid_decision()["facts"][:2],
                ("Output", "Input"),
                strict=True,
            )
        ]
        item["derived_insights"] = [
            {
                "name": "电压功率比",
                "value": 48 / 42,
                "formula": "48 ÷ 42",
                "basis": ["Input voltage", "Power"],
                "explanation": "用于参数间的直接对照。",
            }
        ]

        result = validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
        )

        self.assertEqual(
            ["Power", "Input voltage"],
            [parameter["name"] for parameter in result["datasheet_parameters"]],
        )
        self.assertEqual(
            ["Input voltage", "Power"],
            result["derived_insights"][0]["basis"],
        )
        self.assertAlmostEqual(
            48 / 42,
            result["derived_insights"][0]["value"],
        )

        item["datasheet_parameters"] = [{} for _ in range(31)]
        with self.assertRaisesRegex(DecisionError, "at most 30"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

        item["datasheet_parameters"] = ["not-an-object"]
        with self.assertRaisesRegex(
            DecisionError,
            r"datasheet_parameters\[0\] must be an object",
        ):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_derived_insights_require_recomputable_binary_arithmetic(self) -> None:
        base = valid_decision()
        base["facts"] = base["facts"][:2]
        base["datasheet_parameters"] = []
        base["derived_insights"] = [
            {
                "name": "电压功率比",
                "value": 48 / 42,
                "formula": "48 / 42",
                "basis": ["Input voltage", "Power"],
            }
        ]

        invalid = (
            (
                {"formula": "48 V / 42 W"},
                "exactly two numeric literals",
            ),
            (
                {"formula": "48 / 42 + 1"},
                "exactly two numeric literals",
            ),
            (
                {"basis": ["Input voltage"]},
                "exactly 2 parameter names",
            ),
            (
                {"basis": ["Input voltage", "Input voltage"]},
                "2 distinct verified parameters",
            ),
            (
                {"value": "1.14"},
                "value must be a finite number",
            ),
            (
                {"formula": "42 / 48", "value": 42 / 48},
                "respectively equal",
            ),
            (
                {"value": 99},
                "runtime-recomputed",
            ),
        )
        for replacement, message in invalid:
            with self.subTest(replacement=replacement):
                item = copy.deepcopy(base)
                item["derived_insights"][0].update(replacement)
                with self.assertRaisesRegex(DecisionError, message):
                    validate_decision(
                        item,
                        expected_product_id="P-42",
                        expected_lease_token="1234567890abcdef",
                    )

        too_many = copy.deepcopy(base)
        too_many["derived_insights"] = [
            {
                **base["derived_insights"][0],
                "name": f"参数对照{index}",
            }
            for index in range(6)
        ]
        with self.assertRaisesRegex(DecisionError, "at most 5"):
            validate_decision(
                too_many,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_documented_schema_matches_runtime_fields_and_taxonomy(self) -> None:
        schema = json.loads(
            (
                SCRIPTS.parent
                / "references"
                / "decision.schema.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(set(_TOP_LEVEL), set(schema["properties"]))
        self.assertEqual(
            set(PRODUCT_CATEGORY_LABELS),
            set(schema["properties"]["product_category_code"]["enum"]),
        )
        self.assertEqual(
            set(PRODUCT_CATEGORY_LABELS.values()),
            set(schema["properties"]["product_category"]["enum"]),
        )
        self.assertTrue(
            {
                "datasheet_parameters",
                "derived_insights",
            }
            <= set(schema["required"])
        )
        self.assertEqual(
            30,
            schema["properties"]["datasheet_parameters"]["maxItems"],
        )
        insight_schema = schema["properties"]["derived_insights"]
        self.assertEqual(5, insight_schema["maxItems"])
        self.assertEqual(
            "number",
            insight_schema["items"]["properties"]["value"]["type"],
        )
        self.assertEqual(
            (2, 2),
            (
                insight_schema["items"]["properties"]["basis"]["minItems"],
                insight_schema["items"]["properties"]["basis"]["maxItems"],
            ),
        )
        self.assertIn(
            "×÷",
            insight_schema["items"]["properties"]["formula"]["pattern"],
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
        with self.assertRaises(SourceVerificationError):
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
        with self.assertRaisesRegex(DecisionError, "catalogue identity"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
            )

    def test_publish_uses_catalogue_bound_model_and_ignores_uncited_extracts(
        self,
    ) -> None:
        model = "H3-8.0-E"
        official_url = "https://foxess.example/H3-8.0-E.pdf"
        unrelated_url = "https://market.example/unrelated"
        item = valid_decision()
        item["product_id"] = model
        item["model"] = model
        item["manufacturer"] = "FOXESS"
        item["datasheets"][0]["url"] = official_url
        item["datasheets"][0]["title"] = f"{model} datasheet"
        rows = []
        for fact in item["facts"]:
            fact["evidence_urls"] = [official_url]
            quote = (
                f"{model} {fact['name']} {fact['value']} "
                f"{fact.get('unit', '')}"
            ).strip()
            fact["evidence_quotes"] = [
                {"url": official_url, "quote": quote}
            ]
            rows.append(quote)

        result = _validate_decision(
            item,
            expected_product_id=model,
            expected_lease_token="1234567890abcdef",
            expected_product_name=(
                "8000W Three Phase, Dual MPPT hybrid inverter"
            ),
            trusted_source_domains={"foxess.example"},
            evidence_text_by_url={
                official_url: "\n".join(rows),
                unrelated_url: "A different product with no matching model.",
            },
        )

        self.assertEqual("publish", result["outcome"])

    def test_matching_manufacturer_domain_is_verified_without_an_override(self) -> None:
        item = valid_decision()
        corroborating_url = "https://certifier.example/products/PV-42"
        item["sources"] = [
            {
                "url": corroborating_url,
                "title": "Independent PV-42 listing",
                "source_type": "authorized",
            }
        ]
        body = (
            "Acme official product documentation\n"
            "PV-42 Power 42 W\nPV-42 Input voltage 48 V\n"
            "PV-42 Efficiency 98.5%\n"
            "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
        )

        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            trusted_source_domains=set(),
            expected_product_name="PV-42",
            evidence_text_by_url={
                "https://acme.example/PV-42.pdf": body,
                corroborating_url: "Independent listing: Acme model PV-42.",
            },
        )

        self.assertEqual("publish", result["outcome"])

        single_source_item = valid_decision()
        with self.assertRaises(SourceVerificationError):
            _validate_decision(
                single_source_item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                trusted_source_domains=set(),
                expected_product_name="PV-42",
                evidence_text_by_url={
                    "https://acme.example/PV-42.pdf": body,
                },
            )

    def test_registered_brand_cannot_fall_back_to_an_unregistered_domain(
        self,
    ) -> None:
        item = valid_decision()
        candidate_url = "https://acme.example/PV-42.pdf"
        corroborating_url = "https://certifier.example/products/PV-42"
        item["sources"] = [
            {
                "url": corroborating_url,
                "title": "Independent PV-42 listing",
                "source_type": "authorized",
            }
        ]
        body = (
            "Acme official product documentation\n"
            "PV-42 Power 42 W\nPV-42 Input voltage 48 V\n"
            "PV-42 Efficiency 98.5%\n"
            "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
        )

        with self.assertRaisesRegex(
            SourceVerificationError,
            "original manufacturer source",
        ):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                operator_manufacturer_identity="Acme",
                trusted_source_domains={"registered-acme.example"},
                expected_product_name="PV-42",
                evidence_text_by_url={
                    candidate_url: body,
                    corroborating_url: "Independent listing: Acme model PV-42.",
                },
            )

    def test_operator_manufacturer_identity_is_a_hard_publish_boundary(self) -> None:
        item = valid_decision()
        with self.assertRaisesRegex(
            SourceVerificationError,
            "operator-approved",
        ):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
                operator_manufacturer_identity="Contoso",
                trusted_source_domains={"acme.example"},
                evidence_text_by_url={
                    "https://acme.example/PV-42.pdf": (
                        "PV-42 Power 42 W\nPV-42 Input voltage 48 V\n"
                        "PV-42 Efficiency 98.5%\n"
                        "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
                    )
                },
            )

        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            expected_product_name="PV-42",
            operator_manufacturer_identity="ACME",
            trusted_source_domains={"acme.example"},
            evidence_text_by_url={
                "https://acme.example/PV-42.pdf": (
                    "PV-42 Power 42 W\nPV-42 Input voltage 48 V\n"
                    "PV-42 Efficiency 98.5%\n"
                    "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
                )
            },
        )
        self.assertEqual("publish", result["outcome"])

    def test_manufacturer_named_subdomain_cannot_impersonate_official_domain(self) -> None:
        for spoof_url in (
            "https://acme.marketplace.example/PV-42.pdf",
            "https://notacme.example/PV-42.pdf",
        ):
            item = valid_decision()
            item["datasheets"][0]["url"] = spoof_url
            for fact in item["facts"]:
                fact["evidence_urls"] = [spoof_url]
                fact["evidence_quotes"][0]["url"] = spoof_url

            with self.subTest(spoof_url=spoof_url), self.assertRaises(
                SourceVerificationError
            ):
                _validate_decision(
                    item,
                    expected_product_id="P-42",
                    expected_lease_token="1234567890abcdef",
                    trusted_source_domains=set(),
                    expected_product_name="PV-42",
                    evidence_text_by_url={
                        spoof_url: (
                            "Acme PV-42 Power 42 W\nPV-42 Input voltage 48 V\n"
                            "PV-42 Efficiency 98.5%\n"
                            "PV-42 Ingress protection IP65\nPV-42 Weight 12 kg"
                        )
                    },
                )

    def test_generic_manufacturer_words_cannot_authorize_a_domain(self) -> None:
        for manufacturer, candidate_url in (
            ("Energy", "https://energy.example/PV-42.pdf"),
            ("Solar Acme", "https://solar.example/PV-42.pdf"),
            ("The Solar Company", "https://the.example/PV-42.pdf"),
            ("Global Energy Systems", "https://global.example/PV-42.pdf"),
            ("New Power Technology", "https://new.example/PV-42.pdf"),
        ):
            item = valid_decision()
            item["manufacturer"] = manufacturer
            item["datasheets"][0]["url"] = candidate_url
            corroborating_url = "https://certifier.example/products/PV-42"
            item["sources"] = [
                {
                    "url": corroborating_url,
                    "title": "Independent PV-42 listing",
                    "source_type": "authorized",
                }
            ]
            rows = []
            for fact in item["facts"]:
                quote = fact["evidence_quotes"][0]["quote"]
                fact["evidence_urls"] = [candidate_url, corroborating_url]
                fact["evidence_quotes"] = [
                    {"url": candidate_url, "quote": quote},
                    {"url": corroborating_url, "quote": quote},
                ]
                rows.append(quote)
            evidence = "\n".join(
                [f"{manufacturer} official model PV-42", *rows]
            )

            with self.subTest(manufacturer=manufacturer), self.assertRaises(
                SourceVerificationError
            ):
                _validate_decision(
                    item,
                    expected_product_id="P-42",
                    expected_lease_token="1234567890abcdef",
                    trusted_source_domains=set(),
                    expected_product_name="PV-42",
                    evidence_text_by_url={
                        candidate_url: evidence,
                        corroborating_url: evidence,
                    },
                )

    def test_auto_source_rejects_shared_ip_and_identity_mismatch(self) -> None:
        cases = (
            (
                "GitHub",
                "https://attacker.github.io/PV-42.pdf",
                "https://certifier.example/products/PV-42",
                "GitHub",
            ),
            (
                "Acme",
                "https://acme.example/PV-42.pdf",
                "https://1.1.1.1/PV-42",
                "Acme",
            ),
            (
                "Acme",
                "https://acme.example/PV-42.pdf",
                "https://bucket.storage.googleapis.com/PV-42.html",
                "Acme",
            ),
            (
                "Acme",
                "https://acme.example/PV-42.pdf",
                "https://tenant.blob.core.windows.net/PV-42.html",
                "Acme",
            ),
            (
                "Acme",
                "https://acme.example/PV-42.pdf",
                "https://sites.google.com/view/acme-pv42",
                "Acme",
            ),
            (
                "Acme",
                "https://acme.example/PV-42.pdf",
                "https://docs.google.com/document/d/example",
                "Acme",
            ),
            (
                "Acme",
                "https://acme.example/PV-42.pdf",
                "https://acme.medium.com/pv-42",
                "Acme",
            ),
            (
                "Acme",
                "https://ac-me.example/PV-42.pdf",
                "https://certifier.example/products/PV-42",
                "Acme",
            ),
            (
                "Acme Evil",
                "https://evil.example/PV-42.pdf",
                "https://certifier.example/products/PV-42",
                "Acme",
            ),
        )
        for manufacturer, candidate_url, corroborating_url, body_name in cases:
            item = valid_decision()
            item["manufacturer"] = manufacturer
            item["datasheets"][0]["url"] = candidate_url
            item["sources"] = [
                {
                    "url": corroborating_url,
                    "title": "Independent PV-42 listing",
                    "source_type": "authorized",
                }
            ]
            rows = []
            for fact in item["facts"]:
                quote = fact["evidence_quotes"][0]["quote"]
                fact["evidence_urls"] = [candidate_url, corroborating_url]
                fact["evidence_quotes"] = [
                    {"url": candidate_url, "quote": quote},
                    {"url": corroborating_url, "quote": quote},
                ]
                rows.append(quote)
            evidence = "\n".join(
                [f"{body_name} official model PV-42", *rows]
            )

            with self.subTest(candidate_url=candidate_url), self.assertRaises(
                SourceVerificationError
            ):
                _validate_decision(
                    item,
                    expected_product_id="P-42",
                    expected_lease_token="1234567890abcdef",
                    trusted_source_domains=set(),
                    expected_product_name="PV-42",
                    evidence_text_by_url={
                        candidate_url: evidence,
                        corroborating_url: evidence,
                    },
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
        with self.assertRaisesRegex(DecisionError, "primary manufacturer"):
            validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                evidence_text_by_url={
                    "https://acme.example/PV-42.pdf": (
                        "PV-42 original manufacturer datasheet"
                    ),
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
        with self.assertRaisesRegex(
            SourceVerificationError,
            "original manufacturer source",
        ):
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
                    ),
                    mirror_b: "PV-42 duplicate primary datasheet",
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

    def test_fact_values_use_numeric_and_token_boundaries(self) -> None:
        cases = (
            (0, 42, "W", "PV-42 Power 420 W"),
            (0, 42, "W", "PV-42 Power 42 W / 420 W"),
            (0, 4.2, "kW", "PV-42 Power 14.2 kW"),
            (3, "IP65", "", "PV-42 Ingress protection IP650"),
            (3, "IP65", "", "PV-42 Ingress protection IP65/IP66"),
        )
        for fact_index, value, unit, bad_quote in cases:
            item = valid_decision()
            fact = item["facts"][fact_index]
            fact["value"] = value
            fact["unit"] = unit
            fact["evidence_quotes"][0]["quote"] = bad_quote
            body = "\n".join(
                quote["quote"]
                for candidate in item["facts"]
                for quote in candidate["evidence_quotes"]
            )
            with self.subTest(bad_quote=bad_quote), self.assertRaisesRegex(
                DecisionError,
                "exact supporting",
            ):
                validate_decision(
                    item,
                    expected_product_id="P-42",
                    expected_lease_token="1234567890abcdef",
                    evidence_text_by_url={
                        "https://acme.example/PV-42.pdf": body
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
        item["datasheets"][0]["is_primary"] = False
        item["facts"] = []

        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            trusted_source_domains=set(),
            expected_product_name="PV-42",
            evidence_text_by_url=None,
        )

        self.assertEqual("ambiguous", result["outcome"])

    def test_nonpublish_outcomes_reject_contradictory_publish_material(self) -> None:
        item = valid_decision()
        item["outcome"] = "no_datasheet"
        item["facts"] = []
        with self.assertRaisesRegex(DecisionError, "primary datasheet"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
            )

        item["datasheets"][0]["is_primary"] = False
        with self.assertRaisesRegex(DecisionError, "datasheet candidates"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
            )

        item["datasheets"] = []
        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            expected_product_name="PV-42",
        )
        self.assertEqual("no_datasheet", result["outcome"])

        item["outcome"] = "ambiguous"
        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            expected_product_name="PV-42",
        )
        self.assertEqual("ambiguous", result["outcome"])

        item["outcome"] = "insufficient_identity"
        item["conflicts"] = [
            {
                "field": "Model identity",
                "values": ["PV-42", "PV-43"],
                "source_urls": ["https://acme.example/PV-42.pdf"],
            }
        ]
        item["sources"] = [
            {
                "url": "https://acme.example/PV-42.pdf",
                "title": "Candidate",
                "source_type": "manufacturer",
            }
        ]
        with self.assertRaisesRegex(DecisionError, "use ambiguous"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
            )

    def test_out_of_scope_requires_high_confidence_matching_extract_evidence(self) -> None:
        url = "https://catalog.example/flange-nut-m8"
        item = valid_decision()
        item.update(
            {
                "outcome": "out_of_scope",
                "confidence": 0.92,
                "manufacturer": "",
                "model": "flange nut m8",
                "product_category": "紧固件",
                "summary": "A generic M8 flange nut rather than energy equipment.",
                "classification_evidence_urls": [url],
                "classification_evidence_quotes": [
                    {
                        "url": url,
                        "quote": (
                            "Product: flange nut M8. Type: metric fastener."
                        ),
                    }
                ],
                "decision_notes": "The matching page identifies a commodity fastener.",
                "datasheets": [],
                "sources": [],
                "facts": [],
                "conflicts": [],
            }
        )

        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            expected_product_name="flange nut m8",
            allowed_evidence_urls=set(),
            allowed_classification_urls={url},
            classification_text_by_url={
                url: "Product: flange nut M8. Type: metric fastener."
            },
        )

        self.assertEqual("out_of_scope", result["outcome"])
        self.assertEqual([url], result["classification_evidence_urls"])
        self.assertEqual(
            "Product: flange nut M8. Type: metric fastener.",
            result["classification_evidence_quotes"][0]["quote"],
        )

        item["confidence"] = 0.5
        with self.assertRaisesRegex(DecisionError, "confidence"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="flange nut m8",
                allowed_classification_urls={url},
                classification_text_by_url={url: "flange nut M8 fastener"},
            )

        item["confidence"] = 0.92
        with self.assertRaisesRegex(DecisionError, "complete catalogue identity"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="flange nut m8",
                allowed_classification_urls={url},
                classification_text_by_url={url: "A different M10 fastener"},
            )

        item["classification_evidence_quotes"][0]["quote"] = (
            "flange nut M8 high-efficiency solar module"
        )
        with self.assertRaisesRegex(DecisionError, "hardware-type quote"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="flange nut m8",
                allowed_classification_urls={url},
                classification_text_by_url={
                    url: "flange nut M8 high-efficiency solar module"
                },
            )

        item["classification_evidence_quotes"][0]["quote"] = (
            "PV-42 solar module includes stainless mounting bolts."
        )
        with self.assertRaisesRegex(DecisionError, "hardware-type quote"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="PV-42",
                allowed_classification_urls={url},
                classification_text_by_url={
                    url: (
                        "PV-42 solar module includes stainless mounting bolts."
                    )
                },
            )

    def test_publish_cannot_override_an_explicit_generic_hardware_identity(
        self,
    ) -> None:
        item = valid_decision()
        item["model"] = "flange nut m8"
        item["product_category"] = "紧固件"
        item["summary"] = "该产品是一枚有资料记录的 M8 法兰螺母。"
        for fact in item["facts"]:
            fact["evidence_quotes"][0]["quote"] = (
                fact["evidence_quotes"][0]["quote"].replace(
                    "PV-42",
                    "flange nut m8",
                )
            )
        body = "\n".join(
            quote["quote"]
            for fact in item["facts"]
            for quote in fact["evidence_quotes"]
        )

        with self.assertRaisesRegex(DecisionError, "generic hardware"):
            _validate_decision(
                item,
                expected_product_id="P-42",
                expected_lease_token="1234567890abcdef",
                expected_product_name="flange nut m8",
                trusted_source_domains={"acme.example"},
                evidence_text_by_url={
                    "https://acme.example/PV-42.pdf": body
                },
            )

    def test_out_of_scope_ellipsis_quote_is_grounded_to_source_text(self) -> None:
        url = "https://catalog.example/flange-nut-m8"
        body = (
            "Product: flange nut M8. Galvanized steel construction. "
            "Type: metric fastener."
        )
        item = valid_decision()
        item.update(
            {
                "outcome": "out_of_scope",
                "confidence": 0.95,
                "manufacturer": "",
                "model": "flange nut m8",
                "product_category": "Fastener",
                "summary": "A generic M8 flange nut.",
                "classification_evidence_urls": [url],
                "classification_evidence_quotes": [
                    {
                        "url": url,
                        "quote": (
                            "Product: flange nut M8 ... "
                            "Type: metric fastener."
                        ),
                    }
                ],
                "datasheets": [],
                "sources": [],
                "facts": [],
                "conflicts": [],
            }
        )

        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            expected_product_name="flange nut m8",
            allowed_classification_urls={url},
            classification_text_by_url={url: body},
        )

        self.assertEqual(
            body,
            result["classification_evidence_quotes"][0]["quote"],
        )

    def test_explicit_hardware_identity_can_mention_solar_mounting_use(
        self,
    ) -> None:
        url = "https://catalog.example/flange-nut-m8"
        item = valid_decision()
        item.update(
            {
                "outcome": "out_of_scope",
                "confidence": 0.92,
                "manufacturer": "",
                "model": "flange nut m8",
                "product_category": "Fastener",
                "summary": "A generic M8 flange nut.",
                "classification_evidence_urls": [url],
                "classification_evidence_quotes": [
                    {
                        "url": url,
                        "quote": (
                            "flange nut m8 is a nut used for solar panel "
                            "mounting"
                        ),
                    }
                ],
                "decision_notes": "The matching item is commodity hardware.",
                "datasheets": [],
                "sources": [],
                "facts": [],
                "conflicts": [],
            }
        )

        result = _validate_decision(
            item,
            expected_product_id="P-42",
            expected_lease_token="1234567890abcdef",
            expected_product_name="flange nut m8",
            allowed_classification_urls={url},
            classification_text_by_url={
                url: (
                    "flange nut m8 is a nut used for solar panel mounting"
                )
            },
        )
        self.assertEqual("out_of_scope", result["outcome"])

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
