"""Bounded, SSRF-resistant retrieval and isolated text extraction for PDFs.

Exa remains the discovery and normal content provider. This module is an
optional second path for direct manufacturer datasheets: it pins an HTTPS
connection to a DNS address that was verified as public, validates the PDF
wire format, and parses the in-memory document in a resource-bounded child
process. Raw files are never written to disk or persisted in the state store.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import math
import multiprocessing
import re
import resource
import socket
import ssl
import time
import unicodedata
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .render import validate_public_http_url


PDF_EXTRACTION_CONTRACT_VERSION = "2026-07-29.2"
PDF_MAGIC = b"%PDF-"
PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/x-pdf"})
PDF_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3
READ_CHUNK_BYTES = 64 * 1024
MIN_USABLE_PDF_TEXT_CHARS = 200
MAX_LAYOUT_SCAN_LINES = 2_000
MAX_LAYOUT_LINE_CHARS = 20_000
MAX_DERIVED_LAYOUT_TABLES = 4
MAX_DERIVED_LAYOUT_COLUMNS = 12
MAX_DERIVED_LAYOUT_ROWS = 50
MAX_DERIVED_LAYOUT_CELL_CHARS = 200
MAX_DERIVED_LAYOUT_ROW_CHARS = 500
MAX_DERIVED_LAYOUT_CHARS = 12_000
MAX_MODEL_PARAMETER_TARGETS = 16
MAX_MODEL_PARAMETER_ROWS = 500
MAX_MODEL_PARAMETER_PAGES = 500
MAX_MODEL_PARAMETER_PAGE_CHARS = 200_000
MAX_LAYOUT_TABLE_MISSES = 12
MAX_LAYOUT_CONTINUATION_MISSES = 3
MAX_LAYOUT_CONTINUATION_HEADERS = 3


class PDFDocumentError(RuntimeError):
    """Base error for bounded direct-PDF processing."""


class PDFDownloadError(PDFDocumentError):
    """The remote resource could not be fetched as a safe PDF."""


class PDFParseError(PDFDocumentError):
    """The bounded parser could not produce usable page text."""


@dataclass(frozen=True, slots=True)
class PDFDownload:
    requested_url: str
    final_url: str
    content: bytes
    sha256: str
    content_type: str


@dataclass(frozen=True, slots=True)
class PDFParameterRow:
    """One locally bound value from a target model's datasheet table column."""

    model: str
    source_label: str
    value: str
    unit: str
    section: str
    page: int
    order: int
    model_quote: str
    quote: str
    table_title: str = ""
    value_state: str = "explicit"


@dataclass(frozen=True, slots=True)
class PDFText:
    text: str
    page_count: int
    extracted_pages: int
    truncated: bool
    parameter_rows: tuple[PDFParameterRow, ...] = ()


@dataclass(frozen=True, slots=True)
class PDFEvidence:
    text: str
    requested_url: str
    final_url: str
    sha256: str
    page_count: int
    extracted_pages: int
    truncated: bool
    parameter_rows: tuple[PDFParameterRow, ...] = ()


def looks_like_pdf_url(value: Any) -> bool:
    """Return whether a URL path explicitly identifies a PDF document."""

    if not isinstance(value, str):
        return False
    try:
        path = urllib.parse.unquote(urllib.parse.urlsplit(value).path)
    except ValueError:
        return False
    return path.casefold().endswith(".pdf")


def _public_addresses(
    hostname: str,
    port: int,
    *,
    resolver: Callable[..., Sequence[Any]],
) -> tuple[str, ...]:
    """Resolve a hostname and reject the whole answer if any address is private."""

    try:
        answers = resolver(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise PDFDownloadError("PDF hostname resolution failed") from exc
    addresses: list[str] = []
    for answer in answers:
        try:
            raw_address = answer[4][0]
            address = ipaddress.ip_address(raw_address)
        except (IndexError, TypeError, ValueError) as exc:
            raise PDFDownloadError(
                "PDF hostname returned an invalid network address"
            ) from exc
        if not address.is_global:
            raise PDFDownloadError(
                "PDF hostname resolved to a non-public network address"
            )
        normalized = address.compressed
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise PDFDownloadError("PDF hostname returned no network address")
    return tuple(addresses)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP destination is a prevalidated public IP."""

    def __init__(
        self,
        hostname: str,
        address: str,
        port: int,
        timeout: float,
    ) -> None:
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except BaseException:
            raw_socket.close()
            raise


def _content_type(headers: Any) -> str:
    raw = headers.get("Content-Type") if headers is not None else None
    if not isinstance(raw, str):
        return ""
    return raw.split(";", 1)[0].strip().casefold()


def _content_length(headers: Any) -> int | None:
    raw = headers.get("Content-Length") if headers is not None else None
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _request_target(parts: urllib.parse.SplitResult) -> str:
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PDFDownloadError("PDF download exceeded the configured timeout")
    return remaining


def _bound_connection_socket(
    connection: http.client.HTTPSConnection,
    deadline: float,
) -> None:
    network_socket = getattr(connection, "sock", None)
    set_timeout = getattr(network_socket, "settimeout", None)
    if callable(set_timeout):
        set_timeout(_remaining_timeout(deadline))


def download_pdf(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    connection_factory: Callable[
        [str, str, int, float], http.client.HTTPSConnection
    ] = _PinnedHTTPSConnection,
) -> PDFDownload:
    """Download one PDF over pinned public HTTPS with bounded redirects."""

    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1_000_000 <= max_bytes <= 50_000_000
    ):
        raise ValueError("max_bytes must be between 1000000 and 50000000")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not 1 <= float(timeout) <= 120
    ):
        raise ValueError("timeout must be between 1 and 120 seconds")

    requested_url = validate_public_http_url(url)
    current_url = requested_url
    deadline = time.monotonic() + float(timeout)
    for redirect_number in range(MAX_REDIRECTS + 1):
        current_url = validate_public_http_url(current_url)
        parts = urllib.parse.urlsplit(current_url)
        if parts.scheme.casefold() != "https":
            raise PDFDownloadError("direct PDF retrieval requires HTTPS")
        hostname = parts.hostname or ""
        port = parts.port or 443
        addresses = _public_addresses(
            hostname,
            port,
            resolver=resolver,
        )
        remaining = _remaining_timeout(deadline)
        connection = connection_factory(
            hostname,
            addresses[0],
            port,
            remaining,
        )
        try:
            connection.request(
                "GET",
                _request_target(parts),
                headers={
                    "Accept": "application/pdf",
                    "Accept-Encoding": "identity",
                    "User-Agent": "pv-wiki-worker/0.4",
                },
            )
            _bound_connection_socket(connection, deadline)
            response = connection.getresponse()
            status = getattr(response, "status", 0)
            headers = getattr(response, "headers", None)
            if status in PDF_REDIRECT_STATUSES:
                if redirect_number >= MAX_REDIRECTS:
                    raise PDFDownloadError("PDF redirect limit exceeded")
                location = headers.get("Location") if headers is not None else None
                if not isinstance(location, str) or not location.strip():
                    raise PDFDownloadError(
                        "PDF redirect omitted a valid Location header"
                    )
                current_url = urllib.parse.urljoin(current_url, location.strip())
                continue
            if status != 200:
                raise PDFDownloadError(
                    "PDF server returned a non-success HTTP status"
                )
            encoding = (
                headers.get("Content-Encoding")
                if headers is not None
                else None
            )
            if isinstance(encoding, str) and encoding.strip().casefold() not in {
                "",
                "identity",
            }:
                raise PDFDownloadError(
                    "compressed PDF HTTP responses are not accepted"
                )
            content_type = _content_type(headers)
            if content_type not in PDF_CONTENT_TYPES:
                raise PDFDownloadError(
                    "PDF server returned a non-PDF Content-Type"
                )
            declared_length = _content_length(headers)
            if declared_length is not None and declared_length > max_bytes:
                raise PDFDownloadError("PDF exceeds the configured size limit")
            chunks: list[bytes] = []
            received = 0
            while received <= max_bytes:
                _bound_connection_socket(connection, deadline)
                chunk = response.read(
                    min(READ_CHUNK_BYTES, max_bytes + 1 - received)
                )
                if not isinstance(chunk, bytes):
                    raise PDFDownloadError("PDF response body must be bytes")
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            if received > max_bytes:
                raise PDFDownloadError("PDF exceeds the configured size limit")
            content = b"".join(chunks)
            if PDF_MAGIC not in content[:1024]:
                raise PDFDownloadError("PDF response has an invalid file signature")
            return PDFDownload(
                requested_url=requested_url,
                final_url=current_url,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                content_type=content_type,
            )
        except PDFDocumentError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise PDFDownloadError("PDF HTTPS request failed") from exc
        finally:
            connection.close()
    raise PDFDownloadError("PDF redirect limit exceeded")


def _normalized_page_text(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    text = text.replace("\x00", "")
    text = re.sub(r"\r\n?", "\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip()


@dataclass(frozen=True, slots=True)
class _LayoutHeader:
    """One normalized multi-model header from one or two PDF layout rows."""

    end_line_index: int
    column_count: int
    normalized_row: str
    models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ParameterTableContext:
    """One target-bound table that may continue onto the adjacent PDF page."""

    header: _LayoutHeader
    model_indexes: tuple[tuple[str, int], ...]
    table_title: str
    section: str
    page: int


def _collapsed_layout_cell(value: str) -> str:
    return " ".join(value.replace("\u00a0", " ").split())


def _model_shaped_layout_token(value: str) -> bool:
    compact = "".join(character for character in value if character.isalnum())
    return (
        3 <= len(compact) <= 80
        and any(character.isalpha() for character in compact)
        and any(character.isdecimal() for character in compact)
    )


def _normalized_model_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _source_model_token(value: str) -> bool:
    return (
        len(value) <= 80
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+/\-]*", value) is not None
        and _model_shaped_layout_token(value)
    )


def _single_line_layout_header(
    lines: Sequence[str],
    line_index: int,
) -> _LayoutHeader | None:
    """Recognize a normal fixed-width header containing complete model cells."""

    if not 0 <= line_index < len(lines):
        return None
    line = lines[line_index]
    if not line.strip() or len(line) > MAX_LAYOUT_LINE_CHARS:
        return None
    tokens = list(re.finditer(r"\S+", line))
    if len(tokens) < 3:
        return None

    trailing: list[re.Match[str]] = []
    for token in reversed(tokens):
        if not _source_model_token(token.group(0)):
            break
        trailing.append(token)
    trailing.reverse()
    if not 2 <= len(trailing) <= MAX_DERIVED_LAYOUT_COLUMNS:
        return None

    label = _collapsed_layout_cell(line[: trailing[0].start()])
    label_key = label.casefold().rstrip(":")
    if not (
        label_key in {
            "type",
            "model",
            "model type",
            "models",
            "specification",
            "specifications",
            "technical specification",
            "technical specifications",
            "型号",
            "类型",
        }
        or label_key.endswith(" model")
    ):
        return None
    models = tuple(token.group(0) for token in trailing)
    if len({_normalized_model_key(model) for model in models}) != len(models):
        return None
    normalized_row = "\t".join((label, *models))
    if len(normalized_row) > MAX_DERIVED_LAYOUT_ROW_CHARS:
        return None
    return _LayoutHeader(
        end_line_index=line_index,
        column_count=len(models),
        normalized_row=normalized_row,
        models=models,
    )


def _composite_layout_header(
    lines: Sequence[str],
    line_index: int,
) -> _LayoutHeader | None:
    """Join a repeated model prefix row with its following suffix row.

    Some manufacturer PDFs draw ``SUN2000L`` and ``-4.6KTL`` as separate text
    objects on adjacent visual rows. ``pypdf`` faithfully preserves that shape,
    but the complete catalogue model then never occurs in the extracted text.
    This recognizer is deliberately narrow: the first row must end in 2-12
    identical mixed letter/digit tokens and the next non-empty row must contain
    exactly the same number of short model-shaped suffixes.
    """

    if not 0 <= line_index < len(lines):
        return None
    line = lines[line_index]
    if not line.strip() or len(line) > MAX_LAYOUT_LINE_CHARS:
        return None
    tokens = list(re.finditer(r"\S+", line))
    if len(tokens) < 3:
        return None
    base = tokens[-1].group(0)
    if not _model_shaped_layout_token(base):
        return None

    repeated: list[re.Match[str]] = []
    base_key = base.casefold()
    for token in reversed(tokens):
        if token.group(0).casefold() != base_key:
            break
        repeated.append(token)
    repeated.reverse()
    if not 2 <= len(repeated) <= MAX_DERIVED_LAYOUT_COLUMNS:
        return None
    label = _collapsed_layout_cell(line[: repeated[0].start()])
    if not label or len(label) > MAX_DERIVED_LAYOUT_CELL_CHARS:
        return None

    suffix_line_index = line_index + 1
    while (
        suffix_line_index < len(lines)
        and suffix_line_index <= line_index + 4
        and not lines[suffix_line_index].strip()
    ):
        suffix_line_index += 1
    if suffix_line_index >= len(lines) or suffix_line_index > line_index + 4:
        return None
    suffix_line = lines[suffix_line_index]
    if len(suffix_line) > MAX_LAYOUT_LINE_CHARS:
        return None
    suffixes = re.findall(r"\S+", suffix_line)
    if len(suffixes) != len(repeated):
        return None
    if any(
        len(suffix) > 80
        or re.fullmatch(r"[A-Za-z0-9_.+/\-]+", suffix) is None
        or not any(character.isdecimal() for character in suffix)
        for suffix in suffixes
    ):
        return None

    models = [
        _collapsed_layout_cell(f"{base}{suffix}")
        for suffix in suffixes
    ]
    if (
        len({model.casefold() for model in models}) != len(models)
        or any(not _model_shaped_layout_token(model) for model in models)
    ):
        return None
    normalized_row = "\t".join((label, *models))
    if len(normalized_row) > MAX_DERIVED_LAYOUT_ROW_CHARS:
        return None
    return _LayoutHeader(
        end_line_index=suffix_line_index,
        column_count=len(models),
        normalized_row=normalized_row,
        models=tuple(models),
    )


def _layout_header(
    lines: Sequence[str],
    line_index: int,
) -> _LayoutHeader | None:
    return _single_line_layout_header(
        lines,
        line_index,
    ) or _composite_layout_header(lines, line_index)


def _normalized_layout_data_row(
    line: str,
    *,
    column_count: int,
) -> str | None:
    """Conservatively turn one fixed-width row into label + model-value cells.

    The PDF layout renderer may insert a few spaces between words that belong to
    one cell. Select only the ``column_count`` largest gaps, and reject the row
    unless those gaps are clearly wider than every unselected internal gap.
    This keeps ambiguous merged or irregular rows out of the derived evidence.
    """

    stripped = line.strip()
    if (
        not stripped
        or len(stripped) > MAX_LAYOUT_LINE_CHARS
        or not 2 <= column_count <= MAX_DERIVED_LAYOUT_COLUMNS
    ):
        return None
    gaps = list(re.finditer(r"[ \u00a0]{2,}", stripped))
    if len(gaps) < column_count:
        return None
    ranked = sorted(
        enumerate(gaps),
        key=lambda item: (-len(item[1].group(0)), item[1].start()),
    )
    selected_indexes = {index for index, _match in ranked[:column_count]}
    selected = sorted(
        (match for index, match in enumerate(gaps) if index in selected_indexes),
        key=lambda match: match.start(),
    )
    selected_minimum = min(len(match.group(0)) for match in selected)
    unselected_maximum = max(
        (
            len(match.group(0))
            for index, match in enumerate(gaps)
            if index not in selected_indexes
        ),
        default=0,
    )
    if (
        unselected_maximum
        and selected_minimum < unselected_maximum * 2
    ):
        return None

    cells: list[str] = []
    start = 0
    for gap in selected:
        cells.append(_collapsed_layout_cell(stripped[start: gap.start()]))
        start = gap.end()
    cells.append(_collapsed_layout_cell(stripped[start:]))
    if (
        len(cells) != column_count + 1
        or any(not cell or len(cell) > MAX_DERIVED_LAYOUT_CELL_CHARS for cell in cells)
        or not any(character.isalpha() for character in cells[0])
    ):
        return None
    normalized_row = "\t".join(cells)
    return (
        normalized_row
        if len(normalized_row) <= MAX_DERIVED_LAYOUT_ROW_CHARS
        else None
    )


def _derived_layout_table_text(page_text: str) -> str:
    """Return bounded, marked TSV views while leaving source layout untouched."""

    if not isinstance(page_text, str) or not page_text.strip():
        return ""
    lines = page_text.splitlines()[:MAX_LAYOUT_SCAN_LINES]
    blocks: list[str] = []
    used = 0
    line_index = 0
    while (
        line_index < len(lines)
        and len(blocks) < MAX_DERIVED_LAYOUT_TABLES
        and used < MAX_DERIVED_LAYOUT_CHARS
    ):
        header = _layout_header(lines, line_index)
        if header is None:
            line_index += 1
            continue

        table_number = len(blocks) + 1
        rows = [header.normalized_row]
        scan_index = header.end_line_index + 1
        while scan_index < len(lines) and len(rows) < MAX_DERIVED_LAYOUT_ROWS:
            if _layout_header(lines, scan_index) is not None:
                break
            stripped = lines[scan_index].strip()
            if stripped.startswith("*"):
                break
            normalized_row = _normalized_layout_data_row(
                lines[scan_index],
                column_count=header.column_count,
            )
            if normalized_row is None:
                normalized_row = _shared_layout_data_row(
                    lines[scan_index],
                    column_count=header.column_count,
                )
            if normalized_row is not None:
                rows.append(_auditable_layout_data_row(normalized_row))
            scan_index += 1

        block = "\n".join(
            (
                f"[Derived PDF layout table {table_number}]",
                *rows,
                f"[End derived PDF layout table {table_number}]",
            )
        )
        separator_chars = 2 if blocks else 0
        remaining = MAX_DERIVED_LAYOUT_CHARS - used - separator_chars
        if len(block) > remaining:
            break
        blocks.append(block)
        used += separator_chars + len(block)
        line_index = max(scan_index, header.end_line_index + 1)
    return "\n\n".join(blocks)


def _normalized_target_models(
    target_models: str | Sequence[str],
) -> tuple[str, ...]:
    if isinstance(target_models, str):
        candidates: Sequence[str] = (target_models,)
    elif isinstance(target_models, Sequence) and not isinstance(
        target_models,
        (bytes, bytearray),
    ):
        candidates = target_models
    else:
        raise TypeError("target_models must be a string or a sequence of strings")
    if len(candidates) > MAX_MODEL_PARAMETER_TARGETS:
        raise ValueError(
            f"target_models must contain at most {MAX_MODEL_PARAMETER_TARGETS} entries"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            raise TypeError("target_models entries must be strings")
        model = unicodedata.normalize("NFKC", candidate).strip()
        if not model or len(model) > 200:
            raise ValueError("target model values must contain 1 to 200 characters")
        key = _normalized_model_key(model)
        if key not in seen:
            normalized.append(model)
            seen.add(key)
    return tuple(normalized)


def _text_contains_complete_model(value: str, model: str) -> bool:
    pattern = (
        r"(?<![A-Za-z0-9_.+/\-])"
        + re.escape(unicodedata.normalize("NFKC", model))
        + r"(?![A-Za-z0-9_.+/\-])"
    )
    normalized = unicodedata.normalize("NFKC", value)
    return re.search(pattern, normalized, flags=re.IGNORECASE) is not None


def _compact_title_contains_target_model(value: str, model: str) -> bool:
    """Recognize a bounded series shorthand immediately above a model header.

    A title such as ``R5-8K/9K/10K/12K-T2-15`` does not spell every complete
    model, but it contains the target's bounded model fragments in order. This
    fallback is used only for display metadata, never to bind a value column.
    """

    candidate = _collapsed_layout_cell(value)
    if (
        not candidate
        or len(candidate) > 200
        or len(candidate.split()) > 6
        or re.fullmatch(r"[A-Za-z0-9_.+/\- ]+", candidate) is None
        or "/" not in candidate
    ):
        return False
    tokens = re.findall(
        r"[A-Za-z]+\d*|\d+[A-Za-z]*",
        unicodedata.normalize("NFKC", model),
    )
    if len(tokens) < 3:
        return False
    offset = 0
    normalized_candidate = unicodedata.normalize("NFKC", candidate)
    for token in tokens:
        match = re.search(
            r"(?<![A-Za-z0-9])"
            + re.escape(token)
            + r"(?![A-Za-z0-9])",
            normalized_candidate[offset:],
            flags=re.IGNORECASE,
        )
        if match is None:
            return False
        offset += match.end()
    return True


def _layout_table_title(
    lines: Sequence[str],
    header_line_index: int,
    model_indexes: Sequence[tuple[str, int]],
) -> str:
    """Return one nearby target-bearing table title, never remote page prose."""

    previous: list[str] = []
    scan_index = header_line_index - 1
    while scan_index >= 0 and len(previous) < 3:
        value = _collapsed_layout_cell(lines[scan_index])
        if value:
            previous.append(value)
        scan_index -= 1
    for candidate in previous:
        if (
            len(candidate) <= 200
            and _layout_section_heading(candidate) is None
            and any(
                _text_contains_complete_model(candidate, model)
                or _compact_title_contains_target_model(candidate, model)
                for model, _index in model_indexes
            )
        ):
            return candidate
    return ""


def _continuation_page_header(
    line: str,
    context: _ParameterTableContext,
) -> bool:
    """Allow only a few recognizable page-header lines before a continuation."""

    value = _collapsed_layout_cell(line)
    if not value or len(value) > 160:
        return False
    key = value.casefold()
    if re.fullmatch(
        r"(?:page\s*)?[-–—]?\s*\d{1,4}\s*[-–—]?",
        key,
    ):
        return True
    if any(
        phrase in key
        for phrase in (
            "user manual",
            "technical manual",
            "datasheet",
            "data sheet",
            "product manual",
            " series",
        )
    ):
        return True
    return (
        bool(context.table_title)
        and value == context.table_title
    ) or any(
        _text_contains_complete_model(value, model)
        for model, _index in context.model_indexes
    )


def _shared_layout_data_row(
    line: str,
    *,
    column_count: int,
) -> str | None:
    """Repeat one clearly table-wide value across every model column.

    Grouped values remain ambiguous and are rejected. A shared value is accepted
    only when one dominant layout gap cleanly separates a short field label from
    one bounded scalar or short text value.
    """

    stripped = line.strip()
    if (
        not stripped
        or len(stripped) > MAX_LAYOUT_LINE_CHARS
        or not 2 <= column_count <= MAX_DERIVED_LAYOUT_COLUMNS
    ):
        return None
    gaps = list(re.finditer(r"[ \u00a0]{2,}", stripped))
    if not gaps:
        return None
    ranked = sorted(gaps, key=lambda match: -len(match.group(0)))
    selected = ranked[0]
    if len(selected.group(0)) < 4 or (
        len(ranked) > 1
        and len(selected.group(0)) < len(ranked[1].group(0)) * 2
    ):
        return None

    label = _collapsed_layout_cell(stripped[: selected.start()])
    value = _collapsed_layout_cell(stripped[selected.end() :])
    if (
        not label
        or not value
        or len(label) > MAX_DERIVED_LAYOUT_CELL_CHARS
        or len(value) > MAX_DERIVED_LAYOUT_CELL_CHARS
        or not any(character.isalpha() for character in label)
        or len(value.split()) > 12
        or value.endswith((".", "!", "?"))
    ):
        return None
    cells = (label, *((value,) * column_count))
    normalized = "\t".join(cells)
    return (
        normalized
        if len(normalized) <= MAX_DERIVED_LAYOUT_ROW_CHARS
        else None
    )


def _layout_section_heading(line: str) -> str | None:
    value = _collapsed_layout_cell(line)
    if not value or len(value) > 100 or any(character.isdecimal() for character in value):
        return None
    key = value.casefold().replace("（", "(").replace("）", ")")
    if re.fullmatch(
        r"(?:"
        r"(?:pv |dc |ac |grid |battery |backup )?(?:input|output)"
        r"(?:\s*\([^)]{1,20}\))?"
        r"|efficiency|protection|interface|communication"
        r"|general data|environmental data|mechanical data"
        r"|storage|battery|photovoltaic|operating conditions"
        r"|输入(?:（[^）]{1,20}）|\([^)]{1,20}\))?"
        r"|输出(?:（[^）]{1,20}）|\([^)]{1,20}\))?"
        r"|效率|保护|接口|通信|常规参数|环境参数|机械参数|储能|电池"
        r")",
        key,
    ):
        return value
    return None


_VALUE_UNIT_RE = re.compile(
    r"^(?P<value>.+?\d)\s*(?P<unit>"
    r"kWh|MWh|Wh|kWp|Wp|kVA|VA|kW|MW|W|mA|A|mV|kV|V|"
    r"MHz|kHz|Hz|dBA|dB|kg|mm|cm|km|m|°C|℃|%|years?|year"
    r")$",
    flags=re.IGNORECASE,
)


def _parameter_value_and_unit(
    source_label: str,
    raw_value: str,
) -> tuple[str, str]:
    bracket_units = [
        _collapsed_layout_cell(candidate)
        for candidate in re.findall(r"\[([^\[\]]{1,20})\]", source_label)
    ]
    unit = ""
    for candidate in reversed(bracket_units):
        if candidate.casefold() not in {"dc", "ac", "stc", "h*w*d", "w*h*d"}:
            unit = candidate
            break
    value = _collapsed_layout_cell(raw_value)
    if unit:
        unit_suffix = re.fullmatch(
            rf"(.+?)\s*{re.escape(unit)}",
            value,
            flags=re.IGNORECASE,
        )
        if unit_suffix is not None:
            value = unit_suffix.group(1).strip()
    else:
        match = _VALUE_UNIT_RE.fullmatch(value)
        if match is not None:
            value = match.group("value").strip()
            unit = match.group("unit")
    return value, unit


def _auditable_layout_data_row(normalized_row: str) -> str:
    """Carry a label-level unit into each value cell of a normalized TSV row."""

    cells = normalized_row.split("\t")
    if len(cells) < 2:
        return normalized_row
    source_label = cells[0]
    values = []
    for raw_value in cells[1:]:
        value, unit = _parameter_value_and_unit(source_label, raw_value)
        values.append(f"{value} {unit}".strip())
    return "\t".join((source_label, *values))


def _parameter_input_pages(
    pages_or_text: str | Mapping[int, str] | Sequence[tuple[int, str]],
) -> tuple[tuple[int, str], ...]:
    if isinstance(pages_or_text, str):
        if len(pages_or_text) > MAX_MODEL_PARAMETER_PAGE_CHARS:
            raise ValueError(
                "page-labelled parameter text exceeds the configured character limit"
            )
        matches = tuple(
            re.finditer(
                r"(?ms)^\[PDF page ([1-9]\d*)/[1-9]\d*\]\n"
                r"(.*?)^\[End PDF page \1\]$",
                pages_or_text,
            )
        )
        if matches:
            return tuple(
                (int(match.group(1)), match.group(2))
                for match in matches
            )
        return ((1, _normalized_page_text(pages_or_text)),)

    if isinstance(pages_or_text, Mapping):
        raw_items = tuple(pages_or_text.items())
    elif isinstance(pages_or_text, Sequence) and not isinstance(
        pages_or_text,
        (bytes, bytearray),
    ):
        raw_items = tuple(pages_or_text)
    else:
        raise TypeError(
            "pages_or_text must be page-labelled text, a page mapping, "
            "or a sequence of (page, text) pairs"
        )
    if len(raw_items) > MAX_MODEL_PARAMETER_PAGES:
        raise ValueError(
            f"pages_or_text must contain at most {MAX_MODEL_PARAMETER_PAGES} pages"
        )

    pages: list[tuple[int, str]] = []
    seen_pages: set[int] = set()
    for item in raw_items:
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes, bytearray))
            or len(item) != 2
        ):
            raise TypeError("each parameter page must be a (page, text) pair")
        page, text = item
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or not 1 <= page <= MAX_MODEL_PARAMETER_PAGES
        ):
            raise ValueError(
                f"parameter page numbers must be between 1 and {MAX_MODEL_PARAMETER_PAGES}"
            )
        if page in seen_pages:
            raise ValueError("parameter page numbers must be unique")
        if not isinstance(text, str):
            raise TypeError("parameter page text must be a string")
        normalized = _normalized_page_text(text)
        if len(normalized) > MAX_MODEL_PARAMETER_PAGE_CHARS:
            raise ValueError(
                "parameter page text exceeds the configured character limit"
            )
        pages.append((page, normalized))
        seen_pages.add(page)
    return tuple(sorted(pages))


def _model_indexes_for_header(
    header: _LayoutHeader,
    target_models: Sequence[str],
) -> tuple[tuple[str, int], ...]:
    indexes = {
        _normalized_model_key(model): index
        for index, model in enumerate(header.models)
    }
    return tuple(
        (target, indexes[_normalized_model_key(target)])
        for target in target_models
        if _normalized_model_key(target) in indexes
    )


def _parameter_layout_row(
    line: str,
    *,
    column_count: int,
) -> tuple[str, str] | None:
    """Return one auditable row and whether its value cells were explicit."""

    normalized = _normalized_layout_data_row(
        line,
        column_count=column_count,
    )
    value_state = "explicit"
    if normalized is None:
        normalized = _shared_layout_data_row(
            line,
            column_count=column_count,
        )
        value_state = "merged_shared"
    if normalized is None:
        return None
    return _auditable_layout_data_row(normalized), value_state


def _parameter_rows_from_layout_line(
    line: str,
    *,
    page: int,
    context: _ParameterTableContext,
) -> list[PDFParameterRow]:
    parsed = _parameter_layout_row(
        line,
        column_count=context.header.column_count,
    )
    if parsed is None:
        return []
    normalized_row, value_state = parsed
    cells = normalized_row.split("\t")
    source_label = cells[0]
    rows: list[PDFParameterRow] = []
    for model, model_index in context.model_indexes:
        value, unit = _parameter_value_and_unit(
            source_label,
            cells[model_index + 1],
        )
        # Empty target cells can mean N/A, a visual span, or failed extraction.
        # Never choose among those meanings without an explicit safe value.
        if not value:
            continue
        rows.append(
            PDFParameterRow(
                model=model,
                source_label=source_label,
                value=value,
                unit=unit,
                section=context.section,
                page=page,
                order=0,
                model_quote=context.header.normalized_row,
                quote=normalized_row,
                table_title=context.table_title,
                value_state=value_state,
            )
        )
    return rows


def _scan_parameter_table(
    lines: Sequence[str],
    *,
    start_index: int,
    page: int,
    context: _ParameterTableContext,
    continuation: bool,
) -> tuple[list[PDFParameterRow], int, _ParameterTableContext | None]:
    """Scan one bounded table, optionally inherited from the adjacent page."""

    rows: list[PDFParameterRow] = []
    scan_index = start_index
    section = context.section
    misses = 0
    header_lines = 0
    started = not continuation
    activity = not continuation
    miss_limit = (
        MAX_LAYOUT_CONTINUATION_MISSES
        if continuation
        else MAX_LAYOUT_TABLE_MISSES
    )
    while scan_index < len(lines) and len(rows) < MAX_MODEL_PARAMETER_ROWS:
        if _layout_header(lines, scan_index) is not None:
            return rows, scan_index, None
        stripped = lines[scan_index].strip()
        if not stripped:
            scan_index += 1
            continue
        if stripped.startswith("*"):
            return rows, scan_index, None

        section_heading = _layout_section_heading(stripped)
        if section_heading is not None:
            section = section_heading
            context = _ParameterTableContext(
                header=context.header,
                model_indexes=context.model_indexes,
                table_title=context.table_title,
                section=section,
                page=page,
            )
            started = True
            activity = True
            misses = 0
            scan_index += 1
            continue

        if continuation and not started and (
            header_lines < MAX_LAYOUT_CONTINUATION_HEADERS
            and _continuation_page_header(stripped, context)
        ):
            header_lines += 1
            scan_index += 1
            continue

        active_context = _ParameterTableContext(
            header=context.header,
            model_indexes=context.model_indexes,
            table_title=context.table_title,
            section=section,
            page=page,
        )
        line_rows = _parameter_rows_from_layout_line(
            lines[scan_index],
            page=page,
            context=active_context,
        )
        if line_rows:
            rows.extend(line_rows)
            started = True
            activity = True
            misses = 0
            scan_index += 1
            continue

        # Before the first continuation row, only known page headers, blank
        # lines, a known section, or a table-shaped row are accepted.
        if continuation and not started:
            return rows, scan_index, None
        misses += 1
        if misses >= miss_limit:
            return rows, scan_index, None
        scan_index += 1

    outgoing = (
        _ParameterTableContext(
            header=context.header,
            model_indexes=context.model_indexes,
            table_title=context.table_title,
            section=section,
            page=page,
        )
        if scan_index >= len(lines) and activity
        else None
    )
    return rows, scan_index, outgoing


def _scan_page_model_parameter_rows(
    page_text: str,
    *,
    page: int,
    target_models: Sequence[str],
    incoming_context: _ParameterTableContext | None = None,
) -> tuple[list[PDFParameterRow], _ParameterTableContext | None]:
    lines = page_text.splitlines()[:MAX_LAYOUT_SCAN_LINES]
    rows: list[PDFParameterRow] = []
    line_index = 0
    outgoing_context: _ParameterTableContext | None = None

    if (
        incoming_context is not None
        and page == incoming_context.page + 1
    ):
        continuation_rows, line_index, outgoing_context = (
            _scan_parameter_table(
                lines,
                start_index=0,
                page=page,
                context=incoming_context,
                continuation=True,
            )
        )
        rows.extend(continuation_rows)
        if outgoing_context is not None:
            return rows, outgoing_context

    while line_index < len(lines) and len(rows) < MAX_MODEL_PARAMETER_ROWS:
        header = _layout_header(lines, line_index)
        if header is None:
            line_index += 1
            continue
        model_indexes = _model_indexes_for_header(header, target_models)
        scan_index = header.end_line_index + 1
        if not model_indexes:
            line_index = scan_index
            continue

        context = _ParameterTableContext(
            header=header,
            model_indexes=model_indexes,
            table_title=_layout_table_title(
                lines,
                line_index,
                model_indexes,
            ),
            section="",
            page=page,
        )
        table_rows, scan_index, table_outgoing = _scan_parameter_table(
            lines,
            start_index=scan_index,
            page=page,
            context=context,
            continuation=False,
        )
        rows.extend(table_rows)
        if table_outgoing is not None:
            outgoing_context = table_outgoing
        line_index = max(scan_index, header.end_line_index + 1)
    return rows, outgoing_context


def _page_model_parameter_rows(
    page_text: str,
    *,
    page: int,
    target_models: Sequence[str],
) -> list[PDFParameterRow]:
    """Backward-compatible single-page wrapper around the stateful scanner."""

    rows, _context = _scan_page_model_parameter_rows(
        page_text,
        page=page,
        target_models=target_models,
    )
    return rows


def extract_model_parameter_rows(
    pages_or_text: str | Mapping[int, str] | Sequence[tuple[int, str]],
    target_models: str | Sequence[str],
) -> list[PDFParameterRow]:
    """Return bounded, deterministic target-column rows from PDF layout text.

    ``pages_or_text`` may be the page-labelled output of :func:`parse_pdf_text`,
    a mapping of one-based page numbers to raw layout text, or a sequence of
    ``(page, text)`` pairs. No network, AI, environment, or production state is
    consulted.
    """

    models = _normalized_target_models(target_models)
    if not models:
        return []
    pages = _parameter_input_pages(pages_or_text)
    candidates: list[PDFParameterRow] = []
    context: _ParameterTableContext | None = None
    previous_page: int | None = None
    for page, page_text in pages:
        if previous_page is None or page != previous_page + 1:
            context = None
        page_rows, context = _scan_page_model_parameter_rows(
            page_text,
            page=page,
            target_models=models,
            incoming_context=context,
        )
        candidates.extend(page_rows)
        previous_page = page
        if len(candidates) >= MAX_MODEL_PARAMETER_ROWS:
            break

    result: list[PDFParameterRow] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate in candidates:
        key = (
            _normalized_model_key(candidate.model),
            candidate.source_label.casefold(),
            candidate.value.casefold(),
            candidate.unit.casefold(),
            candidate.section.casefold(),
            candidate.page,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(
            PDFParameterRow(
                model=candidate.model,
                source_label=candidate.source_label,
                value=candidate.value,
                unit=candidate.unit,
                section=candidate.section,
                page=candidate.page,
                order=len(result) + 1,
                model_quote=candidate.model_quote,
                quote=candidate.quote,
                table_title=candidate.table_title,
                value_state=candidate.value_state,
            )
        )
        if len(result) >= MAX_MODEL_PARAMETER_ROWS:
            break
    return result


def _page_text_with_derived_layout_tables(value: Any) -> str:
    """Prepend auditable table projections and retain the normalized source."""

    page_text = _normalized_page_text(value)
    derived = _derived_layout_table_text(page_text)
    return (
        f"{derived}\n\n[Raw PDF layout text]\n{page_text}"
        if derived
        else page_text
    )


@dataclass(frozen=True, slots=True)
class _PDFPageLayoutText:
    """Bounded source text and candidate table projections for one PDF page."""

    page_number: int
    raw_text: str
    derived_tables: tuple[str, ...]
    raw_truncated: bool = False


def _parameter_audit_blocks_for_page(
    page: _PDFPageLayoutText,
    rows: Sequence[PDFParameterRow],
) -> tuple[str, ...]:
    """Build bounded TSV evidence for rows whose header came from a prior page."""

    grouped: dict[str, list[str]] = {}
    for row in rows:
        if (
            row.page != page.page_number
            or not row.model_quote
            or not row.quote
            or any(
                row.model_quote in table and row.quote in table
                for table in page.derived_tables
            )
        ):
            continue
        quotes = grouped.setdefault(row.model_quote, [])
        if row.quote not in quotes:
            quotes.append(row.quote)

    blocks: list[str] = []
    for model_quote, quotes in grouped.items():
        remaining = list(quotes)
        while remaining and len(blocks) < MAX_DERIVED_LAYOUT_TABLES:
            marker = 900_000 + page.page_number * 10 + len(blocks)
            prefix = f"[Derived PDF layout table {marker}]"
            suffix = f"[End derived PDF layout table {marker}]"
            body = [model_quote]
            while (
                remaining
                and len(body) < MAX_DERIVED_LAYOUT_ROWS
            ):
                candidate = "\n".join(
                    (prefix, *body, remaining[0], suffix)
                )
                if len(candidate) > MAX_DERIVED_LAYOUT_CHARS:
                    break
                body.append(remaining.pop(0))
            if len(body) == 1:
                break
            blocks.append("\n".join((prefix, *body, suffix)))
    return tuple(blocks)


def _with_cross_page_parameter_audits(
    pages: Sequence[_PDFPageLayoutText],
    rows: Sequence[PDFParameterRow],
) -> list[_PDFPageLayoutText]:
    """Attach only locally derived, target-bound continuation projections."""

    result: list[_PDFPageLayoutText] = []
    for page in pages:
        audits = _parameter_audit_blocks_for_page(page, rows)
        result.append(
            _PDFPageLayoutText(
                page_number=page.page_number,
                raw_text=page.raw_text,
                derived_tables=(*page.derived_tables, *audits),
                raw_truncated=page.raw_truncated,
            )
        )
    return result


def _derived_layout_blocks(value: str) -> tuple[str, ...]:
    """Split only complete, explicitly marked derived tables."""

    return tuple(
        match.group(0)
        for match in re.finditer(
            r"(?ms)^\[Derived PDF layout table \d+\]\n"
            r".*?^\[End derived PDF layout table \d+\]$",
            value,
        )
    )


def _derived_layout_block_key(value: str) -> str:
    """Ignore local table numbering when deduplicating deterministic TSV."""

    lines = value.splitlines()
    body = lines[1:-1] if len(lines) >= 3 else lines
    return "\n".join(body).casefold()


def _unique_derived_layout_tables(
    pages: Sequence[_PDFPageLayoutText],
    *,
    max_chars: int,
    target_models: Sequence[str] = (),
) -> dict[int, tuple[str, ...]]:
    """Keep target-bearing tables first, then source order, within the budget."""

    budget = min(MAX_DERIVED_LAYOUT_CHARS, max_chars // 3)
    selected: dict[int, list[str]] = {}
    seen: set[str] = set()
    used = 0
    selected_count = 0
    candidates = [
        (page.page_number, table)
        for page in pages
        for table in page.derived_tables
    ]
    if target_models:
        candidates.sort(
            key=lambda item: (
                0
                if any(
                    _text_contains_complete_model(item[1], model)
                    for model in target_models
                )
                else 1,
                item[0],
            )
        )
    for page_number, table in candidates:
        key = _derived_layout_block_key(table)
        if not key or key in seen:
            continue
        seen.add(key)
        separator_chars = 2 if selected_count else 0
        cost = separator_chars + len(table)
        if cost > budget - used:
            continue
        selected.setdefault(page_number, []).append(table)
        used += cost
        selected_count += 1
        if selected_count >= MAX_DERIVED_LAYOUT_TABLES:
            return {
                page_number: tuple(tables)
                for page_number, tables in selected.items()
            }
    return {
        page_number: tuple(tables)
        for page_number, tables in selected.items()
    }


def _page_target_priority(
    page: _PDFPageLayoutText,
    target_models: Sequence[str],
) -> int:
    if not target_models:
        return 0
    if any(
        _text_contains_complete_model(table, model)
        for table in page.derived_tables
        for model in target_models
    ):
        return 2
    lines = page.raw_text.splitlines()[:MAX_LAYOUT_SCAN_LINES]
    for line_index in range(len(lines)):
        header = _layout_header(lines, line_index)
        if header is None:
            continue
        header_keys = {
            _normalized_model_key(model)
            for model in header.models
        }
        if any(
            _normalized_model_key(model) in header_keys
            for model in target_models
        ):
            return 2
    if any(
        _text_contains_complete_model(page.raw_text, model)
        for model in target_models
    ):
        return 1
    return 0


def _page_text_scaffold(
    page: _PDFPageLayoutText,
    *,
    page_count: int,
) -> tuple[str, str]:
    prefix = f"[PDF page {page.page_number}/{page_count}]\n"
    suffix = f"\n[End PDF page {page.page_number}]"
    return prefix, suffix


def _pdf_derived_layout_section(
    derived_by_page: Mapping[int, Sequence[str]],
    *,
    page_count: int,
) -> str:
    """Render selected projections before source text with page provenance."""

    page_sections = [
        (
            f"[Derived from PDF page {page_number}/{page_count}]\n"
            + "\n\n".join(tables)
            + f"\n[End derived from PDF page {page_number}]"
        )
        for page_number, tables in derived_by_page.items()
        if tables
    ]
    if not page_sections:
        return ""
    return (
        "[PDF derived layout projections]\n"
        + "\n\n".join(page_sections)
        + "\n[End PDF derived layout projections]"
    )


def _fair_text_allocations(
    lengths: Sequence[int],
    *,
    total: int,
) -> list[int]:
    """Water-fill a character budget so one long page cannot starve the rest."""

    allocations = [0] * len(lengths)
    remaining_indexes = list(range(len(lengths)))
    remaining = total
    while remaining_indexes:
        share = remaining // len(remaining_indexes)
        completed = [
            index
            for index in remaining_indexes
            if lengths[index] <= share
        ]
        if completed:
            for index in completed:
                allocations[index] = lengths[index]
                remaining -= lengths[index]
            completed_set = set(completed)
            remaining_indexes = [
                index
                for index in remaining_indexes
                if index not in completed_set
            ]
            continue
        for index in remaining_indexes:
            allocations[index] = share
        remaining -= share * len(remaining_indexes)
        for index in remaining_indexes[:remaining]:
            allocations[index] += 1
        break
    return allocations


def _target_first_text_allocations(
    pages: Sequence[_PDFPageLayoutText],
    *,
    total: int,
    target_models: Sequence[str],
) -> list[int]:
    """Preserve one character per page, then fill target tables/pages first."""

    if not target_models:
        return _fair_text_allocations(
            [len(page.raw_text) for page in pages],
            total=total,
        )
    allocations = [
        min(1, len(page.raw_text))
        for page in pages
    ]
    remaining = total - sum(allocations)
    priorities = [
        _page_target_priority(page, target_models)
        for page in pages
    ]
    for priority in (2, 1, 0):
        indexes = [
            index
            for index, page_priority in enumerate(priorities)
            if page_priority == priority
            and allocations[index] < len(pages[index].raw_text)
        ]
        if not indexes or remaining <= 0:
            continue
        needs = [
            len(pages[index].raw_text) - allocations[index]
            for index in indexes
        ]
        tier_budget = min(remaining, sum(needs))
        additions = _fair_text_allocations(needs, total=tier_budget)
        for index, addition in zip(indexes, additions, strict=True):
            allocations[index] += addition
        remaining -= tier_budget
    return allocations


def _assemble_pdf_page_texts(
    pages: Sequence[_PDFPageLayoutText],
    *,
    page_count: int,
    max_chars: int,
    target_models: Sequence[str] = (),
) -> tuple[str, int, bool]:
    """Assemble bounded text, prioritizing target tables/pages when supplied."""

    if not pages:
        return "", 0, False
    derived_by_page = _unique_derived_layout_tables(
        pages,
        max_chars=max_chars,
        target_models=target_models,
    )
    derived_section = _pdf_derived_layout_section(
        derived_by_page,
        page_count=page_count,
    )
    included = list(pages)

    def scaffold_size(candidate_pages: Sequence[_PDFPageLayoutText]) -> int:
        size = max(0, len(candidate_pages) - 1) * 2
        if derived_section and candidate_pages:
            size += len(derived_section) + 2
        for page in candidate_pages:
            prefix, suffix = _page_text_scaffold(
                page,
                page_count=page_count,
            )
            size += len(prefix) + len(suffix)
        return size

    # Keep at least one raw character per included page. If even scaffolding is
    # too large, discard a page without selected evidence before an evidence
    # page, while preserving the source page order of everything retained.
    while (
        included
        and scaffold_size(included) + len(included) > max_chars
    ):
        priorities = [
            (
                3
                if page.page_number in derived_by_page
                else _page_target_priority(page, target_models)
            )
            for page in included
        ]
        minimum_priority = min(priorities)
        removable_index = max(
            index
            for index, priority in enumerate(priorities)
            if priority == minimum_priority
        )
        included.pop(removable_index)
    if not included:
        return "", 0, True

    source_budget = max_chars - scaffold_size(included)
    allocations = _target_first_text_allocations(
        included,
        total=source_budget,
        target_models=target_models,
    )
    pieces: list[str] = [derived_section] if derived_section else []
    truncated = len(included) < len(pages)
    for page, allocation in zip(included, allocations, strict=True):
        prefix, suffix = _page_text_scaffold(
            page,
            page_count=page_count,
        )
        block = f"{prefix}{page.raw_text[:allocation]}{suffix}"
        pieces.append(block)
        truncated = (
            truncated
            or page.raw_truncated
            or allocation < len(page.raw_text)
        )
    text = "\n\n".join(pieces)
    return text, len(included), truncated


def _parse_pdf_in_child(
    sender: Any,
    content: bytes,
    max_pages: int,
    max_chars: int,
    cpu_seconds: int,
    target_models: tuple[str, ...],
) -> None:
    """Child-process target. Send only bounded text or a generic error type."""

    try:
        memory_limit = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (max(1, cpu_seconds), max(2, cpu_seconds + 1)),
        )
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception as exc:
                raise PDFParseError("encrypted PDF could not be opened") from exc
            if not unlocked:
                raise PDFParseError("encrypted PDF requires a password")
        page_count = len(reader.pages)
        if page_count < 1:
            raise PDFParseError("PDF contains no pages")
        if page_count > max_pages:
            raise PDFParseError("PDF exceeds the configured page limit")

        page_layouts: list[_PDFPageLayoutText] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                raw_text = page.extract_text(extraction_mode="layout")
            except TypeError:
                raw_text = page.extract_text()
            page_text = _normalized_page_text(raw_text)
            if not page_text:
                continue
            derived_text = _derived_layout_table_text(page_text)
            page_layouts.append(
                _PDFPageLayoutText(
                    page_number=index,
                    raw_text=page_text[:max_chars],
                    derived_tables=_derived_layout_blocks(derived_text),
                    raw_truncated=len(page_text) > max_chars,
                )
            )
        parameter_rows = extract_model_parameter_rows(
            tuple(
                (page.page_number, page.raw_text)
                for page in page_layouts
            ),
            target_models,
        )
        page_layouts = _with_cross_page_parameter_audits(
            page_layouts,
            parameter_rows,
        )
        text, extracted_pages, truncated = _assemble_pdf_page_texts(
            page_layouts,
            page_count=page_count,
            max_chars=max_chars,
            target_models=target_models,
        )
        if len(text.strip()) < MIN_USABLE_PDF_TEXT_CHARS:
            raise PDFParseError("PDF has no usable embedded text")
        sender.send(
            {
                "ok": True,
                "text": text,
                "page_count": page_count,
                "extracted_pages": extracted_pages,
                "truncated": truncated,
                "parameter_rows": [
                    {
                        "model": row.model,
                        "source_label": row.source_label,
                        "value": row.value,
                        "unit": row.unit,
                        "section": row.section,
                        "page": row.page,
                        "order": row.order,
                        "model_quote": row.model_quote,
                        "quote": row.quote,
                        "table_title": row.table_title,
                        "value_state": row.value_state,
                    }
                    for row in parameter_rows
                ],
            }
        )
    except BaseException as exc:
        try:
            sender.send(
                {
                    "ok": False,
                    "error_type": exc.__class__.__name__[:100],
                }
            )
        except BaseException:
            pass
    finally:
        sender.close()


def parse_pdf_text(
    content: bytes,
    *,
    max_pages: int,
    max_chars: int,
    timeout: float,
    target_models: str | Sequence[str] = (),
) -> PDFText:
    """Extract page-labelled text in a time- and memory-bounded subprocess."""

    if not isinstance(content, bytes) or PDF_MAGIC not in content[:1024]:
        raise PDFParseError("PDF content has an invalid file signature")
    if (
        isinstance(max_pages, bool)
        or not isinstance(max_pages, int)
        or not 1 <= max_pages <= 500
    ):
        raise ValueError("max_pages must be between 1 and 500")
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not 1_000 <= max_chars <= 200_000
    ):
        raise ValueError("max_chars must be between 1000 and 200000")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not 1 <= float(timeout) <= 60
    ):
        raise ValueError("timeout must be between 1 and 60 seconds")
    normalized_target_models = _normalized_target_models(target_models)

    # The worker serves requests from threads. ``spawn`` avoids inheriting
    # locks or partially initialized library state from that threaded parent.
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    cpu_seconds = max(1, int(math.ceil(float(timeout))))
    process = context.Process(
        target=_parse_pdf_in_child,
        args=(
            sender,
            content,
            max_pages,
            max_chars,
            cpu_seconds,
            normalized_target_models,
        ),
        daemon=True,
    )
    process.start()
    sender.close()
    result: Any = None
    try:
        if not receiver.poll(float(timeout)):
            process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            raise PDFParseError("PDF parsing exceeded the configured timeout")
        try:
            result = receiver.recv()
        except EOFError as exc:
            raise PDFParseError("PDF parser exited without a result") from exc
    finally:
        receiver.close()
        if process.is_alive():
            process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

    if not isinstance(result, Mapping) or result.get("ok") is not True:
        error_type = (
            result.get("error_type")
            if isinstance(result, Mapping)
            else "unknown"
        )
        raise PDFParseError(f"PDF parser failed safely ({error_type})")
    return PDFText(
        text=str(result["text"]),
        page_count=int(result["page_count"]),
        extracted_pages=int(result["extracted_pages"]),
        truncated=bool(result["truncated"]),
        parameter_rows=tuple(
            PDFParameterRow(**item)
            for item in result.get("parameter_rows", ())
            if isinstance(item, Mapping)
        ),
    )


def extract_pdf_evidence(
    url: str,
    *,
    max_bytes: int,
    max_pages: int,
    max_chars: int,
    download_timeout: float,
    parse_timeout: float,
    target_models: str | Sequence[str] = (),
    downloader: Callable[..., PDFDownload] = download_pdf,
    parser: Callable[..., PDFText] = parse_pdf_text,
) -> PDFEvidence:
    """Download and parse one bounded PDF without persisting the raw file."""

    downloaded = downloader(
        url,
        max_bytes=max_bytes,
        timeout=download_timeout,
    )
    normalized_target_models = _normalized_target_models(target_models)
    parser_arguments: dict[str, Any] = {
        "max_pages": max_pages,
        "max_chars": max_chars,
        "timeout": parse_timeout,
    }
    if normalized_target_models:
        parser_arguments["target_models"] = normalized_target_models
    parsed = parser(downloaded.content, **parser_arguments)
    return PDFEvidence(
        text=parsed.text,
        requested_url=downloaded.requested_url,
        final_url=downloaded.final_url,
        sha256=downloaded.sha256,
        page_count=parsed.page_count,
        extracted_pages=parsed.extracted_pages,
        truncated=parsed.truncated,
        parameter_rows=parsed.parameter_rows,
    )


__all__ = [
    "PDF_EXTRACTION_CONTRACT_VERSION",
    "PDFDocumentError",
    "PDFDownload",
    "PDFDownloadError",
    "PDFEvidence",
    "PDFParameterRow",
    "PDFParseError",
    "PDFText",
    "download_pdf",
    "extract_pdf_evidence",
    "extract_model_parameter_rows",
    "looks_like_pdf_url",
    "parse_pdf_text",
]
