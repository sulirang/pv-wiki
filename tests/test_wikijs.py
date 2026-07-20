from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import render, wikijs  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        status: int = 200,
        *,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self._body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        self.closed = False
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body if size < 0 else self._body[:size]

    def close(self) -> None:
        self.closed = True


class ScriptedOpener:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Any, float, dict[str, Any]]] = []

    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        payload = json.loads(request.data.decode("utf-8"))
        self.calls.append((request, timeout, payload))
        if not self.responses:
            raise AssertionError("unexpected network call")
        return self.responses.pop(0)


def response(data: Any, *, status: int = 200) -> FakeResponse:
    return FakeResponse({"data": data}, status=status)


def page(
    *,
    content: str = "",
    title: str = "PV-42",
    description: str = "Product page",
    tags: tuple[str, ...] = ("managed-by-hermes", "product"),
    publish_start_date: str | None = None,
    publish_end_date: str | None = None,
    script_css: str | None = "",
    script_js: str | None = "",
    is_private: bool = True,
    is_published: bool = False,
) -> dict[str, Any]:
    return {
        "id": 42,
        "path": "products/acme/pv-42",
        "title": title,
        "description": description,
        "isPrivate": is_private,
        "isPublished": is_published,
        "publishStartDate": publish_start_date,
        "publishEndDate": publish_end_date,
        "scriptCss": script_css,
        "scriptJs": script_js,
        "content": content,
        "createdAt": "2026-07-01T00:00:00.000Z",
        "updatedAt": "2026-07-14T00:00:00.000Z",
        "editor": "markdown",
        "locale": "zh-cn",
        "tags": [{"tag": tag} for tag in tags],
    }


def operation(name: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"pages": {name: value}}


def success(name: str, result_page: dict[str, Any]) -> FakeResponse:
    return response(
        operation(
            name,
            {
                "responseResult": {
                    "succeeded": True,
                    "errorCode": 0,
                    "slug": "ok",
                    "message": "ok",
                },
                "page": result_page,
            },
        )
    )


class WikiJSProtocolTests(unittest.TestCase):
    def test_single_by_path_uses_graphql_and_bearer_token(self) -> None:
        existing = page(content="content")
        opener = ScriptedOpener(response({"pages": {"singleByPath": existing}}))
        client = wikijs.WikiJSClient(
            "https://wiki.example.com/", "secret-token", timeout=7, opener=opener
        )

        result = client.single_by_path("zh-cn", "/products/acme/pv-42/")

        self.assertEqual(existing, result)
        request, timeout, payload = opener.calls[0]
        self.assertEqual("https://wiki.example.com/graphql", request.full_url)
        self.assertEqual("POST", request.method)
        self.assertEqual("Bearer secret-token", request.get_header("Authorization"))
        self.assertEqual(7, timeout)
        self.assertEqual(
            {"locale": "zh-cn", "path": "products/acme/pv-42"},
            payload["variables"],
        )
        self.assertIn("singleByPath", payload["query"])
        for field in (
            "publishStartDate",
            "publishEndDate",
            "scriptCss",
            "scriptJs",
        ):
            self.assertIn(field, payload["query"])

    def test_default_transport_installs_a_redirect_rejecting_handler(self) -> None:
        transport = mock.Mock()
        transport.open.return_value = response(
            {"pages": {"singleByPath": None}}
        )
        with mock.patch.object(
            wikijs.urllib.request,
            "build_opener",
            return_value=transport,
        ) as build_opener:
            client = wikijs.WikiJSClient("https://wiki.example.com", "token")
            self.assertIsNone(client.single_by_path("en", "products/a"))

        build_opener.assert_called_once()
        handler = build_opener.call_args.args[0]
        self.assertIsInstance(handler, wikijs._NoRedirectHandler)
        self.assertIsNone(
            handler.redirect_request(
                mock.Mock(),
                mock.Mock(),
                302,
                "Found",
                {},
                "https://attacker.example/steal",
            )
        )
        redirect_request = wikijs.urllib.request.Request(
            "https://wiki.example.com/graphql",
            data=b"{}",
            headers={"Authorization": "Bearer token"},
            method="POST",
        )
        with self.assertRaises(wikijs.urllib.error.HTTPError) as redirect_error:
            handler.http_error_302(
                redirect_request,
                mock.Mock(),
                302,
                "Found",
                {"Location": "https://attacker.example/steal"},
            )
        self.assertEqual(302, redirect_error.exception.code)
        request = transport.open.call_args.args[0]
        self.assertEqual(
            "Bearer token",
            request.get_header("Authorization"),
        )
        self.assertEqual(20, transport.open.call_args.kwargs["timeout"])

    def test_http_graphql_and_malformed_data_fail_strictly(self) -> None:
        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=ScriptedOpener(response({}, status=503))
        )
        with self.assertRaises(wikijs.WikiJSHTTPError):
            client.single_by_path("en", "products/a")

        graph_errors = FakeResponse(
            {"errors": [{"message": "forbidden"}], "data": {"pages": {}}}
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=ScriptedOpener(graph_errors)
        )
        with self.assertRaisesRegex(wikijs.WikiJSGraphQLError, "forbidden"):
            client.single_by_path("en", "products/a")

        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=ScriptedOpener(FakeResponse({}))
        )
        with self.assertRaises(wikijs.WikiJSResponseError):
            client.single_by_path("en", "products/a")

    def test_new_page_visibility_options_must_be_booleans(self) -> None:
        for option, value in (
            ("new_page_private", 1),
            ("new_page_published", "false"),
        ):
            with self.subTest(option=option):
                with self.assertRaisesRegex(
                    wikijs.WikiJSConfigError,
                    f"{option} must be a boolean",
                ):
                    wikijs.WikiJSClient(
                        "https://wiki.example.com",
                        "token",
                        opener=ScriptedOpener(),
                        **{option: value},
                    )

    def test_declared_oversized_response_is_rejected_before_reading(self) -> None:
        oversized = FakeResponse(
            {"data": {"pages": {"singleByPath": None}}},
            headers={"Content-Length": "33"},
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com",
            "token",
            opener=ScriptedOpener(oversized),
        )

        with mock.patch.object(wikijs, "MAX_RESPONSE_BYTES", 32):
            with self.assertRaisesRegex(
                wikijs.WikiJSResponseError,
                "maximum allowed size",
            ):
                client.single_by_path("en", "products/a")

        self.assertEqual([], oversized.read_sizes)
        self.assertTrue(oversized.closed)

    def test_actual_oversized_response_is_rejected_with_bounded_read(self) -> None:
        oversized = FakeResponse({}, raw_body=b"x" * 33)
        client = wikijs.WikiJSClient(
            "https://wiki.example.com",
            "token",
            opener=ScriptedOpener(oversized),
        )

        with mock.patch.object(wikijs, "MAX_RESPONSE_BYTES", 32):
            with self.assertRaisesRegex(
                wikijs.WikiJSResponseError,
                "maximum allowed size",
            ):
                client.single_by_path("en", "products/a")

        self.assertEqual([33], oversized.read_sizes)
        self.assertTrue(oversized.closed)

    def test_invalid_content_length_is_rejected_before_reading(self) -> None:
        invalid = FakeResponse(
            {"data": {"pages": {"singleByPath": None}}},
            headers={"Content-Length": "not-an-integer"},
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com",
            "token",
            opener=ScriptedOpener(invalid),
        )

        with self.assertRaisesRegex(
            wikijs.WikiJSResponseError,
            "invalid Content-Length",
        ):
            client.single_by_path("en", "products/a")

        self.assertEqual([], invalid.read_sizes)
        self.assertTrue(invalid.closed)

    def test_response_result_succeeded_is_required(self) -> None:
        failed = response(
            operation(
                "create",
                {
                    "responseResult": {
                        "succeeded": False,
                        "errorCode": 6005,
                        "slug": "PageIllegalPath",
                        "message": "illegal path",
                    },
                    "page": None,
                },
            )
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=ScriptedOpener(failed)
        )
        with self.assertRaises(wikijs.WikiJSOperationError) as raised:
            client.create_page("products/a", "en", "A", "", "content", [])
        self.assertEqual(6005, raised.exception.error_code)

    def test_direct_create_and_update_default_to_private_unpublished(self) -> None:
        opener = ScriptedOpener(
            success("create", page(content="created")),
            response({"pages": {"checkConflicts": False}}),
            success("update", page(content="updated")),
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=opener
        )

        client.create_page("products/a", "en", "A", "", "content", [])
        client.update_page(
            42,
            "products/a",
            "en",
            "A",
            "",
            "updated content",
            [],
            updated_at="2026-07-14T00:00:00.000Z",
            publish_start_date=None,
            publish_end_date=None,
            script_css="",
            script_js="",
        )

        create_variables = opener.calls[0][2]["variables"]
        update_variables = opener.calls[2][2]["variables"]
        self.assertIs(create_variables["isPrivate"], True)
        self.assertIs(create_variables["isPublished"], False)
        self.assertIs(update_variables["isPrivate"], True)
        self.assertIs(update_variables["isPublished"], False)


class WikiJSUpsertTests(unittest.TestCase):
    def test_create_uses_exact_lookup_and_all_page_fields(self) -> None:
        created = page(content=render.merge_auto_block("", "generated"))
        opener = ScriptedOpener(
            response({"pages": {"singleByPath": None}}),
            success("create", created),
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=opener
        )

        result = client.upsert_page(
            "products/acme/pv-42",
            "zh-cn",
            "PV-42",
            "Product page",
            "generated",
            ["product", "managed-by-hermes", "product"],
        )

        self.assertEqual("created", result["action"])
        variables = opener.calls[1][2]["variables"]
        self.assertEqual(
            {
                "content",
                "description",
                "editor",
                "isPrivate",
                "isPublished",
                "locale",
                "path",
                "publishEndDate",
                "publishStartDate",
                "scriptCss",
                "scriptJs",
                "tags",
                "title",
            },
            set(variables),
        )
        self.assertEqual("markdown", variables["editor"])
        self.assertIs(variables["isPrivate"], True)
        self.assertIs(variables["isPublished"], False)
        self.assertIsNone(variables["publishStartDate"])
        self.assertIsNone(variables["publishEndDate"])
        self.assertEqual("", variables["scriptCss"])
        self.assertEqual("", variables["scriptJs"])
        self.assertEqual(
            ["managed-by-hermes", "product"],
            variables["tags"],
        )
        self.assertEqual(1, variables["content"].count(render.AUTO_BEGIN))

    def test_create_uses_configured_new_page_visibility(self) -> None:
        opener = ScriptedOpener(
            response({"pages": {"singleByPath": None}}),
            success(
                "create",
                page(content="created", is_private=False, is_published=True),
            ),
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com",
            "token",
            new_page_private=False,
            new_page_published=True,
            opener=opener,
        )

        result = client.upsert_page(
            "products/acme/pv-42",
            "zh-cn",
            "PV-42",
            "Product page",
            "generated",
            ["managed-by-hermes", "product"],
        )

        self.assertEqual("created", result["action"])
        variables = opener.calls[1][2]["variables"]
        self.assertIs(variables["isPrivate"], False)
        self.assertIs(variables["isPublished"], True)

    def test_update_checks_conflict_and_preserves_human_sections(self) -> None:
        prefix = "Human introduction\n\n"
        suffix = "\n\nHuman notes\n"
        current = prefix + render.AUTO_BEGIN + "\nold\n" + render.AUTO_END + suffix
        existing = page(
            content=current,
            tags=(
                "brand-old",
                "category-old",
                "human-review",
                "product",
                "source-mirror",
            ),
            publish_start_date="2026-08-01T00:00:00.000Z",
            publish_end_date="2026-12-31T23:59:59.000Z",
            script_css=".manual { color: red; }",
            script_js="window.manual = true;",
            is_private=False,
            is_published=True,
        )
        updated = page(content="server value")
        opener = ScriptedOpener(
            response({"pages": {"singleByPath": existing}}),
            response({"pages": {"checkConflicts": False}}),
            success("update", updated),
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=opener
        )

        result = client.upsert_page(
            "products/acme/pv-42",
            "zh-cn",
            "PV-42 revised",
            "Revised description",
            render.AUTO_BEGIN + "\nnew\n" + render.AUTO_END,
            [
                "brand-new",
                "category-new",
                "datasheet-found",
                "managed-by-hermes",
                "product",
                "source-manufacturer",
            ],
        )

        self.assertEqual("updated", result["action"])
        self.assertIn("HermesCheckPageConflicts", opener.calls[1][2]["query"])
        self.assertEqual(
            {"id": 42, "checkoutDate": "2026-07-14T00:00:00.000Z"},
            opener.calls[1][2]["variables"],
        )
        variables = opener.calls[2][2]["variables"]
        self.assertEqual(
            {
                "id",
                "content",
                "description",
                "editor",
                "isPrivate",
                "isPublished",
                "locale",
                "path",
                "publishEndDate",
                "publishStartDate",
                "scriptCss",
                "scriptJs",
                "tags",
                "title",
            },
            set(variables),
        )
        self.assertEqual(prefix + render.AUTO_BEGIN + "\nnew\n" + render.AUTO_END + suffix, variables["content"])
        self.assertIs(variables["isPrivate"], False)
        self.assertIs(variables["isPublished"], True)
        self.assertEqual(
            "2026-08-01T00:00:00.000Z",
            variables["publishStartDate"],
        )
        self.assertEqual(
            "2026-12-31T23:59:59.000Z",
            variables["publishEndDate"],
        )
        self.assertEqual(".manual { color: red; }", variables["scriptCss"])
        self.assertEqual("window.manual = true;", variables["scriptJs"])
        self.assertEqual(
            [
                "brand-new",
                "category-new",
                "datasheet-found",
                "human-review",
                "managed-by-hermes",
                "product",
                "source-manufacturer",
            ],
            variables["tags"],
        )
        self.assertNotIn("brand-old", variables["tags"])
        self.assertNotIn("category-old", variables["tags"])
        self.assertNotIn("source-mirror", variables["tags"])

    def test_conflict_refuses_update(self) -> None:
        existing = page(
            content=render.AUTO_BEGIN + "\nold\n" + render.AUTO_END + "\n"
        )
        opener = ScriptedOpener(
            response({"pages": {"singleByPath": existing}}),
            response({"pages": {"checkConflicts": True}}),
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=opener
        )

        with self.assertRaises(wikijs.WikiJSConflictError):
            client.upsert_page(
                "products/acme/pv-42",
                "zh-cn",
                "PV-42",
                "Product page",
                "new",
                ["managed-by-hermes", "product"],
            )
        self.assertEqual(2, len(opener.calls))

    def test_unchanged_page_does_not_create_a_revision(self) -> None:
        managed = render.AUTO_BEGIN + "\nsame\n" + render.AUTO_END + "\n"
        existing = page(content=managed)
        opener = ScriptedOpener(response({"pages": {"singleByPath": existing}}))
        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=opener
        )

        result = client.upsert_page(
            existing["path"],
            existing["locale"],
            existing["title"],
            existing["description"],
            managed,
            ["product", "managed-by-hermes"],
        )

        self.assertEqual("unchanged", result["action"])
        self.assertEqual(1, len(opener.calls))

    def test_duplicate_create_race_refetches_then_updates(self) -> None:
        existing = page(
            content=render.AUTO_BEGIN + "\nracing worker\n" + render.AUTO_END + "\n",
            title="Racing title",
        )
        duplicate = response(
            operation(
                "create",
                {
                    "responseResult": {
                        "succeeded": False,
                        "errorCode": 6002,
                        "slug": "PageAlreadyExists",
                        "message": "already exists",
                    },
                    "page": None,
                },
            )
        )
        opener = ScriptedOpener(
            response({"pages": {"singleByPath": None}}),
            duplicate,
            response({"pages": {"singleByPath": existing}}),
            response({"pages": {"checkConflicts": False}}),
            success("update", page(content="updated")),
        )
        client = wikijs.WikiJSClient(
            "https://wiki.example.com", "token", opener=opener
        )

        result = client.upsert_page(
            "products/acme/pv-42",
            "zh-cn",
            "PV-42",
            "Product page",
            "our content",
            ["managed-by-hermes", "product"],
        )

        self.assertEqual("updated", result["action"])
        self.assertEqual(5, len(opener.calls))
        self.assertIn("HermesCreatePage", opener.calls[1][2]["query"])
        self.assertIn("HermesPageByPath", opener.calls[2][2]["query"])
        self.assertIn("HermesUpdatePage", opener.calls[4][2]["query"])


if __name__ == "__main__":
    unittest.main()
