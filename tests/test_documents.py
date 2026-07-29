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

from pv_wiki import decision, documents  # noqa: E402


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


def sun2000l_fixed_width_layout(*, repeated_rows: int = 1) -> str:
    """Representative pypdf layout text from Huawei's shared series PDF."""

    gap = " " * 24
    header = gap.join(
        (
            "Technical Specification",
            "SUN2000L",
            "SUN2000L",
            "SUN2000L",
            "SUN2000L",
            "SUN2000L",
        )
    )
    suffixes = (" " * 48) + gap.join(
        ("-2KTL", "-3KTL", "-4KTL", "-4.6KTL", "-5KTL")
    )
    rows = [
        gap.join(
            (
                "Max. efficiency",
                "98.4 %",
                "98.5 %",
                "98.6 %",
                "98.6 %",
                "98.6 %",
            )
        ),
        gap.join(
            (
                "European weighted   efficiency",
                "97.0 %",
                "97.6 %",
                "97.9 %",
                "98.0 %",
                "98.0 %",
            )
        ),
        gap.join(("Max. input voltage", "600 V")),
        gap.join(
            (
                "Recommended max. PV power",
                "3,000 Wp",
                "4,500 Wp",
                "6,000 Wp",
                "6,900 Wp",
                "7,500 Wp",
            )
        ),
        gap.join(
            (
                "Rated output power",
                "2,000 W",
                "3,000 W",
                "4,000 W",
                "4,600 W",
                "4,990 W",
            )
        ),
    ]
    if repeated_rows > 1:
        rows.extend([rows[-1]] * (repeated_rows - 1))
    return "\n".join((header, suffixes, *rows))


def r5_fixed_width_layout(*, tail_chars: int = 0) -> str:
    """Representative single-row model table from the SAJ R5 series manual."""

    gap = " " * 12
    lines = [
        "R5-8K/9K/10K/12K-T2-15",
        gap.join(
            (
                "Type",
                "R5-8K-T2-15",
                "R5-9K-T2-15",
                "R5-10K-T2-15",
                "R5-12K-T2-15",
            )
        ),
        "Input (DC)",
        gap.join(
            (
                "Max. PV Array Power [Wp]@STC",
                "12000",
                "13500",
                "15000",
                "15600",
            )
        ),
        "Max. DC Voltage [V]" + (" " * 60) + "1100",
        "MPPT Voltage Range [V]" + (" " * 55) + "160-950",
        gap.join(
            (
                "Max. DC Input Current [A]",
                "15/15",
                "15/15",
                "15/15",
                "15/15",
            )
        ),
        # Two grouped values cannot be bound safely to four model columns.
        "European Efficiency" + (" " * 28) + "98.2%" + (" " * 28) + "98.3%",
        "Output (AC)",
        gap.join(
            (
                "Rated AC Power [W]",
                "8000",
                "9000",
                "10000",
                "12000",
            )
        ),
        "Efficiency",
        "Max. Efficiency" + (" " * 60) + "98.6%",
        "General Data",
        "Ingress Protection" + (" " * 60) + "IP65",
    ]
    if tail_chars:
        lines.append("TARGET-PAGE-TAIL " + ("z" * tail_chars))
    return "\n".join(lines)


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

    def test_shared_series_layout_appends_auditable_composite_tsv(self) -> None:
        source = sun2000l_fixed_width_layout()

        enriched = documents._page_text_with_derived_layout_tables(source)

        self.assertIn(
            "[Derived PDF layout table 1]\n"
            "Technical Specification\tSUN2000L-2KTL\tSUN2000L-3KTL\t"
            "SUN2000L-4KTL\tSUN2000L-4.6KTL\tSUN2000L-5KTL\n",
            enriched,
        )
        self.assertTrue(
            enriched.endswith(documents._normalized_page_text(source))
        )
        self.assertIn("\n\n[Raw PDF layout text]\n", enriched)
        self.assertIn(
            "European weighted efficiency\t97.0 %\t97.6 %\t97.9 %\t"
            "98.0 %\t98.0 %",
            enriched,
        )
        self.assertIn(
            "Max. input voltage\t600 V\t600 V\t600 V\t600 V\t600 V",
            enriched,
        )
        self.assertTrue(
            decision.text_contains_catalogue_identity(
                "SUN2000L-4.6KTL",
                "SUN2000L-4.6KTL",
                enriched,
            )
        )

    def test_derived_tsv_binds_fact_to_target_model_column(self) -> None:
        enriched = documents._page_text_with_derived_layout_tables(
            sun2000l_fixed_width_layout()
        )
        derived_lines = enriched.split("[Derived PDF layout table 1]\n", 1)[
            1
        ].splitlines()
        model_quote = derived_lines[0]
        fact_quote = next(
            line
            for line in derived_lines
            if line.startswith("Rated output power\t")
        )

        self.assertTrue(
            decision._structured_table_quote_supports_fact(
                model_quote=model_quote,
                fact_quote=fact_quote,
                name="Rated output power",
                value=4600,
                unit="W",
                expected_product_name="SUN2000L-4.6KTL",
            )
        )
        self.assertFalse(
            decision._structured_table_quote_supports_fact(
                model_quote=model_quote,
                fact_quote=fact_quote,
                name="Rated output power",
                value=4000,
                unit="W",
                expected_product_name="SUN2000L-4.6KTL",
            )
        )

    def test_single_line_model_header_emits_auditable_tsv(self) -> None:
        enriched = documents._page_text_with_derived_layout_tables(
            r5_fixed_width_layout()
        )

        self.assertIn(
            "[Derived PDF layout table 1]\n"
            "Type\tR5-8K-T2-15\tR5-9K-T2-15\t"
            "R5-10K-T2-15\tR5-12K-T2-15\n",
            enriched,
        )
        self.assertIn(
            "Rated AC Power [W]\t8000 W\t9000 W\t10000 W\t12000 W",
            enriched,
        )

    def test_extract_model_parameter_rows_binds_r5_target_and_sections(self) -> None:
        source = r5_fixed_width_layout()
        rows = documents.extract_model_parameter_rows(
            [(20, source)],
            ("R5-10K-T2-15",),
        )
        by_label = {row.source_label: row for row in rows}

        self.assertEqual(
            (
                "15000",
                "Wp",
                "Input (DC)",
                20,
                1,
            ),
            (
                by_label["Max. PV Array Power [Wp]@STC"].value,
                by_label["Max. PV Array Power [Wp]@STC"].unit,
                by_label["Max. PV Array Power [Wp]@STC"].section,
                by_label["Max. PV Array Power [Wp]@STC"].page,
                by_label["Max. PV Array Power [Wp]@STC"].order,
            ),
        )
        self.assertEqual("1100", by_label["Max. DC Voltage [V]"].value)
        self.assertEqual("V", by_label["Max. DC Voltage [V]"].unit)
        self.assertEqual("10000", by_label["Rated AC Power [W]"].value)
        self.assertEqual("Output (AC)", by_label["Rated AC Power [W]"].section)
        self.assertEqual("98.6", by_label["Max. Efficiency"].value)
        self.assertEqual("%", by_label["Max. Efficiency"].unit)
        self.assertEqual("IP65", by_label["Ingress Protection"].value)
        self.assertNotIn("European Efficiency", by_label)
        self.assertEqual(
            "Type\tR5-8K-T2-15\tR5-9K-T2-15\t"
            "R5-10K-T2-15\tR5-12K-T2-15",
            by_label["Rated AC Power [W]"].model_quote,
        )
        self.assertEqual(
            "Rated AC Power [W]\t8000 W\t9000 W\t10000 W\t12000 W",
            by_label["Rated AC Power [W]"].quote,
        )
        self.assertTrue(
            decision._structured_table_quote_supports_fact(
                model_quote=by_label["Rated AC Power [W]"].model_quote,
                fact_quote=by_label["Rated AC Power [W]"].quote,
                name="Rated AC Power [W]",
                value=10000,
                unit="W",
                expected_product_name="R5-10K-T2-15",
                source_body=documents._page_text_with_derived_layout_tables(
                    source
                ),
            )
        )

    def test_extract_model_parameter_rows_supports_split_model_header(self) -> None:
        rows = documents.extract_model_parameter_rows(
            [(7, sun2000l_fixed_width_layout())],
            "SUN2000L-4.6KTL",
        )
        rated = next(
            row
            for row in rows
            if row.source_label == "Rated output power"
        )

        self.assertEqual("4,600", rated.value)
        self.assertEqual("W", rated.unit)
        self.assertEqual(7, rated.page)
        self.assertIn("SUN2000L-4.6KTL", rated.model_quote)

    def test_grouped_values_are_not_misclassified_as_shared_parameters(self) -> None:
        row = (
            "European Efficiency"
            + (" " * 28)
            + "98.2%"
            + (" " * 28)
            + "98.3%"
        )

        self.assertIsNone(
            documents._shared_layout_data_row(row, column_count=4)
        )
        labels = {
            item.source_label
            for item in documents.extract_model_parameter_rows(
                [(20, r5_fixed_width_layout())],
                ("R5-10K-T2-15",),
            )
        }
        self.assertNotIn("European Efficiency", labels)

    def test_derived_layout_projection_is_row_bounded(self) -> None:
        enriched = documents._page_text_with_derived_layout_tables(
            sun2000l_fixed_width_layout(repeated_rows=100)
        )
        derived = enriched.split("[Derived PDF layout table 1]\n", 1)[1]
        derived = derived.split("[End derived PDF layout table 1]", 1)[0]

        # The cap includes the one composite header plus retained data rows.
        self.assertEqual(
            documents.MAX_DERIVED_LAYOUT_ROWS,
            len(derived.splitlines()),
        )

    def test_multipage_budget_deduplicates_tsv_and_preserves_each_page(self) -> None:
        layout = sun2000l_fixed_width_layout()
        derived_tables = documents._derived_layout_blocks(
            documents._derived_layout_table_text(layout)
        )
        pages = [
            documents._PDFPageLayoutText(
                page_number=page_number,
                raw_text=(
                    f"RAW-PAGE-{page_number}\n"
                    f"{layout}\n"
                    + f"page-{page_number}-detail " * 600
                ),
                derived_tables=derived_tables,
            )
            for page_number in range(1, 7)
        ]

        text, extracted_pages, truncated = documents._assemble_pdf_page_texts(
            pages,
            page_count=6,
            max_chars=30_000,
        )

        self.assertEqual(30_000, len(text))
        self.assertEqual(6, extracted_pages)
        self.assertTrue(truncated)
        self.assertEqual(1, text.count("[Derived PDF layout table 1]"))
        self.assertEqual(
            1,
            text.count(
                "Technical Specification\tSUN2000L-2KTL\t"
                "SUN2000L-3KTL\tSUN2000L-4KTL\t"
                "SUN2000L-4.6KTL\tSUN2000L-5KTL"
            ),
        )
        self.assertLess(
            text.index("[Derived PDF layout table 1]"),
            text.index("RAW-PAGE-1"),
        )
        for page_number in range(1, 7):
            self.assertIn(f"[PDF page {page_number}/6]", text)
            self.assertIn(f"RAW-PAGE-{page_number}", text)
            self.assertIn(f"[End PDF page {page_number}]", text)

    def test_target_table_page_is_preserved_before_non_target_page_details(self) -> None:
        pages = [
            documents._PDFPageLayoutText(
                page_number=page_number,
                raw_text=(
                    r5_fixed_width_layout(tail_chars=1_200)
                    if page_number == 20
                    else (
                        f"NON-TARGET-{page_number}\n"
                        + (f"page-{page_number}-detail " * 200)
                    )
                ),
                derived_tables=(
                    documents._derived_layout_blocks(
                        documents._derived_layout_table_text(
                            r5_fixed_width_layout()
                        )
                    )
                    if page_number == 20
                    else ()
                ),
            )
            for page_number in range(1, 61)
        ]

        text, extracted_pages, truncated = documents._assemble_pdf_page_texts(
            pages,
            page_count=60,
            max_chars=8_000,
            target_models=("R5-10K-T2-15",),
        )

        self.assertEqual(8_000, len(text))
        self.assertEqual(60, extracted_pages)
        self.assertTrue(truncated)
        self.assertIn("TARGET-PAGE-TAIL", text)
        self.assertIn(
            "Type\tR5-8K-T2-15\tR5-9K-T2-15\t"
            "R5-10K-T2-15\tR5-12K-T2-15",
            text,
        )
        parsed_rows = documents.extract_model_parameter_rows(
            text,
            ("R5-10K-T2-15",),
        )
        self.assertEqual(
            "10000",
            next(
                row.value
                for row in parsed_rows
                if row.source_label == "Rated AC Power [W]"
            ),
        )

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

    def test_extract_propagates_target_models_and_local_parameter_rows(self) -> None:
        content = minimal_text_pdf("R5-10K-T2-15 " + ("datasheet " * 30))
        downloaded = documents.PDFDownload(
            requested_url="https://maker.example/r5.pdf",
            final_url="https://maker.example/r5.pdf",
            content=content,
            sha256="b" * 64,
            content_type="application/pdf",
        )
        row = documents.PDFParameterRow(
            model="R5-10K-T2-15",
            source_label="Rated AC Power [W]",
            value="10000",
            unit="W",
            section="Output (AC)",
            page=20,
            order=1,
            model_quote=(
                "Type\tR5-8K-T2-15\tR5-9K-T2-15\t"
                "R5-10K-T2-15\tR5-12K-T2-15"
            ),
            quote="Rated AC Power [W]\t8000 W\t9000 W\t10000 W\t12000 W",
        )
        parsed = documents.PDFText(
            text="[PDF page 20/60]\nR5-10K-T2-15\n[End PDF page 20]",
            page_count=60,
            extracted_pages=60,
            truncated=True,
            parameter_rows=(row,),
        )
        downloader = mock.Mock(return_value=downloaded)
        parser = mock.Mock(return_value=parsed)

        evidence = documents.extract_pdf_evidence(
            downloaded.requested_url,
            max_bytes=1_000_000,
            max_pages=60,
            max_chars=30_000,
            download_timeout=5,
            parse_timeout=6,
            target_models=("R5-10K-T2-15",),
            downloader=downloader,
            parser=parser,
        )

        self.assertEqual((row,), evidence.parameter_rows)
        parser.assert_called_once_with(
            content,
            max_pages=60,
            max_chars=30_000,
            timeout=6,
            target_models=("R5-10K-T2-15",),
        )


if __name__ == "__main__":
    unittest.main()
