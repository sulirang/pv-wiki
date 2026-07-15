"""Minimal Wiki.js 2.5 GraphQL client for idempotent page maintenance."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .render import merge_auto_block


DEFAULT_TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_DUPLICATE_PAGE_CODES = frozenset({6002, 6006})
_MANAGED_TAGS = frozenset(
    {"product", "datasheet-found", "managed-by-hermes"}
)
_MANAGED_TAG_PREFIXES = ("source-", "brand-", "family-")

_PAGE_FIELDS = """
id
path
title
description
isPrivate
isPublished
publishStartDate
publishEndDate
scriptCss
scriptJs
content
createdAt
updatedAt
locale
tags { tag }
""".strip()

# Subset of _PAGE_FIELDS used in mutation responses (create/update).
# Wiki.js may return null for editor, locale, scriptCss, scriptJs when
# the page is created/updated via API key, causing GraphQL to reject
# the response if those non-nullable fields are requested.
_PAGE_FIELDS_MUTATION = """
id
path
title
description
isPrivate
isPublished
publishStartDate
publishEndDate
content
createdAt
updatedAt
tags { tag }
""".strip()

_SINGLE_BY_PATH = f"""
query HermesPageByPath($locale: String!, $path: String!) {{
  pages {{
    singleByPath(locale: $locale, path: $path) {{
      {_PAGE_FIELDS}
    }}
  }}
}}
""".strip()

_CHECK_CONFLICTS = """
query HermesCheckPageConflicts($id: Int!, $checkoutDate: Date!) {
  pages {
    checkConflicts(id: $id, checkoutDate: $checkoutDate)
  }
}
""".strip()

_CREATE_PAGE = f"""
mutation HermesCreatePage(
  $content: String!
  $description: String!
  $editor: String!
  $isPrivate: Boolean!
  $isPublished: Boolean!
  $locale: String!
  $path: String!
  $publishEndDate: Date
  $publishStartDate: Date
  $scriptCss: String!
  $scriptJs: String!
  $tags: [String]!
  $title: String!
) {{
  pages {{
    create(
      content: $content
      description: $description
      editor: $editor
      isPrivate: $isPrivate
      isPublished: $isPublished
      locale: $locale
      path: $path
      publishEndDate: $publishEndDate
      publishStartDate: $publishStartDate
      scriptCss: $scriptCss
      scriptJs: $scriptJs
      tags: $tags
      title: $title
    ) {{
      responseResult {{ succeeded errorCode slug message }}
      page {{ {_PAGE_FIELDS_MUTATION} }}
    }}
  }}
}}
""".strip()

_UPDATE_PAGE = f"""
mutation HermesUpdatePage(
  $id: Int!
  $content: String!
  $description: String!
  $editor: String!
  $isPrivate: Boolean!
  $isPublished: Boolean!
  $locale: String!
  $path: String!
  $publishEndDate: Date
  $publishStartDate: Date
  $scriptCss: String
  $scriptJs: String
  $tags: [String]!
  $title: String!
) {{
  pages {{
    update(
      id: $id
      content: $content
      description: $description
      editor: $editor
      isPrivate: $isPrivate
      isPublished: $isPublished
      locale: $locale
      path: $path
      publishEndDate: $publishEndDate
      publishStartDate: $publishStartDate
      scriptCss: $scriptCss
      scriptJs: $scriptJs
      tags: $tags
      title: $title
    ) {{
      responseResult {{ succeeded errorCode slug message }}
      page {{ {_PAGE_FIELDS_MUTATION} }}
    }}
  }}
}}
""".strip()


class WikiJSError(RuntimeError):
    """Base class for Wiki.js client failures."""


class WikiJSConfigError(WikiJSError):
    """Raised for invalid endpoint or credential configuration."""


class WikiJSHTTPError(WikiJSError):
    """Raised when Wiki.js does not return an HTTP 2xx response."""

    def __init__(self, status: int | None, message: str = "Wiki.js HTTP request failed") -> None:
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{message}{suffix}")


class WikiJSResponseError(WikiJSError):
    """Raised when a Wiki.js response is invalid or incomplete."""


class WikiJSGraphQLError(WikiJSResponseError):
    """Raised whenever the top-level GraphQL ``errors`` array is non-empty."""

    def __init__(self, errors: Any) -> None:
        self.errors = errors
        if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
            messages = []
            for item in errors:
                if isinstance(item, Mapping) and isinstance(item.get("message"), str):
                    messages.append(item["message"])
            detail = "; ".join(messages[:3])
        else:
            detail = ""
        super().__init__(f"Wiki.js GraphQL returned errors{': ' + detail if detail else ''}")


class WikiJSOperationError(WikiJSError):
    """Raised when ``responseResult.succeeded`` is not exactly ``true``."""

    def __init__(
        self,
        operation: str,
        *,
        error_code: Any = None,
        slug: Any = None,
        message: Any = None,
    ) -> None:
        self.operation = operation
        self.error_code = error_code
        self.slug = slug
        self.server_message = message
        details = []
        if error_code not in {None, ""}:
            details.append(f"code={error_code}")
        if slug not in {None, ""}:
            details.append(f"slug={slug}")
        if isinstance(message, str) and message:
            details.append(message)
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"Wiki.js {operation} failed{suffix}")


class WikiJSConflictError(WikiJSError):
    """Raised instead of overwriting a page changed since it was fetched."""

    def __init__(self, page_id: int, updated_at: str) -> None:
        self.page_id = page_id
        self.updated_at = updated_at
        super().__init__(f"Wiki.js page {page_id} changed after {updated_at}; update refused")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every HTTP redirect into ``HTTPError`` without forwarding auth."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None

    def _reject_redirect(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"Wiki.js redirects are disabled: {msg}",
            headers,
            fp,
        )

    http_error_301 = _reject_redirect
    http_error_302 = _reject_redirect
    http_error_303 = _reject_redirect
    http_error_307 = _reject_redirect
    http_error_308 = _reject_redirect


def _clean_string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    cleaned = value.strip()
    if not allow_empty and not cleaned:
        raise ValueError(f"{field} cannot be empty")
    if any(ord(character) < 32 for character in cleaned):
        raise ValueError(f"{field} cannot contain control characters")
    return cleaned


def _clean_path(path: str) -> str:
    cleaned = _clean_string(path, "path").strip("/")
    parts = cleaned.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise ValueError("path must contain safe, non-empty components")
    return "/".join(parts)


def _normalize_tags(tags: Sequence[str] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, (str, bytes, bytearray)):
        raise TypeError("tags must be a sequence of strings")
    unique: dict[str, str] = {}
    for tag in tags:
        cleaned = _clean_string(tag, "tag")
        unique.setdefault(cleaned.casefold(), cleaned)
    return sorted(unique.values(), key=lambda value: (value.casefold(), value))


def _is_managed_tag(tag: str) -> bool:
    normalized = tag.casefold()
    return normalized in _MANAGED_TAGS or normalized.startswith(
        _MANAGED_TAG_PREFIXES
    )


def _merge_page_tags(
    existing_tags: Sequence[str],
    requested_tags: Sequence[str] | None,
) -> list[str]:
    """Replace Hermes-owned tags while retaining every human-owned tag."""

    human_tags = [tag for tag in existing_tags if not _is_managed_tag(tag)]
    return _normalize_tags([*human_tags, *_normalize_tags(requested_tags)])


def _optional_graphql_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    return value


def _page_tags(page: Mapping[str, Any]) -> list[str]:
    values = page.get("tags", [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise WikiJSResponseError("Wiki.js page tags must be an array")
    tags = []
    for item in values:
        if isinstance(item, str):
            tags.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("tag"), str):
            tags.append(item["tag"])
        else:
            raise WikiJSResponseError("Wiki.js returned an invalid page tag")
    return _normalize_tags(tags)


def _error_code_as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class WikiJSClient:
    """Wiki.js 2.5 GraphQL page client using only the Python standard library."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        new_page_private: bool = True,
        new_page_published: bool = False,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise WikiJSConfigError("Wiki.js base URL is required")
        if not isinstance(token, str) or not token.strip():
            raise WikiJSConfigError("Wiki.js API token is required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise WikiJSConfigError("timeout must be greater than zero")
        if not isinstance(new_page_private, bool):
            raise WikiJSConfigError("new_page_private must be a boolean")
        if not isinstance(new_page_published, bool):
            raise WikiJSConfigError("new_page_published must be a boolean")

        try:
            parts = urllib.parse.urlsplit(base_url.strip())
            parts.port
        except ValueError as exc:
            raise WikiJSConfigError("invalid Wiki.js base URL") from exc
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise WikiJSConfigError("Wiki.js base URL must be absolute HTTP(S)")
        if parts.username is not None or parts.password is not None:
            raise WikiJSConfigError("Wiki.js base URL cannot contain credentials")
        if parts.query or parts.fragment:
            raise WikiJSConfigError("Wiki.js base URL cannot contain a query or fragment")

        path = parts.path.rstrip("/")
        if not path.endswith("/graphql"):
            path = f"{path}/graphql"
        self.endpoint = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, path or "/graphql", "", "")
        )
        self.timeout = float(timeout)
        self.new_page_private = new_page_private
        self.new_page_published = new_page_published
        self.__token = token.strip()
        if opener is None:
            # urllib otherwise copies Authorization to a redirected URL,
            # including a different origin.  Wiki.js GraphQL never needs 30x.
            self._opener = urllib.request.build_opener(_NoRedirectHandler()).open
        else:
            self._opener = opener

    def _post(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(
                {"query": query, "variables": dict(variables)},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.__token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "pv-wiki/1.0 (Wiki.js sync agent)",
            },
            method="POST",
        )
        response = None
        try:
            response = self._opener(request, timeout=self.timeout)
            status = getattr(response, "status", None)
            if status is None:
                getcode = getattr(response, "getcode", None)
                status = getcode() if callable(getcode) else None
            if not isinstance(status, int) or not 200 <= status < 300:
                raise WikiJSHTTPError(status)
            headers = getattr(response, "headers", None)
            get_header = getattr(headers, "get", None)
            declared_length = (
                get_header("Content-Length") if callable(get_header) else None
            )
            if declared_length is not None:
                try:
                    declared_bytes = int(declared_length)
                except (TypeError, ValueError) as exc:
                    raise WikiJSResponseError(
                        "Wiki.js returned an invalid Content-Length"
                    ) from exc
                if declared_bytes < 0:
                    raise WikiJSResponseError(
                        "Wiki.js returned an invalid Content-Length"
                    )
                if declared_bytes > MAX_RESPONSE_BYTES:
                    raise WikiJSResponseError(
                        "Wiki.js response exceeds the maximum allowed size"
                    )
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if not isinstance(body, bytes):
                raise WikiJSResponseError("Wiki.js response body must be bytes")
            if len(body) > MAX_RESPONSE_BYTES:
                raise WikiJSResponseError(
                    "Wiki.js response exceeds the maximum allowed size"
                )
        except urllib.error.HTTPError as exc:
            raise WikiJSHTTPError(exc.code) from None
        except WikiJSError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WikiJSHTTPError(None, "Wiki.js network request failed") from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WikiJSResponseError("Wiki.js returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise WikiJSResponseError("Wiki.js response must be a JSON object")
        errors = decoded.get("errors")
        if errors is not None and errors != [] and errors != ():
            raise WikiJSGraphQLError(errors)
        data = decoded.get("data")
        if not isinstance(data, Mapping):
            raise WikiJSResponseError("Wiki.js response is missing GraphQL data")
        return dict(data)

    @staticmethod
    def _pages(data: Mapping[str, Any]) -> Mapping[str, Any]:
        pages = data.get("pages")
        if not isinstance(pages, Mapping):
            raise WikiJSResponseError("Wiki.js response is missing pages data")
        return pages

    @staticmethod
    def _successful_operation(
        container: Any,
        operation: str,
    ) -> Mapping[str, Any]:
        if not isinstance(container, Mapping):
            raise WikiJSResponseError(f"Wiki.js {operation} response is missing")
        result = container.get("responseResult")
        if not isinstance(result, Mapping):
            raise WikiJSResponseError(
                f"Wiki.js {operation} responseResult is missing"
            )
        if result.get("succeeded") is not True:
            raise WikiJSOperationError(
                operation,
                error_code=result.get("errorCode"),
                slug=result.get("slug"),
                message=result.get("message"),
            )
        return container

    def single_by_path(self, locale: str, path: str) -> dict[str, Any] | None:
        """Look up an exact ``(locale, path)`` pair for idempotent writes."""

        locale = _clean_string(locale, "locale")
        path = _clean_path(path)
        try:
            data = self._post(_SINGLE_BY_PATH, {"locale": locale, "path": path})
        except WikiJSGraphQLError as exc:
            # Wiki.js returns a GraphQL error (not null) when the page
            # doesn't exist.  Treat that as "not found".
            msg = str(exc).casefold()
            if "does not exist" in msg or "not found" in msg:
                return None
            raise
        page = self._pages(data).get("singleByPath")
        if page is None:
            return None
        if not isinstance(page, Mapping):
            raise WikiJSResponseError("Wiki.js returned an invalid page")
        return dict(page)

    def get_page(self, path: str, locale: str) -> dict[str, Any] | None:
        """Positional convenience alias for :meth:`single_by_path`."""

        return self.single_by_path(locale, path)

    def check_conflicts(self, page_id: int, updated_at: str) -> bool:
        """Ask Wiki.js whether a page changed after ``updated_at``."""

        if isinstance(page_id, bool) or not isinstance(page_id, int):
            raise TypeError("page_id must be an integer")
        updated_at = _clean_string(updated_at, "updated_at")
        data = self._post(
            _CHECK_CONFLICTS,
            {"id": page_id, "checkoutDate": updated_at},
        )
        conflict = self._pages(data).get("checkConflicts")
        if not isinstance(conflict, bool):
            raise WikiJSResponseError("Wiki.js returned an invalid conflict result")
        return conflict

    def create_page(
        self,
        path: str,
        locale: str,
        title: str,
        description: str,
        content: str,
        tags: Sequence[str] | None = None,
        *,
        editor: str = "markdown",
        is_private: bool = True,
        is_published: bool = False,
        publish_start_date: str | None = None,
        publish_end_date: str | None = None,
        script_css: str = "",
        script_js: str = "",
    ) -> dict[str, Any]:
        """Create a page while supplying every managed page field."""

        if not isinstance(script_css, str) or not isinstance(script_js, str):
            raise TypeError("script_css and script_js must be strings on create")
        variables = self._page_variables(
            path=path,
            locale=locale,
            title=title,
            description=description,
            content=content,
            tags=tags,
            editor=editor,
            is_private=is_private,
            is_published=is_published,
            publish_start_date=publish_start_date,
            publish_end_date=publish_end_date,
            script_css=script_css,
            script_js=script_js,
        )
        data = self._post(_CREATE_PAGE, variables)
        container = self._successful_operation(
            self._pages(data).get("create"), "page create"
        )
        page = container.get("page")
        if not isinstance(page, Mapping):
            raise WikiJSResponseError("Wiki.js page create response has no page")
        return dict(page)

    def update_page(
        self,
        page_id: int,
        path: str,
        locale: str,
        title: str,
        description: str,
        content: str,
        tags: Sequence[str] | None = None,
        *,
        updated_at: str,
        publish_start_date: str | None,
        publish_end_date: str | None,
        script_css: str | None,
        script_js: str | None,
        editor: str = "markdown",
        is_private: bool = True,
        is_published: bool = False,
    ) -> dict[str, Any]:
        """Conflict-check and update a page with every managed page field."""

        if isinstance(page_id, bool) or not isinstance(page_id, int):
            raise TypeError("page_id must be an integer")
        updated_at = _clean_string(updated_at, "updated_at")
        variables = self._page_variables(
            path=path,
            locale=locale,
            title=title,
            description=description,
            content=content,
            tags=tags,
            editor=editor,
            is_private=is_private,
            is_published=is_published,
            publish_start_date=publish_start_date,
            publish_end_date=publish_end_date,
            script_css=script_css,
            script_js=script_js,
        )
        if self.check_conflicts(page_id, updated_at):
            raise WikiJSConflictError(page_id, updated_at)
        return self._update_page_mutation(page_id, variables)

    def _update_page_mutation(
        self,
        page_id: int,
        variables: Mapping[str, Any],
    ) -> dict[str, Any]:
        variables = {"id": page_id, **variables}
        data = self._post(_UPDATE_PAGE, variables)
        container = self._successful_operation(
            self._pages(data).get("update"), "page update"
        )
        page = container.get("page")
        if not isinstance(page, Mapping):
            raise WikiJSResponseError("Wiki.js page update response has no page")
        return dict(page)

    @staticmethod
    def _page_variables(
        *,
        path: str,
        locale: str,
        title: str,
        description: str,
        content: str,
        tags: Sequence[str] | None,
        editor: str,
        is_private: bool,
        is_published: bool,
        publish_start_date: str | None,
        publish_end_date: str | None,
        script_css: str | None,
        script_js: str | None,
    ) -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if not isinstance(is_private, bool) or not isinstance(is_published, bool):
            raise TypeError("is_private and is_published must be booleans")
        return {
            "content": content,
            "description": _clean_string(description, "description", allow_empty=True),
            "editor": _clean_string(editor, "editor"),
            "isPrivate": is_private,
            "isPublished": is_published,
            "locale": _clean_string(locale, "locale"),
            "path": _clean_path(path),
            "publishEndDate": _optional_graphql_string(
                publish_end_date, "publish_end_date"
            ),
            "publishStartDate": _optional_graphql_string(
                publish_start_date, "publish_start_date"
            ),
            "scriptCss": _optional_graphql_string(script_css, "script_css"),
            "scriptJs": _optional_graphql_string(script_js, "script_js"),
            "tags": _normalize_tags(tags),
            "title": _clean_string(title, "title"),
        }

    def _update_existing(
        self,
        existing: Mapping[str, Any],
        *,
        path: str,
        locale: str,
        title: str,
        description: str,
        managed_content: str,
        tags: Sequence[str] | None,
    ) -> dict[str, Any]:
        page_id = existing.get("id")
        if isinstance(page_id, bool) or not isinstance(page_id, int):
            raise WikiJSResponseError("Wiki.js page is missing an integer id")
        updated_at = existing.get("updatedAt")
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise WikiJSResponseError("Wiki.js page is missing updatedAt")
        current_content = existing.get("content")
        if not isinstance(current_content, str):
            raise WikiJSResponseError("Wiki.js page is missing Markdown content")

        editor = existing.get("editor", "markdown")
        if not isinstance(editor, str) or editor.casefold() != "markdown":
            raise WikiJSResponseError(
                "Wiki.js page is not a Markdown page; automatic update refused"
            )
        is_private = existing.get("isPrivate", False)
        is_published = existing.get("isPublished", True)
        if not isinstance(is_private, bool) or not isinstance(is_published, bool):
            raise WikiJSResponseError("Wiki.js page visibility fields are invalid")
        required_metadata = (
            "publishStartDate",
            "publishEndDate",
            "scriptCss",
            "scriptJs",
        )
        missing_metadata = [key for key in required_metadata if key not in existing]
        if missing_metadata:
            raise WikiJSResponseError(
                "Wiki.js page is missing update metadata: "
                + ", ".join(missing_metadata)
            )
        publish_start_date = _optional_graphql_string(
            existing["publishStartDate"], "publishStartDate"
        )
        publish_end_date = _optional_graphql_string(
            existing["publishEndDate"], "publishEndDate"
        )
        script_css = _optional_graphql_string(existing["scriptCss"], "scriptCss")
        script_js = _optional_graphql_string(existing["scriptJs"], "scriptJs")

        content = merge_auto_block(current_content, managed_content)
        current_tags = _page_tags(existing)
        merged_tags = _merge_page_tags(current_tags, tags)
        variables = self._page_variables(
            path=path,
            locale=locale,
            title=title,
            description=description,
            content=content,
            tags=merged_tags,
            editor=editor,
            is_private=is_private,
            is_published=is_published,
            publish_start_date=publish_start_date,
            publish_end_date=publish_end_date,
            script_css=script_css,
            script_js=script_js,
        )

        unchanged = (
            current_content == variables["content"]
            and existing.get("title") == variables["title"]
            and existing.get("description", "") == variables["description"]
            and existing.get("path") == variables["path"]
            and existing.get("locale") == variables["locale"]
            and current_tags == variables["tags"]
            and editor == variables["editor"]
            and is_private == variables["isPrivate"]
            and is_published == variables["isPublished"]
            and publish_start_date == variables["publishStartDate"]
            and publish_end_date == variables["publishEndDate"]
            and script_css == variables["scriptCss"]
            and script_js == variables["scriptJs"]
        )
        if unchanged:
            return {"action": "unchanged", "page": dict(existing)}

        page = self.update_page(
            page_id,
            variables["path"],
            variables["locale"],
            variables["title"],
            variables["description"],
            variables["content"],
            variables["tags"],
            updated_at=updated_at,
            publish_start_date=variables["publishStartDate"],
            publish_end_date=variables["publishEndDate"],
            script_css=variables["scriptCss"],
            script_js=variables["scriptJs"],
            editor=variables["editor"],
            is_private=variables["isPrivate"],
            is_published=variables["isPublished"],
        )
        return {"action": "updated", "page": page}

    def upsert_page(
        self,
        path: str,
        locale: str,
        title: str,
        description: str,
        managed_content: str,
        tags: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Create or safely update an exact Wiki.js page.

        ``managed_content`` may be a rendered auto block or its inner Markdown.
        On updates only that block is replaced.  A duplicate-create race is
        recovered by refetching the page and following the normal conflict-safe
        update path.
        """

        path = _clean_path(path)
        locale = _clean_string(locale, "locale")
        title = _clean_string(title, "title")
        description = _clean_string(description, "description", allow_empty=True)
        normalized_tags = _normalize_tags(tags)
        if not isinstance(managed_content, str):
            raise TypeError("managed_content must be a string")

        existing = self.single_by_path(locale, path)
        if existing is not None:
            return self._update_existing(
                existing,
                path=path,
                locale=locale,
                title=title,
                description=description,
                managed_content=managed_content,
                tags=normalized_tags,
            )

        content = merge_auto_block("", managed_content)
        try:
            page = self.create_page(
                path,
                locale,
                title,
                description,
                content,
                normalized_tags,
                is_private=self.new_page_private,
                is_published=self.new_page_published,
            )
        except WikiJSOperationError as exc:
            if _error_code_as_int(exc.error_code) not in _DUPLICATE_PAGE_CODES:
                raise
            # Another worker created the exact page after our lookup.  Do not
            # retry create; refetch and apply the same conflict-safe update.
            existing = self.single_by_path(locale, path)
            if existing is None:
                raise
            return self._update_existing(
                existing,
                path=path,
                locale=locale,
                title=title,
                description=description,
                managed_content=managed_content,
                tags=normalized_tags,
            )
        return {"action": "created", "page": page}
