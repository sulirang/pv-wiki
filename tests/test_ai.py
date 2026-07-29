from __future__ import annotations

import io
import json
import multiprocessing
import sys
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import ai  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        payload: dict,
        *,
        status: int = 200,
        include_content_length: bool = True,
    ) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = Message()
        if include_content_length:
            self.headers["Content-Length"] = str(len(self._body))
        self.last_read_size: int | None = None

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.last_read_size = size
        return self._body if size < 0 else self._body[:size]


def settings(**overrides: object) -> ai.AISettings:
    values = {
        "base_url": "https://llm.example/v1",
        "api_key": "super-secret-key",
        "model": "compatible-model",
        "timeout": 12,
        "max_tokens": 2048,
        "max_response_bytes": 4096,
        "max_evidence_chars": 4000,
    }
    values.update(overrides)
    return ai.AISettings(**values)


def analysis_parameters() -> list[dict[str, str]]:
    return [
        {
            "section": "Input (DC)",
            "name": "Max. DC Voltage [V]",
            "value": "1100",
            "unit": "",
        },
        {
            "section": "Output (AC)",
            "name": "Rated Power [W]",
            "value": "10000",
            "unit": "",
        },
        {
            "section": "Efficiency",
            "name": "Max. Efficiency [%]",
            "value": "98.6",
            "unit": "",
        },
    ]


def valid_parameter_enrichment() -> dict:
    return {
        "translations": [
            {
                "parameter_id": "p001",
                "name_zh": "最大直流电压 [V]",
                "section_zh": "直流输入（DC）",
                "subsection_zh": "",
                "value_zh": "",
            },
            {
                "parameter_id": "p002",
                "name_zh": "额定功率 [W]",
                "section_zh": "交流输出（AC）",
                "subsection_zh": "",
                "value_zh": "",
            },
            {
                "parameter_id": "p003",
                "name_zh": "最大效率 [%]",
                "section_zh": "效率",
                "subsection_zh": "",
                "value_zh": "",
            },
        ],
        "sections": [
            {
                "section_code": "product_positioning",
                "paragraphs": [
                    {
                        "analysis_kind": "engineering_interpretation",
                        "basis_parameter_ids": ["p002"],
                        "analysis_zh": (
                            "额定功率参数为10000，这是产品功率配置的直接依据；"
                            "实际系统设计仍需结合并网条件、负载边界和项目约束进行核对。"
                        ),
                        "conditions_zh": [],
                        "limitations_zh": ["参数表本身未提供项目侧设计输入。"],
                    }
                ],
            }
        ],
        "overall_limitations_zh": [
            "以上内容仅解释已核验参数，不替代项目设计、厂家确认或现场校核。"
        ],
    }


def blocking_provider_worker(
    _send_connection: object,
    _endpoint: str,
    _data: bytes,
    _headers: object,
    _socket_timeout: float,
    _max_response_bytes: int,
) -> None:
    """Test target that can only finish if the deadline process is not killed."""

    time.sleep(30)


class AISettingsTests(unittest.TestCase):
    def test_loads_ai_variables_and_llm_aliases_without_secret_repr(self) -> None:
        direct = ai.AISettings.from_env(
            {
                "AI_BASE_URL": "https://primary.example/v1/",
                "AI_API_KEY": "primary-key",
                "AI_MODEL": "primary-model",
                "LLM_BASE_URL": "https://alias.example/v1",
                "LLM_API_KEY": "alias-key",
                "LLM_MODEL": "alias-model",
                "AI_TIMEOUT_SECONDS": "75",
                "AI_MAX_TOKENS": "3072",
                "AI_MAX_RESPONSE_BYTES": "8192",
                "AI_MAX_EVIDENCE_CHARS": "9000",
                "AI_JSON_RESPONSE_FORMAT": "true",
                "AI_THINKING_MODE": "DISABLED",
                "AI_REASONING_EFFORT": "HIGH",
            }
        )
        self.assertEqual("https://primary.example/v1", direct.base_url)
        self.assertEqual("primary-key", direct.api_key)
        self.assertEqual("primary-model", direct.model)
        self.assertEqual(75, direct.timeout)
        self.assertEqual(3072, direct.max_tokens)
        self.assertTrue(direct.json_response_format)
        self.assertEqual("disabled", direct.thinking_mode)
        self.assertEqual("high", direct.reasoning_effort)
        self.assertNotIn("primary-key", repr(direct))

        aliases = ai.AISettings.from_env(
            {
                "LLM_BASE_URL": "https://alias.example/v1",
                "LLM_API_KEY": "alias-key",
                "LLM_MODEL": "alias-model",
            }
        )
        self.assertEqual("alias-model", aliases.model)
        self.assertFalse(aliases.json_response_format)
        self.assertIsNone(aliases.thinking_mode)
        self.assertIsNone(aliases.reasoning_effort)

    def test_requires_https_except_for_loopback(self) -> None:
        for url in (
            "http://api.example/v1",
            "https://user:password@api.example/v1",
            "https://api.example/v1?key=value",
            "https://api.example/v1/chat/completions",
        ):
            with self.subTest(url=url), self.assertRaises(ai.AIConfigError):
                settings(base_url=url)

        for url in (
            "http://localhost:11434/v1",
            "http://127.0.0.1:8000/v1",
            "http://[::1]:8000/v1",
            "https://api.example:8443/v1",
        ):
            with self.subTest(url=url):
                self.assertEqual(url, settings(base_url=url).base_url)

        opted_in = settings(
            base_url="http://model.internal:8000/v1",
            allow_insecure_http=True,
        )
        self.assertEqual(
            "http://model.internal:8000/v1",
            opted_in.base_url,
        )

    def test_missing_or_unbounded_settings_are_rejected(self) -> None:
        with self.assertRaisesRegex(ai.AIConfigError, "AI_BASE_URL"):
            ai.AISettings.from_env({})
        with self.assertRaisesRegex(ai.AIConfigError, "AI_MAX_TOKENS"):
            ai.AISettings.from_env(
                {
                    "AI_BASE_URL": "https://llm.example/v1",
                    "AI_API_KEY": "key",
                    "AI_MODEL": "model",
                    "AI_MAX_TOKENS": "999999",
                }
            )
        with self.assertRaisesRegex(ai.AIConfigError, "bearer token"):
            settings(api_key="secret\r\nInjected: header")
        with self.assertRaisesRegex(ai.AIConfigError, "bearer token"):
            settings(api_key="replace-with-user-owned-ai-key")
        with self.assertRaisesRegex(ai.AIConfigError, "model name"):
            settings(model="replace-with-model-name")
        with self.assertRaisesRegex(
            ai.AIConfigError,
            "AI_JSON_RESPONSE_FORMAT",
        ):
            ai.AISettings.from_env(
                {
                    "AI_BASE_URL": "https://llm.example/v1",
                    "AI_API_KEY": "key",
                    "AI_MODEL": "model",
                    "AI_JSON_RESPONSE_FORMAT": "sometimes",
                }
            )
        with self.assertRaisesRegex(ai.AIConfigError, "AI_THINKING_MODE"):
            ai.AISettings.from_env(
                {
                    "AI_BASE_URL": "https://llm.example/v1",
                    "AI_API_KEY": "key",
                    "AI_MODEL": "model",
                    "AI_THINKING_MODE": "automatic",
                }
            )
        with self.assertRaisesRegex(ai.AIConfigError, "AI_THINKING_MODE"):
            settings(thinking_mode=False)
        with self.assertRaisesRegex(ai.AIConfigError, "AI_REASONING_EFFORT"):
            settings(reasoning_effort="low")
        with self.assertRaisesRegex(ai.AIConfigError, "AI_REASONING_EFFORT"):
            settings(reasoning_effort=False)

    def test_nonfinite_json_constants_are_rejected(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ai.AIResponseError):
                ai.parse_decision_content('{"confidence":' + value + "}")


class PromptTests(unittest.TestCase):
    def test_prompt_excludes_authorization_and_internal_product_metadata(self) -> None:
        messages = ai.build_decision_messages(
            product={
                "name": "PV-42",
                "product_id": "internal-id-9",
                "internal_family": "SO003",
                "family_code": "SO003",
            },
            search={
                "queries": ['"PV-42" datasheet'],
                "results": [
                    {
                        "title": "Result",
                        "url": "https://search.example/pv-42",
                        "content": "s" * 2000,
                        "score": 0.9,
                    }
                ],
            },
            extract={
                "query": "PV-42 specifications",
                "results": [
                    {
                        "url": "https://maker.example/pv-42.pdf",
                        "raw_content": "e" * 6000,
                        "content_source": "direct_pdf_text",
                        "pdf_page_count": 12,
                    }
                ],
            },
            max_evidence_chars=3000,
        )

        self.assertEqual(["system", "user"], [item["role"] for item in messages])
        self.assertIn("Never request a per-product issue", messages[0]["content"])
        prompt = json.loads(messages[1]["content"])
        self.assertNotIn("lease", prompt)
        self.assertNotIn("product_id", prompt["product"])
        self.assertNotIn("internal_family", prompt["product"])
        self.assertNotIn("family_code", prompt["product"])
        self.assertNotIn("internal-id-9", messages[1]["content"])
        self.assertNotIn("SO003", messages[1]["content"])
        self.assertNotIn("product_id", messages[1]["content"])
        self.assertNotIn("lease_token", messages[1]["content"])
        self.assertIn(
            "reader-facing category",
            " ".join(prompt["source_policy"]),
        )
        self.assertIn(
            "product_description_zh",
            prompt["output_contract"]["required_top_level_fields"],
        )
        self.assertNotIn(
            "display_title_zh",
            prompt["output_contract"]["required_top_level_fields"],
        )
        for field in (
            "product_category_code",
            "product_type",
            "datasheet_parameters",
            "derived_insights",
        ):
            self.assertIn(
                field,
                prompt["output_contract"]["required_top_level_fields"],
            )
        self.assertEqual(
            "逆变器",
            prompt["output_contract"]["product_categories"]["inverter"],
        )
        self.assertIn(
            "manufacturer_zh",
            prompt["output_contract"]["required_top_level_fields"],
        )
        display_policy = " ".join(prompt["source_policy"])
        self.assertIn("Simplified Chinese", display_policy)
        self.assertIn("PostgreSQL English product_name", display_policy)
        self.assertIn("exactly two distinct", display_policy)
        self.assertIn("operand order", display_policy)
        self.assertIn("does not validate engineering meaning", display_policy)
        insight_contract = prompt["output_contract"]["derived_insight_item"]
        self.assertEqual("finite numeric result", insight_contract["value"])
        self.assertIn("no units", insight_contract["formula"])
        self.assertIn("exactly 2 distinct", insight_contract["basis"][0])
        self.assertIn("fluent Simplified Chinese prose", display_policy)
        self.assertIn(
            "multiple sibling models",
            " ".join(prompt["source_policy"]),
        )
        self.assertIn(
            "return ambiguous",
            " ".join(prompt["source_policy"]),
        )
        self.assertEqual(
            "https://maker.example/pv-42.pdf",
            prompt["retrieval"]["extract"]["results"][0]["url"],
        )
        search_result = prompt["retrieval"]["search"]["results"][0]
        self.assertEqual("Result", search_result["title"])
        self.assertNotIn("url", search_result)
        self.assertNotIn(
            "https://search.example/pv-42",
            messages[1]["content"],
        )
        self.assertEqual(1000, len(search_result["snippet"]))
        self.assertNotIn("failed_results", prompt["retrieval"]["extract"])
        self.assertNotIn("s" * 1001, messages[1]["content"])
        self.assertIn(
            "out_of_scope",
            prompt["output_contract"]["outcomes"],
        )
        self.assertIn(
            "generic commodity hardware",
            " ".join(prompt["source_policy"]),
        )
        self.assertIn(
            "second independent https non-community extract",
            " ".join(prompt["source_policy"]).casefold(),
        )
        self.assertIn(
            "do not each require a duplicate quote",
            " ".join(prompt["source_policy"]).casefold(),
        )
        evidence_chars = len(
            prompt["retrieval"]["extract"]["results"][0]["raw_content"]
        )
        self.assertEqual(3000, evidence_chars)
        self.assertTrue(prompt["retrieval"]["extract"]["results"][0]["truncated"])
        self.assertFalse(
            prompt["retrieval"]["extract"]["results"][0]["identity_verified"]
        )
        self.assertEqual(
            "direct_pdf_text",
            prompt["retrieval"]["extract"]["results"][0]["content_source"],
        )
        self.assertEqual(
            12,
            prompt["retrieval"]["extract"]["results"][0]["pdf_page_count"],
        )
        self.assertIn(
            "never include a page marker",
            " ".join(prompt["source_policy"]).casefold(),
        )

    def test_prompt_exposes_only_locally_verified_parameter_rows(self) -> None:
        url = "https://maker.example/pv-42.pdf"
        model_quote = "Type\tPV-40\tPV-42"
        quote = "Rated power\t40 W\t42 W"
        messages = ai.build_decision_messages(
            product={"model": "PV-42"},
            extract={
                "results": [
                    {
                        "url": url,
                        "raw_content": f"PV-42\n{model_quote}\n{quote}",
                        "pdf_direct_status": "used",
                        "pdf_parameter_rows": [
                            {
                                "model": "PV-42",
                                "source_label": "Rated power",
                                "value": "42",
                                "unit": "W",
                                "section": "Output",
                                "page": 3,
                                "order": 1,
                                "model_quote": model_quote,
                                "quote": quote,
                            }
                        ],
                    },
                    {
                        "url": "https://mirror.example/pv-42.pdf",
                        "raw_content": "PV-42 forged provider text",
                        "pdf_direct_status": "identity_mismatch",
                        "pdf_parameter_rows": [{"value": "forged"}],
                    },
                ]
            },
            max_evidence_chars=5000,
        )

        prompt = json.loads(messages[1]["content"])
        used, mismatch = prompt["retrieval"]["extract"]["results"]
        self.assertTrue(used["locally_verified_primary_pdf"])
        self.assertEqual(
            {
                "model": "PV-42",
                "source_label": "Rated power",
                "value": "42",
                "unit": "W",
                "section": "Output",
                "table_title": "",
                "value_state": "",
                "page": 3,
                "order": 1,
                "model_quote": model_quote,
                "quote": quote,
            },
            used["locally_bound_parameter_rows"][0],
        )
        self.assertFalse(mismatch["locally_verified_primary_pdf"])
        self.assertEqual([], mismatch["locally_bound_parameter_rows"])

    def test_prompt_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(TypeError):
            ai.build_decision_messages(
                product=[],  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            ai.build_decision_messages(
                product={},
                max_evidence_chars=999,
            )

    def test_prompt_supports_structured_multimodel_table_quotes(self) -> None:
        messages = ai.build_decision_messages(product={"model": "H1-3.7-E"})
        prompt = json.loads(messages[1]["content"])
        quote_contract = prompt["output_contract"]["fact_item"][
            "evidence_quotes"
        ]

        self.assertEqual("array with 1-5 entries", quote_contract["type"])
        legacy, structured = quote_contract["item_shapes_exactly_one_of"]
        self.assertEqual({"url", "quote"}, set(legacy))
        self.assertEqual(
            {"url", "model_quote", "quote"},
            set(structured),
        )
        self.assertIn("model header row", structured["model_quote"])
        self.assertIn("target-column value", structured["quote"])

        policy = " ".join(prompt["source_policy"])
        self.assertIn("{url, model_quote, quote}", policy)
        self.assertIn("Markdown-pipe or TSV", policy)
        self.assertIn("fixed-width", policy)
        self.assertIn("two-or-more-space-delimited cells", policy)
        self.assertIn("same target column", policy)
        self.assertIn("unambiguous same-column", policy)
        self.assertIn("no minimum fact count", policy)
        self.assertIn("omit that fact and still publish", policy)

    def test_research_prompt_has_bounded_actions_and_safe_validation_feedback(
        self,
    ) -> None:
        feedback = ai.ValidationFeedback(
            ai.ResearchGap.INDEPENDENT_CORROBORATION,
            "A second independent exact extract is still missing.",
        )
        messages = ai.build_research_messages(
            product={"model": "PV-42"},
            search={"queries": ['"PV-42" datasheet']},
            candidate_manufacturer="Acme Solar",
            previous_queries=['"PV-42" datasheet'],
            validation_feedback=feedback,
            max_evidence_chars=3000,
        )

        self.assertEqual(["system", "user"], [item["role"] for item in messages])
        self.assertIn("never authorizes a domain", messages[0]["content"])
        prompt = json.loads(messages[1]["content"])
        self.assertEqual("choose_next_research_action", prompt["task"])
        self.assertEqual(
            ["PV-42", "Acme Solar"],
            prompt["research_context"]["query_binding_terms"],
        )
        self.assertEqual(
            ["PV-42"],
            prompt["research_context"]["required_query_binding_terms"],
        )
        self.assertEqual(
            {
                "gap": "independent_corroboration",
                "note": "A second independent exact extract is still missing.",
            },
            prompt["research_context"]["validation_feedback"],
        )
        gaps = prompt["action_contract"]["exact_top_level_shapes"][
            "search_more"
        ]["gap"]
        self.assertEqual(
            {
                "manufacturer_identity",
                "primary_datasheet",
                "independent_corroboration",
                "missing_exact_fact",
                "conflict_resolution",
                "scope_classification",
            },
            set(gaps),
        )
        rules = " ".join(prompt["action_contract"]["rules"])
        self.assertIn("conservative final non-publish", rules)
        self.assertIn("corrected final proposal", rules)
        self.assertIn("only those existing extracted URLs", rules)
        self.assertNotIn("Do not return final publish", rules)
        self.assertIn("cannot name or authorize a source or domain", rules)

    def test_final_only_research_prompt_forbids_more_search(self) -> None:
        messages = ai.build_research_messages(
            product={"model": "PV-42"},
            previous_queries=["PV-42 datasheet"],
            final_only=True,
        )

        prompt = json.loads(messages[1]["content"])
        self.assertTrue(prompt["research_context"]["final_only"])
        self.assertEqual(
            {"final"},
            set(prompt["action_contract"]["exact_top_level_shapes"]),
        )
        rules = " ".join(prompt["action_contract"]["rules"])
        self.assertIn("research budget is exhausted", rules)
        self.assertIn("search_more is forbidden", rules)

    def test_research_prompt_uses_bounded_registered_supplier_policy(
        self,
    ) -> None:
        policy = ai.TrustedSourcePolicy(
            manufacturer="Huawei",
            domains=(
                "support.huawei.com",
                "solar.huawei.com",
            ),
        )
        messages = ai.build_research_messages(
            product={"model": "JUPITER-3000K-H1"},
            trusted_source_policy=policy,
        )

        prompt = json.loads(messages[1]["content"])
        self.assertEqual(
            {
                "manufacturer": "Huawei",
                "trusted_domains": [
                    "solar.huawei.com",
                    "support.huawei.com",
                ],
                "independent_corroboration_required": False,
            },
            prompt["trusted_source_policy"],
        )
        source_policy = " ".join(prompt["source_policy"])
        self.assertIn(
            "independent-domain corroboration is not required",
            source_policy,
        )
        self.assertIn(
            "only the runtime decision gate can authorize",
            source_policy,
        )
        action_rules = " ".join(prompt["action_contract"]["rules"])
        self.assertIn(
            "do not request independent_corroboration",
            action_rules,
        )

    def test_registered_supplier_policy_rejects_unsafe_context(self) -> None:
        with self.assertRaises(ValueError):
            ai.TrustedSourcePolicy("", ("solar.huawei.com",))
        with self.assertRaises(ValueError):
            ai.TrustedSourcePolicy(
                "Huawei",
                ("https://solar.huawei.com/path",),
            )
        with self.assertRaises(TypeError):
            ai.build_research_messages(
                product={"model": "PV-42"},
                trusted_source_policy={  # type: ignore[arg-type]
                    "manufacturer": "Acme",
                    "domains": ["acme.example"],
                },
            )

    def test_validation_feedback_rejects_unbounded_or_unsafe_text(self) -> None:
        fixed = ai.ValidationFeedback.for_gap(
            ai.ResearchGap.INDEPENDENT_CORROBORATION
        )
        self.assertEqual(ai.ResearchGap.INDEPENDENT_CORROBORATION, fixed.gap)
        self.assertIn("independent extract", fixed.note)
        with self.assertRaises(TypeError):
            ai.ValidationFeedback(  # type: ignore[arg-type]
                "independent_corroboration",
                "Missing evidence.",
            )
        for note in (
            "See https://private.example/error",
            "unsafe\ntrace",
            "x" * (ai.MAX_VALIDATION_FEEDBACK_CHARS + 1),
            "Raw exception includes an API key.",
        ):
            with self.subTest(note=note), self.assertRaises(ValueError):
                ai.ValidationFeedback(
                    ai.ResearchGap.INDEPENDENT_CORROBORATION,
                    note,
                )
        with self.assertRaises(TypeError):
            ai.build_research_messages(product=[])  # type: ignore[arg-type]

    def test_parameter_analysis_prompt_contains_every_verified_row(self) -> None:
        messages = ai.build_parameter_analysis_messages(
            product={
                "name": "R5-10K-T2-15",
                "product_id": "private-db-id",
                "family_code": "internal-family",
            },
            parameters=analysis_parameters(),
            max_evidence_chars=12_000,
        )

        self.assertEqual(["system", "user"], [item["role"] for item in messages])
        prompt = json.loads(messages[1]["content"])
        self.assertEqual(
            "translate_and_analyze_complete_verified_parameter_set",
            prompt["task"],
        )
        self.assertEqual(3, prompt["input_guarantees"]["parameter_count"])
        self.assertTrue(
            prompt["input_guarantees"]["complete_within_runtime_budget"]
        )
        self.assertEqual(
            ["p001", "p002", "p003"],
            [
                item["parameter_id"]
                for item in prompt["verified_parameters"]
            ],
        )
        self.assertEqual(
            analysis_parameters(),
            [
                {
                    key: item[key]
                    for key in ("section", "name", "value", "unit")
                }
                for item in prompt["verified_parameters"]
            ],
        )
        self.assertNotIn("product_id", prompt["product"])
        self.assertNotIn("family_code", prompt["product"])
        self.assertIn(
            "Never alter, convert, infer, or manufacture",
            messages[0]["content"],
        )
        self.assertIn(
            "Return every translation",
            " ".join(prompt["analysis_policy"]),
        )
        self.assertIn(
            "one 60-240 character",
            " ".join(prompt["analysis_policy"]),
        )
        self.assertEqual(
            2,
            prompt["output_contract"]["sections"]["item"]["paragraphs"][
                "maxItems"
            ],
        )
        self.assertEqual(
            "直流输入（DC）",
            prompt["controlled_section_translations"]["input (dc)"],
        )
        self.assertEqual(
            ["过流保护", "过电流保护"],
            prompt["controlled_compound_terms"]["Over Current Protection"],
        )
        self.assertIn(
            "compound term",
            prompt["controlled_term_rule"],
        )

    def test_parameter_analysis_prompt_refuses_truncated_input(self) -> None:
        with self.assertRaisesRegex(
            ai.ParameterAnalysisError,
            "complete parameter analysis request",
        ):
            ai.build_parameter_analysis_messages(
                product={"name": "R5-10K-T2-15"},
                parameters=analysis_parameters(),
                max_evidence_chars=1000,
            )


class ParsingTests(unittest.TestCase):
    def test_accepts_plain_json_or_one_json_fence(self) -> None:
        expected = {"schema_version": "1", "outcome": "no_datasheet"}
        self.assertEqual(expected, ai.parse_decision_content(json.dumps(expected)))
        self.assertEqual(
            expected,
            ai.parse_decision_content(
                f"```json\n{json.dumps(expected)}\n```"
            ),
        )

    def test_rejects_chatter_non_objects_other_fences_and_duplicate_keys(self) -> None:
        invalid = (
            'Here is the answer: {"outcome":"no_datasheet"}',
            '```json\n{"outcome":"no_datasheet"}\n```\nThanks',
            '```\n{"outcome":"no_datasheet"}\n```',
            '[{"outcome":"no_datasheet"}]',
            '{"outcome":"publish","outcome":"ambiguous"}',
        )
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(
                ai.AIResponseError
            ):
                ai.parse_decision_content(content)

    def test_parses_typed_final_and_removes_model_owned_authorization(self) -> None:
        action = ai.parse_research_action_content(
            json.dumps(
                {
                    "action": "final",
                    "decision": {
                        "schema_version": "model-owned",
                        "product_id": "model-owned",
                        "lease_token": "model-owned",
                        "outcome": "no_datasheet",
                    },
                }
            ),
            product={"model": "PV-42"},
        )

        self.assertIsInstance(action, ai.FinalAction)
        self.assertEqual("final", action.action)
        self.assertEqual({"outcome": "no_datasheet"}, action.decision)

    def test_parses_search_more_and_deduplicates_queries(self) -> None:
        action = ai.parse_research_action_content(
            json.dumps(
                {
                    "action": "search_more",
                    "gap": "primary_datasheet",
                    "queries": [
                        "PV-42 technical manual",
                        "  pv-42   TECHNICAL manual  ",
                    ],
                }
            ),
            product={"model": "PV-42"},
        )

        self.assertIsInstance(action, ai.SearchMoreAction)
        self.assertEqual(ai.ResearchGap.PRIMARY_DATASHEET, action.gap)
        self.assertEqual(("PV-42 technical manual",), action.queries)

    def test_final_only_search_more_becomes_a_conservative_stop_signal(
        self,
    ) -> None:
        action = ai.validate_research_action(
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["PV-42 datasheet"],
            },
            product={"model": "PV-42"},
            previous_queries=["pv-42 DATASHEET"],
            final_only=True,
        )

        self.assertIsInstance(action, ai.SearchMoreAction)
        self.assertEqual(ai.ResearchGap.PRIMARY_DATASHEET, action.gap)
        self.assertEqual((), action.queries)

    def test_search_query_may_bind_current_candidate_manufacturer(self) -> None:
        action = ai.validate_research_action(
            {
                "action": "search_more",
                "gap": "manufacturer_identity",
                "queries": ["Acme Solar manufacturer catalogue"],
            },
            product={},
            candidate_manufacturer="Acme Solar",
        )

        self.assertEqual(
            ("Acme Solar manufacturer catalogue",),
            action.queries,
        )

    def test_plain_url_constraints_are_removed_from_search_queries(self) -> None:
        for raw, expected in (
            (
                "PV-42 https://maker.example/data technical manual",
                "PV-42 technical manual",
            ),
            (
                "PV-42 maker.example technical manual",
                "PV-42 technical manual",
            ),
            (
                "PV-42 technical manual site:maker.example",
                "PV-42 technical manual",
            ),
        ):
            with self.subTest(raw=raw):
                action = ai.validate_research_action(
                    {
                        "action": "search_more",
                        "gap": "primary_datasheet",
                        "queries": [raw],
                    },
                    product={"model": "PV-42"},
                )
                self.assertEqual((expected,), action.queries)

    def test_decimal_model_binding_is_opaque_during_url_detection(self) -> None:
        for model in ("H3-8.0-E", "HS3-3.6K-S2-W"):
            query = f"{model} official technical manual"
            with self.subTest(model=model):
                action = ai.validate_research_action(
                    {
                        "action": "search_more",
                        "gap": "primary_datasheet",
                        "queries": [query],
                    },
                    product={"model": model},
                )
                self.assertEqual((query,), action.queries)

    def test_decimal_model_does_not_hide_other_urlish_query_text(self) -> None:
        model = "H3-8.0-E"
        repaired = ai.validate_research_action(
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": [f"{model} maker.example technical manual"],
            },
            product={"model": model},
        )
        self.assertEqual(
            (f"{model} technical manual",),
            repaired.queries,
        )

        for unsafe in (
            f"{model} evil[.]example manual",
            f"{model} 192.0.2.10 manual",
            f"{model} https[:]//evil.example manual",
            f"{model}.evil.example manual",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(
                ai.AIInvalidOutputError
            ):
                ai.validate_research_action(
                    {
                        "action": "search_more",
                        "gap": "primary_datasheet",
                        "queries": [unsafe],
                    },
                    product={"model": model},
                )

        for unsafe_binding in ("maker.example", "192.0.2.10"):
            with self.subTest(
                unsafe_binding=unsafe_binding
            ), self.assertRaises(ai.AIInvalidOutputError):
                ai.validate_research_action(
                    {
                        "action": "search_more",
                        "gap": "primary_datasheet",
                        "queries": [f"{unsafe_binding} manual"],
                    },
                    product={"model": unsafe_binding},
                )

    def test_rejects_unsafe_unbound_or_non_novel_search_actions(self) -> None:
        invalid_actions = (
            {
                "action": "search_more",
                "gap": "not_a_gap",
                "queries": ["PV-42 datasheet"],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["PV-42 evil\u3002example data"],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["PV-42 evil\uff61example data"],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["PV-42 evil[.]example data"],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["PV-42 例子.公司 data"],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["PV-42 [2001:db8::1] data"],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["PV-42\nmanual"],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": [
                    "PV-42 " + "x" * ai.MAX_RESEARCH_QUERY_CHARS
                ],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["another product manual"],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": [
                    "PV-42 one",
                    "PV-42 two",
                    "PV-42 three",
                ],
            },
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["PV-42 manual"],
                "source": "manufacturer",
            },
        )
        for action in invalid_actions:
            with self.subTest(action=action), self.assertRaises(
                ai.AIInvalidOutputError
            ):
                ai.validate_research_action(
                    action,
                    product={"model": "PV-42"},
                )

        with self.assertRaisesRegex(ai.AIInvalidOutputError, "novel"):
            ai.validate_research_action(
                {
                    "action": "search_more",
                    "gap": "primary_datasheet",
                    "queries": ["PV-42 manual"],
                },
                product={"model": "PV-42"},
                previous_queries=["pv-42   MANUAL"],
            )

        for generic_manufacturer in ("official", "solar", "x"):
            with self.subTest(
                generic_manufacturer=generic_manufacturer
            ), self.assertRaises(ai.AIInvalidOutputError):
                ai.validate_research_action(
                    {
                        "action": "search_more",
                        "gap": "manufacturer_identity",
                        "queries": [
                            f"{generic_manufacturer} technical manual"
                        ],
                    },
                    product={},
                    candidate_manufacturer=generic_manufacturer,
                )

    def test_product_model_is_required_even_with_candidate_manufacturer(self) -> None:
        with self.assertRaisesRegex(ai.AIInvalidOutputError, "exact model"):
            ai.validate_research_action(
                {
                    "action": "search_more",
                    "gap": "manufacturer_identity",
                    "queries": ["Acme Solar manufacturer catalogue"],
                },
                product={"model": "PV-42"},
                candidate_manufacturer="Acme Solar",
            )

    def test_validation_feedback_constrains_the_next_action(self) -> None:
        feedback = ai.ValidationFeedback(
            ai.ResearchGap.MISSING_EXACT_FACT,
            "One specification lacks an exact target-model quote.",
        )
        invalid_actions = (
            {
                "action": "search_more",
                "gap": "primary_datasheet",
                "queries": ["PV-42 datasheet"],
            },
        )
        for action in invalid_actions:
            with self.subTest(action=action), self.assertRaises(
                ai.AIInvalidOutputError
            ):
                ai.validate_research_action(
                    action,
                    product={"model": "PV-42"},
                    validation_feedback=feedback,
                )

        corrected_publish = ai.validate_research_action(
            {
                "action": "final",
                "decision": {"outcome": "publish"},
            },
            product={"model": "PV-42"},
            validation_feedback=feedback,
        )
        self.assertEqual("publish", corrected_publish.decision["outcome"])

        conservative = ai.validate_research_action(
            {
                "action": "final",
                "decision": {"outcome": "ambiguous"},
            },
            product={"model": "PV-42"},
            validation_feedback=feedback,
        )
        self.assertEqual("ambiguous", conservative.decision["outcome"])

    def test_final_normalizes_decimal_string_confidence(self) -> None:
        action = ai.validate_research_action(
            {
                "action": "final",
                "decision": {
                    "outcome": "publish",
                    "confidence": "0.95",
                    "facts": [
                        {
                            "name": "Power",
                            "confidence": "1.0",
                        }
                    ],
                    "datasheet_parameters": [
                        {
                            "name": "Rated power",
                            "confidence": "0.99",
                        }
                    ],
                },
            },
            product={"model": "PV-42"},
        )

        self.assertEqual(0.95, action.decision["confidence"])
        self.assertEqual(1.0, action.decision["facts"][0]["confidence"])
        self.assertEqual(
            0.99,
            action.decision["datasheet_parameters"][0]["confidence"],
        )

        for invalid in ("95%", "nan", "1.1", True):
            with self.subTest(invalid=invalid), self.assertRaises(
                ai.AIInvalidOutputError
            ):
                ai.validate_research_action(
                    {
                        "action": "final",
                        "decision": {
                            "outcome": "publish",
                            "confidence": invalid,
                        },
                    },
                    product={"model": "PV-42"},
                )


class OpenAICompatibleClientTests(unittest.TestCase):
    def test_posts_chat_completions_with_bounded_settings_and_bearer_auth(self) -> None:
        calls: list[tuple[object, float, dict]] = []
        decision = {
            "schema_version": "1",
            "product_id": "P-42",
            "lease_token": "lease-123",
            "outcome": "no_datasheet",
        }

        def fake_open(request: object, *, timeout: float) -> FakeResponse:
            payload = json.loads(request.data.decode("utf-8"))
            calls.append((request, timeout, payload))
            return FakeResponse(
                {"choices": [{"message": {"content": json.dumps(decision)}}]}
            )

        client = ai.OpenAICompatibleClient(settings(), opener=fake_open)
        result = client.decide(
            product={"name": "PV-42"},
            search={},
            extract={},
        )

        self.assertEqual({"outcome": "no_datasheet"}, result)
        self.assertEqual(1, len(calls))
        request, timeout, payload = calls[0]
        self.assertEqual(
            "https://llm.example/v1/chat/completions", request.full_url
        )
        self.assertEqual("POST", request.method)
        self.assertEqual(
            "Bearer super-secret-key", request.get_header("Authorization")
        )
        self.assertEqual(12, timeout)
        self.assertEqual("compatible-model", payload["model"])
        self.assertEqual(2048, payload["max_tokens"])
        self.assertEqual(0, payload["temperature"])
        self.assertIs(payload["stream"], False)
        self.assertNotIn("response_format", payload)
        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_parameter_analysis_is_validated_and_repaired_once(self) -> None:
        calls: list[dict] = []
        responses = [
            {"translations": [], "sections": [], "overall_limitations_zh": []},
            valid_parameter_enrichment(),
        ]

        def fake_open(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            calls.append(json.loads(request.data.decode("utf-8")))
            result = responses[len(calls) - 1]
            return FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(result)},
                        }
                    ],
                    "usage": {"total_tokens": 321},
                }
            )

        client = ai.OpenAICompatibleClient(
            settings(
                max_response_bytes=20_000,
                max_evidence_chars=12_000,
            ),
            opener=fake_open,
        )
        result = client.analyze_parameters(
            product={"name": "R5-10K-T2-15"},
            parameters=analysis_parameters(),
        )

        self.assertEqual(2, client.last_parameter_analysis_provider_requests)
        self.assertEqual(2, len(calls))
        self.assertEqual(3, result["input_parameter_count"])
        self.assertTrue(result["input_complete"])
        self.assertEqual(
            ["p001", "p002", "p003"],
            [item["parameter_id"] for item in result["translations"]],
        )
        repair_content = calls[1]["messages"][-1]["content"]
        self.assertIn("Retry once", repair_content)
        self.assertIn("every input parameter", repair_content)
        self.assertIn(
            "translations must contain exactly one item",
            repair_content,
        )

    def test_optional_thinking_controls_are_sent_as_provider_extensions(
        self,
    ) -> None:
        calls: list[dict] = []

        def fake_open(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            calls.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"outcome": "no_datasheet"}
                                )
                            }
                        }
                    ]
                }
            )

        client = ai.OpenAICompatibleClient(
            settings(thinking_mode="enabled", reasoning_effort="high"),
            opener=fake_open,
        )
        self.assertEqual(
            {"outcome": "no_datasheet"},
            client.decide(product={"name": "PV-42"}),
        )
        self.assertEqual(
            {"type": "enabled"},
            calls[0]["thinking"],
        )
        self.assertEqual("high", calls[0]["reasoning_effort"])

    def test_json_response_format_and_bounded_provider_metadata(self) -> None:
        calls: list[dict] = []

        def fake_open(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            calls.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {"outcome": "no_datasheet"}
                                )
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 15,
                        "total_tokens": 135,
                        "completion_tokens_details": {
                            "reasoning_tokens": 7,
                        },
                        "provider_note": "must-not-be-preserved",
                    },
                }
            )

        client = ai.OpenAICompatibleClient(
            settings(json_response_format=True),
            opener=fake_open,
        )
        result = client.decide(product={"name": "PV-42"})

        self.assertEqual({"outcome": "no_datasheet"}, result)
        self.assertEqual(
            {"type": "json_object"},
            calls[0]["response_format"],
        )
        self.assertEqual("stop", client.last_finish_reason)
        self.assertEqual(
            {
                "prompt_tokens": 120,
                "completion_tokens": 15,
                "total_tokens": 135,
                "completion_tokens_details": {
                    "reasoning_tokens": 7,
                },
            },
            client.last_usage,
        )
        self.assertEqual(1, len(client.last_response_metadata))
        self.assertNotIn(
            "must-not-be-preserved",
            repr(client.last_response_metadata),
        )

    def test_nonfinal_finish_reason_repairs_before_content_is_accepted(
        self,
    ) -> None:
        for finish_reason in (
            "length",
            "content_filter",
            "tool_calls",
            "provider_specific_stop",
        ):
            calls: list[dict] = []
            responses = iter(
                [
                    FakeResponse(
                        {
                            "choices": [
                                {
                                    "finish_reason": finish_reason,
                                    "message": {
                                        "content": json.dumps(
                                            {"outcome": "publish"}
                                        )
                                    },
                                }
                            ],
                            "usage": {"total_tokens": 200},
                        }
                    ),
                    FakeResponse(
                        {
                            "choices": [
                                {
                                    "finish_reason": "stop",
                                    "message": {
                                        "content": json.dumps(
                                            {"outcome": "no_datasheet"}
                                        )
                                    },
                                }
                            ],
                            "usage": {"total_tokens": 20},
                        }
                    ),
                ]
            )

            def fake_open(
                request: object,
                *,
                timeout: float,
            ) -> FakeResponse:
                del timeout
                calls.append(json.loads(request.data.decode("utf-8")))
                return next(responses)

            with self.subTest(finish_reason=finish_reason):
                client = ai.OpenAICompatibleClient(
                    settings(),
                    opener=fake_open,
                )
                self.assertEqual(
                    {"outcome": "no_datasheet"},
                    client.decide(product={"name": "PV-42"}),
                )
                self.assertEqual(2, len(calls))
                self.assertIn(
                    "incomplete_response",
                    calls[1]["messages"][-1]["content"],
                )
                self.assertEqual(
                    [finish_reason, "stop"],
                    [
                        item.finish_reason
                        for item in client.last_response_metadata
                    ],
                )
                self.assertEqual(
                    {"total_tokens": 200},
                    client.last_response_metadata[0].usage,
                )

    def test_retries_one_empty_model_output_with_a_bounded_repair_prompt(self) -> None:
        calls: list[dict] = []
        responses = iter(
            [
                FakeResponse({"choices": [{"message": {"content": ""}}]}),
                FakeResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {"outcome": "no_datasheet"}
                                    )
                                }
                            }
                        ]
                    }
                ),
            ]
        )

        def fake_open(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            calls.append(json.loads(request.data.decode("utf-8")))
            return next(responses)

        client = ai.OpenAICompatibleClient(settings(), opener=fake_open)
        result = client.decide(product={"name": "PV-42"})

        self.assertEqual({"outcome": "no_datasheet"}, result)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, len(calls[0]["messages"]))
        self.assertEqual(3, len(calls[1]["messages"]))
        self.assertIn("Retry once", calls[1]["messages"][-1]["content"])
        self.assertIn(
            "empty_content",
            calls[1]["messages"][-1]["content"],
        )

    def test_repair_prompt_uses_safe_category_without_echoing_output(self) -> None:
        calls: list[dict] = []
        responses = iter(
            [
                FakeResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "sensitive-provider-output api-key"
                                }
                            }
                        ]
                    }
                ),
                FakeResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {"outcome": "no_datasheet"}
                                    )
                                }
                            }
                        ]
                    }
                ),
            ]
        )

        def fake_open(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            calls.append(json.loads(request.data.decode("utf-8")))
            return next(responses)

        client = ai.OpenAICompatibleClient(settings(), opener=fake_open)
        self.assertEqual(
            {"outcome": "no_datasheet"},
            client.decide(product={"name": "PV-42"}),
        )
        repair = calls[1]["messages"][-1]["content"]
        self.assertIn("invalid_json", repair)
        self.assertNotIn("sensitive-provider-output", repair)
        self.assertNotIn("api-key", repair)

    def test_research_retries_one_structurally_invalid_action(self) -> None:
        calls: list[dict] = []
        responses = iter(
            [
                FakeResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "action": "search_more",
                                            "gap": "invented_gap",
                                            "queries": ["PV-42 datasheet"],
                                        }
                                    )
                                }
                            }
                        ]
                    }
                ),
                FakeResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "action": "search_more",
                                            "gap": "primary_datasheet",
                                            "queries": [
                                                "PV-42 official technical manual"
                                            ],
                                        }
                                    )
                                }
                            }
                        ]
                    }
                ),
            ]
        )

        def fake_open(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            calls.append(json.loads(request.data.decode("utf-8")))
            return next(responses)

        client = ai.OpenAICompatibleClient(settings(), opener=fake_open)
        action = client.next_research_action(product={"model": "PV-42"})

        self.assertIsInstance(action, ai.SearchMoreAction)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, client.last_research_provider_requests)
        self.assertIn(
            "action_contract",
            calls[1]["messages"][-1]["content"],
        )

    def test_research_never_repairs_invalid_structure_more_than_once(self) -> None:
        calls = 0

        def fake_open(_request: object, *, timeout: float) -> FakeResponse:
            nonlocal calls
            del timeout
            calls += 1
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "action": "search_more",
                                        "gap": "invalid",
                                        "queries": ["PV-42 datasheet"],
                                    }
                                )
                            }
                        }
                    ]
                }
            )

        client = ai.OpenAICompatibleClient(settings(), opener=fake_open)
        with self.assertRaises(ai.AIInvalidOutputError):
            client.research(product={"model": "PV-42"})
        self.assertEqual(2, calls)
        self.assertEqual(2, client.last_research_provider_requests)

    def test_validation_feedback_allows_one_corrected_final_publish_proposal(
        self,
    ) -> None:
        calls = 0
        response = {
            "action": "final",
            "decision": {"outcome": "publish"},
        }

        def fake_open(_request: object, *, timeout: float) -> FakeResponse:
            nonlocal calls
            del timeout
            calls += 1
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(response)
                            }
                        }
                    ]
                }
            )

        client = ai.OpenAICompatibleClient(settings(), opener=fake_open)
        action = client.research(
            product={"model": "PV-42"},
            validation_feedback=ai.ValidationFeedback.for_gap(
                ai.ResearchGap.INDEPENDENT_CORROBORATION
            ),
        )

        self.assertIsInstance(action, ai.FinalAction)
        self.assertEqual("publish", action.decision["outcome"])
        self.assertEqual(1, calls)

    def test_redirects_are_disabled(self) -> None:
        self.assertTrue(
            any(
                isinstance(handler, ai._NoRedirectHandler)
                for handler in ai._NO_REDIRECT_OPENER.handlers
            )
        )
        handler = ai._NoRedirectHandler()
        request = ai.urllib.request.Request(
            "https://llm.example/v1/chat/completions",
            headers={"Authorization": "Bearer secret"},
            method="POST",
        )
        headers = Message()
        headers["Location"] = "https://attacker.example/collect"

        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.redirect_request(
                request,
                io.BytesIO(b""),
                302,
                "Found",
                headers,
                headers["Location"],
            )
        self.assertEqual(302, raised.exception.code)
        self.assertEqual(
            "https://llm.example/v1/chat/completions",
            raised.exception.url,
        )

    def test_http_error_does_not_leak_key_or_response_body(self) -> None:
        secret_body = b'{"error":"sensitive provider details"}'

        def fail(_request: object, *, timeout: float) -> FakeResponse:
            raise urllib.error.HTTPError(
                "https://llm.example/v1/chat/completions",
                401,
                "Unauthorized super-secret-key",
                Message(),
                io.BytesIO(secret_body),
            )

        client = ai.OpenAICompatibleClient(settings(), opener=fail)
        with self.assertRaises(ai.AIHTTPError) as raised:
            client.decide(
                product={},
            )
        message = str(raised.exception)
        self.assertIn("401", message)
        self.assertNotIn("super-secret-key", message)
        self.assertNotIn("sensitive provider details", message)

    def test_default_opener_has_a_killable_wall_clock_deadline(self) -> None:
        before = {child.pid for child in multiprocessing.active_children()}
        started_at = time.monotonic()
        with self.assertRaises(ai.AITimeoutError) as raised:
            ai._isolated_default_provider_request(
                endpoint="https://llm.example/v1/chat/completions",
                data=b"{}",
                headers={"Authorization": "Bearer super-secret-key"},
                timeout=0.05,
                max_response_bytes=1024,
                _context=multiprocessing.get_context("spawn"),
                _worker=blocking_provider_worker,
            )

        elapsed = time.monotonic() - started_at
        self.assertLess(elapsed, 1)
        self.assertNotIn("super-secret-key", str(raised.exception))
        after = {child.pid for child in multiprocessing.active_children()}
        self.assertEqual(before, after)

    def test_local_isolation_setup_failures_are_not_network_uncertainty(
        self,
    ) -> None:
        request = {
            "endpoint": "https://llm.example/v1/chat/completions",
            "data": b"{}",
            "headers": {"Authorization": "Bearer super-secret-key"},
            "timeout": 1,
            "max_response_bytes": 1024,
        }

        with (
            mock.patch.object(
                ai.multiprocessing,
                "get_context",
                side_effect=RuntimeError("spawn unavailable"),
            ),
            self.assertRaises(ai.AILocalExecutionError) as context_error,
        ):
            ai._isolated_default_provider_request(**request)
        self.assertNotIsInstance(
            context_error.exception,
            ai.AINetworkError,
        )

        pipe_context = mock.Mock()
        pipe_context.Pipe.side_effect = OSError("file descriptors exhausted")
        with self.assertRaises(ai.AILocalExecutionError) as pipe_error:
            ai._isolated_default_provider_request(
                **request,
                _context=pipe_context,
            )
        self.assertNotIsInstance(pipe_error.exception, ai.AINetworkError)

        receive_connection = mock.Mock()
        send_connection = mock.Mock()
        process_context = mock.Mock()
        process_context.Pipe.return_value = (
            receive_connection,
            send_connection,
        )
        process_context.Process.side_effect = RuntimeError(
            "process construction failed"
        )
        with self.assertRaises(ai.AILocalExecutionError):
            ai._isolated_default_provider_request(
                **request,
                _context=process_context,
            )
        receive_connection.close.assert_called_once_with()
        send_connection.close.assert_called_once_with()

        receive_connection = mock.Mock()
        send_connection = mock.Mock()
        process = mock.Mock()
        process.start.side_effect = OSError("process start failed")
        start_context = mock.Mock()
        start_context.Pipe.return_value = (
            receive_connection,
            send_connection,
        )
        start_context.Process.return_value = process
        with self.assertRaises(ai.AILocalExecutionError) as start_error:
            ai._isolated_default_provider_request(
                **request,
                _context=start_context,
            )
        self.assertIsInstance(start_error.exception, ai.AIError)
        self.assertNotIsInstance(start_error.exception, ai.AINetworkError)
        self.assertNotIn("super-secret-key", str(start_error.exception))
        receive_connection.close.assert_called_once_with()
        send_connection.close.assert_called_once_with()
        process.close.assert_called_once_with()
        self.assertIn("AILocalExecutionError", ai.__all__)

    def test_response_body_is_hard_limited_without_content_length(self) -> None:
        class OversizedResponse:
            status = 200
            headers = Message()

            def __enter__(self) -> "OversizedResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                self.assert_size = size
                return b"x" * size

        response = OversizedResponse()
        client = ai.OpenAICompatibleClient(
            settings(max_response_bytes=1024),
            opener=lambda _request, *, timeout: response,
        )
        with self.assertRaisesRegex(ai.AIResponseError, "size limit"):
            client.decide(
                product={},
            )
        self.assertEqual(1025, response.assert_size)

    def test_declared_oversized_body_is_rejected_before_read(self) -> None:
        class DeclaredOversizedResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = Message()
                self.headers["Content-Length"] = "4097"
                self.read_called = False

            def __enter__(self) -> "DeclaredOversizedResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                self.read_called = True
                return b"{}"

        response = DeclaredOversizedResponse()
        client = ai.OpenAICompatibleClient(
            settings(max_response_bytes=4096),
            opener=lambda _request, *, timeout: response,
        )
        with self.assertRaisesRegex(ai.AIResponseError, "size limit"):
            client.decide(
                product={},
            )
        self.assertFalse(response.read_called)

    def test_malformed_provider_response_is_rejected_without_echoing_it(self) -> None:
        client = ai.OpenAICompatibleClient(
            settings(),
            opener=lambda _request, *, timeout: FakeResponse(
                {"choices": [{"message": {"content": "provider secret chatter"}}]}
            ),
        )
        with self.assertRaises(ai.AIResponseError) as raised:
            client.decide(
                product={},
            )
        self.assertNotIn("provider secret chatter", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
