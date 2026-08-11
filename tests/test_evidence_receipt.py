from __future__ import annotations

import os
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
    load_evidence_hmac_key,
    verify_evidence_receipt,
)


KEY = b"pv-wiki-test-evidence-receipt-key-0001"
URL = "HTTPS://Docs.Acme.Example:443/manual one.pdf"
NORMALIZED_URL = "https://docs.acme.example/manual%20one.pdf"
CONTENT = "PV-42 rated output 42 W\nExact extracted text."


class EvidenceReceiptTests(unittest.TestCase):
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
