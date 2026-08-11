from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import sys
import tempfile
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

from pv_wiki import documents, exa_mcp, exa_pool  # noqa: E402
from pv_wiki.evidence_receipt import verify_evidence_receipt  # noqa: E402


EVIDENCE_HMAC_KEY = b"pv-wiki-test-evidence-receipt-key-0001"


class FakeResponse:
    status = 200

    def __init__(
        self,
        payload: object,
        *,
        content_length: int | None = None,
    ) -> None:
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = Message()
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


def http_error(
    request: object,
    status: int,
    payload: object,
    *,
    retry_after: str | None = None,
) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        request.full_url,
        status,
        "provider error",
        headers,
        io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


class KeyLoadingTests(unittest.TestCase):
    def test_keys_file_has_priority_and_values_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exa.keys"
            path.write_text(
                "exa-file-one\nexa-file-two,exa-file-one\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            keys = exa_pool.resolve_api_keys(
                environ={
                    "EXA_API_KEYS_FILE": str(path),
                    "EXA_API_KEYS": "exa-env-ignored",
                    "EXA_API_KEY": "exa-single-ignored",
                }
            )

        self.assertEqual(["exa-file-one", "exa-file-two"], keys)

    def test_multi_key_environment_falls_back_to_single_key(self) -> None:
        self.assertEqual(
            ["exa-one", "exa-two"],
            exa_pool.resolve_api_keys(
                environ={"EXA_API_KEYS": "exa-one, exa-two exa-one"}
            ),
        )
        self.assertEqual(
            ["exa-single"],
            exa_pool.resolve_api_keys(
                environ={"EXA_API_KEY": "exa-single"}
            ),
        )

    def test_missing_or_unreadable_key_configuration_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            exa_pool.ExaPoolConfigError,
            "At least one Exa key",
        ):
            exa_pool.resolve_api_keys(environ={})
        with self.assertRaisesRegex(
            exa_pool.ExaPoolConfigError,
            "cannot be read",
        ):
            exa_pool.resolve_api_keys(
                environ={"EXA_API_KEYS_FILE": "/missing/exa.keys"}
            )

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics")
    def test_keys_file_rejects_group_or_world_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exa.keys"
            path.write_text("exa-secret\n", encoding="utf-8")
            path.chmod(0o640)
            with self.assertRaisesRegex(
                exa_pool.ExaPoolConfigError,
                "group or world permissions",
            ):
                exa_pool.resolve_api_keys(
                    environ={"EXA_API_KEYS_FILE": str(path)}
                )

    @unittest.skipUnless(os.name == "posix", "POSIX symlink semantics")
    def test_keys_file_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.keys"
            target.write_text("exa-secret\n", encoding="utf-8")
            target.chmod(0o600)
            link = Path(directory) / "exa.keys"
            link.symlink_to(target)
            with self.assertRaisesRegex(
                exa_pool.ExaPoolConfigError,
                "cannot be read",
            ):
                exa_pool.resolve_api_keys(
                    environ={"EXA_API_KEYS_FILE": str(link)}
                )


class ExaPoolClientTests(unittest.TestCase):
    def test_successful_requests_use_true_round_robin(self) -> None:
        seen_keys: list[str | None] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            self.assertEqual(7, timeout)
            seen_keys.append(request.get_header("X-api-key"))
            return FakeResponse({"results": []})

        client = exa_pool.ExaPoolClient(
            ["exa-first", "exa-second", "exa-third"],
            timeout=7,
            opener=opener,
        )
        for _ in range(4):
            client.post("/search", {"query": "module datasheet"})

        self.assertEqual(
            ["exa-first", "exa-second", "exa-third", "exa-first"],
            seen_keys,
        )

    def test_401_disables_key_and_uses_next_healthy_key(self) -> None:
        seen_keys: list[str | None] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            key = request.get_header("X-api-key")
            seen_keys.append(key)
            if key == "exa-invalid":
                raise http_error(request, 401, {"error": "INVALID_API_KEY"})
            return FakeResponse({"results": []})

        client = exa_pool.ExaPoolClient(
            ["exa-invalid", "exa-good"],
            opener=opener,
        )
        client.post("/search", {"query": "first"})
        client.post("/search", {"query": "second"})

        self.assertEqual(
            ["exa-invalid", "exa-good", "exa-good"],
            seen_keys,
        )
        self.assertEqual(
            "invalid_api_key",
            client.pool.snapshot()[0]["disabled_reason"],
        )

    def test_key_budget_402_disables_only_that_key(self) -> None:
        seen_keys: list[str | None] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            key = request.get_header("X-api-key")
            seen_keys.append(key)
            if key == "exa-spent":
                raise http_error(
                    request,
                    402,
                    {"error": "API_KEY_BUDGET_EXCEEDED"},
                )
            return FakeResponse({"results": []})

        client = exa_pool.ExaPoolClient(
            ["exa-spent", "exa-funded"],
            opener=opener,
        )
        client.post("/contents", {"urls": ["https://example.com"]})

        self.assertEqual(["exa-spent", "exa-funded"], seen_keys)
        self.assertEqual(
            "api_key_budget_exceeded",
            client.pool.snapshot()[0]["disabled_reason"],
        )

    def test_team_budget_402_fast_fails_without_trying_more_keys(self) -> None:
        seen_keys: list[str | None] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            seen_keys.append(request.get_header("X-api-key"))
            raise http_error(
                request,
                402,
                {"error": {"code": "TEAM_BUDGET_EXCEEDED"}},
            )

        client = exa_pool.ExaPoolClient(
            ["exa-first", "exa-second"],
            opener=opener,
        )
        with self.assertRaises(exa_pool.ExaPoolHTTPError) as raised:
            client.post("/search", {"query": "module datasheet"})

        self.assertEqual(402, raised.exception.status_code)
        self.assertEqual(
            "TEAM_BUDGET_EXCEEDED",
            raised.exception.error_code,
        )
        self.assertEqual(["exa-first"], seen_keys)
        with self.assertRaisesRegex(
            exa_pool.ExaPoolUnavailableError,
            "team budget",
        ):
            client.post("/search", {"query": "must fast fail"})
        self.assertEqual(["exa-first"], seen_keys)

    def test_no_more_credits_is_a_global_402(self) -> None:
        def opener(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            raise http_error(
                request,
                402,
                {"message": "NO_MORE_CREDITS"},
            )

        client = exa_pool.ExaPoolClient(
            ["exa-first", "exa-second"],
            opener=opener,
        )
        with self.assertRaises(exa_pool.ExaPoolHTTPError) as raised:
            client.post("/search", {"query": "module datasheet"})
        self.assertEqual("NO_MORE_CREDITS", raised.exception.error_code)

    def test_unknown_402_is_not_replayed_across_the_pool(self) -> None:
        seen_keys: list[str | None] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            seen_keys.append(request.get_header("X-api-key"))
            raise http_error(request, 402, {"error": "PAYMENT_REQUIRED"})

        client = exa_pool.ExaPoolClient(
            ["exa-first", "exa-second"],
            opener=opener,
        )
        with self.assertRaises(exa_pool.ExaPoolHTTPError):
            client.post("/search", {"query": "module datasheet"})
        self.assertEqual(["exa-first"], seen_keys)

    def test_429_cools_key_rotates_and_restores_it_after_expiry(self) -> None:
        now = [1_000.0]
        seen_keys: list[str | None] = []
        first_limited = False

        def opener(request: object, *, timeout: float) -> FakeResponse:
            nonlocal first_limited
            del timeout
            key = request.get_header("X-api-key")
            seen_keys.append(key)
            if key == "exa-first" and not first_limited:
                first_limited = True
                raise http_error(
                    request,
                    429,
                    {"error": "RATE_LIMIT_EXCEEDED"},
                    retry_after="30",
                )
            return FakeResponse({"results": []})

        client = exa_pool.ExaPoolClient(
            ["exa-first", "exa-second"],
            opener=opener,
            clock=lambda: now[0],
        )
        client.post("/search", {"query": "first call"})
        client.post("/search", {"query": "during cooldown"})
        now[0] += 31
        client.post("/search", {"query": "after cooldown"})

        self.assertEqual(
            ["exa-first", "exa-second", "exa-second", "exa-first"],
            seen_keys,
        )

    def test_all_429_returns_shortest_retry_without_sleeping(self) -> None:
        retry_by_key = {"exa-first": "20", "exa-second": "8"}

        def opener(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            key = request.get_header("X-api-key")
            raise http_error(
                request,
                429,
                {"error": "RATE_LIMIT_EXCEEDED"},
                retry_after=retry_by_key[key],
            )

        client = exa_pool.ExaPoolClient(
            ["exa-first", "exa-second"],
            opener=opener,
            clock=lambda: 1_000.0,
        )
        with self.assertRaises(exa_pool.ExaPoolUnavailableError) as raised:
            client.post("/search", {"query": "module datasheet"})
        self.assertEqual(8, raised.exception.retry_after)

    def test_network_error_is_never_blindly_replayed(self) -> None:
        seen_keys: list[str | None] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            seen_keys.append(request.get_header("X-api-key"))
            raise urllib.error.URLError("connection reset")

        client = exa_pool.ExaPoolClient(
            ["exa-first", "exa-second"],
            opener=opener,
        )
        with self.assertRaisesRegex(
            exa_pool.ExaPoolNetworkError,
            "was not replayed",
        ):
            client.post("/search", {"query": "module datasheet"})
        self.assertEqual(["exa-first"], seen_keys)

    def test_non_key_http_errors_are_not_replayed(self) -> None:
        for status in (400, 403, 422, 500, 503):
            with self.subTest(status=status):
                seen_keys: list[str | None] = []

                def opener(request: object, *, timeout: float) -> FakeResponse:
                    del timeout
                    seen_keys.append(request.get_header("X-api-key"))
                    raise http_error(request, status, {"error": "UPSTREAM"})

                client = exa_pool.ExaPoolClient(
                    ["exa-first", "exa-second"],
                    opener=opener,
                )
                with self.assertRaises(exa_pool.ExaPoolHTTPError):
                    client.post("/search", {"query": "module datasheet"})
                self.assertEqual(["exa-first"], seen_keys)

    def test_errors_and_diagnostics_never_contain_raw_keys(self) -> None:
        keys = ["exa-super-secret-one", "exa-super-secret-two"]
        client = exa_pool.ExaPoolClient(keys, opener=mock.Mock())

        serialized = json.dumps(client.pool.snapshot())
        for key in keys:
            self.assertNotIn(key, serialized)

        client.pool.block_globally("TEAM_BUDGET_EXCEEDED")
        with self.assertRaises(exa_pool.ExaPoolUnavailableError) as raised:
            client.post("/search", {"query": "module datasheet"})
        for key in keys:
            self.assertNotIn(key, str(raised.exception))

    def test_only_fixed_exa_endpoints_are_allowed(self) -> None:
        opener = mock.Mock()
        client = exa_pool.ExaPoolClient("exa-key", opener=opener)
        with self.assertRaisesRegex(ValueError, "unsupported Exa endpoint"):
            client.post("https://attacker.example", {})
        opener.assert_not_called()

    def test_redirect_handler_never_replays_the_credential(self) -> None:
        handler = exa_pool._NoRedirectHandler()
        request = exa_pool.urllib.request.Request(
            "https://api.exa.ai/search",
            headers={"x-api-key": "exa-super-secret"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.redirect_request(
                request,
                io.BytesIO(b""),
                302,
                "Found",
                Message(),
                "https://attacker.example/collect",
            )
        self.assertEqual("https://api.exa.ai/search", raised.exception.url)
        self.assertNotIn("exa-super-secret", str(raised.exception))

    def test_response_size_is_bounded_before_reading(self) -> None:
        response = FakeResponse(
            {"results": []},
            content_length=exa_pool.MAX_RESPONSE_BYTES + 1,
        )
        client = exa_pool.ExaPoolClient(
            "exa-key",
            opener=lambda *_args, **_kwargs: response,
        )
        with self.assertRaisesRegex(
            exa_pool.ExaPoolResponseError,
            "exceeds",
        ):
            client.post("/search", {"query": "module datasheet"})


@unittest.skipUnless(
    importlib.util.find_spec("mcp") is not None,
    "MCP runtime is not installed",
)
class MCPMetadataTests(unittest.TestCase):
    def test_network_tools_are_read_only_open_world_operations(self) -> None:
        server = exa_mcp.build_server(
            client=object(),
            evidence_hmac_key=EVIDENCE_HMAC_KEY,
        )
        tools = asyncio.run(server.list_tools())

        self.assertEqual(
            {
                "web_search_exa",
                "web_search_advanced_exa",
                "web_fetch_exa",
            },
            {tool.name for tool in tools},
        )
        for tool in tools:
            with self.subTest(tool=tool.name):
                annotations = tool.annotations
                self.assertIsNotNone(annotations)
                self.assertTrue(annotations.read_only_hint)
                self.assertFalse(annotations.destructive_hint)
                self.assertFalse(annotations.idempotent_hint)
                self.assertTrue(annotations.open_world_hint)

    def test_fetch_returns_exact_content_and_verifiable_receipt_only_for_fetch(self) -> None:
        content = "PV-42 rated output 42 W\nsecond line"

        class Client:
            def post(self, path: str, _payload: object, *, timeout: float) -> dict:
                del timeout
                if path == "/contents":
                    return {
                        "results": [
                            {
                                "url": "HTTPS://Docs.Acme.Example:443/pv 42.pdf",
                                "title": "PV-42 datasheet",
                                "text": content,
                            }
                        ]
                    }
                return {
                    "results": [
                        {
                            "url": "https://docs.acme.example/pv-42.pdf",
                            "title": "PV-42 datasheet",
                            "highlights": ["PV-42 rated output 42 W"],
                        }
                    ]
                }

        server = exa_mcp.build_server(
            client=Client(),
            evidence_hmac_key=EVIDENCE_HMAC_KEY,
        )
        tools = server._tool_manager._tools
        fetched = tools["web_fetch_exa"].fn(
            urls=["https://docs.acme.example/pv-42.pdf"]
        )
        self.assertFalse(fetched.is_error)
        payload = json.loads(fetched.content[0].text)
        self.assertEqual(1, len(payload["results"]))
        evidence = payload["results"][0]
        self.assertEqual("https://docs.acme.example/pv%2042.pdf", evidence["url"])
        self.assertEqual(content, evidence["content"])
        verify_evidence_receipt(
            EVIDENCE_HMAC_KEY,
            url=evidence["url"],
            content=evidence["content"],
            receipt=evidence["receipt"],
        )

        searched = tools["web_search_exa"].fn(query="PV-42")
        self.assertFalse(searched.is_error)
        self.assertNotIn("receipt", searched.content[0].text)

    def test_pdf_fallback_uses_one_exa_call_and_returns_signed_v2_rows(self) -> None:
        calls: list[str] = []
        pdf_url = "https://docs.acme.example/pv-42.pdf"
        pdf_text = "PV-42 rated output 42 W\n" + ("datasheet " * 30)

        class Client:
            def post(self, path: str, _payload: object, *, timeout: float) -> dict:
                del timeout
                calls.append(path)
                return {
                    "statuses": [
                        {
                            "id": pdf_url,
                            "status": "error",
                            "source": "contents",
                            "error": {"tag": "unsupported"},
                        }
                    ],
                    "results": [],
                }

        row = documents.PDFParameterRow(
            model="PV-42",
            source_label="Rated output",
            value="42",
            unit="W",
            section="Output",
            page=2,
            order=1,
            model_quote="Type\tPV-42",
            quote="Rated output\t42 W",
        )
        extractor = mock.Mock(
            return_value=documents.PDFEvidence(
                text=pdf_text,
                requested_url=pdf_url,
                final_url=pdf_url,
                sha256="b" * 64,
                page_count=2,
                extracted_pages=2,
                truncated=False,
                parameter_rows=(row,),
                redirect_chain=(pdf_url,),
            )
        )
        server = exa_mcp.build_server(
            client=Client(),
            evidence_hmac_key=EVIDENCE_HMAC_KEY,
            pdf_extractor=extractor,
        )
        fetched = server._tool_manager._tools["web_fetch_exa"].fn(
            urls=[pdf_url],
            targetModels=["PV-42"],
        )
        self.assertFalse(fetched.is_error)
        self.assertEqual(["/contents"], calls)
        payload = json.loads(fetched.content[0].text)
        evidence = payload["results"][0]
        self.assertEqual("p001", evidence["parameter_rows"][0]["parameter_id"])
        verify_evidence_receipt(
            EVIDENCE_HMAC_KEY,
            url=evidence["url"],
            content=evidence["content"],
            receipt=evidence["receipt"],
        )

    def test_fetch_url_cap_is_transport_only_and_fails_before_exa(self) -> None:
        client = mock.Mock()
        server = exa_mcp.build_server(
            client=client,
            evidence_hmac_key=EVIDENCE_HMAC_KEY,
        )
        result = server._tool_manager._tools["web_fetch_exa"].fn(
            urls=[
                f"https://docs.acme.example/{index}.html"
                for index in range(21)
            ]
        )
        self.assertTrue(result.is_error)
        self.assertIn("transport", result.content[0].text)
        client.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
