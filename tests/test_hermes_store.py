from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import tempfile
import threading
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


import sys


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki.config import WikiSettings  # noqa: E402
from pv_wiki.catalogue_snapshot import replace_catalogue_snapshot  # noqa: E402
from pv_wiki.evidence_receipt import (  # noqa: E402
    issue_evidence_receipt,
    issue_evidence_receipt_v2,
)
from pv_wiki.hermes_store import (  # noqa: E402
    CompletionError,
    CompletionValidationError,
    HermesCompletionStore,
    PublicationFenceError,
    ProductSourceChangedError,
    RenderModel,
    publish_researched,
    rerender_completion,
)
from pv_wiki import research_mcp  # noqa: E402
from pv_wiki.research_mcp import _next_product  # noqa: E402
from pv_wiki.render import stable_path  # noqa: E402
from pv_wiki.state import StateStore  # noqa: E402


EVIDENCE_URL = "https://docs.acme.example/pv-42.pdf"
EVIDENCE_TEXT = "\n".join(
    (
        "PV-42 Rated power 42 W",
        "PV-42 Input voltage 48 V",
        "PV-42 Efficiency 98.5 %",
        "PV-42 Ingress protection IP65",
        "PV-42 Weight 12 kg",
    )
)
NONPUBLISH_URL = "https://catalog.acme.example/products/pv-42"
EVIDENCE_HMAC_KEY = b"pv-wiki-test-evidence-receipt-key-0001"


def evidence_document(url: str, content: str) -> dict[str, str]:
    return {
        "url": url,
        "content": content,
        "receipt": issue_evidence_receipt(
            EVIDENCE_HMAC_KEY,
            url=url,
            content=content,
        ),
    }


def pdf_parameter_rows() -> list[dict[str, object]]:
    rows = (
        ("Max. PV Array Power [Wp]@STC", "15000", "Wp", "Input (DC)"),
        ("MPPT Voltage Range [V]", "160-950", "V", "Input (DC)"),
        ("Rated AC Power [W]", "10000", "W", "Output (AC)"),
    )
    return [
        {
            "parameter_id": f"p{index:03d}",
            "model": "PV-42",
            "source_label": label,
            "value": value,
            "unit": unit,
            "section": section,
            "page": index,
            "order": index,
            "model_quote": "Type PV-42",
            "quote": f"{label} {value} {unit}",
            "table_title": "Technical data",
            "value_state": "explicit",
        }
        for index, (label, value, unit, section) in enumerate(rows, start=1)
    ]


def pdf_evidence_document() -> dict[str, object]:
    return {
        "url": EVIDENCE_URL,
        "content": EVIDENCE_TEXT,
        "receipt": issue_evidence_receipt_v2(
            EVIDENCE_HMAC_KEY,
            requested_url=EVIDENCE_URL,
            final_url=EVIDENCE_URL,
            redirect_chain=[EVIDENCE_URL],
            content=EVIDENCE_TEXT,
            artifact_sha256="a" * 64,
            parser_metadata={
                "contract_version": "2026-07-29.2",
                "page_count": 3,
                "extracted_pages": 3,
                "truncated": False,
            },
            target_models=["PV-42"],
            parameter_rows=pdf_parameter_rows(),
        ),
    }


def parameter_enrichment() -> dict[str, object]:
    return {
        "translations": [
            {
                "parameter_id": "p001",
                "name_zh": "最大光伏阵列功率 [Wp]（STC）",
                "section_zh": "直流输入（DC）",
                "subsection_zh": "",
                "value_zh": "",
            },
            {
                "parameter_id": "p002",
                "name_zh": "MPPT 电压范围 [V]",
                "section_zh": "直流输入（DC）",
                "subsection_zh": "",
                "value_zh": "",
            },
            {
                "parameter_id": "p003",
                "name_zh": "额定交流功率 [W]",
                "section_zh": "交流输出（AC）",
                "subsection_zh": "",
                "value_zh": "",
            },
        ],
        "sections": [
            {
                "section_code": "dc_input",
                "paragraphs": [
                    {
                        "analysis_kind": "engineering_interpretation",
                        "basis_parameter_ids": ["p001", "p002"],
                        "analysis_zh": (
                            "直流侧最大光伏阵列功率为 15000 Wp，MPPT "
                            "电压范围为 160–950 V；组串设计还需核对现场条件。"
                        ),
                        "conditions_zh": ["仅以制造商数据表参数为依据。"],
                        "limitations_zh": ["不能替代具体项目设计。"],
                    }
                ],
            }
        ],
        "overall_limitations_zh": [
            "本分析仅解释制造商数据表参数，不构成项目设计结论。"
        ],
    }


def publish_decision_v3() -> dict[str, object]:
    decision: dict[str, object] = publish_decision()
    decision.update(
        {
            "schema_version": "3",
            "manufacturer_zh": "艾克米",
            "product_description_zh": "面向光伏系统的并网逆变器产品。",
            "product_category_code": "inverter",
            "product_type": "并网光伏逆变器",
            "parameters": pdf_parameter_rows(),
            "parameter_enrichment": parameter_enrichment(),
            "derived_insights": [
                {
                    "name": "直交流容量比",
                    "value": 1.5,
                    "unit": "",
                    "formula": "15000 / 10000",
                    "basis_parameter_ids": ["p001", "p003"],
                    "explanation": "数据表参数计算得到的直流与交流额定容量比。",
                }
            ],
        }
    )
    return decision


def product(product_id: str = "P-42") -> dict:
    return {
        "product_id": product_id,
        "brand_code": "Acme",
        "family_code": "PV",
        "product_name": product_id.replace("P-", "PV-"),
        "unit_of_measure": "EA",
        "created_at": None,
        "updated_at": None,
    }


def publish_decision(product_id: str = "P-42") -> dict:
    model = product_id.replace("P-", "PV-")
    return {
        "schema_version": "2",
        "product_id": product_id,
        "outcome": "publish",
        "confidence": 0.96,
        "manufacturer": "Acme",
        "model": model,
        "product_category": "Solar inverter",
        "summary": f"Cited specifications for {model}.",
        "review_summary": "",
        "review_evidence_urls": [],
        "decision_notes": "The exact model is stated in the manufacturer document.",
        "datasheets": [
            {
                "url": EVIDENCE_URL,
                "title": f"{model} datasheet",
                "source_type": "manufacturer",
                "is_primary": True,
            }
        ],
        "sources": [],
        "facts": [
            {
                "name": name,
                "value": value,
                "unit": unit,
                "category": category,
                "confidence": 0.96,
                "evidence_urls": [EVIDENCE_URL],
                "evidence_quotes": [
                    {
                        "url": EVIDENCE_URL,
                        "quote": f"{model} {name} {value} {unit}".strip(),
                    }
                ],
            }
            for name, value, unit, category in (
                ("Rated power", 42, "W", "Output"),
                ("Input voltage", 48, "V", "Input"),
                ("Efficiency", 98.5, "%", "Efficiency"),
                ("Ingress protection", "IP65", "", "General"),
                ("Weight", 12, "kg", "General"),
            )
        ],
        "conflicts": [],
    }


def nonpublish_decision(
    product_id: str = "P-42", outcome: str = "no_datasheet"
) -> dict:
    model = product_id.replace("P-", "PV-")
    return {
        "schema_version": "2",
        "product_id": product_id,
        "outcome": outcome,
        "confidence": 0.7,
        "manufacturer": "",
        "model": "",
        "product_category": "",
        "summary": "",
        "review_summary": "",
        "decision_notes": "No public document for the exact model was found.",
        "datasheets": [],
        "sources": [],
        "facts": [],
        "conflicts": [],
        "conclusion_evidence": [
            {
                "url": NONPUBLISH_URL,
                "quote": f"Catalogue result for exact model {model}",
            }
        ],
    }


def nonpublish_evidence(product_id: str = "P-42") -> list[dict[str, str]]:
    model = product_id.replace("P-", "PV-")
    return [
        evidence_document(
            NONPUBLISH_URL,
            (
                f"Catalogue result for exact model {model}. "
                "No technical-document download is shown on this extracted page."
            ),
        )
    ]


class HermesCompletionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary.name) / "state.sqlite3"
        self.evidence_key_path = Path(self.temporary.name) / "evidence-hmac-key"
        self.evidence_key_path.write_bytes(EVIDENCE_HMAC_KEY)
        self.evidence_key_path.chmod(0o600)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_EVIDENCE_HMAC_KEY_FILE": str(self.evidence_key_path),
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON": '{"Acme":"Acme"}',
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON": (
                    '{"Acme":["acme.example"]}'
                ),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def add_products(self, *product_ids: str) -> dict[str, str]:
        hashes: dict[str, str] = {}
        with StateStore(self.state_path) as store:
            for product_id in product_ids:
                saved = store.upsert_product(product(product_id))
                hashes[product_id] = saved.source_hash
        return hashes

    @staticmethod
    def wiki_settings() -> WikiSettings:
        return WikiSettings(
            base_url="https://wiki.example",
            token="test-only-token",
            locale="en",
            path_prefix="products",
            home_path="home",
            home_title="PV Wiki",
            timeout=10.0,
            new_page_private=True,
            new_page_published=False,
        )

    def test_v3_signed_parameters_render_without_rewriting_completion(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        original = publish_decision_v3()
        with HermesCompletionStore(self.state_path) as store:
            saved = store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=original,
                evidence_documents=[pdf_evidence_document()],
            )
            completion = saved.completion
            self.assertEqual("3", completion.decision["schema_version"])
            self.assertEqual(64, len(completion.decision["parameter_set_sha256"]))
            stored_before = copy.deepcopy(completion.decision)
            rendered = RenderModel.from_completion(completion)
            self.assertEqual("逆变器", rendered.decision["product_category"])
            self.assertEqual(
                "最大光伏阵列功率 [Wp]（STC）",
                rendered.decision["datasheet_parameters"][0]["name_zh"],
            )
            self.assertEqual(
                3,
                rendered.decision["professional_analysis"][
                    "input_parameter_count"
                ],
            )
            self.assertEqual(stored_before, completion.decision)
            self.assertNotIn("datasheet_parameters", completion.decision)

    def test_v3_rejects_partial_enrichment_and_future_schema(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        partial = publish_decision_v3()
        partial["parameter_enrichment"]["translations"].pop()  # type: ignore[index]
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaisesRegex(
                CompletionValidationError,
                "invalid or incomplete",
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=partial,
                    evidence_documents=[pdf_evidence_document()],
                )

            future = publish_decision()
            future["schema_version"] = "4"
            with self.assertRaisesRegex(
                CompletionValidationError,
                "unsupported; refusing future schema",
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=future,
                    evidence_documents=[
                        evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)
                    ],
                )

    def test_v2_render_adapter_does_not_mutate_or_trigger_research(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        with HermesCompletionStore(self.state_path) as store:
            completion = store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=publish_decision(),
                evidence_documents=[
                    evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)
                ],
            ).completion
            before = copy.deepcopy(completion.decision)
            rendered = RenderModel.from_completion(completion)
            self.assertEqual(before, completion.decision)
            self.assertEqual(before, rendered.decision)
            self.assertNotIn("datasheet_parameters", rendered.decision)

    def test_suppress_resume_is_append_only_and_fenced(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        with HermesCompletionStore(self.state_path) as store:
            completion = store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=publish_decision(),
                evidence_documents=[
                    evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)
                ],
            ).completion
            initial_fence = store.publication_fence("P-42")
            suppressed_fence = store.append_control_event(
                "P-42",
                action="suppress",
                reason="operator hold",
                expected_fence=initial_fence,
            )
            self.assertTrue(store.is_suppressed("P-42"))
            self.assertIsNone(store.pending_publication())
            self.assertEqual(completion, store.get_completion("P-42"))
            with self.assertRaises(PublicationFenceError):
                store.append_control_event(
                    "P-42",
                    action="resume",
                    reason="stale operator action",
                    expected_fence=initial_fence,
                )
            resumed_fence = store.append_control_event(
                "P-42",
                action="resume",
                reason="review complete",
                expected_fence=suppressed_fence,
            )
            self.assertFalse(store.is_suppressed("P-42"))
            self.assertIsNotNone(store.pending_publication())
            self.assertNotEqual(suppressed_fence, resumed_fence)

    def test_rerender_preview_apply_and_missing_page_refusal(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        settings = self.wiki_settings()
        path = stable_path(product(), prefix=settings.path_prefix)
        page = {
            "id": 19,
            "path": path,
            "locale": "en",
            "content": "Human introduction\n\nHuman maintenance notes",
        }
        updates: list[str] = []

        class ExistingClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def get_page(self, *_args: object) -> dict[str, object]:
                return dict(page)

            def update_existing_page(
                self,
                _path: str,
                _locale: str,
                _title: str,
                _description: str,
                managed: str,
                _tags: list[str],
            ) -> dict[str, object]:
                updates.append(managed)
                return {"action": "updated", "page": dict(page)}

        with HermesCompletionStore(self.state_path) as store:
            store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=publish_decision(),
                evidence_documents=[
                    evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)
                ],
            )
            store.mark_published(
                "P-42",
                wiki_path=path,
                wiki_action="created",
            )
            self.assertEqual("direct", store.publication_status("P-42")["mode"])
            preview = rerender_completion(
                store,
                "P-42",
                settings=settings,
                client_factory=ExistingClient,
            )
            self.assertTrue(preview["preview"])
            self.assertEqual([], updates)
            applied = rerender_completion(
                store,
                "P-42",
                apply=True,
                settings=settings,
                client_factory=ExistingClient,
            )
            self.assertTrue(applied["applied"])
            self.assertEqual(1, len(updates))
            self.assertEqual("rerender", store.publication_status("P-42")["mode"])

            class MissingClient(ExistingClient):
                def get_page(self, *_args: object) -> None:
                    return None

            with self.assertRaisesRegex(CompletionError, "create is refused"):
                rerender_completion(
                    store,
                    "P-42",
                    apply=True,
                    settings=settings,
                    client_factory=MissingClient,
                )

    def test_startup_backfills_legacy_success_and_skips_it(self) -> None:
        hashes = self.add_products("P-42", "P-43")
        del hashes
        with StateStore(self.state_path) as legacy:
            lease = legacy.lease_next("legacy-worker")
            self.assertIsNotNone(lease)
            assert lease is not None
            legacy.record_outcome(
                lease,
                "synced",
                payload={
                    "decision": {"outcome": "publish"},
                    "wiki_action": "created",
                },
                wiki_path="products/pv-42-old",
                now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

        with HermesCompletionStore(self.state_path) as store:
            migrated = store.get_completion(lease.product_id)
            self.assertIsNotNone(migrated)
            assert migrated is not None
            self.assertTrue(migrated.publishable)
            self.assertIsNotNone(migrated.published_at)
            self.assertEqual("products/pv-42-old", migrated.wiki_path)
            selected = store.next_product()
            self.assertIsNotNone(selected)
            assert selected is not None
            self.assertNotEqual(lease.product_id, selected.product_id)

        # Reopening the MCP store must use the NOT EXISTS anti-join instead of
        # reparsing every historical success on every tool call.
        with mock.patch.object(
            HermesCompletionStore,
            "_decoded_object",
            wraps=HermesCompletionStore._decoded_object,
        ) as decode:
            with HermesCompletionStore(self.state_path) as reopened:
                self.assertIsNotNone(reopened.get_completion(lease.product_id))
        decode.assert_not_called()

    def test_nonpublish_outcome_is_completed_and_not_pending(self) -> None:
        hashes = self.add_products("P-42", "P-43")
        with HermesCompletionStore(self.state_path) as store:
            saved = store.save_completion(
                product_id="P-42",
                source_hash=hashes["P-42"],
                decision=nonpublish_decision(),
                evidence_documents=nonpublish_evidence(),
            )
            self.assertTrue(saved.created)
            self.assertFalse(saved.completion.publishable)
            self.assertEqual(1, len(saved.completion.evidence))
            self.assertEqual(
                "Catalogue result for exact model PV-42",
                saved.completion.decision["conclusion_evidence"][0]["quote"],
            )
            self.assertIsNone(store.pending_publication())
            selected = store.next_product()
            self.assertIsNotNone(selected)
            assert selected is not None
            self.assertEqual("P-43", selected.product_id)

    def test_unfinished_products_use_fair_process_cursor_with_wraparound(self) -> None:
        self.add_products("P-42", "P-43", "P-44")
        with HermesCompletionStore(self.state_path) as store:
            self.assertEqual("P-42", store.next_product().product_id)
            self.assertEqual(
                "P-43",
                store.next_product(after_product_id="P-42").product_id,
            )
            self.assertEqual(
                "P-42",
                store.next_product(after_product_id="P-99").product_id,
            )

    def test_every_nonpublish_outcome_requires_auditable_evidence(self) -> None:
        product_ids = ("P-42", "P-43", "P-44", "P-45")
        hashes = self.add_products(*product_ids)
        outcomes = (
            "no_datasheet",
            "ambiguous",
            "insufficient_identity",
            "out_of_scope",
        )
        with HermesCompletionStore(self.state_path) as store:
            for product_id, outcome in zip(product_ids, outcomes, strict=True):
                with self.subTest(outcome=outcome):
                    decision = nonpublish_decision(product_id, outcome)
                    if outcome == "out_of_scope":
                        decision["product_category"] = "Fastener"
                        decision["summary"] = "The exact item is a screw."
                    with self.assertRaisesRegex(
                        CompletionValidationError,
                        "require extracted evidence",
                    ):
                        store.save_completion(
                            product_id=product_id,
                            source_hash=hashes[product_id],
                            decision=decision,
                            evidence_documents=[],
                        )

    def test_permanent_nonpublish_evidence_quote_must_identify_product(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        with HermesCompletionStore(self.state_path) as store:
            for outcome in (
                "no_datasheet",
                "ambiguous",
                "insufficient_identity",
            ):
                decision = nonpublish_decision(outcome=outcome)
                decision["conclusion_evidence"][0]["quote"] = (
                    "No technical-document download is shown"
                )
                with self.subTest(outcome=outcome), self.assertRaisesRegex(
                    CompletionValidationError,
                    "must identify the catalogue product",
                ):
                    store.save_completion(
                        product_id="P-42",
                        source_hash=source_hash,
                        decision=decision,
                        evidence_documents=nonpublish_evidence(),
                    )

    def test_nonpublish_accepts_more_than_five_extracted_urls(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        evidence = nonpublish_evidence()
        evidence.extend(
            evidence_document(
                f"https://research-{index}.example/pv-42",
                f"PV-42 research result {index}",
            )
            for index in range(6)
        )
        with HermesCompletionStore(self.state_path) as store:
            saved = store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=nonpublish_decision(),
                evidence_documents=evidence,
            )
        self.assertTrue(saved.created)
        self.assertEqual(7, len(saved.completion.evidence))

    def test_save_rejects_missing_malformed_or_tampered_receipts(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        content = nonpublish_evidence()[0]["content"]
        valid = evidence_document(NONPUBLISH_URL, content)
        cases: dict[str, dict[str, str]] = {
            "missing": {"url": valid["url"], "content": valid["content"]},
            "malformed": {**valid, "receipt": "not-a-receipt"},
            "content": {**valid, "content": valid["content"] + " changed"},
            "url": {**valid, "url": "https://catalog.acme.example/other"},
            "receipt": {
                **valid,
                "receipt": valid["receipt"][:-1]
                + ("A" if valid["receipt"][-1] != "A" else "B"),
            },
        }
        with HermesCompletionStore(self.state_path) as store:
            for name, document in cases.items():
                with self.subTest(name=name), self.assertRaisesRegex(
                    CompletionValidationError,
                    "receipt is invalid",
                ):
                    store.save_completion(
                        product_id="P-42",
                        source_hash=source_hash,
                        decision=nonpublish_decision(),
                        evidence_documents=[document],
                    )

    def test_idempotent_replay_still_requires_receipts(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        with HermesCompletionStore(self.state_path) as store:
            store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=nonpublish_decision(),
                evidence_documents=nonpublish_evidence(),
            )
            with self.assertRaisesRegex(
                CompletionValidationError,
                "receipt is invalid",
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=nonpublish_decision(),
                    evidence_documents=[
                        {
                            "url": NONPUBLISH_URL,
                            "content": nonpublish_evidence()[0]["content"],
                        }
                    ],
                )

    def test_save_replay_does_not_overwrite_first_completion(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        with HermesCompletionStore(self.state_path) as store:
            first = store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=nonpublish_decision(outcome="ambiguous"),
                evidence_documents=nonpublish_evidence(),
            )
            replay = store.save_completion(
                product_id="P-42",
                source_hash="stale-replay-is-ignored",
                decision=nonpublish_decision(outcome="out_of_scope"),
                evidence_documents=nonpublish_evidence(),
            )
        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual("ambiguous", replay.completion.outcome)

    def test_concurrent_save_has_exactly_one_insert_winner(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        # Initialize the table before racing only the completion boundary.
        with HermesCompletionStore(self.state_path):
            pass
        barrier = threading.Barrier(2)
        created: list[bool] = []
        failures: list[BaseException] = []

        def save() -> None:
            try:
                with HermesCompletionStore(self.state_path) as store:
                    barrier.wait(timeout=5)
                    result = store.save_completion(
                        product_id="P-42",
                        source_hash=source_hash,
                        decision=nonpublish_decision(outcome="ambiguous"),
                        evidence_documents=nonpublish_evidence(),
                    )
                    created.append(result.created)
            except BaseException as exc:  # surfaced in the main test thread
                failures.append(exc)

        threads = [threading.Thread(target=save) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(failures)
        self.assertEqual([False, True], sorted(created))
        with HermesCompletionStore(self.state_path) as store:
            self.assertEqual(1, store.counts()["completed"])

    def test_rejects_private_evidence_url(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        decision = publish_decision()
        decision["datasheets"][0]["url"] = "http://127.0.0.1/private"
        decision["facts"][0]["evidence_urls"] = ["http://127.0.0.1/private"]
        decision["facts"][0]["evidence_quotes"][0]["url"] = (
            "http://127.0.0.1/private"
        )
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaises(CompletionValidationError):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=[
                        {"url": "http://127.0.0.1/private", "content": EVIDENCE_TEXT}
                    ],
                )

    def test_rejects_nonstandard_evidence_port(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        decision = nonpublish_decision()
        decision["conclusion_evidence"][0]["url"] = (
            "https://catalog.acme.example:8443/products/pv-42"
        )
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaisesRegex(
                CompletionValidationError,
                "non-standard port",
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=[
                        {
                            "url": "https://catalog.acme.example:8443/products/pv-42",
                            "content": "Catalogue result for exact model PV-42",
                        }
                    ],
                )

    def test_publish_model_must_match_catalogue_identity(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        decision = publish_decision()
        decision["model"] = "PV-43"
        for fact in decision["facts"]:
            fact["evidence_quotes"][0]["quote"] = fact["evidence_quotes"][0][
                "quote"
            ].replace("PV-42", "PV-43")
        evidence_text = EVIDENCE_TEXT.replace("PV-42", "PV-43")
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaisesRegex(
                CompletionValidationError,
                "not bound to the catalogue identity",
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=[
                        evidence_document(EVIDENCE_URL, evidence_text)
                    ],
                )

    def test_publish_manufacturer_must_match_catalogue_brand_alias(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        decision = publish_decision()
        decision["manufacturer"] = "Impostor Industries"
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaisesRegex(
                CompletionValidationError,
                "catalogue brand identity",
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=[
                        evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)
                    ],
                )

    def test_self_declared_manufacturer_source_is_not_trusted(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        untrusted_url = "https://attacker.example/pv-42.pdf"
        decision = publish_decision()
        decision["datasheets"][0]["url"] = untrusted_url
        for fact in decision["facts"]:
            fact["evidence_urls"] = [untrusted_url]
            fact["evidence_quotes"][0]["url"] = untrusted_url
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaisesRegex(
                CompletionValidationError,
                "operator-configured trusted domain",
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=[
                        evidence_document(untrusted_url, EVIDENCE_TEXT)
                    ],
                )

    def test_unregistered_manufacturer_domain_cannot_self_authorize(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        manufacturer_url = "https://acme.example/pv-42.pdf"
        corroborating_url = "https://regulator.example/acme-pv-42"
        decision = publish_decision()
        decision["datasheets"][0]["url"] = manufacturer_url
        for fact in decision["facts"]:
            fact["evidence_urls"] = [manufacturer_url]
            fact["evidence_quotes"][0]["url"] = manufacturer_url
        manufacturer_text = f"Acme product documentation\n{EVIDENCE_TEXT}"
        with mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON": "{}",
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON": "{}",
            },
        ):
            with HermesCompletionStore(self.state_path) as store:
                with self.assertRaisesRegex(
                    CompletionValidationError,
                    "operator-approved catalogue brand identity",
                ):
                    store.save_completion(
                        product_id="P-42",
                        source_hash=source_hash,
                        decision=decision,
                        evidence_documents=[
                            evidence_document(manufacturer_url, manufacturer_text)
                        ],
                    )

                decision["sources"].append(
                    {
                        "url": corroborating_url,
                        "title": "Independent Acme PV-42 registration",
                        "source_type": "regulatory",
                    }
                )
                with self.assertRaisesRegex(
                    CompletionValidationError,
                    "operator-approved catalogue brand identity",
                ):
                    store.save_completion(
                        product_id="P-42",
                        source_hash=source_hash,
                        decision=decision,
                        evidence_documents=[
                            evidence_document(manufacturer_url, manufacturer_text),
                            evidence_document(
                                corroborating_url,
                                "Regulatory listing for Acme model PV-42.",
                            ),
                        ],
                    )

    def test_every_fact_needs_quote_from_trusted_primary_datasheet(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        secondary_url = "https://independent.example/pv-42-review"
        decision = publish_decision()
        decision["sources"].append(
            {
                "url": secondary_url,
                "title": "PV-42 technical review",
                "source_type": "mirror",
            }
        )
        first = decision["facts"][0]
        first["evidence_urls"] = [secondary_url]
        first["evidence_quotes"][0]["url"] = secondary_url
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaisesRegex(
                CompletionValidationError,
                r"facts\[0\].*trusted primary",
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=[
                        evidence_document(EVIDENCE_URL, EVIDENCE_TEXT),
                        evidence_document(secondary_url, EVIDENCE_TEXT),
                    ],
                )

    def test_fact_quote_must_semantically_support_claimed_value(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        decision = publish_decision()
        decision["facts"][0]["value"] = 999
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaisesRegex(
                CompletionValidationError,
                "does not bind the exact model, fact name, and value",
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=[
                        evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)
                    ],
                )

    def test_publish_and_fact_confidence_thresholds_are_preserved(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        for field in ("decision", "fact"):
            with self.subTest(field=field):
                decision = publish_decision()
                if field == "decision":
                    decision["confidence"] = 0.1
                    message = "publish confidence"
                else:
                    decision["facts"][0]["confidence"] = 0.1
                    message = r"facts\[0\].confidence"
                with HermesCompletionStore(self.state_path) as store:
                    with self.assertRaisesRegex(
                        CompletionValidationError,
                        message,
                    ):
                        store.save_completion(
                            product_id="P-42",
                            source_hash=source_hash,
                            decision=decision,
                            evidence_documents=[
                                evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)
                            ],
                        )

    def test_specification_citations_must_be_declared_sources(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        other_url = "https://reviews.example/pv-42"
        decision = publish_decision()
        decision["facts"][0]["evidence_urls"] = [other_url]
        decision["facts"][0]["evidence_quotes"] = []
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaisesRegex(
                CompletionValidationError, "outside the supplied source set"
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=[
                        evidence_document(EVIDENCE_URL, EVIDENCE_TEXT),
                        evidence_document(other_url, "Independent review."),
                    ],
                )

    def test_publish_fact_requires_an_exact_evidence_quote(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        decision = publish_decision()
        decision["facts"][0]["evidence_quotes"] = []
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaisesRegex(
                CompletionValidationError, "evidence_quotes must include"
            ):
                store.save_completion(
                    product_id="P-42",
                    source_hash=source_hash,
                    decision=decision,
                    evidence_documents=[
                        evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)
                    ],
                )

    def test_exact_evidence_quote_preserves_internal_newline(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        decision = publish_decision()
        exact_quote = "PV-42 rated power\nis 42 W"
        decision["facts"][0]["evidence_quotes"][0]["quote"] = exact_quote
        with HermesCompletionStore(self.state_path) as store:
            saved = store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=decision,
                evidence_documents=[
                    evidence_document(
                        EVIDENCE_URL,
                        f"{EVIDENCE_TEXT}\n{exact_quote}",
                    )
                ],
            )
        self.assertTrue(saved.created)
        self.assertEqual(
            exact_quote,
            saved.completion.decision["facts"][0]["evidence_quotes"][0]["quote"],
        )

    def test_rejects_changed_source_hash(self) -> None:
        self.add_products("P-42")
        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaises(ProductSourceChangedError):
                store.save_completion(
                    product_id="P-42",
                    source_hash="old-source-hash",
                    decision=nonpublish_decision(),
                    evidence_documents=nonpublish_evidence(),
                )

    def test_manual_refresh_rejects_a_stale_selected_snapshot(self) -> None:
        old_hash = self.add_products("P-42")["P-42"]
        changed = product("P-42")
        changed["product_name"] = "PV-42 revised"
        with StateStore(self.state_path) as state:
            replace_catalogue_snapshot(state, [changed])

        with HermesCompletionStore(self.state_path) as store:
            with self.assertRaises(ProductSourceChangedError):
                store.save_completion(
                    product_id="P-42",
                    source_hash=old_hash,
                    decision=nonpublish_decision(),
                    evidence_documents=nonpublish_evidence(),
                )

    def test_completed_product_stays_complete_after_removal_and_readdition(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        with HermesCompletionStore(self.state_path) as store:
            store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=nonpublish_decision(),
                evidence_documents=nonpublish_evidence(),
            )

        with StateStore(self.state_path) as state:
            replace_catalogue_snapshot(state, [product("P-43")])
        with HermesCompletionStore(self.state_path) as store:
            self.assertIsNotNone(store.get_completion("P-42"))
            selected = store.next_product()
            self.assertIsNotNone(selected)
            assert selected is not None
            self.assertEqual("P-43", selected.product_id)

        with StateStore(self.state_path) as state:
            replace_catalogue_snapshot(
                state,
                [product("P-42"), product("P-43")],
            )
        with HermesCompletionStore(self.state_path) as store:
            selected = store.next_product()
            self.assertIsNotNone(selected)
            assert selected is not None
            self.assertEqual("P-43", selected.product_id)

    def test_snapshot_removal_does_not_cancel_a_completed_publication(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        with HermesCompletionStore(self.state_path) as store:
            store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=publish_decision(),
                evidence_documents=[evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)],
            )

        with StateStore(self.state_path) as state:
            replace_catalogue_snapshot(state, [product("P-43")])
        with HermesCompletionStore(self.state_path) as store:
            pending = store.pending_publication()
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual("P-42", pending.product_id)

    def test_publish_failure_remains_pending_without_reresearch(self) -> None:
        source_hash = self.add_products("P-42")["P-42"]
        settings = WikiSettings(
            base_url="https://wiki.example",
            token="secret",
            locale="en",
            path_prefix="products",
            home_path="home",
            home_title="PV Wiki",
            timeout=10.0,
            new_page_private=True,
            new_page_published=False,
        )

        class FailingClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def upsert_page(self, *_args: object, **_kwargs: object) -> dict:
                raise RuntimeError("Wiki unavailable")

        calls: list[tuple[object, ...]] = []

        class SuccessfulClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def upsert_page(self, *args: object, **_kwargs: object) -> dict:
                calls.append(args)
                return {"action": "created", "page": {"id": 7}}

        with HermesCompletionStore(self.state_path) as store:
            store.save_completion(
                product_id="P-42",
                source_hash=source_hash,
                decision=publish_decision(),
                evidence_documents=[
                    evidence_document(EVIDENCE_URL, EVIDENCE_TEXT)
                ],
            )
            with self.assertRaisesRegex(RuntimeError, "Wiki unavailable"):
                publish_researched(
                    store,
                    "P-42",
                    settings=settings,
                    client_factory=FailingClient,
                )
            self.assertIsNotNone(store.pending_publication())
            self.assertIsNone(store.next_product())

            published = publish_researched(
                store,
                "P-42",
                settings=settings,
                client_factory=SuccessfulClient,
                now=datetime(2026, 2, 3, tzinfo=timezone.utc),
            )
            self.assertTrue(published["published"])
            self.assertEqual("created", published["wiki"]["action"])
            self.assertEqual(1, len(calls))

            replay = publish_researched(
                store,
                "P-42",
                settings=settings,
                client_factory=lambda *_args, **_kwargs: self.fail(
                    "already-published completion must not call Wiki.js"
                ),
            )
            self.assertTrue(replay["published"])
            self.assertFalse(replay["changed"])
            self.assertEqual(1, len(calls))

    def test_next_product_helper_uses_local_snapshot_without_refresh(self) -> None:
        self.add_products("P-42")

        payload = _next_product(
            store_factory=lambda: HermesCompletionStore(self.state_path),
        )
        self.assertTrue(payload["found"])
        self.assertEqual("P-42", payload["product_id"])
        self.assertTrue(payload["catalogue"]["initialized"])

    def test_mcp_v2_server_builds_exact_five_tools(self) -> None:
        self.add_products("P-42", "P-43")
        registered: dict[str, tuple[object, dict[str, object]]] = {}

        class FakeAnnotations:
            @classmethod
            def model_validate(cls, value: dict[str, object]) -> dict[str, object]:
                return value

        class FakeTextContent:
            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class FakeCallToolResult:
            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class FakeServer:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def tool(self, **metadata: object):
                def decorate(function: object) -> object:
                    registered[str(metadata["name"])] = (function, metadata)
                    return function

                return decorate

        mcp_package = types.ModuleType("mcp")
        mcp_package.__path__ = []  # type: ignore[attr-defined]
        mcp_types = types.ModuleType("mcp.types")
        mcp_types.ToolAnnotations = FakeAnnotations
        mcp_types.TextContent = FakeTextContent
        mcp_types.CallToolResult = FakeCallToolResult
        mcp_server = types.ModuleType("mcp.server")
        mcp_server.MCPServer = FakeServer
        with mock.patch.dict(
            sys.modules,
            {
                "mcp": mcp_package,
                "mcp.types": mcp_types,
                "mcp.server": mcp_server,
            },
        ):
            server = research_mcp.build_server(
                store_factory=lambda: HermesCompletionStore(self.state_path),
            )
        self.assertIsInstance(server, FakeServer)
        self.assertEqual(
            {
                "pv_pending_publication",
                "pv_next_product",
                "pv_save_research",
                "pv_publish_result",
                "pv_research_status",
            },
            set(registered),
        )
        next_tool = registered["pv_next_product"][0]
        self.assertEqual({}, dict(inspect.signature(next_tool).parameters))
        first = json.loads(next_tool().content[0].text)  # type: ignore[operator]
        second = json.loads(next_tool().content[0].text)  # type: ignore[operator]
        third = json.loads(next_tool().content[0].text)  # type: ignore[operator]
        self.assertEqual("P-42", first["product_id"])
        self.assertEqual("P-43", second["product_id"])
        self.assertEqual("P-42", third["product_id"])

    def test_real_mcp_v2_build_and_tool_runner(self) -> None:
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("MCP runtime is not installed")
        server = research_mcp.build_server(
            store_factory=lambda: HermesCompletionStore(self.state_path),
        )
        tools = server._tool_manager._tools
        self.assertEqual(
            {
                "pv_pending_publication",
                "pv_next_product",
                "pv_save_research",
                "pv_publish_result",
                "pv_research_status",
            },
            set(tools),
        )
        # The registered implementation is always exercised.  The restricted
        # host runner cannot wake an asyncio loop from any worker thread, so a
        # network-disabled local Docker validation opts into the real MCP
        # async tool runner and covers that bridge on this same machine.
        if os.environ.get("PV_WIKI_TEST_REAL_MCP_RUNNER") == "1":
            # MCP v2's runner requires a context. None is safe because this
            # zero-argument tool declares no context parameter or resolver.
            output = asyncio.run(
                tools["pv_research_status"].run({}, None)
            )
        else:
            output = tools["pv_research_status"].fn()
        self.assertFalse(output.is_error)
        payload = json.loads(output.content[0].text)
        self.assertEqual(0, payload["counts"]["completed"])


if __name__ == "__main__":
    unittest.main()
