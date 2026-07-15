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

    def test_transient_and_conflict_outcomes_use_short_backoff(self):
        self.store.upsert_product(product(), now=T0)
        current = T0
        outcomes = (
            "tavily_error",
            "wikijs_error",
            "error",
            "invalid_decision",
            "publish_error",
        )
        expected_hours = (1, 6, 24, 24, 24)
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
            self.store.finish_extract(lease, {}, now=T0)

        self.store.begin_extract(lease, search_urls[:5], now=T0)
        with self.assertRaises(state.AttemptBudgetError):
            self.store.begin_extract(lease, search_urls[:1], now=T0)
        self.assertEqual(self.store.allowed_evidence_urls(lease.token, now=T0), [])
        self.store.finish_extract(lease, {"credits": 1}, now=T0)
        with self.assertRaises(state.AttemptBudgetError):
            self.store.finish_extract(lease, {"credits": 2}, now=T0)

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

    def test_migrates_v2_attempt_audit_to_v3(self):
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
            self.assertEqual(migrated.schema_version, 3)
            attempt = migrated.attempt_history("LEGACY")[0]
            self.assertIsNone(attempt.search_started_at)
            self.assertIsNone(attempt.search_urls)
            self.assertIsNone(attempt.search_usage)
            self.assertIsNone(attempt.extract_started_at)
            self.assertIsNone(attempt.extract_urls)
            self.assertIsNone(attempt.extract_usage)
            migrated_connection = sqlite3.connect(legacy_path)
            try:
                columns = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA table_info(attempts)"
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
                    "extract_usage_json",
                }.issubset(columns)
            )
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
