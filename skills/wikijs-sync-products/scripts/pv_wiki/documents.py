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
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .render import validate_public_http_url


PDF_EXTRACTION_CONTRACT_VERSION = "2026-07-28.3"
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
class PDFText:
    text: str
    page_count: int
    extracted_pages: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class PDFEvidence:
    text: str
    requested_url: str
    final_url: str
    sha256: str
    page_count: int
    extracted_pages: int
    truncated: bool


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
class _CompositeLayoutHeader:
    """One visually tabular model header split across two PDF text rows."""

    suffix_line_index: int
    column_count: int
    normalized_row: str


def _collapsed_layout_cell(value: str) -> str:
    return " ".join(value.replace("\u00a0", " ").split())


def _model_shaped_layout_token(value: str) -> bool:
    compact = "".join(character for character in value if character.isalnum())
    return (
        3 <= len(compact) <= 80
        and any(character.isalpha() for character in compact)
        and any(character.isdecimal() for character in compact)
    )


def _composite_layout_header(
    lines: Sequence[str],
    line_index: int,
) -> _CompositeLayoutHeader | None:
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
    return _CompositeLayoutHeader(
        suffix_line_index=suffix_line_index,
        column_count=len(models),
        normalized_row=normalized_row,
    )


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
        header = _composite_layout_header(lines, line_index)
        if header is None:
            line_index += 1
            continue

        table_number = len(blocks) + 1
        rows = [header.normalized_row]
        scan_index = header.suffix_line_index + 1
        while scan_index < len(lines) and len(rows) < MAX_DERIVED_LAYOUT_ROWS:
            if _composite_layout_header(lines, scan_index) is not None:
                break
            stripped = lines[scan_index].strip()
            if stripped.startswith("*"):
                break
            normalized_row = _normalized_layout_data_row(
                lines[scan_index],
                column_count=header.column_count,
            )
            if normalized_row is not None:
                rows.append(normalized_row)
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
        line_index = max(scan_index, header.suffix_line_index + 1)
    return "\n\n".join(blocks)


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
) -> dict[int, tuple[str, ...]]:
    """Keep the earliest unique tables within one PDF-wide enrichment budget."""

    budget = min(MAX_DERIVED_LAYOUT_CHARS, max_chars // 3)
    selected: dict[int, list[str]] = {}
    seen: set[str] = set()
    used = 0
    selected_count = 0
    for page in pages:
        for table in page.derived_tables:
            key = _derived_layout_block_key(table)
            if not key or key in seen:
                continue
            seen.add(key)
            separator_chars = 2 if selected_count else 0
            cost = separator_chars + len(table)
            if cost > budget - used:
                continue
            selected.setdefault(page.page_number, []).append(table)
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


def _assemble_pdf_page_texts(
    pages: Sequence[_PDFPageLayoutText],
    *,
    page_count: int,
    max_chars: int,
) -> tuple[str, int, bool]:
    """Assemble a fair, PDF-wide budget with deduplicated TSV projections."""

    if not pages:
        return "", 0, False
    derived_by_page = _unique_derived_layout_tables(
        pages,
        max_chars=max_chars,
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
        removable_index = next(
            (
                index
                for index in range(len(included) - 1, -1, -1)
                if included[index].page_number not in derived_by_page
            ),
            len(included) - 1,
        )
        included.pop(removable_index)
    if not included:
        return "", 0, True

    source_budget = max_chars - scaffold_size(included)
    allocations = _fair_text_allocations(
        [len(page.raw_text) for page in included],
        total=source_budget,
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
        text, extracted_pages, truncated = _assemble_pdf_page_texts(
            page_layouts,
            page_count=page_count,
            max_chars=max_chars,
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

    # The worker serves requests from threads. ``spawn`` avoids inheriting
    # locks or partially initialized library state from that threaded parent.
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    cpu_seconds = max(1, int(math.ceil(float(timeout))))
    process = context.Process(
        target=_parse_pdf_in_child,
        args=(sender, content, max_pages, max_chars, cpu_seconds),
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
    )


def extract_pdf_evidence(
    url: str,
    *,
    max_bytes: int,
    max_pages: int,
    max_chars: int,
    download_timeout: float,
    parse_timeout: float,
    downloader: Callable[..., PDFDownload] = download_pdf,
    parser: Callable[..., PDFText] = parse_pdf_text,
) -> PDFEvidence:
    """Download and parse one bounded PDF without persisting the raw file."""

    downloaded = downloader(
        url,
        max_bytes=max_bytes,
        timeout=download_timeout,
    )
    parsed = parser(
        downloaded.content,
        max_pages=max_pages,
        max_chars=max_chars,
        timeout=parse_timeout,
    )
    return PDFEvidence(
        text=parsed.text,
        requested_url=downloaded.requested_url,
        final_url=downloaded.final_url,
        sha256=downloaded.sha256,
        page_count=parsed.page_count,
        extracted_pages=parsed.extracted_pages,
        truncated=parsed.truncated,
    )


__all__ = [
    "PDF_EXTRACTION_CONTRACT_VERSION",
    "PDFDocumentError",
    "PDFDownload",
    "PDFDownloadError",
    "PDFEvidence",
    "PDFParseError",
    "PDFText",
    "download_pdf",
    "extract_pdf_evidence",
    "looks_like_pdf_url",
    "parse_pdf_text",
]
