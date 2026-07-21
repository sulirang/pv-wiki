from __future__ import annotations

import io
import json
import os
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

from pv_wiki import tavily  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        payload: dict,
        status: int = 200,
        *,
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


class BuildQueriesTests(unittest.TestCase):
    def test_build_queries_is_focused_unique_and_bounded(self) -> None:
        product = {
            "brand": "Acme",
            "part_number": "PV-42",
            "name": "High Voltage Widget",
            "category": "Power supplies",
        }

        queries = tavily.build_queries(product)

        self.assertEqual(3, len(queries))
        self.assertEqual(len(queries), len(set(queries)))
        self.assertTrue(all(len(query) <= 400 for query in queries))
        self.assertTrue(all('"Acme"' in query for query in queries))
        self.assertTrue(all('"PV-42"' in query for query in queries))
        self.assertIn("datasheet PDF", queries[0])
        self.assertIn("review user experience", queries[2])

    def test_build_queries_requires_meaningful_identity(self) -> None:
        with self.assertRaises(ValueError):
            tavily.build_queries({"id": 123})


class TavilyClientTests(unittest.TestCase):
    def test_missing_api_key_is_clear_and_does_not_touch_network(self) -> None:
        opener = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(tavily.TavilyConfigError, "TAVILY_API_KEY"):
                tavily.TavilyClient(opener=opener)
        opener.assert_not_called()

    def test_search_product_uses_safe_defaults_dedupes_and_tracks_usage(self) -> None:
        calls: list[tuple[object, float, dict]] = []

        def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
            payload = json.loads(request.data.decode("utf-8"))
            calls.append((request, timeout, payload))
            index = len(calls)
            if index == 1:
                results = [
                    {
                        "title": "Acme PV-42",
                        "url": "https://EXAMPLE.com/products/pv-42/?utm_source=test",
                        "content": "first",
                        "score": 0.6,
                    }
                ]
            elif index == 2:
                results = [
                    {
                        "title": "Official datasheet",
                        "url": "https://example.com/products/pv-42",
                        "content": "better",
                        "score": 0.95,
                    }
                ]
            else:
                results = [
                    {
                        "title": "Distributor",
                        "url": "https://parts.example.org/pv-42",
                        "content": "secondary",
                        "score": 0.5,
                    }
                ]
            return FakeResponse(
                {
                    "results": results,
                    "usage": {"credits": 1},
                    "request_id": f"request-{index}",
                }
            )

        client = tavily.TavilyClient(
            api_key="tvly-test-secret", timeout=7, opener=fake_urlopen
        )
        bundle = client.search_product(
            {"manufacturer": "Acme", "model": "PV-42"}, max_results=5
        )

        self.assertEqual(3, len(calls))
        for request, timeout, payload in calls:
            self.assertEqual("https://api.tavily.com/search", request.full_url)
            self.assertEqual("Bearer tvly-test-secret", request.get_header("Authorization"))
            self.assertEqual(7, timeout)
            self.assertEqual("basic", payload["search_depth"])
            self.assertIs(payload["include_usage"], True)
            self.assertEqual(5, payload["max_results"])

        self.assertEqual(2, len(bundle["results"]))
        self.assertEqual("better", bundle["results"][0]["content"])
        self.assertEqual(3, bundle["usage"]["credits"])
        serialized = json.dumps(bundle)
        self.assertNotIn("tvly-test-secret", serialized)

    def test_429_respects_retry_after_then_succeeds(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def fake_urlopen(_request: object, *, timeout: float) -> FakeResponse:
            nonlocal calls
            self.assertEqual(4, timeout)
            calls += 1
            if calls == 1:
                headers = Message()
                headers["Retry-After"] = "2.5"
                raise urllib.error.HTTPError(
                    "https://api.tavily.com/search",
                    429,
                    "Too Many Requests",
                    headers,
                    io.BytesIO(b"{}"),
                )
            return FakeResponse({"results": [], "usage": {"credits": 1}})

        client = tavily.TavilyClient(
            api_key="tvly-test",
            timeout=4,
            max_retries=2,
            backoff_base=0.25,
            sleep=sleeps.append,
            opener=fake_urlopen,
        )
        client.search_product({"model": "PV-42"})

        self.assertEqual(4, calls)  # one retry plus the remaining two queries
        self.assertEqual([2.5], sleeps)

    def test_429_without_retry_after_uses_exponential_backoff(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def fake_urlopen(_request: object, *, timeout: float) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise urllib.error.HTTPError(
                    "https://api.tavily.com/search",
                    429,
                    "Too Many Requests",
                    Message(),
                    io.BytesIO(b"{}"),
                )
            return FakeResponse({"results": [], "usage": {"credits": 0}})

        client = tavily.TavilyClient(
            api_key="tvly-test",
            max_retries=2,
            backoff_base=0.25,
            sleep=sleeps.append,
            opener=fake_urlopen,
        )
        client.search_product({"model": "PV-42"})

        self.assertEqual([0.25, 0.5], sleeps)

    def test_429_rotates_to_next_key_without_sleeping(self) -> None:
        authorizations: list[str | None] = []
        sleeps: list[float] = []

        def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
            authorizations.append(request.get_header("Authorization"))
            if len(authorizations) == 1:
                raise urllib.error.HTTPError(
                    "https://api.tavily.com/search",
                    429,
                    "Too Many Requests",
                    Message(),
                    io.BytesIO(b"{}"),
                )
            return FakeResponse({"results": [], "usage": {"credits": 0}})

        client = tavily.TavilyClient(
            api_key=["tvly-first", "tvly-second"],
            max_retries=1,
            sleep=sleeps.append,
            opener=fake_urlopen,
        )
        client.search_product({"model": "PV-42"})

        self.assertEqual("Bearer tvly-first", authorizations[0])
        self.assertTrue(
            all(value == "Bearer tvly-second" for value in authorizations[1:])
        )
        self.assertEqual([], sleeps)

    def test_all_rate_limited_keys_use_bounded_retry_round(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def fake_urlopen(_request: object, *, timeout: float) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise urllib.error.HTTPError(
                    "https://api.tavily.com/search",
                    429,
                    "Too Many Requests",
                    Message(),
                    io.BytesIO(b"{}"),
                )
            return FakeResponse({"results": [], "usage": {"credits": 0}})

        client = tavily.TavilyClient(
            api_key=["tvly-first", "tvly-second"],
            max_retries=1,
            backoff_base=0.25,
            sleep=sleeps.append,
            opener=fake_urlopen,
        )
        client.search_product({"model": "PV-42"})

        self.assertEqual([0.25], sleeps)

    def test_monthly_quota_rotates_keys_without_sleeping(self) -> None:
        authorizations: list[str | None] = []
        sleeps: list[float] = []

        def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
            authorization = request.get_header("Authorization")
            authorizations.append(authorization)
            if authorization == "Bearer tvly-first":
                raise urllib.error.HTTPError(
                    "https://api.tavily.com/search",
                    432,
                    "Plan Limit Exceeded",
                    Message(),
                    io.BytesIO(b"{}"),
                )
            return FakeResponse({"results": [], "usage": {"credits": 0}})

        client = tavily.TavilyClient(
            api_key=["tvly-first", "tvly-second"],
            sleep=sleeps.append,
            opener=fake_urlopen,
        )
        client.search_product({"model": "PV-42"})

        self.assertEqual("Bearer tvly-first", authorizations[0])
        self.assertTrue(
            all(value == "Bearer tvly-second" for value in authorizations[1:])
        )
        self.assertEqual([], sleeps)

    def test_all_monthly_quotas_exhausted_is_a_distinct_error(self) -> None:
        authorizations: list[str | None] = []

        def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
            authorization = request.get_header("Authorization")
            authorizations.append(authorization)
            status = 432 if authorization == "Bearer tvly-first" else 433
            raise urllib.error.HTTPError(
                "https://api.tavily.com/search",
                status,
                "Credit Limit Exceeded",
                Message(),
                io.BytesIO(b"{}"),
            )

        client = tavily.TavilyClient(
            api_key=["tvly-first", "tvly-second"],
            opener=fake_urlopen,
        )
        with self.assertRaisesRegex(
            tavily.TavilyQuotaExhaustedError,
            "monthly quota",
        ):
            client.search_product({"model": "PV-42"})

        self.assertEqual(
            ["Bearer tvly-first", "Bearer tvly-second"],
            authorizations,
        )

    def test_extract_urls_validates_caps_and_uses_tavily_only(self) -> None:
        seen: list[tuple[object, dict]] = []

        def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
            self.assertEqual(6, timeout)
            payload = json.loads(request.data.decode("utf-8"))
            seen.append((request, payload))
            return FakeResponse(
                {
                    "results": [
                        {
                            "url": "https://example.com/pv-42",
                            "raw_content": "# PV-42\nSpecifications",
                        }
                    ],
                    "failed_results": [],
                    "usage": {"credits": 1},
                    "request_id": "extract-1",
                }
            )

        client = tavily.TavilyClient(
            api_key="tvly-test", timeout=6, opener=fake_urlopen
        )
        bundle = client.extract_urls(
            ["https://example.com/pv-42#specs", "https://example.com/pv-42"],
            "PV-42 electrical specifications",
        )

        self.assertEqual(1, len(seen))
        request, payload = seen[0]
        self.assertEqual("https://api.tavily.com/extract", request.full_url)
        self.assertEqual(["https://example.com/pv-42"], payload["urls"])
        self.assertEqual("advanced", payload["extract_depth"])
        self.assertEqual(5, payload["chunks_per_source"])
        self.assertIs(payload["include_usage"], True)
        self.assertEqual("# PV-42\nSpecifications", bundle["results"][0]["raw_content"])
        json.dumps(bundle)

        with self.assertRaises(ValueError):
            client.extract_urls(
                [f"https://example.com/{index}" for index in range(6)], "specs"
            )
        with self.assertRaises(ValueError):
            client.extract_urls(["http://127.0.0.1/private"], "specs")
        with self.assertRaises(ValueError):
            client.extract_urls(["file:///etc/passwd"], "specs")

    def test_extract_rejects_ambiguous_loopback_and_nonstandard_ports(self) -> None:
        opener = mock.Mock()
        client = tavily.TavilyClient(api_key="tvly-test", opener=opener)

        for url in (
            "http://127.1/private",
            "http://2130706433/private",
            "http://0x7f000001/private",
            "https://example.com:8443/private",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    client.extract_urls([url], "specs")
        opener.assert_not_called()

    def test_redirect_handler_refuses_redirect_without_replaying_request(self) -> None:
        self.assertTrue(
            any(
                isinstance(handler, tavily._NoRedirectHandler)
                for handler in tavily._NO_REDIRECT_OPENER.handlers
            )
        )
        handler = tavily._NoRedirectHandler()
        request = tavily.urllib.request.Request(
            "https://api.tavily.com/search",
            headers={"Authorization": "Bearer tvly-secret"},
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
        self.assertEqual("https://api.tavily.com/search", raised.exception.url)

    def test_retry_after_and_exponential_sleep_are_capped_at_sixty_seconds(self) -> None:
        for retry_after, backoff_base in (("600", 0.25), (None, 600.0)):
            calls = 0
            sleeps: list[float] = []

            def fake_urlopen(_request: object, *, timeout: float) -> FakeResponse:
                nonlocal calls
                calls += 1
                if calls == 1:
                    headers = Message()
                    if retry_after is not None:
                        headers["Retry-After"] = retry_after
                    raise urllib.error.HTTPError(
                        "https://api.tavily.com/search",
                        429,
                        "Too Many Requests",
                        headers,
                        io.BytesIO(b"{}"),
                    )
                return FakeResponse({"results": [], "usage": {"credits": 0}})

            client = tavily.TavilyClient(
                api_key="tvly-test",
                max_retries=1,
                backoff_base=backoff_base,
                sleep=sleeps.append,
                opener=fake_urlopen,
            )
            client.search_product({"model": "PV-42"})
            self.assertEqual([60.0], sleeps)

    def test_response_body_is_hard_limited_without_content_length(self) -> None:
        class OversizedResponse:
            status = 200
            headers = Message()

            def __enter__(self) -> "OversizedResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                expected_size = tavily.MAX_RESPONSE_BYTES + 1
                if size != expected_size:
                    raise AssertionError(f"expected bounded read of {expected_size}, got {size}")
                return b"x" * expected_size

        client = tavily.TavilyClient(
            api_key="tvly-test",
            opener=lambda _request, *, timeout: OversizedResponse(),
        )
        with self.assertRaisesRegex(tavily.TavilyResponseError, "exceeds"):
            client.search_product({"model": "PV-42"})

    def test_content_length_over_limit_is_rejected_before_read(self) -> None:
        class DeclaredOversizedResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = Message()
                self.headers["Content-Length"] = str(tavily.MAX_RESPONSE_BYTES + 1)
                self.read_called = False

            def __enter__(self) -> "DeclaredOversizedResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                self.read_called = True
                return b"{}"

        response = DeclaredOversizedResponse()
        client = tavily.TavilyClient(
            api_key="tvly-test",
            opener=lambda _request, *, timeout: response,
        )
        with self.assertRaisesRegex(tavily.TavilyResponseError, "exceeds"):
            client.search_product({"model": "PV-42"})
        self.assertFalse(response.read_called)


if __name__ == "__main__":
    unittest.main()
