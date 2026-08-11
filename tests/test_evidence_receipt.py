from __future__ import annotations

import os
import copy
import tempfile
import unittest
from pathlib import Path

import sys


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki.evidence_receipt import (  # noqa: E402
    EvidenceReceiptError,
    content_sha256,
    issue_evidence_receipt,
    issue_evidence_receipt_v2,
    load_evidence_hmac_key,
    verify_evidence_receipt,
    verify_evidence_receipt_v2,
)


KEY = b"pv-wiki-test-evidence-receipt-key-0001"
URL = "HTTPS://Docs.Acme.Example:443/manual one.pdf"
NORMALIZED_URL = "https://docs.acme.example/manual%20one.pdf"
CONTENT = "PV-42 rated output 42 W\nExact extracted text."


class EvidenceReceiptTests(unittest.TestCase):
    def v2_receipt(self) -> dict:
        return issue_evidence_receipt_v2(
            KEY,
            requested_url=URL,
            final_url=NORMALIZED_URL,
            redirect_chain=[NORMALIZED_URL],
            content=CONTENT,
            artifact_sha256="a" * 64,
            parser_metadata={
                "contract_version": "2026-07-29.2",
                "page_count": 2,
                "extracted_pages": 2,
                "truncated": False,
            },
            target_models=["PV-42"],
            parameter_rows=[
                {
                    "parameter_id": "p001",
                    "model": "PV-42",
                    "source_label": "Rated output",
                    "value": "42",
                    "unit": "W",
                    "section": "Output",
                    "page": 2,
                    "order": 1,
                    "model_quote": "Type\tPV-42",
                    "quote": "Rated output\t42 W",
                    "table_title": "",
                    "value_state": "explicit",
                }
            ],
        )

    def test_valid_receipt_binds_normalized_url_and_exact_content(self) -> None:
        receipt = issue_evidence_receipt(KEY, url=URL, content=CONTENT)
        self.assertEqual(
            NORMALIZED_URL,
            verify_evidence_receipt(
                KEY,
                url=NORMALIZED_URL,
                content=CONTENT,
                receipt=receipt,
            ),
        )
        self.assertEqual(
            content_sha256(CONTENT),
            "961201012eaf0470ef0bc8aaad2dd24b12501380970048a191c025f43f97cc15",
        )

    def test_receipt_does_not_contain_secret(self) -> None:
        receipt = issue_evidence_receipt(KEY, url=URL, content=CONTENT)
        self.assertTrue(receipt.startswith("pvwiki-evidence-v1."))
        self.assertNotIn(KEY.decode("ascii"), receipt)
        self.assertNotIn(content_sha256(CONTENT), receipt)

    def test_v2_receipt_signs_pdf_provenance_and_keeps_v1_compatible(self) -> None:
        receipt = self.v2_receipt()
        payload = verify_evidence_receipt_v2(
            KEY,
            content=CONTENT,
            receipt=receipt,
        )
        self.assertEqual(NORMALIZED_URL, payload["final_url"])
        self.assertEqual("p001", payload["parameter_rows"][0]["parameter_id"])
        self.assertEqual(
            NORMALIZED_URL,
            verify_evidence_receipt(
                KEY,
                url=NORMALIZED_URL,
                content=CONTENT,
                receipt=receipt,
            ),
        )

    def test_v2_receipt_rejects_unknown_fields_and_noncanonical_numbers(self) -> None:
        receipt = self.v2_receipt()
        unknown = copy.deepcopy(receipt)
        unknown["future"] = True
        with self.assertRaises(EvidenceReceiptError):
            verify_evidence_receipt_v2(
                KEY,
                content=CONTENT,
                receipt=unknown,
            )

        invalid = copy.deepcopy(receipt)
        invalid["parser_metadata"]["page_count"] = float("nan")
        with self.assertRaises(EvidenceReceiptError):
            verify_evidence_receipt_v2(
                KEY,
                content=CONTENT,
                receipt=invalid,
            )

    def test_tampered_url_content_and_receipt_are_rejected(self) -> None:
        receipt = issue_evidence_receipt(KEY, url=URL, content=CONTENT)
        cases = (
            ("https://docs.acme.example/other.pdf", CONTENT, receipt),
            (NORMALIZED_URL, CONTENT + "!", receipt),
            (
                NORMALIZED_URL,
                CONTENT,
                receipt[:-1] + ("A" if receipt[-1] != "A" else "B"),
            ),
        )
        for url, content, candidate in cases:
            with self.subTest(url=url, content=content), self.assertRaises(
                EvidenceReceiptError
            ):
                verify_evidence_receipt(
                    KEY,
                    url=url,
                    content=content,
                    receipt=candidate,
                )

    def test_missing_and_malformed_receipts_are_rejected(self) -> None:
        for candidate in (
            None,
            "",
            "v1.bad",
            "pvwiki-evidence-v1." + "*" * 43,
        ):
            with self.subTest(receipt=candidate), self.assertRaises(
                EvidenceReceiptError
            ):
                verify_evidence_receipt(
                    KEY,
                    url=URL,
                    content=CONTENT,
                    receipt=candidate,
                )

    def test_key_load_requires_mode_0600_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt-key"
            path.write_bytes(KEY)
            path.chmod(0o600)
            environ = {"PV_WIKI_EVIDENCE_HMAC_KEY_FILE": str(path)}
            self.assertEqual(KEY, load_evidence_hmac_key(environ=environ))

            if os.name == "posix":
                path.chmod(0o640)
                with self.assertRaisesRegex(EvidenceReceiptError, "mode 0600"):
                    load_evidence_hmac_key(environ=environ)

    def test_key_load_rejects_short_key_and_non_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "short-key"
            path.write_bytes(b"short")
            path.chmod(0o600)
            with self.assertRaisesRegex(EvidenceReceiptError, "at least 32"):
                load_evidence_hmac_key(
                    environ={"PV_WIKI_EVIDENCE_HMAC_KEY_FILE": str(path)}
                )
            with self.assertRaises(EvidenceReceiptError):
                load_evidence_hmac_key(
                    environ={"PV_WIKI_EVIDENCE_HMAC_KEY_FILE": str(directory)}
                )


if __name__ == "__main__":
    unittest.main()
