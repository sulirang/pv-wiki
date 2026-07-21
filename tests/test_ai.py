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
            prompt["tavily"]["extract"]["results"][0]["url"],
        )
        self.assertNotIn("results", prompt["tavily"]["search"])
        self.assertNotIn("failed_results", prompt["tavily"]["extract"])
        self.assertNotIn("s" * 100, messages[1]["content"])
        evidence_chars = len(
            prompt["tavily"]["extract"]["results"][0]["raw_content"]
        )
        self.assertEqual(3000, evidence_chars)
        self.assertTrue(prompt["tavily"]["extract"]["results"][0]["truncated"])

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
