from __future__ import annotations

import io
import json
import sys
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
            }
        )
        self.assertEqual("https://primary.example/v1", direct.base_url)
        self.assertEqual("primary-key", direct.api_key)
        self.assertEqual("primary-model", direct.model)
        self.assertEqual(75, direct.timeout)
        self.assertEqual(3072, direct.max_tokens)
        self.assertNotIn("primary-key", repr(direct))

        aliases = ai.AISettings.from_env(
            {
                "LLM_BASE_URL": "https://alias.example/v1",
                "LLM_API_KEY": "alias-key",
                "LLM_MODEL": "alias-model",
            }
        )
        self.assertEqual("alias-model", aliases.model)

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
            "dual evidence is required",
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
        self.assertIn("cannot name or authorize a source or domain", rules)

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
                "action": "final",
                "decision": {"outcome": "publish"},
            },
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
                },
            },
            product={"model": "PV-42"},
        )

        self.assertEqual(0.95, action.decision["confidence"])
        self.assertEqual(1.0, action.decision["facts"][0]["confidence"])

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
            "bounded action contract",
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

    def test_validation_feedback_repairs_a_repeated_publish_into_search_more(
        self,
    ) -> None:
        calls = 0
        responses = iter(
            [
                {
                    "action": "final",
                    "decision": {"outcome": "publish"},
                },
                {
                    "action": "search_more",
                    "gap": "independent_corroboration",
                    "queries": ["PV-42 independent test report"],
                },
            ]
        )

        def fake_open(_request: object, *, timeout: float) -> FakeResponse:
            nonlocal calls
            del timeout
            calls += 1
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(next(responses))
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

        self.assertIsInstance(action, ai.SearchMoreAction)
        self.assertEqual(
            ai.ResearchGap.INDEPENDENT_CORROBORATION,
            action.gap,
        )
        self.assertEqual(2, calls)

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
