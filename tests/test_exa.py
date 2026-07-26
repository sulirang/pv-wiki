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

from pv_wiki import exa  # noqa: E402


class FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.headers = Message()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class ExaClientTests(unittest.TestCase):
    def test_missing_key_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(exa.ExaConfigError, "EXA_API_KEY"):
                exa.ExaClient(opener=mock.Mock())

    def test_search_normalizes_rank_content_cost_and_budget_units(self) -> None:
        seen: list[tuple[object, dict]] = []

        def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
            self.assertEqual(7, timeout)
            payload = json.loads(request.data.decode("utf-8"))
            seen.append((request, payload))
            query = payload["query"]
            return FakeResponse(
                {
                    "requestId": f"request-{len(seen)}",
                    "results": [
                        {
                            "title": "Official datasheet",
                            "url": "https://maker.example/model.pdf",
                            "highlights": ["MODEL-42", "Maximum efficiency 98.8%"],
                        },
                        {
                            "title": "Duplicate",
                            "url": "https://maker.example/model.pdf?utm_source=test",
                            "highlights": ["MODEL-42"],
                        },
                    ],
                    "costDollars": {"total": 0.007},
                }
            )

        client = exa.ExaClient(
            api_key="exa-test",
            timeout=7,
            opener=fake_urlopen,
        )
        bundle = client.search_queries(
            ['"Maker" "MODEL-42" datasheet PDF', '"MODEL-42" specifications'],
            max_results=5,
        )

        self.assertEqual(2, len(seen))
        self.assertEqual("https://api.exa.ai/search", seen[0][0].full_url)
        self.assertEqual("auto", seen[0][1]["type"])
        self.assertEqual(5, seen[0][1]["numResults"])
        self.assertIn("highlights", seen[0][1]["contents"])
        self.assertEqual("exa", bundle["provider"])
        self.assertEqual(1, len(bundle["results"]))
        self.assertIn("Maximum efficiency", bundle["results"][0]["content"])
        self.assertEqual(2, bundle["usage"]["credits"])
        self.assertEqual(0.014, bundle["usage"]["cost_dollars"])
        self.assertEqual(2, client.last_operation_completed_requests)

    def test_extract_normalizes_highlights_and_reports_per_url_failures(self) -> None:
        seen: list[tuple[object, dict]] = []

        def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
            payload = json.loads(request.data.decode("utf-8"))
            seen.append((request, payload))
            return FakeResponse(
                {
                    "requestId": "contents-1",
                    "results": [
                        {
                            "url": "https://maker.example/model.pdf",
                            "highlights": [
                                "MODEL-42 Technical Specification",
                                "Maximum efficiency 98.8%",
                            ],
                        }
                    ],
                    "statuses": [
                        {
                            "id": "https://maker.example/model.pdf",
                            "status": "success",
                        },
                        {
                            "id": "https://second.example/model",
                            "status": "error",
                            "error": {"tag": "CRAWL_TIMEOUT"},
                        },
                    ],
                    "costDollars": {"total": 0.002},
                }
            )

        client = exa.ExaClient(api_key="exa-test", opener=fake_urlopen)
        bundle = client.extract_urls(
            [
                "https://maker.example/model.pdf#specs",
                "https://second.example/model",
            ],
            "MODEL-42 complete specifications",
        )

        self.assertEqual(1, len(seen))
        request, payload = seen[0]
        self.assertEqual("https://api.exa.ai/contents", request.full_url)
        self.assertEqual(
            [
                "https://maker.example/model.pdf",
                "https://second.example/model",
            ],
            payload["urls"],
        )
        self.assertEqual(12_000, payload["highlights"]["maxCharacters"])
        self.assertIn("MODEL-42", bundle["results"][0]["raw_content"])
        self.assertEqual("CRAWL_TIMEOUT", bundle["failed_results"][0]["error"])
        self.assertEqual(2, bundle["usage"]["credits"])
        self.assertEqual(0.002, bundle["usage"]["cost_dollars"])

    def test_quota_exhaustion_rotates_keys(self) -> None:
        seen_keys: list[str | None] = []

        def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
            del timeout
            key = request.get_header("X-api-key")
            seen_keys.append(key)
            if key == "exa-first":
                raise urllib.error.HTTPError(
                    request.full_url,
                    402,
                    "Payment Required",
                    Message(),
                    io.BytesIO(b"{}"),
                )
            return FakeResponse(
                {
                    "results": [],
                    "costDollars": {"total": 0.007},
                    "requestId": "ok",
                }
            )

        client = exa.ExaClient(
            api_key=["exa-first", "exa-second"],
            opener=fake_urlopen,
        )
        client.search_queries(["MODEL-42 datasheet"])
        self.assertEqual(["exa-first", "exa-second"], seen_keys)

    def test_extract_rejects_private_or_unbounded_urls_before_network(self) -> None:
        opener = mock.Mock()
        client = exa.ExaClient(api_key="exa-test", opener=opener)

        with self.assertRaises(ValueError):
            client.extract_urls(["http://127.0.0.1/private"], "specs")
        with self.assertRaises(ValueError):
            client.extract_urls(
                [f"https://example.com/{index}" for index in range(6)],
                "specs",
            )
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
