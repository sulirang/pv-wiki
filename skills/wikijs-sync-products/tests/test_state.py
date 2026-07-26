from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import state  # noqa: E402


T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def product(product_id="P-1", *, name="Panel", updated_at=T0):
    return {
        "product_id": product_id,
        "brand_code": "ACME",
        "family_code": "PV",
        "product_name": name,
        "unit_of_measure": "EA",
        "created_at": T0 - timedelta(days=10),
        "updated_at": updated_at,
    }


class StateStoreTests(unittest.TestCase):
    def test_creates_missing_parent_for_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "existing"
            existing.mkdir(mode=0o751)
            if os.name == "posix":
                existing.chmod(0o751)
            path = existing / "nested" / "deeper" / "state.sqlite3"
            store = state.StateStore(path)
            self.assertTrue(path.exists())
            store.close()
            if os.name == "posix":
                self.assertEqual(existing.stat().st_mode & 0o777, 0o751)
                self.assertEqual(path.parent.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "state.sqlite3"
        self.store = state.StateStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_schema_and_source_hash_upsert_are_idempotent(self):
        self.assertEqual(self.store.schema_version, state.SCHEMA_VERSION)
        first = self.store.upsert_product(product(), now=T0)
        replay = self.store.upsert_product(dict(reversed(list(product().items()))), now=T0)

        self.assertTrue(first.created)
        self.assertTrue(first.changed)
        self.assertFalse(replay.created)
        self.assertFalse(replay.changed)
        self.assertEqual(first.source_hash, replay.source_hash)
        self.assertTrue(self.store.is_due("P-1", now=T0))
        self.assertEqual(self.store.status_counts(now=T0)["due"], 1)

    def test_changed_database_row_is_rescheduled_immediately(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("worker-a", now=T0)
        result = self.store.record_outcome(lease, "synced", now=T0)
        self.assertEqual(
            result.next_attempt_at,
            T0 + timedelta(days=state.SYNC_REFRESH_DAYS),
        )
        self.assertFalse(self.store.is_due("P-1", now=T0 + timedelta(days=1)))

        changed_at = T0 + timedelta(days=2)
        changed = self.store.upsert_product(
            product(name="Panel revision B", updated_at=changed_at),
            now=changed_at,
        )

        self.assertTrue(changed.changed)
        self.assertTrue(changed.rescheduled)
        current = self.store.get_product("P-1")
        self.assertEqual(current.status, "due")
        self.assertEqual(current.consecutive_failures, 0)
        self.assertTrue(self.store.is_due("P-1", now=changed_at))

    def test_atomic_lease_precheck_lookup_and_success_refresh(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("worker-a", lease_seconds=60, now=T0)

        self.assertIsNotNone(lease)
        self.assertTrue(self.store.precheck(lease, now=T0).ready)
        looked_up = self.store.get_by_lease(lease.token, now=T0)
        self.assertEqual(looked_up, lease)
        self.assertIsNone(self.store.lease_next("worker-b", now=T0))

        outcome = self.store.record_outcome(
            product_id="P-1",
            lease_token=lease.token,
            outcome="success",
            payload={"sources": 2},
            wiki_path="/products/p-1",
            now=T0 + timedelta(seconds=1),
        )
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.status, "synced")
        refresh_at = T0 + timedelta(
            seconds=1,
            days=state.SYNC_REFRESH_DAYS,
        )
        self.assertEqual(outcome.next_run_at, refresh_at)
        self.assertFalse(
            self.store.is_due(
                "P-1", now=refresh_at - timedelta(microseconds=1)
            )
        )
        self.assertTrue(self.store.is_due("P-1", now=refresh_at))

        history = self.store.attempt_history("P-1")
        self.assertEqual(history[0].outcome, "synced")
        self.assertEqual(history[0].details["wiki_path"], "/products/p-1")
        self.assertEqual(history[0].payload["product_name"], "Panel")

    def test_published_products_uses_latest_success_not_queue_status(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("worker-a", now=T0)
        self.store.record_outcome(
            lease,
            "synced",
            payload={
                "decision": {
                    "manufacturer": "Acme Public",
                    "model": "Panel One",
                    "product_category": "光伏组件",
                }
            },
            wiki_path="products/p-1-a1",
            now=T0 + timedelta(minutes=1),
        )

        changed_at = T0 + timedelta(days=1)
        self.store.upsert_product(
            product(name="Panel revision B", updated_at=changed_at),
            now=changed_at,
        )

        current = self.store.get_product("P-1")
        self.assertEqual("due", current.status)
        published = self.store.published_products()
        self.assertEqual(1, len(published))
        self.assertEqual("P-1", published[0].product_id)
        self.assertEqual("Acme Public", published[0].decision["manufacturer"])
        self.assertEqual("products/p-1-a1", published[0].wiki_path)
        self.assertEqual(T0 + timedelta(minutes=1), published[0].published_at)

        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.store.published_products(limit=0)

    def test_failures_back_off_30_90_then_180_days(self):
        self.store.upsert_product(product(), now=T0)
        current = T0
        expected_delays = [30, 90, 180, 180]

        for failure_number, expected_days in enumerate(expected_delays, start=1):
            lease = self.store.lease_next("worker-a", now=current)
            self.assertIsNotNone(lease)
            outcome = self.store.record_outcome(
                lease,
                "no_datasheet",
                error="no official source",
                now=current,
            )
            self.assertEqual(outcome.status, "backoff")
            self.assertEqual(outcome.consecutive_failures, failure_number)
            self.assertEqual(outcome.next_run_at, current + timedelta(days=expected_days))
            current = outcome.next_run_at

    def test_out_of_scope_is_skipped_until_the_annual_source_refresh(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("scope-worker", now=T0)

        outcome = self.store.record_outcome(lease, "out_of_scope", now=T0)

        self.assertEqual("backoff", outcome.status)
        self.assertEqual(
            T0 + timedelta(days=state.SYNC_REFRESH_DAYS),
            outcome.next_run_at,
        )

    def test_source_unverified_retries_without_creating_a_manual_queue(self):
        self.store.upsert_product(product(), now=T0)
        current = T0
        for expected_days in (7, 30, 90, 365, 365):
            lease = self.store.lease_next("source-worker", now=current)
            outcome = self.store.record_outcome(
                lease,
                "source_unverified",
                now=current,
            )
            self.assertEqual("backoff", outcome.status)
            self.assertEqual(
                current + timedelta(days=expected_days),
                outcome.next_run_at,
            )
            current = outcome.next_run_at

        self.assertEqual(
            {"source_unverified": 5},
            self.store.outcome_counts(),
        )

    def test_transient_and_conflict_outcomes_use_short_backoff(self):
        self.store.upsert_product(product(), now=T0)
        current = T0
        outcomes = (
            "search_error",
            "wikijs_error",
            "error",
            "invalid_decision",
            "publish_error",
        )
        expected_hours = (1, 1, 1, 24, 1)
        for failure_number, (name, hours) in enumerate(
            zip(outcomes, expected_hours),
            start=1,
        ):
            lease = self.store.lease_next("worker-a", now=current)
            result = self.store.record_outcome(lease, name, now=current)
            self.assertEqual(result.consecutive_failures, failure_number)
            self.assertEqual(result.next_run_at, current + timedelta(hours=hours))
            current = result.next_run_at

        recovered = self.store.lease_next("worker-a", now=current)
        self.store.record_outcome(recovered, "synced", now=current)

        conflict_start = T0 + timedelta(days=10)
        self.store.upsert_product(product("P-CONFLICT"), now=conflict_start)
        for _ in range(2):
            lease = self.store.lease_next("worker-b", now=conflict_start)
            result = self.store.record_outcome(
                lease,
                "wikijs_conflict",
                now=conflict_start,
            )
            self.assertEqual(result.next_run_at, conflict_start + timedelta(hours=24))
            conflict_start = result.next_run_at

        self.store.upsert_product(product("P-AI"), now=T0)
        ai_lease = self.store.lease_next("worker-ai", now=T0)
        ai_result = self.store.record_outcome(ai_lease, "ai_error", now=T0)
        self.assertEqual(T0 + timedelta(hours=1), ai_result.next_run_at)

    def test_legacy_quota_outcome_can_still_be_resumed(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("worker-quota", now=T0)

        result = self.store.record_outcome(
            lease,
            "tavily_quota_exhausted",
            error="monthly quota exhausted",
            now=T0,
        )

        self.assertEqual("backoff", result.status)
        self.assertEqual(0, result.consecutive_failures)
        self.assertEqual(
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            result.next_attempt_at,
        )
        self.assertFalse(
            self.store.is_due(
                "P-1",
                now=datetime(2026, 1, 31, 23, 59, tzinfo=timezone.utc),
            )
        )
        self.assertTrue(
            self.store.is_due(
                "P-1",
                now=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
        )

        resumed_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
        self.assertEqual(1, self.store.resume_search_quota_waits(now=resumed_at))
        self.assertEqual(0, self.store.resume_search_quota_waits(now=resumed_at))
        current = self.store.get_product("P-1")
        self.assertEqual("due", current.status)
        self.assertEqual(0, current.consecutive_failures)
        self.assertTrue(self.store.is_due("P-1", now=resumed_at))

    def test_generic_search_quota_uses_the_same_nonfailure_pause(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("worker-exa-quota", now=T0)

        result = self.store.record_outcome(
            lease,
            "search_quota_exhausted",
            error="provider spend budget exhausted",
            now=T0,
        )

        self.assertEqual("backoff", result.status)
        self.assertEqual(0, result.consecutive_failures)
        self.assertEqual(
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            result.next_attempt_at,
        )
        resumed_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
        self.assertEqual(
            1,
            self.store.resume_search_quota_waits(now=resumed_at),
        )

    def test_search_and_extract_audit_bind_completed_evidence(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("worker-a", lease_seconds=3600, now=T0)

        self.assertEqual(self.store.begin_search(lease, now=T0), T0)
        candidates = self.store.finish_search(
            lease,
            [
                "HTTPS://Manufacturer.Example:443/a.pdf#page=2",
                "https://docs.example/manual",
                "https://docs.example/manual",
            ],
            {"credits": 3, "request_ids": ["request-1"]},
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(
            candidates,
            [
                "https://manufacturer.example/a.pdf",
                "https://docs.example/manual",
            ],
        )
        self.assertEqual(
            self.store.allowed_evidence_urls(
                lease.token,
                now=T0 + timedelta(seconds=1),
            ),
            [],
        )

        selected = self.store.begin_extract(
            lease.token,
            ["https://MANUFACTURER.example:443/a.pdf#ignored"],
            now=T0 + timedelta(seconds=2),
        )
        self.assertEqual(selected, ["https://manufacturer.example/a.pdf"])
        self.assertEqual(
            self.store.allowed_evidence_urls(
                lease.token,
                now=T0 + timedelta(seconds=2),
            ),
            [],
        )
        self.assertEqual(
            self.store.finish_extract(
                lease,
                selected,
                {"credits": 1},
                now=T0 + timedelta(seconds=3),
            ),
            selected,
        )
        self.assertEqual(
            self.store.allowed_evidence_urls(
                lease.token,
                now=T0 + timedelta(seconds=3),
            ),
            selected,
        )

        attempt = self.store.attempt_history("P-1")[0]
        self.assertEqual(attempt.search_started_at, T0)
        self.assertEqual(attempt.search_urls, candidates)
        self.assertEqual(attempt.search_usage["credits"], 3)
        self.assertEqual(attempt.extract_started_at, T0 + timedelta(seconds=2))
        self.assertEqual(attempt.extract_urls, selected)
        self.assertEqual(attempt.extract_success_urls, selected)
        self.assertEqual(attempt.extract_usage, {"credits": 1})

    def test_web_budget_is_one_shot_and_ordered_fail_closed(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("worker-a", lease_seconds=3600, now=T0)

        with self.assertRaises(state.AttemptBudgetError):
            self.store.finish_search(lease, [], {}, now=T0)
        with self.assertRaises(state.AttemptBudgetError):
            self.store.begin_extract(
                lease,
                ["https://example.com/a"],
                now=T0,
            )

        self.store.begin_search(lease, now=T0)
        with self.assertRaises(state.AttemptBudgetError):
            self.store.begin_search(lease.token, now=T0)
        with self.assertRaises(state.AttemptBudgetError):
            self.store.begin_extract(
                lease,
                ["https://example.com/a"],
                now=T0,
            )

        search_urls = [f"https://example.com/{index}" for index in range(6)]
        self.store.finish_search(lease, search_urls, {"credits": 3}, now=T0)
        # Exact finish replay is idempotent, but a different audit is rejected.
        self.store.finish_search(lease, search_urls, {"credits": 3}, now=T0)
        with self.assertRaises(state.AttemptBudgetError):
            self.store.finish_search(
                lease,
                search_urls[:1],
                {"credits": 3},
                now=T0,
            )
        with self.assertRaises(ValueError):
            self.store.begin_extract(lease, search_urls, now=T0)
        with self.assertRaises(state.AttemptBudgetError):
            self.store.begin_extract(
                lease,
                ["https://outside.example/not-searched"],
                now=T0,
            )
        with self.assertRaises(state.AttemptBudgetError):
            self.store.finish_extract(lease, [], {}, now=T0)

        self.store.begin_extract(lease, search_urls[:5], now=T0)
        with self.assertRaises(state.AttemptBudgetError):
            self.store.begin_extract(lease, search_urls[:1], now=T0)
        with self.assertRaises(state.AttemptBudgetError):
            self.store.finish_extract(
                lease,
                ["https://outside.example/not-searched"],
                {"credits": 1},
                now=T0,
            )
        self.assertEqual(self.store.allowed_evidence_urls(lease.token, now=T0), [])
        self.store.finish_extract(lease, search_urls[:3], {"credits": 1}, now=T0)
        self.assertEqual(
            self.store.allowed_evidence_urls(lease.token, now=T0),
            search_urls[:3],
        )
        with self.assertRaises(state.AttemptBudgetError):
            self.store.finish_extract(lease, search_urls[:3], {"credits": 2}, now=T0)

        self.store.record_outcome(lease, "synced", now=T0)
        with self.assertRaises(state.LeaseLostError):
            self.store.allowed_evidence_urls(lease.token, now=T0)

    def test_search_begin_survives_restart_and_still_consumes_budget(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("worker-a", lease_seconds=3600, now=T0)
        self.store.begin_search(lease, now=T0)

        reopened = state.StateStore(self.path)
        try:
            with self.assertRaises(state.AttemptBudgetError):
                reopened.begin_search(lease.token, now=T0 + timedelta(seconds=1))
            reopened.finish_search(
                lease.token,
                ["https://example.com/a"],
                {"credits": 3},
                now=T0 + timedelta(seconds=1),
            )
        finally:
            reopened.close()

    def test_research_action_ledger_is_at_most_once_and_queryable(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next(
            "research-worker",
            lease_seconds=3600,
            now=T0,
        )
        search_request = {
            "max_results": 5,
            "queries": ['"Panel" datasheet'],
        }
        search_fingerprint = state.research_request_fingerprint(
            "search",
            search_request,
        )
        self.assertEqual(
            search_fingerprint,
            state.research_request_fingerprint(
                "search",
                dict(reversed(list(search_request.items()))),
            ),
        )
        self.assertNotEqual(
            search_fingerprint,
            state.research_request_fingerprint("extract", search_request),
        )

        started = self.store.begin_research_action(
            lease,
            round_number=0,
            action="search",
            request_fingerprint=search_fingerprint,
            now=T0,
        )
        self.assertTrue(started.should_execute)
        self.assertEqual("started", started.record.status)
        self.assertIsNone(started.record.credits)

        replay = self.store.begin_research_action(
            lease.token,
            round_number=0,
            action="search",
            request_fingerprint=search_fingerprint,
            now=T0 + timedelta(seconds=1),
        )
        self.assertFalse(replay.should_execute)
        self.assertEqual(started.record.action_id, replay.record.action_id)
        self.assertEqual("started", replay.record.status)

        with self.assertRaisesRegex(
            state.AttemptBudgetError,
            "different request",
        ):
            self.store.begin_research_action(
                lease,
                round_number=0,
                action="search",
                request_fingerprint=state.research_request_fingerprint(
                    "search",
                    {"queries": ['"Panel" manual']},
                ),
                now=T0 + timedelta(seconds=1),
            )
        with self.assertRaisesRegex(
            state.AttemptBudgetError,
            "already recorded",
        ):
            self.store.begin_research_action(
                lease,
                round_number=1,
                action="search",
                request_fingerprint=search_fingerprint,
                now=T0 + timedelta(seconds=1),
            )

        search_summary = {
            "queries": ['  "Panel"   datasheet  '],
            "candidate_urls": [
                "HTTPS://Manufacturer.Example:443/panel.pdf#page=1",
                "https://docs.example/panel",
            ],
            "request_ids": ["request-1"],
        }
        completed_search = self.store.finish_research_action(
            lease,
            round_number=0,
            action="search",
            request_fingerprint=search_fingerprint,
            status="completed",
            result_summary=search_summary,
            credits=3,
            now=T0 + timedelta(seconds=2),
        )
        self.assertEqual("completed", completed_search.status)
        self.assertEqual(3.0, completed_search.credits)
        self.assertEqual(
            ['"Panel" datasheet'],
            completed_search.result_summary["queries"],
        )
        self.assertEqual(
            "https://manufacturer.example/panel.pdf",
            completed_search.result_summary["candidate_urls"][0],
        )
        exact_finish_replay = self.store.finish_research_action(
            lease,
            round_number=0,
            action="search",
            request_fingerprint=search_fingerprint,
            status="completed",
            result_summary=search_summary,
            credits=3,
            now=T0 + timedelta(seconds=3),
        )
        self.assertEqual(completed_search, exact_finish_replay)
        with self.assertRaisesRegex(
            state.AttemptBudgetError,
            "terminal state",
        ):
            self.store.finish_research_action(
                lease,
                round_number=0,
                action="search",
                request_fingerprint=search_fingerprint,
                status="failed",
                credits=3,
                error="late failure",
                now=T0 + timedelta(seconds=3),
            )

        extract_fingerprint = state.research_request_fingerprint(
            "extract",
            {"urls": ["https://manufacturer.example/panel.pdf"]},
        )
        self.store.begin_research_action(
            lease,
            round_number=0,
            action="extract",
            request_fingerprint=extract_fingerprint,
            now=T0 + timedelta(seconds=3),
        )
        with self.assertRaisesRegex(
            ValueError,
            "require a result_summary",
        ):
            self.store.finish_research_action(
                lease,
                round_number=0,
                action="extract",
                request_fingerprint=extract_fingerprint,
                status="completed",
                result_summary=None,
                credits=1,
                now=T0 + timedelta(seconds=4),
            )
        with self.assertRaisesRegex(
            state.AttemptBudgetError,
            "completed search action",
        ):
            self.store.finish_research_action(
                lease,
                round_number=0,
                action="extract",
                request_fingerprint=extract_fingerprint,
                status="completed",
                result_summary={
                    "submitted_urls": ["https://outside.example/panel"],
                    "successful_urls": ["https://outside.example/panel"]
                },
                credits=1,
                now=T0 + timedelta(seconds=4),
            )
        with self.assertRaisesRegex(
            state.AttemptBudgetError,
            "submitted set",
        ):
            self.store.finish_research_action(
                lease,
                round_number=0,
                action="extract",
                request_fingerprint=extract_fingerprint,
                status="completed",
                result_summary={
                    "submitted_urls": [
                        "https://manufacturer.example/panel.pdf"
                    ],
                    "successful_urls": [
                        "https://docs.example/panel"
                    ],
                },
                credits=1,
                now=T0 + timedelta(seconds=4),
            )
        completed_extract = self.store.finish_research_action(
            lease,
            round_number=0,
            action="extract",
            request_fingerprint=extract_fingerprint,
            status="completed",
            result_summary={
                "submitted_urls": [
                    "https://manufacturer.example/panel.pdf"
                ],
                "successful_urls": [
                    "https://manufacturer.example/panel.pdf"
                ]
            },
            credits=1,
            now=T0 + timedelta(seconds=4),
        )
        self.assertEqual("completed", completed_extract.status)

        failed_fingerprint = state.research_request_fingerprint(
            "ai",
            {"round": 0},
        )
        self.store.begin_research_action(
            lease,
            round_number=0,
            action="ai",
            request_fingerprint=failed_fingerprint,
            now=T0 + timedelta(seconds=5),
        )
        with self.assertRaisesRegex(ValueError, "explicit known credit"):
            self.store.finish_research_action(
                lease,
                round_number=0,
                action="ai",
                request_fingerprint=failed_fingerprint,
                status="failed",
                error="provider rejected request",
                now=T0 + timedelta(seconds=6),
            )
        failed = self.store.finish_research_action(
            lease,
            round_number=0,
            action="ai",
            request_fingerprint=failed_fingerprint,
            status="failed",
            credits=0,
            error="provider rejected request",
            now=T0 + timedelta(seconds=6),
        )
        self.assertEqual("failed", failed.status)

        uncertain_fingerprint = state.research_request_fingerprint(
            "ai",
            {"round": 1},
        )
        self.store.begin_research_action(
            lease,
            round_number=1,
            action="ai",
            request_fingerprint=uncertain_fingerprint,
            now=T0 + timedelta(seconds=7),
        )
        with self.assertRaisesRegex(
            ValueError,
            "require a result_summary",
        ):
            self.store.finish_research_action(
                lease,
                round_number=1,
                action="ai",
                request_fingerprint=uncertain_fingerprint,
                status="completed",
                result_summary=None,
                credits=0,
                now=T0 + timedelta(seconds=8),
            )
        with self.assertRaisesRegex(ValueError, "response bodies"):
            self.store.finish_research_action(
                lease,
                round_number=1,
                action="ai",
                request_fingerprint=uncertain_fingerprint,
                status="completed",
                result_summary={"raw_content": "must not be persisted"},
                now=T0 + timedelta(seconds=8),
            )
        with self.assertRaisesRegex(ValueError, "response bodies"):
            self.store.finish_research_action(
                lease,
                round_number=1,
                action="ai",
                request_fingerprint=uncertain_fingerprint,
                status="completed",
                result_summary={"rawContent": "must not be persisted"},
                now=T0 + timedelta(seconds=8),
            )
        for forbidden_key in (
            "responseBody",
            "promptText",
            "evidenceText",
            "fullText",
            "htmlBody",
            "document",
        ):
            with self.subTest(forbidden_key=forbidden_key), self.assertRaisesRegex(
                ValueError,
                "response bodies",
            ):
                self.store.finish_research_action(
                    lease,
                    round_number=1,
                    action="ai",
                    request_fingerprint=uncertain_fingerprint,
                    status="completed",
                    result_summary={
                        forbidden_key: "must not be persisted"
                    },
                    now=T0 + timedelta(seconds=8),
                )
        for field, nested_value in (
            ("manufacturer", {"payload": "full response"}),
            ("gap", {"transcript": "full response"}),
            ("provider_requests", {"value": 1}),
        ):
            with self.subTest(field=field), self.assertRaises(
                (TypeError, ValueError)
            ):
                self.store.finish_research_action(
                    lease,
                    round_number=1,
                    action="ai",
                    request_fingerprint=uncertain_fingerprint,
                    status="completed",
                    result_summary={field: nested_value},
                    credits=0,
                    now=T0 + timedelta(seconds=8),
                )
        uncertain = self.store.finish_research_action(
            lease,
            round_number=1,
            action="ai",
            request_fingerprint=uncertain_fingerprint,
            status="uncertain",
            error="response may have been consumed",
            now=T0 + timedelta(seconds=8),
        )
        self.assertEqual("uncertain", uncertain.status)
        self.assertIsNone(uncertain.credits)

        self.assertEqual(
            completed_search,
            self.store.get_research_action(lease.attempt_id, 0, "search"),
        )
        history = self.store.research_action_history(
            attempt_id=lease.attempt_id
        )
        self.assertEqual(
            ["search", "extract", "ai", "ai"],
            [item.action for item in history],
        )
        self.assertEqual(
            history,
            self.store.research_action_history(product_id="P-1"),
        )
        stats = self.store.research_action_stats(attempt_id=lease.attempt_id)
        self.assertEqual(4, stats["actions"])
        self.assertEqual(4.0, stats["known_credits"])
        self.assertEqual(1, stats["unknown_credit_actions"])
        self.assertEqual(1, stats["max_round"])
        self.assertEqual(2, stats["by_status"]["completed"])
        self.assertEqual(1, stats["by_status"]["failed"])
        self.assertEqual(1, stats["by_status"]["uncertain"])
        self.assertEqual(
            {"actions": 1, "credits": 3.0},
            stats["by_action"]["search"],
        )

    def test_completed_research_extracts_extend_legacy_allowed_evidence(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next(
            "research-worker",
            lease_seconds=3600,
            now=T0,
        )
        legacy_url = "https://legacy.example/panel.pdf"
        researched_url = "https://manufacturer.example/panel.pdf"
        self.store.begin_search(lease, now=T0)
        self.store.finish_search(
            lease,
            [legacy_url],
            {"credits": 1},
            now=T0 + timedelta(seconds=1),
        )
        self.store.begin_extract(
            lease,
            [legacy_url],
            now=T0 + timedelta(seconds=2),
        )
        self.store.finish_extract(
            lease,
            [legacy_url],
            {"credits": 1},
            now=T0 + timedelta(seconds=3),
        )

        search_fingerprint = state.research_request_fingerprint(
            "search",
            {"queries": ['"Panel" manufacturer']},
        )
        self.store.begin_research_action(
            lease,
            round_number=0,
            action="search",
            request_fingerprint=search_fingerprint,
            now=T0 + timedelta(seconds=4),
        )
        self.store.finish_research_action(
            lease,
            round_number=0,
            action="search",
            request_fingerprint=search_fingerprint,
            status="completed",
            result_summary={
                "queries": ['"Panel" manufacturer'],
                "candidate_urls": [researched_url],
            },
            credits=1,
            now=T0 + timedelta(seconds=5),
        )
        extract_fingerprint = state.research_request_fingerprint(
            "extract",
            {"urls": [researched_url]},
        )
        self.store.begin_research_action(
            lease,
            round_number=0,
            action="extract",
            request_fingerprint=extract_fingerprint,
            now=T0 + timedelta(seconds=6),
        )
        self.store.finish_research_action(
            lease,
            round_number=0,
            action="extract",
            request_fingerprint=extract_fingerprint,
            status="completed",
            result_summary={
                "submitted_urls": [researched_url],
                "successful_urls": [researched_url],
            },
            credits=1,
            now=T0 + timedelta(seconds=7),
        )

        self.assertEqual(
            [legacy_url, researched_url],
            self.store.allowed_evidence_urls(
                lease.token,
                now=T0 + timedelta(seconds=8),
            ),
        )

    def test_started_research_action_becomes_uncertain_on_lease_expiry(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next(
            "research-worker",
            lease_seconds=10,
            now=T0,
        )
        fingerprint = state.research_request_fingerprint(
            "ai",
            {"context_sha256": "a" * 64},
        )
        self.store.begin_research_action(
            lease,
            round_number=0,
            action="search",
            request_fingerprint=fingerprint,
            now=T0,
        )

        reopened = state.StateStore(self.path)
        try:
            replay = reopened.begin_research_action(
                lease.token,
                round_number=0,
                action="search",
                request_fingerprint=fingerprint,
                now=T0 + timedelta(seconds=1),
            )
            self.assertFalse(replay.should_execute)
            self.assertEqual("started", replay.record.status)
            self.assertEqual(
                1,
                reopened.reclaim_expired_leases(
                    now=T0 + timedelta(seconds=11)
                ),
            )
            action = reopened.get_research_action(
                lease.attempt_id,
                0,
                "search",
            )
            self.assertEqual("uncertain", action.status)
            self.assertIsNotNone(action.finished_at)
            self.assertIsNone(action.credits)
            self.assertIn("lease expired", action.error)
            with self.assertRaises(state.LeaseLostError):
                reopened.begin_research_action(
                    lease.token,
                    round_number=0,
                    action="search",
                    request_fingerprint=fingerprint,
                    now=T0 + timedelta(seconds=11),
                )
        finally:
            reopened.close()

    def test_abnormal_prior_attempt_uses_the_prior_actions_provider_scope(self):
        self.store.upsert_product(product(), now=T0)
        first = self.store.lease_next(
            "research-worker-1",
            lease_seconds=3600,
            now=T0,
        )
        scopes = {
            "search": "a" * 64,
            "extract": "a" * 64,
            "ai": "b" * 64,
        }
        fingerprint = state.research_request_fingerprint(
            "search",
            {"queries": ['"Panel" datasheet']},
        )
        started = self.store.begin_research_action(
            first,
            round_number=0,
            action="ai",
            request_fingerprint=fingerprint,
            scope_fingerprint=scopes["ai"],
            blocking_scope_fingerprints=scopes,
            now=T0,
        )
        self.store.finish_research_action(
            first,
            round_number=0,
            action="ai",
            request_fingerprint=fingerprint,
            status="uncertain",
            error="response status is unknown",
            now=T0 + timedelta(seconds=1),
        )
        self.store.record_outcome(
            first,
            "search_error",
            error="network timeout",
            now=T0 + timedelta(seconds=2),
        )

        second = self.store.lease_next(
            "research-worker-2",
            lease_seconds=3600,
            now=T0 + timedelta(hours=2),
        )
        replay = self.store.begin_research_action(
            second,
            round_number=0,
            action="search",
            request_fingerprint=state.research_request_fingerprint(
                "search",
                {"queries": ['"Panel" datasheet']},
            ),
            scope_fingerprint="c" * 64,
            blocking_scope_fingerprints={
                **scopes,
                "search": "c" * 64,
                "extract": "c" * 64,
            },
            now=T0 + timedelta(hours=2),
        )

        self.assertFalse(replay.should_execute)
        self.assertEqual(started.record.action_id, replay.record.action_id)
        self.assertEqual(first.attempt_id, replay.record.attempt_id)
        self.assertEqual("uncertain", replay.record.status)
        self.assertEqual(
            [],
            self.store.research_action_history(attempt_id=second.attempt_id),
        )
        self.store.record_outcome(
            second,
            "research_uncertain",
            now=T0 + timedelta(hours=2, seconds=1),
        )

        third = self.store.lease_next(
            "research-worker-3",
            lease_seconds=3600,
            now=T0 + timedelta(days=31),
        )
        changed_ai_scope = self.store.begin_research_action(
            third,
            round_number=0,
            action="search",
            request_fingerprint=state.research_request_fingerprint(
                "search",
                {"queries": ['"Panel" datasheet']},
            ),
            scope_fingerprint="c" * 64,
            blocking_scope_fingerprints={
                "search": "c" * 64,
                "extract": "c" * 64,
                "ai": "d" * 64,
            },
            now=T0 + timedelta(days=31),
        )
        self.assertTrue(changed_ai_scope.should_execute)

    def test_normal_finished_attempt_allows_intentional_future_refresh(self):
        self.store.upsert_product(product(), now=T0)
        first = self.store.lease_next("research-worker-1", now=T0)
        fingerprint = state.research_request_fingerprint(
            "search",
            {"queries": ['"Panel" datasheet']},
        )
        self.store.begin_research_action(
            first,
            round_number=0,
            action="search",
            request_fingerprint=fingerprint,
            now=T0,
        )
        self.store.finish_research_action(
            first,
            round_number=0,
            action="search",
            request_fingerprint=fingerprint,
            status="completed",
            result_summary={
                "queries": ['"Panel" datasheet'],
                "candidate_urls": [],
            },
            credits=1,
            now=T0 + timedelta(seconds=1),
        )
        self.store.record_outcome(
            first,
            "no_datasheet",
            now=T0 + timedelta(seconds=2),
        )

        second = self.store.lease_next(
            "research-worker-2",
            now=T0 + timedelta(days=31),
        )
        refreshed = self.store.begin_research_action(
            second,
            round_number=0,
            action="search",
            request_fingerprint=fingerprint,
            now=T0 + timedelta(days=31),
        )
        self.assertTrue(refreshed.should_execute)
        self.assertNotEqual(first.attempt_id, refreshed.record.attempt_id)

    def test_counts_distinct_products_for_ai_output_circuit(self):
        provider_fingerprint = "e" * 64
        for index in range(2):
            product_id = f"P-AI-{index}"
            self.store.upsert_product(
                product(product_id=product_id),
                now=T0 + timedelta(seconds=index),
            )
            lease = self.store.lease_next(
                f"worker-{index}",
                now=T0 + timedelta(seconds=index),
            )
            fingerprint = state.research_request_fingerprint(
                "ai",
                {"product": product_id},
            )
            self.store.begin_research_action(
                lease,
                round_number=0,
                action="ai",
                request_fingerprint=fingerprint,
                now=T0 + timedelta(seconds=index),
            )
            self.store.finish_research_action(
                lease,
                round_number=0,
                action="ai",
                request_fingerprint=fingerprint,
                status="uncertain",
                result_summary={
                    "provider_fingerprint": provider_fingerprint,
                    "provider_requests": 2,
                    "error_type": "AIInvalidOutputError",
                },
                error="invalid output after repair",
                now=T0 + timedelta(seconds=index + 1),
            )
            self.store.record_outcome(
                lease,
                "ai_error",
                error="invalid output after repair",
                now=T0 + timedelta(seconds=index + 2),
            )

        self.assertEqual(
            2,
            self.store.recent_ai_provider_error_products(
                provider_fingerprint,
                error_types={"AIInvalidOutputError"},
                within=timedelta(hours=1),
                now=T0 + timedelta(minutes=1),
            ),
        )

    def test_research_compiled_ceilings_include_legacy_audit(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("research-worker", now=T0)
        legacy_urls = [
            f"https://legacy.example/panel-{index}.pdf"
            for index in range(5)
        ]
        researched_url = "https://manufacturer.example/panel.pdf"
        self.store.begin_search(lease, now=T0)
        self.store.finish_search(
            lease,
            [*legacy_urls, researched_url],
            {"credits": 4},
            now=T0 + timedelta(seconds=1),
        )
        self.store.begin_extract(
            lease,
            legacy_urls,
            now=T0 + timedelta(seconds=2),
        )
        self.store.finish_extract(
            lease,
            legacy_urls,
            {"credits": 0},
            now=T0 + timedelta(seconds=3),
        )

        for round_number, queries, credits in (
            (0, ["q0", "q1", "q2"], 90),
            (1, ["q3"], 6),
        ):
            fingerprint = state.research_request_fingerprint(
                "search",
                {"queries": queries},
            )
            self.store.begin_research_action(
                lease,
                round_number=round_number,
                action="search",
                request_fingerprint=fingerprint,
                now=T0 + timedelta(seconds=4 + round_number * 2),
            )
            self.store.finish_research_action(
                lease,
                round_number=round_number,
                action="search",
                request_fingerprint=fingerprint,
                status="completed",
                result_summary={
                    "queries": queries,
                    "candidate_urls": [researched_url],
                },
                credits=credits,
                now=T0 + timedelta(seconds=5 + round_number * 2),
            )

        too_many_queries = state.research_request_fingerprint(
            "search",
            {"queries": ["q4"]},
        )
        self.store.begin_research_action(
            lease,
            round_number=2,
            action="search",
            request_fingerprint=too_many_queries,
            now=T0 + timedelta(seconds=8),
        )
        with self.assertRaisesRegex(
            state.AttemptBudgetError,
            "7-query ceiling",
        ):
            self.store.finish_research_action(
                lease,
                round_number=2,
                action="search",
                request_fingerprint=too_many_queries,
                status="completed",
                result_summary={
                    "queries": ["q4"],
                    "candidate_urls": [],
                },
                credits=0,
                now=T0 + timedelta(seconds=9),
            )

        extract_fingerprint = state.research_request_fingerprint(
            "extract",
            {"urls": [researched_url]},
        )
        self.store.begin_research_action(
            lease,
            round_number=0,
            action="extract",
            request_fingerprint=extract_fingerprint,
            now=T0 + timedelta(seconds=10),
        )
        with self.assertRaisesRegex(
            state.AttemptBudgetError,
            "5-URL ceiling",
        ):
            self.store.finish_research_action(
                lease,
                round_number=0,
                action="extract",
                request_fingerprint=extract_fingerprint,
                status="completed",
                result_summary={
                    "submitted_urls": [researched_url],
                    "successful_urls": [researched_url],
                },
                credits=0,
                now=T0 + timedelta(seconds=11),
            )

        ai_fingerprint = state.research_request_fingerprint(
            "ai",
            {"round": 2},
        )
        self.store.begin_research_action(
            lease,
            round_number=2,
            action="ai",
            request_fingerprint=ai_fingerprint,
            now=T0 + timedelta(seconds=12),
        )
        with self.assertRaisesRegex(
            state.AttemptBudgetError,
            "100-credit ceiling",
        ):
            self.store.finish_research_action(
                lease,
                round_number=2,
                action="ai",
                request_fingerprint=ai_fingerprint,
                status="completed",
                result_summary={"action_type": "final"},
                credits=1,
                now=T0 + timedelta(seconds=13),
            )

        with self.assertRaisesRegex(ValueError, "one of"):
            state.research_request_fingerprint(
                "search_extra",
                {"queries": ["bypass"]},
            )
        with self.assertRaisesRegex(ValueError, "between 0 and 2"):
            self.store.begin_research_action(
                lease,
                round_number=3,
                action="ai",
                request_fingerprint=ai_fingerprint,
                now=T0 + timedelta(seconds=14),
            )

    def test_finishing_attempt_seals_started_research_action_as_uncertain(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("research-worker", now=T0)
        fingerprint = state.research_request_fingerprint(
            "ai",
            {"round": 0},
        )
        self.store.begin_research_action(
            lease,
            round_number=0,
            action="ai",
            request_fingerprint=fingerprint,
            now=T0,
        )

        self.store.record_outcome(
            lease,
            "no_datasheet",
            now=T0 + timedelta(seconds=1),
        )

        action = self.store.get_research_action(
            lease.attempt_id,
            0,
            "ai",
        )
        self.assertEqual("uncertain", action.status)
        self.assertIsNone(action.credits)
        self.assertIn("attempt ended", action.error)

    def test_expired_lease_is_reclaimed_and_audited(self):
        self.store.upsert_product(product(), now=T0)
        expired = self.store.lease_next("dead-worker", lease_seconds=10, now=T0)

        reclaimed = self.store.reclaim_expired_leases(now=T0 + timedelta(seconds=11))

        self.assertEqual(reclaimed, 1)
        self.assertIsNone(
            self.store.get_by_lease(expired.token, now=T0 + timedelta(seconds=11))
        )
        current = self.store.get_product("P-1")
        self.assertEqual(current.status, "backoff")
        self.assertEqual(current.consecutive_failures, 1)
        retry_at = T0 + timedelta(seconds=11, hours=1)
        self.assertEqual(current.next_run_at, retry_at)
        history = self.store.attempt_history("P-1")
        self.assertEqual(history[0].outcome, "lease_expired")
        self.assertIsNone(
            self.store.lease_next("worker-b", now=retry_at - timedelta(microseconds=1))
        )
        replacement = self.store.lease_next("worker-b", now=retry_at)
        self.assertNotEqual(replacement.token, expired.token)

    def test_source_change_during_lease_rejects_stale_outcome(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("worker-a", now=T0)
        changed = product(
            name="Panel revision B",
            updated_at=T0 + timedelta(seconds=5),
        )
        self.store.upsert_product(changed, now=T0 + timedelta(seconds=5))

        durable_lease = self.store.get_by_lease(
            lease.token, now=T0 + timedelta(seconds=5)
        )
        # Token lookup returns the original snapshot, not the concurrently
        # updated product payload.
        self.assertEqual(durable_lease.payload["product_name"], "Panel")
        check = self.store.precheck(lease, now=T0 + timedelta(seconds=5))
        self.assertFalse(check.ready)
        self.assertEqual(check.reason, "source_changed")

        outcome = self.store.record_outcome(
            lease,
            "synced",
            now=T0 + timedelta(seconds=5),
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.outcome, "stale_source")
        self.assertEqual(outcome.status, "due")
        replacement = self.store.lease_next(
            "worker-b", now=T0 + timedelta(seconds=5)
        )
        self.assertNotEqual(replacement.source_hash, lease.source_hash)
        self.assertEqual(replacement.payload["product_name"], "Panel revision B")

    def test_changed_source_is_due_immediately_when_its_lease_expires(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next(
            "dead-worker",
            lease_seconds=10,
            now=T0,
        )
        changed = product(
            name="Panel revision B",
            updated_at=T0 + timedelta(seconds=5),
        )
        self.store.upsert_product(changed, now=T0 + timedelta(seconds=5))

        reclaimed_at = T0 + timedelta(seconds=11)
        reclaimed = self.store.reclaim_expired_leases(now=reclaimed_at)

        self.assertEqual(1, reclaimed)
        current = self.store.get_product("P-1")
        self.assertEqual("due", current.status)
        self.assertEqual(0, current.consecutive_failures)
        self.assertEqual(reclaimed_at, current.next_run_at)
        self.assertEqual("stale_source", current.last_outcome)
        history = self.store.attempt_history("P-1")
        self.assertEqual("stale_source", history[0].outcome)
        replacement = self.store.lease_next(
            "worker-b",
            now=reclaimed_at,
        )
        self.assertNotEqual(lease.source_hash, replacement.source_hash)
        self.assertEqual(
            "Panel revision B",
            replacement.payload["product_name"],
        )

    def test_two_store_instances_cannot_lease_the_same_task(self):
        self.store.upsert_product(product(), now=T0)
        other = state.StateStore(self.path)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(store.lease_next, worker, now=T0)
                    for store, worker in (
                        (self.store, "worker-a"),
                        (other, "worker-b"),
                    )
                ]
                leases = [future.result() for future in futures]
            self.assertEqual(sum(lease is not None for lease in leases), 1)
        finally:
            other.close()

    def test_migrates_v2_attempt_audit_to_current_schema(self):
        legacy_path = Path(self.tempdir.name) / "legacy-v2.sqlite3"
        timestamp = "2026-01-01T12:00:00.000000Z"
        lease_until = "2027-01-01T12:00:00.000000Z"
        connection = sqlite3.connect(legacy_path)
        for statement in state._CREATE_SCHEMA:
            connection.execute(statement)
        connection.execute("ALTER TABLE attempts ADD COLUMN payload_json TEXT")
        connection.execute(
            """
            INSERT INTO products (
                product_id, source_hash, payload_json, status, next_run_at,
                lease_token, lease_owner, lease_until, leased_source_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'leased', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "LEGACY",
                "legacy-hash",
                '{"product_id":"LEGACY"}',
                timestamp,
                "legacy-token",
                "legacy-worker",
                lease_until,
                "legacy-hash",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO attempts (
                product_id, source_hash, lease_token, worker_id,
                started_at, lease_until, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "LEGACY",
                "legacy-hash",
                "legacy-token",
                "legacy-worker",
                timestamp,
                lease_until,
                '{"product_id":"LEGACY"}',
            ),
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()

        migrated = state.StateStore(legacy_path)
        try:
            self.assertEqual(migrated.schema_version, state.SCHEMA_VERSION)
            attempt = migrated.attempt_history("LEGACY")[0]
            self.assertIsNone(attempt.search_started_at)
            self.assertIsNone(attempt.search_urls)
            self.assertIsNone(attempt.search_usage)
            self.assertIsNone(attempt.extract_started_at)
            self.assertIsNone(attempt.extract_urls)
            self.assertIsNone(attempt.extract_success_urls)
            self.assertIsNone(attempt.extract_usage)
            migrated_connection = sqlite3.connect(legacy_path)
            try:
                columns = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA table_info(attempts)"
                    )
                }
                tables = {
                    row[0]
                    for row in migrated_connection.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table'
                        """
                    )
                }
                research_indexes = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA index_list(research_actions)"
                    )
                }
                research_columns = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA table_info(research_actions)"
                    )
                }
            finally:
                migrated_connection.close()
            self.assertTrue(
                {
                    "search_started_at",
                    "search_urls_json",
                    "search_usage_json",
                    "extract_started_at",
                    "extract_urls_json",
                    "extract_success_urls_json",
                    "extract_usage_json",
                }.issubset(columns)
            )
            self.assertIn("research_actions", tables)
            self.assertIn("research_actions_attempt_idx", research_indexes)
            self.assertIn("research_actions_status_idx", research_indexes)
            self.assertIn("research_actions_scope_idx", research_indexes)
            self.assertIn("scope_fingerprint", research_columns)

            fingerprint = state.research_request_fingerprint(
                "search",
                {"queries": ['"LEGACY" datasheet']},
            )
            started = migrated.begin_research_action(
                "legacy-token",
                round_number=0,
                action="search",
                request_fingerprint=fingerprint,
                now=T0,
            )
            self.assertTrue(started.should_execute)
            self.assertEqual("started", started.record.status)
        finally:
            migrated.close()

    def test_rejects_newer_schema(self):
        newer_path = Path(self.tempdir.name) / "newer.sqlite3"
        connection = sqlite3.connect(newer_path)
        connection.execute(f"PRAGMA user_version = {state.SCHEMA_VERSION + 1}")
        connection.close()

        with self.assertRaises(state.SchemaVersionError):
            state.StateStore(newer_path)

    def test_in_memory_store_keeps_state_between_operations(self):
        with state.StateStore(":memory:") as memory_store:
            memory_store.upsert_product(product("MEM-1"), now=T0)
            self.assertIsNotNone(memory_store.get_product("MEM-1"))


if __name__ == "__main__":
    unittest.main()
