from __future__ import annotations

import io
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import documents  # noqa: E402


def minimal_text_pdf(text: str) -> bytes:
    """Build one small, dependency-free PDF fixture with an embedded text layer."""

    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 11 Tf 36 760 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        ),
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, target, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def public_resolver(
    _hostname: str,
    _port: int,
    *,
    type: socket.SocketKind,
) -> list[tuple[object, ...]]:
    assert type == socket.SOCK_STREAM
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class PDFDocumentTests(unittest.TestCase):
    def test_parse_pdf_returns_page_labelled_embedded_text(self) -> None:
        source = (
            "MODEL-42 Technical Datasheet Rated power 420 W "
            + "Maximum efficiency 98.8 percent " * 8
        )
        parsed = documents.parse_pdf_text(
            minimal_text_pdf(source),
            max_pages=10,
            max_chars=10_000,
            timeout=10,
        )

        self.assertEqual(1, parsed.page_count)
        self.assertEqual(1, parsed.extracted_pages)
        self.assertFalse(parsed.truncated)
        self.assertIn("[PDF page 1/1]", parsed.text)
        self.assertIn("MODEL-42", parsed.text)
        self.assertIn("[End PDF page 1]", parsed.text)

    def test_download_pins_public_dns_and_validates_pdf_wire_format(self) -> None:
        body = minimal_text_pdf("MODEL-42 " + ("specification " * 20))
        response = FakeResponse(
            body,
            headers={
                "Content-Type": "application/pdf; charset=binary",
                "Content-Length": str(len(body)),
            },
        )
        connection = FakeConnection(response)
        calls: list[tuple[str, str, int, float]] = []

        def factory(
            hostname: str,
            address: str,
            port: int,
            timeout: float,
        ) -> FakeConnection:
            calls.append((hostname, address, port, timeout))
            return connection

        downloaded = documents.download_pdf(
            "https://maker.example/files/model.pdf?download=1",
            max_bytes=1_000_000,
            timeout=5,
            resolver=public_resolver,
            connection_factory=factory,
        )

        self.assertEqual(
            ("maker.example", "93.184.216.34", 443),
            calls[0][:3],
        )
        self.assertGreater(calls[0][3], 0)
        self.assertLessEqual(calls[0][3], 5)
        self.assertEqual(
            "https://maker.example/files/model.pdf?download=1",
            downloaded.final_url,
        )
        self.assertEqual(body, downloaded.content)
        self.assertEqual(64, len(downloaded.sha256))
        self.assertEqual("GET", connection.requests[0][0])
        self.assertEqual(
            "/files/model.pdf?download=1",
            connection.requests[0][1],
        )
        self.assertTrue(connection.closed)

    def test_download_rejects_non_https_private_dns_and_non_pdf_mime(self) -> None:
        with self.assertRaisesRegex(documents.PDFDownloadError, "HTTPS"):
            documents.download_pdf(
                "http://maker.example/model.pdf",
                max_bytes=1_000_000,
                timeout=5,
                resolver=public_resolver,
            )

        def private_resolver(
            *_args: object,
            **_kwargs: object,
        ) -> list[tuple[object, ...]]:
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("127.0.0.1", 443),
                )
            ]

        with self.assertRaisesRegex(documents.PDFDownloadError, "non-public"):
            documents.download_pdf(
                "https://maker.example/model.pdf",
                max_bytes=1_000_000,
                timeout=5,
                resolver=private_resolver,
            )

        response = FakeResponse(
            b"%PDF-1.4\n",
            headers={"Content-Type": "text/html"},
        )
        connection = FakeConnection(response)
        with self.assertRaisesRegex(documents.PDFDownloadError, "Content-Type"):
            documents.download_pdf(
                "https://maker.example/model.pdf",
                max_bytes=1_000_000,
                timeout=5,
                resolver=public_resolver,
                connection_factory=lambda *_args: connection,
            )

    def test_redirect_revalidates_dns_and_size_limit_is_enforced(self) -> None:
        redirect = FakeConnection(
            FakeResponse(
                b"",
                status=302,
                headers={
                    "Location": "https://redirected.example/model.pdf",
                },
            )
        )

        def resolver(
            hostname: str,
            _port: int,
            *,
            type: socket.SocketKind,
        ) -> list[tuple[object, ...]]:
            assert type == socket.SOCK_STREAM
            address = (
                "93.184.216.34"
                if hostname == "maker.example"
                else "10.0.0.7"
            )
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (address, 443),
                )
            ]

        with self.assertRaisesRegex(documents.PDFDownloadError, "non-public"):
            documents.download_pdf(
                "https://maker.example/model.pdf",
                max_bytes=1_000_000,
                timeout=5,
                resolver=resolver,
                connection_factory=lambda *_args: redirect,
            )
        self.assertTrue(redirect.closed)

        oversized = FakeConnection(
            FakeResponse(
                b"",
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Length": "1000001",
                },
            )
        )
        with self.assertRaisesRegex(documents.PDFDownloadError, "size limit"):
            documents.download_pdf(
                "https://maker.example/model.pdf",
                max_bytes=1_000_000,
                timeout=5,
                resolver=public_resolver,
                connection_factory=lambda *_args: oversized,
            )

    def test_extract_combines_download_hash_and_isolated_page_text(self) -> None:
        content = minimal_text_pdf("MODEL-42 " + ("rated voltage 400 V " * 12))
        downloaded = documents.PDFDownload(
            requested_url="https://maker.example/model.pdf",
            final_url="https://cdn.maker.example/model.pdf",
            content=content,
            sha256="a" * 64,
            content_type="application/pdf",
        )
        parsed = documents.PDFText(
            text="[PDF page 1/1]\nMODEL-42 rated voltage 400 V",
            page_count=1,
            extracted_pages=1,
            truncated=False,
        )
        downloader = mock.Mock(return_value=downloaded)
        parser = mock.Mock(return_value=parsed)

        evidence = documents.extract_pdf_evidence(
            downloaded.requested_url,
            max_bytes=1_000_000,
            max_pages=20,
            max_chars=30_000,
            download_timeout=5,
            parse_timeout=6,
            downloader=downloader,
            parser=parser,
        )

        self.assertEqual("a" * 64, evidence.sha256)
        self.assertEqual(1, evidence.page_count)
        self.assertIn("MODEL-42", evidence.text)
        downloader.assert_called_once_with(
            downloaded.requested_url,
            max_bytes=1_000_000,
            timeout=5,
        )
        parser.assert_called_once_with(
            content,
            max_pages=20,
            max_chars=30_000,
            timeout=6,
        )


if __name__ == "__main__":
    unittest.main()
