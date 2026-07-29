from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "wikijs-sync-products" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki.parameter_analysis import (  # noqa: E402
    ParameterAnalysisError,
    apply_parameter_translations,
    parameter_analysis_input,
    parameter_set_sha256,
    professional_analysis,
    validate_parameter_enrichment,
)


def parameters() -> list[dict[str, object]]:
    return [
        {
            "name": "Max. PV Array Power [Wp]@STC",
            "section": "Input (DC)",
            "value": "15000",
            "unit": "Wp",
            "confidence": 1.0,
        },
        {
            "name": "MPPT Voltage Range [V]",
            "section": "Input (DC)",
            "value": "160-950",
            "unit": "V",
            "confidence": 1.0,
        },
        {
            "name": "Rated AC Power [W]",
            "section": "Output (AC)",
            "value": "10000",
            "unit": "W",
            "confidence": 1.0,
        },
    ]


def enrichment() -> dict[str, object]:
    return {
        "translations": [
            {
                "parameter_id": "p001",
                "name_zh": "最大光伏阵列功率 [Wp]（STC）",
                "section_zh": "直流输入（DC）",
                "subsection_zh": "",
                "value_zh": "",
            },
            {
                "parameter_id": "p002",
                "name_zh": "MPPT 电压范围 [V]",
                "section_zh": "直流输入（DC）",
                "subsection_zh": "",
                "value_zh": "",
            },
            {
                "parameter_id": "p003",
                "name_zh": "额定交流功率 [W]",
                "section_zh": "交流输出（AC）",
                "subsection_zh": "",
                "value_zh": "",
            },
        ],
        "sections": [
            {
                "section_code": "dc_input",
                "paragraphs": [
                    {
                        "analysis_kind": "engineering_interpretation",
                        "basis_parameter_ids": ["p001", "p002"],
                        "analysis_zh": (
                            "直流侧最大光伏阵列功率为 15000 Wp，MPPT "
                            "电压范围为 160–950 V；组串设计应同时核对"
                            "组件在不同温度下的工作电压。"
                        ),
                        "conditions_zh": [
                            "上述判断仅以制造商数据表给出的直流输入参数为依据。"
                        ],
                        "limitations_zh": [
                            "数据表参数不能替代具体组件串并联设计。"
                        ],
                    }
                ],
            }
        ],
        "overall_limitations_zh": [
            "本分析仅解释制造商数据表参数，不构成项目设计、合规或采购结论。"
        ],
    }


class ParameterAnalysisTests(unittest.TestCase):
    def test_validates_complete_translations_and_grounded_analysis(self) -> None:
        result = validate_parameter_enrichment(enrichment(), parameters())

        self.assertEqual(3, result["input_parameter_count"])
        self.assertTrue(result["input_complete"])
        self.assertEqual("dc_input", result["sections"][0]["section_code"])
        self.assertEqual(
            "最大光伏阵列功率 [Wp]（STC）",
            result["translations"][0]["name_zh"],
        )

        translated = apply_parameter_translations(parameters(), enrichment())
        self.assertEqual("Max. PV Array Power [Wp]@STC", translated[0]["name"])
        self.assertEqual("15000", translated[0]["value"])
        self.assertEqual(
            "最大光伏阵列功率 [Wp]（STC）",
            translated[0]["name_zh"],
        )
        self.assertEqual(
            3,
            professional_analysis(enrichment(), parameters())[
                "input_parameter_count"
            ],
        )
        self.assertEqual(
            result,
            validate_parameter_enrichment(result, parameters()),
        )

        tampered = copy.deepcopy(result)
        tampered["input_parameter_count"] = 2
        with self.assertRaisesRegex(ParameterAnalysisError, "does not match"):
            validate_parameter_enrichment(tampered, parameters())

    def test_model_code_table_heading_may_remain_source_only(self) -> None:
        code = "R5-8K/9K/10K/12K-T2-15"
        source = parameters()
        for row in source:
            row["subsection"] = row["section"]
            row["section"] = code
        item = enrichment()
        for translation in item["translations"]:
            translation["subsection_zh"] = translation["section_zh"]
            translation["section_zh"] = code

        result = validate_parameter_enrichment(item, source)
        self.assertEqual(
            ["", "", ""],
            [value["section_zh"] for value in result["translations"]],
        )
        blank = copy.deepcopy(item)
        for translation in blank["translations"]:
            translation["section_zh"] = ""
        validate_parameter_enrichment(blank, source)

    def test_requires_exactly_one_translation_per_parameter(self) -> None:
        item = enrichment()
        item["translations"] = item["translations"][:-1]

        with self.assertRaisesRegex(ParameterAnalysisError, "exactly one"):
            validate_parameter_enrichment(item, parameters())

    def test_restores_safe_source_tokens_and_requires_controlled_terms(self) -> None:
        missing_unit = copy.deepcopy(enrichment())
        missing_unit["translations"][0]["name_zh"] = "最大光伏阵列功率（STC）"
        restored = validate_parameter_enrichment(missing_unit, parameters())
        self.assertEqual(
            "最大光伏阵列功率（STC） [Wp]",
            restored["translations"][0]["name_zh"],
        )

        dci_source = parameters()
        dci_source[0]["name"] = "DCI Monitoring"
        dci_item = copy.deepcopy(enrichment())
        dci_item["translations"][0]["name_zh"] = "直流分量监测"
        restored = validate_parameter_enrichment(dci_item, dci_source)
        self.assertEqual(
            "直流分量监测（DCI）",
            restored["translations"][0]["name_zh"],
        )
        self.assertEqual(
            restored,
            validate_parameter_enrichment(restored, dci_source),
        )

        substring_item = copy.deepcopy(enrichment())
        substring_item["translations"][0]["name_zh"] = "GDCI 监测"
        restored = validate_parameter_enrichment(substring_item, dci_source)
        self.assertEqual(
            "GDCI 监测（DCI）",
            restored["translations"][0]["name_zh"],
        )

        compound_source = parameters()
        compound_source[0]["name"] = "Power Density [W/m²]"
        compound_item = copy.deepcopy(enrichment())
        compound_item["translations"][0]["name_zh"] = "功率密度"
        restored = validate_parameter_enrichment(compound_item, compound_source)
        self.assertEqual(
            "功率密度 [W/m²]",
            restored["translations"][0]["name_zh"],
        )

        for source_name, name_zh in (
            ("Input index [2]", "输入索引"),
            ("Input option [Optional]", "输入选项"),
        ):
            with self.subTest(source_name=source_name):
                unsafe_source = parameters()
                unsafe_source[0]["name"] = source_name
                unsafe_item = copy.deepcopy(enrichment())
                unsafe_item["translations"][0]["name_zh"] = name_zh
                with self.assertRaisesRegex(
                    ParameterAnalysisError,
                    "protected token",
                ):
                    validate_parameter_enrichment(unsafe_item, unsafe_source)

        long_item = copy.deepcopy(enrichment())
        long_item["translations"][0]["name_zh"] = "中" * 196
        with self.assertRaisesRegex(ParameterAnalysisError, "exceeds 200"):
            validate_parameter_enrichment(long_item, dci_source)

        missing_controlled = copy.deepcopy(enrichment())
        missing_controlled["translations"][0]["name_zh"] = "光伏阵列功率 [Wp]（STC）"
        with self.assertRaisesRegex(ParameterAnalysisError, "controlled term"):
            validate_parameter_enrichment(missing_controlled, parameters())

        numeric_source = parameters()
        numeric_source[0]["name"] = "Input 2 Power [Wp]"
        numeric_item = copy.deepcopy(enrichment())
        numeric_item["translations"][0]["name_zh"] = "输入功率 [Wp]"
        with self.assertRaisesRegex(ParameterAnalysisError, "protected token 2"):
            validate_parameter_enrichment(numeric_item, numeric_source)

        numeric_item["translations"][0]["name_zh"] = "输入 12 功率 [Wp]"
        with self.assertRaisesRegex(ParameterAnalysisError, "protected token 2"):
            validate_parameter_enrichment(numeric_item, numeric_source)

        numeric_item["translations"][0]["name_zh"] = "输入 2 功率 [Wp]"
        validate_parameter_enrichment(numeric_item, numeric_source)

        multi_source = parameters()
        multi_source[0]["name"] = "Max. MPPT 2 Voltage [V]"
        multi_item = copy.deepcopy(enrichment())
        multi_item["translations"][0]["name_zh"] = "最大 2 电压"
        restored = validate_parameter_enrichment(multi_item, multi_source)
        self.assertEqual(
            "最大 2 电压（MPPT） [V]",
            restored["translations"][0]["name_zh"],
        )

    def test_rejects_numeric_claim_not_present_in_basis(self) -> None:
        item = copy.deepcopy(enrichment())
        item["sections"][0]["paragraphs"][0]["analysis_zh"] += (
            " 建议采用 1200 V 设备。"
        )

        with self.assertRaisesRegex(ParameterAnalysisError, "numeric text"):
            validate_parameter_enrichment(item, parameters())

    def test_rejects_unit_conversion_even_when_arithmetic_is_plausible(self) -> None:
        item = copy.deepcopy(enrichment())
        item["sections"][0]["paragraphs"][0]["analysis_zh"] = (
            "直流侧最大光伏阵列功率可写为 15 kWp，但这属于未经过"
            "运行时验证的单位换算，因此不能作为自动发布的分析内容。"
        )

        with self.assertRaisesRegex(ParameterAnalysisError, "numeric text"):
            validate_parameter_enrichment(item, parameters())

    def test_parameter_input_and_hash_are_order_sensitive_and_bounded(self) -> None:
        first = parameter_analysis_input(parameters())
        self.assertEqual(["p001", "p002", "p003"], [row["parameter_id"] for row in first])
        original_hash = parameter_set_sha256(parameters())
        reordered = list(reversed(parameters()))
        self.assertNotEqual(original_hash, parameter_set_sha256(reordered))

        with self.assertRaisesRegex(ParameterAnalysisError, "1-200"):
            parameter_analysis_input([])


if __name__ == "__main__":
    unittest.main()
