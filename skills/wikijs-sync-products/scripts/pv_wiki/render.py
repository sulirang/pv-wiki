"""Deterministic, injection-resistant Markdown rendering for product pages.

Only the block delimited by :data:`AUTO_BEGIN` and :data:`AUTO_END` belongs
to Hermes.  Call :func:`merge_auto_block` before updating an existing page so
text written by people outside that block remains byte-for-byte unchanged.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import math
import re
import unicodedata
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any


AUTO_BEGIN = "<!-- HERMES-AUTO:BEGIN -->"
AUTO_END = "<!-- HERMES-AUTO:END -->"

_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
)
_NUMERIC_HOST = re.compile(
    r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\Z",
    re.IGNORECASE,
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _first(source: Any, keys: Sequence[str]) -> Any:
    for key in keys:
        value = _get(source, key)
        if value is not None and str(value).strip():
            return value
    return None


def _plain_text(value: Any) -> str:
    """Return a deterministic one-line representation of an input value."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        value = json.dumps(
            {str(key): item for key, item in value.items()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    elif isinstance(value, (set, frozenset)):
        value = ", ".join(sorted((_plain_text(item) for item in value), key=str.casefold))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        value = ", ".join(_plain_text(item) for item in value)
    text = str(value).replace("\x00", "")
    return re.sub(r"\s+", " ", text).strip()


def stable_slug(value: Any, *, max_length: int = 96) -> str:
    """Create a stable Wiki.js path component without third-party libraries."""

    if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 16:
        raise ValueError("max_length must be an integer of at least 16")
    original = _plain_text(value)
    if not original:
        raise ValueError("cannot create a slug from an empty value")

    normalized = unicodedata.normalize("NFKC", original).casefold()
    pieces: list[str] = []
    pending_separator = False
    for character in normalized:
        if character.isalnum():
            if pending_separator and pieces:
                pieces.append("-")
            pieces.append(character)
            pending_separator = False
        else:
            pending_separator = True
    slug = "".join(pieces).strip("-")
    if not slug or slug in {".", ".."}:
        raise ValueError("value does not contain a usable slug component")
    if len(slug) > max_length:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        slug = f"{slug[: max_length - len(digest) - 1].rstrip('-')}-{digest}"
    return slug


def stable_path(product: Any, prefix: str = "products") -> str:
    """Return an injective ``prefix/readable-id-hash`` path.

    The database ``product_id`` is preferred over descriptive fields.  A hash
    of the original identifier prevents slug collisions (``A+B`` vs ``A/B``),
    while excluding brand/family hints keeps the path stable when metadata is
    corrected later.
    """

    if not isinstance(prefix, str) or not prefix.strip("/"):
        raise ValueError("prefix must contain at least one path component")
    prefix_parts = [stable_slug(part) for part in prefix.strip("/").split("/")]

    identity = _first(
        product,
        (
            "product_id",
            "id",
            "model",
            "model_number",
            "part_number",
            "mpn",
            "sku",
            "product_code",
            "code",
            "product_name",
            "name",
            "title",
        ),
    )
    if identity is None:
        raise ValueError("product needs a stable id, model, part number, or name")

    raw_identity = _plain_text(identity)
    digest = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()[:10]
    component = f"{stable_slug(raw_identity, max_length=80)}-{digest}"
    return "/".join([*prefix_parts, component])


def validate_public_http_url(url: str) -> str:
    """Validate and normalize a public HTTP(S) URL for a Markdown link.

    Literal loopback, private, link-local, reserved, multicast and unspecified
    addresses are rejected.  DNS is deliberately not resolved while rendering,
    so callers that fetch URLs must repeat SSRF checks after DNS resolution.
    """

    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string")
    candidate = url.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise ValueError("URL cannot contain control characters")
    try:
        parts = urllib.parse.urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid URL") from exc

    scheme = parts.scheme.casefold()
    hostname = (parts.hostname or "").rstrip(".").casefold()
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("only absolute HTTP(S) URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs containing credentials are not allowed")
    if hostname == "localhost" or hostname.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ValueError("local or internal URLs are not allowed")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
        # Browsers and resolvers can interpret values such as 127.1 or
        # 0x7f000001 as numeric addresses even though ipaddress rejects them.
        if _NUMERIC_HOST.fullmatch(hostname) or ":" in hostname or "%" in hostname:
            raise ValueError("ambiguous numeric IP URLs are not allowed")
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("invalid URL hostname") from exc
        labels = ascii_hostname.split(".")
        if (
            len(ascii_hostname) > 253
            or not labels
            or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
        ):
            raise ValueError("invalid URL hostname")
    else:
        if not address.is_global:
            raise ValueError("non-public IP URLs are not allowed")
        ascii_hostname = address.compressed

    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid URL port")
    host_for_url = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    default_port = (scheme == "http" and port in {None, 80}) or (
        scheme == "https" and port in {None, 443}
    )
    netloc = host_for_url if default_port else f"{host_for_url}:{port}"

    # Exclude parentheses, brackets, backslashes and whitespace from the safe
    # sets so an otherwise valid URL cannot terminate a Markdown link early.
    path = urllib.parse.quote(parts.path or "/", safe="/@:-._~!$&'*+,;=%")
    query = urllib.parse.quote(parts.query, safe="=&?/:@-._~!$'*+,;%")
    fragment = urllib.parse.quote(parts.fragment, safe="?/:@-._~!$&'*+,;=%")
    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))


def escape_table_cell(value: Any) -> str:
    """Escape untrusted text for one GitHub/Wiki.js Markdown table cell."""

    text = _plain_text(value)
    text = html.escape(text, quote=False)
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _escape_markdown_text(value: Any) -> str:
    text = html.escape(_plain_text(value), quote=False)
    text = text.replace("\\", "\\\\")
    return re.sub(r"([\[\]()*_`])", r"\\\1", text)


def markdown_link(label: Any, url: str) -> str:
    """Build a safe Markdown link or raise for a non-public URL."""

    safe_label = _escape_markdown_text(label) or "资料"
    return f"[{safe_label}]({validate_public_http_url(url)})"


def _as_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _source_from_item(item: Any, *, datasheet: bool) -> tuple[str, str, bool] | None:
    if isinstance(item, str):
        url = item
        label = "Datasheet" if datasheet else "相关资料"
    elif isinstance(item, Mapping) or any(
        hasattr(item, key)
        for key in ("url", "href", "link", "source_url", "datasheet_url")
    ):
        url = _first(item, ("url", "href", "link", "source_url", "datasheet_url"))
        if url is None:
            return None
        label = _first(item, ("title", "name", "label")) or (
            "Datasheet" if datasheet else "相关资料"
        )
        kind = _plain_text(_get(item, "kind", "")).casefold()
        datasheet = datasheet or "datasheet" in kind or bool(_get(item, "is_datasheet", False))
    else:
        raise TypeError("source entries must be URLs or mappings")
    normalized_url = validate_public_http_url(str(url))
    return _plain_text(label), normalized_url, datasheet


def _collect_sources(decision: Any) -> list[tuple[str, str, bool]]:
    candidates: list[tuple[Any, bool]] = []
    for key in ("datasheet_url", "datasheet", "selected_datasheet"):
        candidates.extend((item, True) for item in _as_items(_get(decision, key)))
    candidates.extend((item, True) for item in _as_items(_get(decision, "datasheets")))
    for key in ("source_url", "sources", "related_links", "references"):
        candidates.extend((item, False) for item in _as_items(_get(decision, key)))
    for fact in _as_items(_get(decision, "facts")):
        if not isinstance(fact, Mapping):
            continue
        fact_name = _first(fact, ("name", "label")) or "规格参数"
        for url in _as_items(_get(fact, "evidence_urls")):
            candidates.append(({"title": f"{fact_name}（证据）", "url": url}, False))
    for conflict in _as_items(_get(decision, "conflicts")):
        if not isinstance(conflict, Mapping):
            continue
        field = _first(conflict, ("field", "name")) or "冲突项"
        for url in _as_items(_get(conflict, "source_urls")):
            candidates.append(({"title": f"{field}（冲突证据）", "url": url}, False))

    # Validate every supplied URL before deduplication.  Invalid search output
    # must fail closed rather than being silently copied into the wiki.
    normalized = []
    for item, is_datasheet in candidates:
        source = _source_from_item(item, datasheet=is_datasheet)
        if source is not None:
            normalized.append(source)
    normalized.sort(key=lambda item: (not item[2], item[1].casefold(), item[0].casefold()))

    unique: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for source in normalized:
        if source[1] not in seen:
            seen.add(source[1])
            unique.append(source)
    return unique


def _specifications(product: Any, decision: Any) -> list[tuple[str, Any]]:
    merged: dict[str, Any] = {}
    for owner in (product, decision):
        value = _first(owner, ("specifications", "specs", "attributes", "facts"))
        if isinstance(value, Mapping):
            for key, item in value.items():
                merged[_plain_text(key)] = item
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                key = _first(item, ("name", "key", "label", "parameter"))
                item_value = _first(item, ("value", "content", "text"))
                if key is not None and item_value is not None:
                    unit = _first(item, ("unit", "units"))
                    if unit is not None:
                        item_value = f"{_plain_text(item_value)} {_plain_text(unit)}"
                    merged[_plain_text(key)] = item_value
    return sorted(merged.items(), key=lambda item: (item[0].casefold(), item[0]))


def _product_rows(product: Any) -> list[tuple[str, Any]]:
    fields = (
        ("产品 ID", ("product_id", "id")),
        ("品牌/制造商", ("manufacturer", "brand", "vendor", "maker", "brand_code")),
        (
            "型号/料号",
            (
                "model",
                "model_number",
                "part_number",
                "mpn",
                "sku",
                "product_code",
                "code",
            ),
        ),
        ("产品名称", ("product_name", "name", "title")),
        ("产品系列", ("family_code", "family")),
        ("类别", ("category", "product_type", "type")),
        ("计量单位", ("unit_of_measure", "uom", "unit")),
        ("数据库描述", ("description",)),
    )
    rows = []
    for label, aliases in fields:
        value = _first(product, aliases)
        if value is not None:
            rows.append((label, value))
    return rows


def _checked_at(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return _plain_text(value)


def render_product_page(
    product: Any,
    decision: Any | None,
    checked_at: Any | None = None,
) -> str:
    """Render one complete Hermes-managed Markdown block.

    No current timestamp is generated implicitly; omitting ``checked_at``
    therefore produces byte-identical output for identical inputs.
    """

    decision = {} if decision is None else decision
    title = _first(product, ("product_name", "name", "title", "model", "product_id", "id"))
    if title is None:
        raise ValueError("product needs a display name, model, or id")

    lines = [
        AUTO_BEGIN,
        f"# {_escape_markdown_text(title)}",
        "",
        "> 此区块由 Hermes 自动维护；请在标记区块外添加人工内容。",
        "",
        "## 产品目录信息",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
    ]
    rows = _product_rows(product)
    if rows:
        lines.extend(
            f"| {escape_table_cell(label)} | {escape_table_cell(value)} |"
            for label, value in rows
        )
    else:  # pragma: no cover - title guarantees a useful row for normal inputs
        lines.append("| 产品 | 未提供 |")

    specifications = _specifications(product, decision)
    if specifications:
        lines.extend(["", "## 规格参数", "", "| 参数 | 值 |", "| --- | --- |"])
        lines.extend(
            f"| {escape_table_cell(key)} | {escape_table_cell(value)} |"
            for key, value in specifications
        )

    sources = _collect_sources(decision)
    datasheets = [source for source in sources if source[2]]
    related = [source for source in sources if not source[2]]
    lines.extend(["", "## Datasheet", ""])
    if datasheets:
        lines.extend(f"- {markdown_link(label, url)}" for label, url, _ in datasheets)
    else:
        lines.append("- 未找到可验证的公开 datasheet。")

    if related:
        lines.extend(["", "## 相关资料", ""])
        lines.extend(f"- {markdown_link(label, url)}" for label, url, _ in related)

    conflicts = [
        item
        for item in _as_items(_get(decision, "conflicts"))
        if isinstance(item, Mapping)
    ]
    if conflicts:
        lines.extend(["", "## 未解决的来源冲突", ""])
        for item in sorted(
            conflicts,
            key=lambda value: _plain_text(_get(value, "field", "")).casefold(),
        ):
            field = _first(item, ("field", "name")) or "冲突项"
            values = " / ".join(
                _plain_text(value) for value in _as_items(_get(item, "values"))
            )
            urls = [
                validate_public_http_url(str(url))
                for url in _as_items(_get(item, "source_urls"))
            ]
            links = ", ".join(
                markdown_link(f"来源 {index}", url)
                for index, url in enumerate(urls, start=1)
            )
            suffix = f"（{links}）" if links else ""
            lines.append(
                f"- **{_escape_markdown_text(field)}**："
                f"{_escape_markdown_text(values)}{suffix}"
            )

    summary = _first(
        decision,
        ("summary", "reasoning", "assessment", "notes", "decision_notes"),
    )
    confidence = _get(decision, "confidence")
    outcome = _get(decision, "outcome")
    checked = _checked_at(checked_at)
    if summary is not None or confidence is not None or outcome is not None or checked:
        lines.extend(["", "## 检索判定", ""])
        if summary is not None:
            lines.append(_escape_markdown_text(summary))
        if outcome is not None:
            lines.append(f"- 判定：{_escape_markdown_text(outcome)}")
        if confidence is not None:
            try:
                numeric_confidence = float(confidence)
            except (TypeError, ValueError):
                numeric_confidence = math.nan
            rendered_confidence = (
                format(numeric_confidence, ".6g")
                if math.isfinite(numeric_confidence)
                else _plain_text(confidence)
            )
            lines.append(f"- 置信度：{_escape_markdown_text(rendered_confidence)}")
        if checked:
            lines.append(f"- 核验时间：{_escape_markdown_text(checked)}")

    lines.extend(["", AUTO_END])
    return "\n".join(lines) + "\n"


def _canonical_managed_block(managed: str) -> str:
    if not isinstance(managed, str):
        raise TypeError("managed content must be a string")
    normalized = managed.replace("\r\n", "\n").replace("\r", "\n")
    begin_count = normalized.count(AUTO_BEGIN)
    end_count = normalized.count(AUTO_END)
    if begin_count == end_count == 0:
        body = normalized.strip("\n")
    elif begin_count == end_count == 1:
        begin = normalized.index(AUTO_BEGIN)
        end = normalized.index(AUTO_END, begin + len(AUTO_BEGIN))
        if normalized[:begin].strip() or normalized[end + len(AUTO_END) :].strip():
            raise ValueError("managed content cannot contain text outside its auto block")
        body = normalized[begin + len(AUTO_BEGIN) : end].strip("\n")
    else:
        raise ValueError("managed content must contain exactly one complete auto block")

    if AUTO_BEGIN in body or AUTO_END in body:
        raise ValueError("nested auto-block markers are not allowed")
    if body:
        return f"{AUTO_BEGIN}\n{body}\n{AUTO_END}"
    return f"{AUTO_BEGIN}\n{AUTO_END}"


def merge_auto_block(existing: str | None, managed: str) -> str:
    """Replace only the Hermes block, preserving all human-authored bytes."""

    block = _canonical_managed_block(managed)
    if existing is None:
        existing = ""
    if not isinstance(existing, str):
        raise TypeError("existing content must be a string or None")

    begin_count = existing.count(AUTO_BEGIN)
    end_count = existing.count(AUTO_END)
    if begin_count == end_count == 0:
        if not existing:
            return f"{block}\n"
        separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        return f"{existing}{separator}{block}\n"
    if begin_count != 1 or end_count != 1:
        raise ValueError("existing page has malformed or duplicate auto-block markers")

    begin = existing.index(AUTO_BEGIN)
    end = existing.index(AUTO_END)
    if end < begin + len(AUTO_BEGIN):
        raise ValueError("existing page has auto-block markers in the wrong order")
    end += len(AUTO_END)
    return f"{existing[:begin]}{block}{existing[end:]}"
