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
PRODUCT_TEMPLATE = SCRIPTS.parent / "templates" / "product-page.md"
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
        self.assertEqual(
            r"\[x\]\(javascript:bad\) \`code\` \*em\*",
            render.escape_table_cell("[x](javascript:bad) `code` *em*"),
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
    def test_home_page_is_a_deterministic_reader_catalogue(self) -> None:
        products = [
            {
                "product_id": "HP-1",
                "model": "HeatPro 1",
                "product_name": "HeatPro 1 air-to-water heat pump",
                "product_description_zh": "空气源热泵",
                "manufacturer": "Acme",
                "product_category": "热泵",
                "wiki_path": "products/hp-1-a1",
                "published_at": datetime(2026, 7, 18, tzinfo=timezone.utc),
            },
            {
                "product_id": "INV-1",
                "model": "SUN 1",
                "product_name": "SUN 1 three-phase solar inverter",
                "product_description_zh": "三相太阳能逆变器",
                "manufacturer": "Huawei",
                "product_category": "逆变器",
                "wiki_path": "products/inv-1-b2",
                "published_at": datetime(2026, 7, 19, tzinfo=timezone.utc),
            },
            {
                "product_id": "HP-2",
                "model": "HeatPro 2",
                "product_name": "HeatPro 2 air-to-water heat pump",
                "product_description_zh": "空气源热泵",
                "manufacturer": "Acme",
                "product_category": "热泵",
                "wiki_path": "products/hp-2-c3",
                "published_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
            },
        ]

        rendered = render.render_home_page(
            products,
            title="PV Wiki",
        )

        self.assertTrue(rendered.startswith(render.AUTO_BEGIN + "\n"))
        self.assertTrue(rendered.endswith(render.AUTO_END + "\n"))
        self.assertIn("当前已更新 **3** 款产品", rendered)
        self.assertIn("| 已更新产品 | 3 |", rendered)
        self.assertIn("| [Acme](/t/brand-acme) | 2 |", rendered)
        self.assertIn(
            "| [热泵](/t/category-%E7%83%AD%E6%B3%B5) | 2 |",
            rendered,
        )
        self.assertLess(rendered.index("HeatPro 2"), rendered.index("SUN 1"))
        self.assertIn("[HP-2](/products/hp-2-c3)", rendered)
        self.assertIn(
            "HeatPro 2 air-to-water heat pump｜空气源热泵",
            rendered,
        )
        self.assertNotIn("Due now", rendered)
        self.assertNotIn("family_code", rendered)
        self.assertEqual(
            rendered,
            render.render_home_page(
                list(reversed(products)),
                title="PV Wiki",
            ),
        )

    def test_empty_home_page_has_no_broken_tables_or_links(self) -> None:
        rendered = render.render_home_page([], title="GTI 产品百科")

        self.assertIn("当前已更新 **0** 款产品", rendered)
        self.assertIn("暂无已分类产品", rendered)
        self.assertIn("暂无已收录品牌", rendered)
        self.assertIn("暂无已更新产品", rendered)

    def test_render_is_deterministic_and_sorts_unordered_content(self) -> None:
        product = {
            "product_id": "42",
            "brand_code": "ACME",
            "family_code": "SO003",
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
        self.assertIn("| 产品 ID | 42 |", left)
        self.assertIn(r"Voltage\|input", left)
        self.assertNotIn("Hermes 自动维护", left)
        self.assertNotIn("SO003", left)
        self.assertIn("## 产品信息", left)
        self.assertIn("## 参考文献", left)
        self.assertNotIn("## Datasheet", left)
        self.assertNotIn("## 相关资料", left)
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
                "manufacturer": "Acme",
                "manufacturer_zh": "艾克米（Acme）",
                "model": "PV-42",
                "product_description_zh": "商用光伏并网逆变器",
                "product_category": "逆变器",
                "product_type": "商用光伏并网逆变器",
                "summary": "艾克米 PV-42 是一款面向商用屋顶的光伏并网逆变器。",
                "review_summary": "Installers praise its compact enclosure and clear commissioning workflow.",
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
                        "category": "直流输入",
                        "value": 48,
                        "unit": "V",
                        "confidence": 0.95,
                        "evidence_urls": ["https://example.com/manual.html"],
                    }
                ],
                "sources": [
                    {
                        "title": "Installer review A",
                        "url": "https://reviews.example.com/pv-42-a",
                        "source_type": "community",
                    },
                    {
                        "title": "Installer review B",
                        "url": "https://reviews.example.com/pv-42-b",
                        "source_type": "community",
                    },
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

        self.assertIn("# 42", rendered)
        self.assertIn("## PV-42｜商用光伏并网逆变器", rendered)
        self.assertIn("| 品牌/制造商 | 艾克米（Acme） |", rendered)
        self.assertIn("| 产品类别 | 逆变器 |", rendered)
        self.assertIn("| 产品类型 | 商用光伏并网逆变器 |", rendered)
        self.assertIn("| 产品描述 | PV-42｜商用光伏并网逆变器 |", rendered)
        self.assertIn(
            "艾克米 PV-42 是一款面向商用屋顶的光伏并网逆变器。",
            rendered,
        )
        self.assertIn("| 直流输入 | Input voltage | 48 V |", rendered)
        self.assertIn("**市场与用户反馈：**", rendered)
        self.assertIn("（官方数据表）", rendered)
        self.assertIn("[Input voltage（证据）](https://example.com/manual.html)", rendered)
        self.assertIn("- 判定：publish", rendered)
        self.assertIn("## 未解决的来源冲突", rendered)
        self.assertIn("IP65 / IP67", rendered)

    def test_datasheet_parameters_keep_source_order_and_show_derived_basis(
        self,
    ) -> None:
        rendered = render.render_product_page(
            {
                "product_id": "R5-10K-T2-15",
                "product_name": "10kW Three Phase Solar Inverter, Dual MPPT",
            },
            {
                "product_description_zh": "10kW 三相太阳能逆变器，双 MPPT",
                "datasheet_parameters": [
                    {
                        "section": "DC Input",
                        "section_zh": "直流输入",
                        "subsection": "PV input",
                        "subsection_zh": "光伏输入",
                        "name": "Max. DC voltage",
                        "name_zh": "最大直流电压",
                        "value": 1000,
                        "unit": "V",
                    },
                    {
                        "section": "AC Output",
                        "section_zh": "交流输出",
                        "name": "Rated output power",
                        "name_zh": "额定输出功率",
                        "value": 10,
                        "unit": "kW",
                    },
                    {
                        "section": "DC Input",
                        "section_zh": "直流输入",
                        "name": "Start voltage",
                        "name_zh": "启动电压",
                        "value": 160,
                        "unit": "V",
                    },
                ],
                "facts": [],
                "derived_insights": [
                    {
                        "name": "直流交流电压比",
                        "value": 100,
                        "formula": "1000 ÷ 10",
                        "basis": ["Max. DC voltage", "Rated output power"],
                        "explanation": "仅用于参数对照。",
                    }
                ],
            },
        )

        self.assertIn("# R5-10K-T2-15", rendered)
        self.assertIn(
            "## 10kW Three Phase Solar Inverter, Dual MPPT｜"
            "10kW 三相太阳能逆变器，双 MPPT",
            rendered,
        )
        self.assertIn("## 产品参数", rendered)
        self.assertLess(
            rendered.index("Max. DC voltage"),
            rendered.index("Rated output power"),
        )
        self.assertLess(
            rendered.index("Rated output power"),
            rendered.index("Start voltage"),
        )
        self.assertEqual(2, rendered.count("#### DC Input｜直流输入"))
        self.assertIn("#### DC Input｜直流输入", rendered)
        self.assertIn("##### PV input｜光伏输入", rendered)
        self.assertIn(
            "| 英文原文参数 | 专业中文参数 | 值 |",
            rendered,
        )
        self.assertIn(
            "| Max. DC voltage | 最大直流电压 | 1000 V |",
            rendered,
        )
        self.assertIn(
            "| Rated output power | 额定输出功率 | 10 kW |",
            rendered,
        )
        self.assertNotIn("| 数据表章节 | 参数 | 值 |", rendered)
        self.assertIn("### AI 参数对照（需复核）", rendered)
        self.assertIn("1000 ÷ 10", rendered)
        self.assertIn("Max. DC voltage、Rated output power", rendered)
        self.assertIn("仅复核二元算术", rendered)
        self.assertIn("不验证工程含义、量纲兼容性或单位换算", rendered)

    def test_datasheet_parameter_groups_escape_titles_and_preserve_empty_values(
        self,
    ) -> None:
        rendered = render.render_product_page(
            {"product_id": "SAFE-1", "product_name": "Safe rendering"},
            {
                "datasheet_parameters": [
                    {
                        "section": "DC\n## Injected <script> *unsafe*",
                        "section_zh": "直流|输入",
                        "subsection": "MPPT [A]",
                        "subsection_zh": "跟踪器\nA",
                        "name": "Max|voltage <x>",
                        "name_zh": "最大电压",
                        "value": 0,
                        "unit": "V|dc",
                    },
                    {
                        "section": "DC\n## Injected <script> *unsafe*",
                        "section_zh": "直流|输入",
                        "subsection": "MPPT [A]",
                        "subsection_zh": "跟踪器\nA",
                        "name": "Enabled",
                        "value": False,
                    },
                    {
                        "section": "DC\n## Injected <script> *unsafe*",
                        "section_zh": "直流|输入",
                        "name": "Blank value",
                        "name_zh": "空值",
                        "value": "",
                        "unit": "V",
                    },
                    {
                        "name": "Ungrouped",
                        "name_zh": "未分组参数",
                        "value": 1,
                    },
                ]
            },
        )

        self.assertIn(
            r"#### DC ## Injected &lt;script&gt; \*unsafe\*｜直流|输入",
            rendered,
        )
        self.assertIn(r"##### MPPT \[A\]｜跟踪器 A", rendered)
        self.assertNotIn("\n## Injected", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn(
            r"| Max\|voltage &lt;x&gt; | 最大电压 | 0 V\|dc |",
            rendered,
        )
        self.assertIn("| Enabled | — | false |", rendered)
        self.assertIn("| Blank value | 空值 |  |", rendered)
        self.assertNotIn("| Blank value | 空值 | V |", rendered)
        self.assertIn("##### 未分组", rendered)
        self.assertIn("#### 未分组", rendered)
        self.assertEqual(
            3,
            rendered.count("| 英文原文参数 | 专业中文参数 | 值 |"),
        )
        self.assertLess(rendered.index("Max\\|voltage"), rendered.index("Enabled"))
        self.assertLess(rendered.index("Enabled"), rendered.index("Blank value"))
        self.assertLess(rendered.index("Blank value"), rendered.index("Ungrouped"))

    def test_professional_analysis_resolves_parameter_ids_and_escapes_content(
        self,
    ) -> None:
        section_titles = {
            "product_positioning": "产品定位与功率配置",
            "dc_input": "直流输入与 MPPT",
            "ac_output": "交流输出与电网侧",
            "efficiency": "效率表现",
            "protection": "保护功能与电气安全",
            "installation": "安装、环境与运维",
            "limitations": "数据边界与选型注意事项",
        }
        rendered = render.render_product_page(
            {"product_id": "ANALYSIS-1", "product_name": "Analysis product"},
            {
                "summary": "This legacy summary must not be duplicated.",
                "datasheet_parameters": [
                    {
                        "section": "Input",
                        "name": "Max. DC voltage",
                        "name_zh": "最大直流电压",
                        "value": 1000,
                        "unit": "V",
                    },
                    {
                        "section": "Output",
                        "name": "Rated output power",
                        "name_zh": "额定输出功率",
                        "value": 10,
                        "unit": "kW",
                    },
                ],
                "professional_analysis": {
                    "input_parameter_count": 2,
                    "input_complete": True,
                    "sections": [
                        {
                            "section_code": code,
                            "paragraphs": [
                                {
                                    "analysis_kind": "parameter_interpretation",
                                    "analysis_zh": (
                                        f"{title}结论 [需复核] <script>。"
                                    ),
                                    "basis_parameter_ids": [
                                        "p001",
                                        "p002",
                                        "p001",
                                        "unknown\n## Injected",
                                    ],
                                    "conditions_zh": ["在 `额定` 条件下。"],
                                    "limitations_zh": [
                                        "不代表 *系统兼容性*。"
                                    ],
                                }
                            ],
                        }
                        for code, title in section_titles.items()
                    ]
                    + [
                        {
                            "section_code": "unknown\n## Injected",
                            "paragraphs": [
                                {
                                    "analysis_kind": "unsafe",
                                    "analysis_zh": "不应显示",
                                    "basis_parameter_ids": ["p001"],
                                }
                            ],
                        }
                    ],
                    "overall_limitations_zh": [
                        "仍需核对 [项目现场] <script> 条件。"
                    ],
                },
            },
        )

        self.assertLess(
            rendered.index("| Rated output power |"),
            rendered.index("## 产品分析"),
        )
        self.assertIn(
            "系统向模型提供了本页全部 2 项已核验参数。",
            rendered,
        )
        self.assertNotIn("逐项审阅", rendered)
        for title in section_titles.values():
            self.assertIn(f"### {title}", rendered)
        self.assertEqual(
            7,
            rendered.count(
                "- **依据参数：** p001 Max. DC voltage = 1000 V；"
                "p002 Rated output power = 10 kW"
            ),
        )
        self.assertIn(r"\[需复核\] &lt;script&gt;。", rendered)
        self.assertIn(r"- **适用条件：** 在 \`额定\` 条件下。", rendered)
        self.assertIn(r"- **注意事项：** 不代表 \*系统兼容性\*。", rendered)
        self.assertIn(
            r"- 仍需核对 \[项目现场\] &lt;script&gt; 条件。",
            rendered,
        )
        self.assertEqual(
            1,
            rendered.count("### 数据边界与选型注意事项"),
        )
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("\n## Injected", rendered)
        self.assertNotIn("不应显示", rendered)
        self.assertNotIn("This legacy summary must not be duplicated.", rendered)

    def test_overall_limitations_create_the_fixed_section_when_missing(
        self,
    ) -> None:
        rendered = render.render_product_page(
            {"product_id": "LIMIT-1", "product_name": "Limited product"},
            {
                "datasheet_parameters": [
                    {
                        "name": "Rated power",
                        "name_zh": "额定功率",
                        "value": 10,
                        "unit": "kW",
                    }
                ],
                "professional_analysis": {
                    "input_parameter_count": 1,
                    "input_complete": True,
                    "sections": [
                        {
                            "section_code": "product_positioning",
                            "paragraphs": [
                                {
                                    "analysis_kind": "engineering_interpretation",
                                    "analysis_zh": "该参数可用于描述产品的额定功率边界。",
                                    "basis_parameter_ids": ["p001"],
                                    "conditions_zh": [],
                                    "limitations_zh": [],
                                }
                            ],
                        }
                    ],
                    "overall_limitations_zh": [
                        "仍需结合项目条件完成选型。"
                    ],
                },
            },
        )

        self.assertEqual(
            1,
            rendered.count("### 数据边界与选型注意事项"),
        )
        self.assertIn("- 仍需结合项目条件完成选型。", rendered)

    def test_legacy_summary_falls_back_to_product_analysis_after_parameters(
        self,
    ) -> None:
        rendered = render.render_product_page(
            {"product_id": "LEGACY-1", "product_name": "Legacy product"},
            {"summary": "Legacy [summary] remains available."},
        )

        self.assertIn("## 产品分析", rendered)
        self.assertIn(r"Legacy \[summary\] remains available.", rendered)
        self.assertLess(rendered.index("## 产品参数"), rendered.index("## 产品分析"))
        self.assertEqual(
            1,
            rendered.count(r"Legacy \[summary\] remains available."),
        )

    def test_product_template_documents_nested_parameter_groups_and_analysis(
        self,
    ) -> None:
        template = PRODUCT_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("## 产品参数", template)
        self.assertIn("### 数据表参数", template)
        self.assertIn("#### {{ bilingual_section }}", template)
        self.assertIn("##### {{ bilingual_subsection }}", template)
        self.assertIn("| 英文原文参数 | 专业中文参数 | 值 |", template)
        self.assertNotIn("| 数据表章节 | 参数 | 值 |", template)
        self.assertIn("## 产品分析", template)
        self.assertIn("{{ professional_analysis_or_product_summary }}", template)
        self.assertLess(template.index("## 产品参数"), template.index("## 产品分析"))

    def test_legacy_description_removes_brand_model_and_series_prefixes(
        self,
    ) -> None:
        product = {
            "product_id": "R5-10K-T2-15",
            "brand_code": "SOLIS",
            "product_name": "10kW Three Phase Solar Inverter, Dual MPPT",
        }
        branded = {
            "manufacturer": "Solis",
            "model": "R5-10K-T2-15",
            "display_title_zh": (
                "Solis R5-10K-T2-15 10kW 三相太阳能逆变器，双 MPPT"
            ),
        }

        self.assertEqual(
            "10kW 三相太阳能逆变器，双 MPPT",
            render.product_description_zh(product, branded),
        )
        self.assertEqual(
            "10kW 三相太阳能逆变器，双 MPPT",
            render.product_description_zh(
                product,
                {
                    "display_title_zh": (
                        "R5/R6系列 10kW 三相太阳能逆变器，双 MPPT"
                    )
                },
            ),
        )
        rendered = render.render_product_page(product, branded)
        self.assertIn(
            "## 10kW Three Phase Solar Inverter, Dual MPPT｜"
            "10kW 三相太阳能逆变器，双 MPPT",
            rendered,
        )

    def test_empty_parameters_section_is_still_visible(self) -> None:
        rendered = render.render_product_page(
            {"product_id": "EMPTY-1", "product_name": "Empty product"},
            {},
        )

        self.assertIn("## 产品参数", rendered)
        self.assertIn("暂未提取到可安全归属该型号的数据表参数", rendered)
        self.assertIn("暂无可安全展示的已核验关键参数", rendered)


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

    def test_migrates_one_legacy_hermes_block_in_place(self) -> None:
        existing = (
            "Manual before\n\n"
            + render.LEGACY_AUTO_BEGIN
            + "\nold generated text\n"
            + render.LEGACY_AUTO_END
            + "\n\nManual after\n"
        )

        merged = render.merge_auto_block(existing, "new generated text")

        self.assertEqual(1, merged.count(render.AUTO_BEGIN))
        self.assertEqual(1, merged.count(render.AUTO_END))
        self.assertNotIn(render.LEGACY_AUTO_BEGIN, merged)
        self.assertNotIn(render.LEGACY_AUTO_END, merged)
        self.assertTrue(merged.startswith("Manual before\n\n"))
        self.assertTrue(merged.endswith("\n\nManual after\n"))

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
        with self.assertRaises(ValueError):
            render.merge_auto_block(
                "",
                f"{render.LEGACY_AUTO_BEGIN}\nx\n{render.LEGACY_AUTO_END}",
            )
        with self.assertRaises(ValueError):
            render.merge_auto_block(
                render.AUTO_BEGIN
                + "\nok\n"
                + render.AUTO_END
                + render.LEGACY_AUTO_BEGIN,
                "new",
            )
        with self.assertRaises(ValueError):
            render.merge_auto_block(
                render.LEGACY_AUTO_BEGIN
                + "\nok\n"
                + render.LEGACY_AUTO_END
                + render.AUTO_END,
                "new",
            )


if __name__ == "__main__":
    unittest.main()
