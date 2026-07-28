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


PDF_EXTRACTION_CONTRACT_VERSION = "2026-07-28.1"
PDF_MAGIC = b"%PDF-"
PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/x-pdf"})
PDF_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3
READ_CHUNK_BYTES = 64 * 1024
MIN_USABLE_PDF_TEXT_CHARS = 200


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

        pieces: list[str] = []
        used = 0
        extracted_pages = 0
        truncated = False
        for index, page in enumerate(reader.pages, start=1):
            try:
                raw_text = page.extract_text(extraction_mode="layout")
            except TypeError:
                raw_text = page.extract_text()
            page_text = _normalized_page_text(raw_text)
            if not page_text:
                continue
            marker = f"[PDF page {index}/{page_count}]"
            block = f"{marker}\n{page_text}\n[End PDF page {index}]"
            separator = "\n\n" if pieces else ""
            remaining = max_chars - used - len(separator)
            if remaining <= 0:
                truncated = True
                break
            if len(block) > remaining:
                block = block[:remaining]
                truncated = True
            pieces.append(f"{separator}{block}")
            used += len(separator) + len(block)
            extracted_pages += 1
            if truncated:
                break
        text = "".join(pieces)
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
