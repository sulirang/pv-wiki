from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
    def test_postgres_placeholder_translation_skips_literals_and_functions(
        self,
    ) -> None:
        sql = (
            "SELECT ?, '?', \"?\", $function$ ? $function$ "
            "FROM attempts WHERE product_id = ?"
        )
        self.assertEqual(
            (
                "SELECT %s, '?', \"?\", $function$ ? $function$ "
                "FROM attempts WHERE product_id = %s"
            ),
            state._postgres_placeholders(sql),
        )

    def test_parse_time_accepts_postgres_timestamptz_datetime(self) -> None:
        value = datetime(
            2026,
            1,
            1,
            13,
            0,
            tzinfo=timezone(timedelta(hours=1)),
        )

        self.assertEqual(T0, state._parse_time(value))

    def test_postgres_publication_fence_uses_independent_session_lock(
        self,
    ) -> None:
        class FakeConnection:
            def __init__(self) -> None:
                self.calls = []

            def execute(self, statement, parameters=()):
                self.calls.append((statement, tuple(parameters)))

        connection = FakeConnection()

        @contextmanager
        def connection_scope():
            yield connection

        store = object.__new__(state.StateStore)
        store.backend = "postgresql"
        store._connection = connection_scope

        with store.publication_fence():
            pass

        lock_calls = [
            call
            for call in connection.calls
            if "pg_advisory_lock" in call[0]
            and "unlock" not in call[0]
        ]
        unlock_calls = [
            call
            for call in connection.calls
            if "pg_advisory_unlock" in call[0]
        ]
        self.assertEqual(
            [("SELECT pg_advisory_lock(?)", (state._POSTGRES_PUBLICATION_LOCK,))],
            lock_calls,
        )
        self.assertEqual(
            [
                (
                    "SELECT pg_advisory_unlock(?)",
                    (state._POSTGRES_PUBLICATION_LOCK,),
                )
            ],
            unlock_calls,
        )
        self.assertNotEqual(
            state._POSTGRES_ADVISORY_LOCK,
            state._POSTGRES_PUBLICATION_LOCK,
        )

    def test_postgres_v10_migration_rechecks_version_under_lock(self) -> None:
        class Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class InitialConnection:
            def execute(self, statement, _parameters=()):
                if "to_regclass" in statement:
                    return Cursor({"metadata_table": "state_metadata"})
                if "SELECT schema_version" in statement:
                    return Cursor({"schema_version": 9})
                raise AssertionError(f"unexpected initial SQL: {statement}")

        class LockedConnection:
            def __init__(self):
                self.calls = []

            def execute(self, statement, _parameters=()):
                self.calls.append(statement)
                if "SELECT schema_version" in statement:
                    return Cursor({"schema_version": state.SCHEMA_VERSION})
                raise AssertionError(f"DDL should have been skipped: {statement}")

        initial = InitialConnection()
        locked = LockedConnection()

        @contextmanager
        def initial_scope():
            yield initial

        @contextmanager
        def locked_scope():
            yield locked

        store = object.__new__(state.StateStore)
        store.backend = "postgresql"
        store._connection = initial_scope
        store._write_transaction = locked_scope

        self.assertEqual(state.SCHEMA_VERSION, store._migrate_postgresql())
        self.assertEqual(1, len(locked.calls))

    def test_postgres_v11_migrates_v10_parameter_ledger_under_lock(self) -> None:
        class Cursor:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class InitialConnection:
            def execute(self, statement, _parameters=()):
                if "to_regclass" in statement:
                    return Cursor({"metadata_table": "state_metadata"})
                if "SELECT schema_version" in statement:
                    return Cursor({"schema_version": 10})
                raise AssertionError(f"unexpected initial SQL: {statement}")

        class LockedConnection:
            def __init__(self):
                self.calls = []

            def execute(self, statement, parameters=()):
                self.calls.append((statement, tuple(parameters)))
                if "SELECT schema_version" in statement:
                    return Cursor({"schema_version": 10})
                return Cursor()

        initial = InitialConnection()
        locked = LockedConnection()

        @contextmanager
        def initial_scope():
            yield initial

        @contextmanager
        def locked_scope():
            yield locked

        store = object.__new__(state.StateStore)
        store.backend = "postgresql"
        store._connection = initial_scope
        store._write_transaction = locked_scope

        self.assertEqual(state.SCHEMA_VERSION, store._migrate_postgresql())
        statements = "\n".join(statement for statement, _ in locked.calls)
        self.assertIn("CREATE TABLE IF NOT EXISTS verified_parameter_sets", statements)
        self.assertIn("CREATE TABLE IF NOT EXISTS parameter_analysis_runs", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS parameter_set_id", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS analysis_run_id", statements)
        self.assertIn(
            (state.SCHEMA_VERSION, 10),
            [parameters for _, parameters in locked.calls],
        )

    def test_postgres_v12_migrates_v11_page_retirement_ledger(self) -> None:
        class Cursor:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class InitialConnection:
            def execute(self, statement, _parameters=()):
                if "to_regclass" in statement:
                    return Cursor({"metadata_table": "state_metadata"})
                if "SELECT schema_version" in statement:
                    return Cursor({"schema_version": 11})
                raise AssertionError(f"unexpected initial SQL: {statement}")

        class LockedConnection:
            def __init__(self):
                self.calls = []

            def execute(self, statement, parameters=()):
                self.calls.append((statement, tuple(parameters)))
                if "SELECT schema_version" in statement:
                    return Cursor({"schema_version": 11})
                return Cursor()

        initial = InitialConnection()
        locked = LockedConnection()

        @contextmanager
        def initial_scope():
            yield initial

        @contextmanager
        def locked_scope():
            yield locked

        store = object.__new__(state.StateStore)
        store.backend = "postgresql"
        store._connection = initial_scope
        store._write_transaction = locked_scope

        self.assertEqual(state.SCHEMA_VERSION, store._migrate_postgresql())
        statements = "\n".join(statement for statement, _ in locked.calls)
        self.assertIn("CREATE TABLE IF NOT EXISTS page_retirements", statements)
        self.assertIn("page_retirements_active_product_idx", statements)
        self.assertIn("page_retirements_active_path_idx", statements)
        self.assertIn("page_retirements_active_page_id_idx", statements)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS verified_parameter_sets", statements)
        self.assertIn(
            (state.SCHEMA_VERSION, 11),
            [parameters for _, parameters in locked.calls],
        )

    def test_postgres_v13_migrates_v12_retirement_basis(self) -> None:
        class Cursor:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class InitialConnection:
            def execute(self, statement, _parameters=()):
                if "to_regclass" in statement:
                    return Cursor({"metadata_table": "state_metadata"})
                if "SELECT schema_version" in statement:
                    return Cursor({"schema_version": 12})
                raise AssertionError(f"unexpected initial SQL: {statement}")

        class LockedConnection:
            def __init__(self):
                self.calls = []

            def execute(self, statement, parameters=()):
                self.calls.append((statement, tuple(parameters)))
                if "SELECT schema_version" in statement:
                    return Cursor({"schema_version": 12})
                return Cursor()

        initial = InitialConnection()
        locked = LockedConnection()

        @contextmanager
        def initial_scope():
            yield initial

        @contextmanager
        def locked_scope():
            yield locked

        store = object.__new__(state.StateStore)
        store.backend = "postgresql"
        store._connection = initial_scope
        store._write_transaction = locked_scope

        self.assertEqual(state.SCHEMA_VERSION, store._migrate_postgresql())
        statements = "\n".join(statement for statement, _ in locked.calls)
        self.assertIn("ALTER TABLE page_retirements", statements)
        self.assertIn("ADD COLUMN basis", statements)
        self.assertIn("DEFAULT 'synced_publication'", statements)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS page_retirements", statements)
        self.assertIn(
            (state.SCHEMA_VERSION, 12),
            [parameters for _, parameters in locked.calls],
        )

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

    def _publish_refresh_candidate(
        self,
        product_id: str = "P-1",
        *,
        now: datetime = T0,
    ):
        self.store.upsert_product(product(product_id), now=now)
        lease = self.store.lease_next(f"publisher-{product_id}", now=now)
        self.assertIsNotNone(lease)
        self.store.record_outcome(
            lease,
            "synced",
            payload={
                "decision": {
                    "outcome": "publish",
                    "manufacturer": "Acme Public",
                    "model": product_id,
                    "facts": [],
                },
                "validation_policy_fingerprint": "verified-policy",
                "fact_diagnostics": {
                    "complete": True,
                    "proposed": 0,
                    "retained": 0,
                    "rejected": 0,
                    "rejection_reasons": {},
                },
            },
            wiki_path=f"products/{product_id.casefold()}",
            now=now + timedelta(seconds=1),
        )
        candidates = self.store.content_refresh_candidates(
            1,
            product_ids=[product_id],
        )
        self.assertEqual(1, len(candidates))
        return candidates[0]

    def _publish_page(
        self,
        product_id: str,
        *,
        content_schema_version: int = 3,
        now: datetime = T0,
    ):
        self.store.upsert_product(product(product_id), now=now)
        lease = self.store.lease_next(f"publisher-{product_id}", now=now)
        self.assertIsNotNone(lease)
        published_at = now + timedelta(seconds=1)
        self.store.record_outcome(
            lease,
            "synced",
            payload={
                "decision": {
                    "outcome": "publish",
                    "manufacturer": "Acme Public",
                    "model": product_id,
                    "facts": [],
                },
                "validation_policy_fingerprint": "verified-policy",
                "fact_diagnostics": {
                    "complete": True,
                    "proposed": 0,
                    "retained": 0,
                    "rejected": 0,
                    "rejection_reasons": {},
                },
            },
            wiki_path=f"products/{product_id.casefold()}",
            content_schema_version=content_schema_version,
            now=published_at,
        )
        return lease, published_at

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
        self.assertEqual(self.store.due_count(now=T0), 1)
        self.assertEqual(
            0,
            self.store.get_product("P-1").content_failure_cutoff_attempt_id,
        )
        self.assertIsNone(
            self.store.get_product("P-1").leased_from_status,
        )
        self.assertIsNone(
            self.store.get_product("P-1").leased_from_next_run_at,
        )
        self.assertEqual([], self.store.requeue_event_history())

    def test_sqlite_publication_fence_serializes_store_instances(self):
        other = state.StateStore(self.path)
        started = threading.Event()
        acquired = threading.Event()

        def acquire_other() -> None:
            started.set()
            with other.publication_fence():
                acquired.set()

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                with self.store.publication_fence():
                    future = executor.submit(acquire_other)
                    self.assertTrue(started.wait(1))
                    self.assertFalse(acquired.wait(0.1))
                self.assertTrue(acquired.wait(2))
                future.result()
        finally:
            other.close()

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
        self.assertEqual(self.store.due_count(now=T0), 0)
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
        completed = self.store.get_product("P-1")
        self.assertIsNone(completed.leased_from_status)
        self.assertIsNone(completed.leased_from_next_run_at)

    def test_defer_lease_restores_backoff_without_product_penalty(self):
        self.store.upsert_product(product(), now=T0)
        first = self.store.lease_next("worker-first", now=T0)
        failed = self.store.record_outcome(
            first,
            "no_datasheet",
            error="no official source",
            now=T0 + timedelta(seconds=1),
        )
        original = self.store.get_product("P-1")
        self.assertEqual("backoff", original.status)

        leased = self.store.lease_next(
            "worker-paused",
            now=failed.next_run_at,
        )
        leased_state = self.store.get_product("P-1")
        self.assertEqual("leased", leased_state.status)
        self.assertEqual("backoff", leased_state.leased_from_status)
        self.assertEqual(
            original.next_run_at,
            leased_state.leased_from_next_run_at,
        )

        deferred_at = failed.next_run_at + timedelta(seconds=1)
        deferred = self.store.defer_lease(
            leased,
            "  Exa   provider circuit open  ",
            now=deferred_at,
        )

        self.assertEqual("backoff", deferred.status)
        self.assertEqual(original.next_run_at, deferred.next_run_at)
        self.assertEqual(
            leased_state.consecutive_failures,
            deferred.consecutive_failures,
        )
        self.assertEqual(leased_state.last_outcome, deferred.last_outcome)
        self.assertEqual(leased_state.last_error, deferred.last_error)
        self.assertEqual(
            leased_state.last_attempt_at,
            deferred.last_attempt_at,
        )
        self.assertEqual(
            leased_state.last_success_at,
            deferred.last_success_at,
        )
        self.assertIsNone(deferred.lease_token)
        self.assertIsNone(deferred.leased_from_status)
        self.assertIsNone(deferred.leased_from_next_run_at)

        history = self.store.attempt_history("P-1")
        self.assertEqual(["no_datasheet", "system_paused"], [
            attempt.outcome for attempt in history
        ])
        self.assertEqual("Exa provider circuit open", history[-1].error)
        self.assertEqual(deferred_at, history[-1].finished_at)
        self.assertEqual(1, self.store.content_failure_streak("P-1"))
        self.assertEqual(
            1,
            self.store.requeue_products(
                ("no_datasheet",),
                reason="content-policy-v2",
                now=deferred_at,
            ),
        )

        replacement = self.store.lease_next(
            "worker-after-pause",
            now=deferred_at,
        )
        retried = self.store.record_outcome(
            replacement,
            "no_datasheet",
            now=deferred_at + timedelta(seconds=1),
        )
        self.assertEqual(2, retried.consecutive_failures)
        self.assertEqual(
            deferred_at + timedelta(seconds=1, days=90),
            retried.next_run_at,
        )

    def test_defer_lease_preserves_synced_publication_metadata(self):
        self.store.upsert_product(product(), now=T0)
        first = self.store.lease_next("publisher", now=T0)
        published_at = T0 + timedelta(seconds=1)
        published = self.store.record_outcome(
            first,
            "synced",
            payload={
                "decision": {
                    "manufacturer": "Acme Public",
                    "model": "Panel One",
                }
            },
            wiki_path="products/p-1-a1",
            now=published_at,
        )
        before = self.store.published_products()
        refresh = self.store.lease_next(
            "refresh-worker",
            now=published.next_run_at,
        )
        deferred = self.store.defer_lease(
            refresh,
            "AI provider unavailable",
            now=published.next_run_at + timedelta(seconds=1),
        )

        self.assertEqual("synced", deferred.status)
        self.assertEqual(published.next_run_at, deferred.next_run_at)
        self.assertEqual("synced", deferred.last_outcome)
        self.assertEqual(published_at, deferred.last_success_at)
        self.assertEqual(before, self.store.published_products())

    def test_defer_lease_is_single_winner_across_store_instances(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("paused-worker", now=T0)
        other = state.StateStore(self.path)

        def defer(store):
            try:
                store.defer_lease(
                    lease,
                    "provider circuit open",
                    now=T0 + timedelta(seconds=1),
                )
            except state.LeaseLostError:
                return "lost"
            return "deferred"

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(defer, (self.store, other)))
            self.assertCountEqual(["deferred", "lost"], results)
            self.assertEqual(
                ["system_paused"],
                [
                    attempt.outcome
                    for attempt in self.store.attempt_history("P-1")
                ],
            )
            current = self.store.get_product("P-1")
            self.assertEqual("due", current.status)
            self.assertIsNone(current.lease_token)
        finally:
            other.close()

    def test_defer_lease_fails_closed_after_source_change(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("paused-worker", now=T0)
        changed_at = T0 + timedelta(seconds=1)
        self.store.upsert_product(
            product(
                name="Panel revision B",
                updated_at=changed_at,
            ),
            now=changed_at,
        )

        with self.assertRaisesRegex(state.LeaseLostError, "source changed"):
            self.store.defer_lease(
                lease,
                "provider circuit open",
                now=changed_at + timedelta(seconds=1),
            )

        unchanged = self.store.get_product("P-1")
        self.assertEqual("leased", unchanged.status)
        self.assertTrue(unchanged.reschedule_requested)
        self.assertIsNone(self.store.attempt_history("P-1")[0].finished_at)
        stale = self.store.record_outcome(
            lease,
            "no_datasheet",
            now=changed_at + timedelta(seconds=2),
        )
        self.assertEqual("stale_source", stale.outcome)
        self.assertEqual(
            ["stale_source"],
            [
                attempt.outcome
                for attempt in self.store.attempt_history("P-1")
            ],
        )

    def test_defer_lease_seals_started_action_as_uncertain(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("paused-worker", now=T0)
        fingerprint = state.research_request_fingerprint(
            "search",
            {"queries": ["panel datasheet"]},
        )
        self.store.begin_research_action(
            lease,
            round_number=0,
            action="search",
            request_fingerprint=fingerprint,
            now=T0,
        )

        self.store.defer_lease(
            lease,
            "provider circuit opened",
            now=T0 + timedelta(seconds=1),
        )

        action = self.store.get_research_action(
            lease.attempt_id,
            0,
            "search",
        )
        self.assertEqual("uncertain", action.status)
        self.assertEqual(
            "attempt paused before research action completion",
            action.error,
        )
        self.assertEqual(
            "system_paused",
            self.store.attempt_history("P-1")[0].outcome,
        )

    def test_defer_lease_at_expiry_reclaims_instead_of_restoring(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next(
            "expired-worker",
            lease_seconds=10,
            now=T0,
        )

        with self.assertRaises(state.LeaseLostError):
            self.store.defer_lease(
                lease,
                "provider circuit opened",
                now=T0 + timedelta(seconds=10),
            )

        current = self.store.get_product("P-1")
        self.assertEqual("backoff", current.status)
        self.assertEqual(1, current.consecutive_failures)
        self.assertIsNone(current.leased_from_status)
        self.assertIsNone(current.leased_from_next_run_at)
        history = self.store.attempt_history("P-1")
        self.assertEqual("lease_expired", history[0].outcome)
        self.assertNotEqual("system_paused", history[0].outcome)

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
        self.assertEqual("Panel", published[0].payload["product_name"])
        self.assertEqual(0, published[0].content_schema_version)

        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.store.published_products(limit=0)

    def test_page_retirement_is_atomic_idempotent_and_suppresses_publication(self):
        first_lease, first_published_at = self._publish_page("P-1")
        second_lease, second_published_at = self._publish_page(
            "P-2",
            now=T0 + timedelta(minutes=1),
        )
        candidate = self.store.content_refresh_candidates(
            4,
            product_ids=["P-1"],
        )[0]
        entries = (
            state.PageRetirementInput(
                product_id="P-1",
                source_attempt_id=first_lease.attempt_id,
                wiki_path="/products/p-1/",
                wiki_page_id=101,
                page_content_sha256="a" * 64,
                page_updated_at=first_published_at,
            ),
            state.PageRetirementInput(
                product_id="P-2",
                source_attempt_id=second_lease.attempt_id,
                wiki_path="products/p-2",
                wiki_page_id=102,
                page_content_sha256="b" * 64,
                page_updated_at=second_published_at,
            ),
        )
        retired_at = T0 + timedelta(days=2)

        retired = self.store.record_page_retirements(
            entries,
            reason="legacy-content-schema",
            backup_sha256="c" * 64,
            backup_reference="backups/pages.json",
            retired_at=retired_at,
        )
        replay = self.store.record_page_retirements(
            entries,
            reason="legacy-content-schema",
            backup_sha256="c" * 64,
            backup_reference="backups/pages.json",
            retired_at=retired_at + timedelta(hours=1),
        )

        self.assertEqual(retired, replay)
        self.assertEqual(2, len(retired))
        self.assertEqual(first_lease.source_hash, retired[0].source_hash)
        self.assertEqual("synced_publication", retired[0].basis)
        self.assertEqual(3, retired[0].retired_content_schema_version)
        self.assertEqual("products/p-1", retired[0].wiki_path)
        self.assertEqual("backups/pages.json", retired[0].backup_reference)
        self.assertEqual(retired_at, retired[0].retired_at)
        self.assertEqual(retired[0], self.store.active_page_retirement("P-1"))
        self.assertEqual(list(retired), self.store.page_retirement_history())
        self.assertEqual(
            {"due": 0, "leased": 0, "backoff": 0, "synced": 0, "retired": 2},
            self.store.status_counts(now=retired_at),
        )
        refresh_at = second_published_at + timedelta(
            days=state.SYNC_REFRESH_DAYS,
        )
        self.assertEqual(0, self.store.due_count(now=refresh_at))
        self.assertEqual([], self.store.list_due(now=refresh_at))
        self.assertIsNone(self.store.lease_next("blocked", now=refresh_at))
        self.assertEqual(
            "page_retired",
            self.store.precheck("P-1", now=refresh_at).reason,
        )
        self.assertEqual([], self.store.published_products())
        self.assertEqual(
            [],
            self.store.published_products(minimum_content_schema_version=3),
        )
        self.assertEqual([], self.store.content_refresh_candidates(4))
        self.assertEqual(
            0,
            self.store.requeue_products(
                ("synced",),
                reason="must-remain-retired",
                now=refresh_at,
            ),
        )
        with self.assertRaisesRegex(state.StateError, "page is retired"):
            self.store.record_content_refresh(
                candidate,
                4,
                wiki_action="updated",
                fact_diagnostics=candidate.fact_diagnostics,
                now=refresh_at,
            )

        changed = self.store.upsert_product(
            product(
                "P-1",
                name="Retired catalogue revision",
                updated_at=refresh_at,
            ),
            now=refresh_at,
        )
        self.assertTrue(changed.changed)
        self.assertFalse(changed.rescheduled)
        self.assertEqual(0, self.store.due_count(now=refresh_at))

        resolved = self.store.resolve_page_retirement(
            "P-1",
            reason="operator-approved-republication",
            now=refresh_at,
        )
        self.assertEqual(refresh_at, resolved.resolved_at)
        self.assertEqual(
            "operator-approved-republication",
            resolved.resolution_reason,
        )
        self.assertIsNone(self.store.active_page_retirement("P-1"))
        self.assertEqual(1, self.store.due_count(now=refresh_at))
        self.assertEqual(1, self.store.status_counts(now=refresh_at)["retired"])
        self.assertEqual(
            "P-1",
            self.store.lease_next("republisher", now=refresh_at).product_id,
        )

    def test_page_retirement_batch_rolls_back_on_leased_product(self):
        first_lease, first_published_at = self._publish_page("P-1")
        second_lease, second_published_at = self._publish_page("P-2")
        refresh_at = second_published_at + timedelta(
            days=state.SYNC_REFRESH_DAYS,
        )
        active_refresh = self.store.lease_next(
            "active-refresh",
            lease_seconds=3600,
            now=refresh_at,
        )
        self.assertIsNotNone(active_refresh)
        self.assertEqual("P-1", active_refresh.product_id)

        with self.assertRaisesRegex(state.StateError, "leased product"):
            self.store.record_page_retirements(
                (
                    state.PageRetirementInput(
                        "P-2",
                        second_lease.attempt_id,
                        "products/p-2",
                        202,
                        "d" * 64,
                        second_published_at,
                    ),
                    state.PageRetirementInput(
                        "P-1",
                        first_lease.attempt_id,
                        "products/p-1",
                        201,
                        "e" * 64,
                        first_published_at,
                    ),
                ),
                reason="legacy-content-schema",
                backup_sha256="f" * 64,
                retired_at=refresh_at,
            )

        self.assertEqual([], self.store.page_retirement_history())

    def test_page_retirement_reclaims_expired_lease_using_current_time(self):
        historical = datetime(2024, 1, 1, tzinfo=timezone.utc)
        lease, published_at = self._publish_page("P-1", now=historical)
        refresh_at = published_at + timedelta(days=state.SYNC_REFRESH_DAYS)
        expired_refresh = self.store.lease_next(
            "expired-refresh",
            lease_seconds=1,
            now=refresh_at,
        )
        self.assertIsNotNone(expired_refresh)

        retired = self.store.record_page_retirements(
            (
                state.PageRetirementInput(
                    "P-1",
                    lease.attempt_id,
                    "products/p-1",
                    301,
                    "1" * 64,
                    published_at,
                ),
            ),
            reason="historical-import",
            backup_sha256="2" * 64,
            retired_at=refresh_at,
        )

        self.assertEqual(1, len(retired))
        self.assertEqual(
            "lease_expired",
            self.store.attempt_history("P-1")[-1].outcome,
        )

    def test_retired_search_quota_wait_is_not_resumed(self):
        lease, published_at = self._publish_page("P-1")
        refresh_at = published_at + timedelta(days=state.SYNC_REFRESH_DAYS)
        refresh = self.store.lease_next("quota-refresh", now=refresh_at)
        self.store.record_outcome(
            refresh,
            "search_quota_exhausted",
            now=refresh_at + timedelta(seconds=1),
        )
        self.store.record_page_retirements(
            (
                state.PageRetirementInput(
                    "P-1",
                    lease.attempt_id,
                    "products/p-1",
                    302,
                    "6" * 64,
                    published_at,
                ),
            ),
            reason="legacy-content-schema",
            backup_sha256="7" * 64,
        )

        self.assertEqual(
            0,
            self.store.resume_search_quota_waits(
                now=refresh_at + timedelta(days=1),
            ),
        )
        self.assertEqual("backoff", self.store.get_product("P-1").status)
        self.assertEqual(0, self.store.due_count(now=refresh_at + timedelta(days=1)))

    def test_page_retirement_rejects_duplicate_page_identity_and_conflicts(self):
        lease, published_at = self._publish_page("P-1")
        self._publish_page("P-2")
        duplicate_entries = (
            state.PageRetirementInput(
                "P-1", lease.attempt_id, "products/p-1", 401, "3" * 64
            ),
            state.PageRetirementInput(
                "P-2", 2, "products/p-2", 401, "4" * 64
            ),
        )
        with self.assertRaisesRegex(ValueError, "wiki_page_id"):
            self.store.record_page_retirements(
                duplicate_entries,
                reason="duplicate-input",
                backup_sha256="5" * 64,
            )

        entry = state.PageRetirementInput(
            "P-1",
            lease.attempt_id,
            "products/p-1",
            401,
            "3" * 64,
            published_at,
        )
        self.store.record_page_retirements(
            (entry,),
            reason="first-retirement",
            backup_sha256="5" * 64,
        )
        with self.assertRaisesRegex(state.StateError, "conflicting"):
            self.store.record_page_retirements(
                (entry,),
                reason="different-reason",
                backup_sha256="5" * 64,
            )

    def test_failed_publication_retirement_uses_latest_path_bearing_failure(self):
        self.store.upsert_product(product("FAILED"), now=T0)
        first = self.store.lease_next("failed-first", now=T0)
        first_result = self.store.record_outcome(
            first,
            "wikijs_error",
            wiki_path="products/failed",
            error="write response was ambiguous",
            now=T0 + timedelta(seconds=1),
        )
        no_path = self.store.lease_next(
            "failed-no-path",
            now=first_result.next_run_at,
        )
        self.store.record_outcome(
            no_path,
            "publish_error",
            error="failed before selecting a path",
            now=first_result.next_run_at + timedelta(seconds=1),
        )

        record = self.store.record_page_retirements(
            (
                state.PageRetirementInput(
                    "FAILED",
                    first.attempt_id,
                    "products/failed",
                    501,
                    "8" * 64,
                    basis="failed_publication",
                ),
            ),
            reason="failed-publication-left-managed-page",
            backup_sha256="9" * 64,
        )[0]

        self.assertEqual("failed_publication", record.basis)
        self.assertEqual(first.attempt_id, record.source_attempt_id)

        self.store.upsert_product(product("NEWER"), now=T0)
        old = self.store.lease_next("newer-old", now=T0)
        old_result = self.store.record_outcome(
            old,
            "wikijs_conflict",
            wiki_path="products/newer",
            now=T0 + timedelta(seconds=1),
        )
        newest = self.store.lease_next(
            "newer-newest",
            now=old_result.next_run_at,
        )
        self.store.record_outcome(
            newest,
            "publish_error",
            wiki_path="products/newer",
            now=old_result.next_run_at + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(state.StateError, "latest path-bearing"):
            self.store.record_page_retirements(
                (
                    state.PageRetirementInput(
                        "NEWER",
                        old.attempt_id,
                        "products/newer",
                        502,
                        "a" * 64,
                        basis="failed_publication",
                    ),
                ),
                reason="stale-failure-anchor",
                backup_sha256="b" * 64,
            )

    def test_failed_publication_basis_rejects_non_publication_failure(self):
        self.store.upsert_product(product("NO-PDF"), now=T0)
        lease = self.store.lease_next("no-pdf", now=T0)
        self.store.record_outcome(
            lease,
            "no_datasheet",
            wiki_path="products/no-pdf",
            now=T0 + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(
            state.StateError,
            "requires a Wiki publication failure",
        ):
            self.store.record_page_retirements(
                (
                    state.PageRetirementInput(
                        "NO-PDF",
                        lease.attempt_id,
                        "products/no-pdf",
                        503,
                        "c" * 64,
                        basis="failed_publication",
                    ),
                ),
                reason="invalid-basis",
                backup_sha256="d" * 64,
            )

    def test_legacy_managed_page_uses_latest_completed_attempt_without_path(self):
        self.store.upsert_product(product("LEGACY"), now=T0)
        old = self.store.lease_next("legacy-old", now=T0)
        old_result = self.store.record_outcome(
            old,
            "no_datasheet",
            now=T0 + timedelta(seconds=1),
        )
        latest = self.store.lease_next(
            "legacy-latest",
            now=old_result.next_run_at,
        )
        self.store.record_outcome(
            latest,
            "ambiguous",
            now=old_result.next_run_at + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(state.StateError, "latest completed"):
            self.store.record_page_retirements(
                (
                    state.PageRetirementInput(
                        "LEGACY",
                        old.attempt_id,
                        "products/legacy-import",
                        504,
                        "e" * 64,
                        basis="legacy_managed_page",
                    ),
                ),
                reason="stale-legacy-anchor",
                backup_sha256="f" * 64,
            )

        record = self.store.record_page_retirements(
            (
                state.PageRetirementInput(
                    "LEGACY",
                    latest.attempt_id,
                    "products/legacy-import",
                    504,
                    "e" * 64,
                    basis="legacy_managed_page",
                ),
            ),
            reason="legacy-managed-import",
            backup_sha256="f" * 64,
        )[0]
        self.assertEqual("legacy_managed_page", record.basis)
        self.assertEqual(latest.attempt_id, record.source_attempt_id)

    def test_page_retirement_rejects_unknown_basis(self):
        with self.assertRaisesRegex(ValueError, "basis must be one of"):
            state._page_retirement_input(
                {
                    "product_id": "P-1",
                    "source_attempt_id": 1,
                    "wiki_path": "products/p-1",
                    "wiki_page_id": 1,
                    "page_content_sha256": "0" * 64,
                    "basis": "invented",
                }
            )

    def test_published_products_filters_minimum_content_schema_version(self):
        self._publish_page("P-1", content_schema_version=1)
        self._publish_page("P-3", content_schema_version=3)
        self._publish_page("P-4", content_schema_version=4)

        published = self.store.published_products(
            minimum_content_schema_version=3,
        )

        self.assertEqual({"P-3", "P-4"}, {item.product_id for item in published})
        with self.assertRaises(TypeError):
            self.store.published_products(
                minimum_content_schema_version=True,
            )

    def test_content_refresh_uses_last_synced_snapshot_and_audits_counts(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("publisher", now=T0)
        verified_at = T0 + timedelta(minutes=1)
        self.store.record_outcome(
            lease,
            "synced",
            payload={
                "decision": {
                    "outcome": "publish",
                    "manufacturer": "Acme Public",
                    "model": "Panel One",
                    "facts": [{"name": "Power", "value": 42}],
                },
                "validation_policy_fingerprint": "verified-policy",
                "fact_diagnostics": {
                    "complete": True,
                    "proposed": 2,
                    "retained": 1,
                    "rejected": 1,
                    "rejection_reasons": {"validation_failed": 1},
                },
            },
            wiki_path="products/p-1-a1",
            now=verified_at,
        )
        self.store.upsert_product(
            product(
                name="Panel unverified revision B",
                updated_at=T0 + timedelta(days=1),
            ),
            now=T0 + timedelta(days=1),
        )

        candidates = self.store.content_refresh_candidates(1)

        self.assertEqual(1, len(candidates))
        candidate = candidates[0]
        self.assertEqual("Panel", candidate.payload["product_name"])
        self.assertEqual("products/p-1-a1", candidate.wiki_path)
        self.assertEqual(verified_at, candidate.verified_at)
        self.assertEqual(0, candidate.previous_content_schema_version)
        self.assertEqual(1, candidate.fact_diagnostics["retained"])
        event = self.store.record_content_refresh(
            candidate,
            1,
            wiki_action="updated",
            fact_diagnostics=candidate.fact_diagnostics,
            now=T0 + timedelta(days=2),
        )

        self.assertEqual(0, event.previous_content_schema_version)
        self.assertEqual(1, event.content_schema_version)
        self.assertEqual("updated", event.wiki_action)
        self.assertIsNone(event.parameter_set_id)
        self.assertIsNone(event.analysis_run_id)
        self.assertEqual(
            1,
            self.store.get_product("P-1").content_schema_version,
        )
        self.assertEqual([], self.store.content_refresh_candidates(1))
        self.assertEqual(
            [event],
            self.store.content_refresh_event_history("P-1"),
        )

    def test_verified_parameter_sets_are_canonical_and_deduplicated(self):
        candidate = self._publish_refresh_candidate()
        first = self.store.record_verified_parameter_set(
            candidate,
            pdf_url="HTTPS://Example.COM:443/datasheet.pdf#page=1",
            pdf_sha256="A" * 64,
            parameters=[
                {
                    "name": "Maximum power",
                    "value": 10,
                    "unit": "kW",
                }
            ],
            extractor_version=" extractor-v1 ",
            validation_policy_fingerprint="B" * 64,
            now=T0 + timedelta(minutes=1),
        )
        replay = self.store.record_verified_parameter_set(
            candidate,
            pdf_url="https://example.com/datasheet.pdf#page=2",
            pdf_sha256="a" * 64,
            parameters=[
                {
                    "unit": "kW",
                    "value": 10,
                    "name": "Maximum power",
                }
            ],
            extractor_version="extractor-v1",
            validation_policy_fingerprint="b" * 64,
            now=T0 + timedelta(minutes=2),
        )

        self.assertEqual(first.parameter_set_id, replay.parameter_set_id)
        self.assertEqual(first.parameter_set_sha256, replay.parameter_set_sha256)
        self.assertEqual(1, first.parameter_count)
        self.assertEqual("https://example.com/datasheet.pdf", first.pdf_url)
        self.assertEqual("a" * 64, first.pdf_sha256)
        self.assertEqual(
            [first],
            self.store.verified_parameter_set_history("P-1"),
        )
        self.assertEqual(
            first,
            self.store.get_verified_parameter_set(first.parameter_set_id),
        )

    def test_parameter_analysis_run_lifecycle_is_append_only(self):
        candidate = self._publish_refresh_candidate()
        parameter_set = self.store.record_verified_parameter_set(
            candidate,
            pdf_url="https://example.com/datasheet.pdf",
            pdf_sha256="a" * 64,
            parameters=[{"name": "Power", "value": "10 kW"}],
            extractor_version="extractor-v1",
            validation_policy_fingerprint="b" * 64,
            now=T0 + timedelta(minutes=1),
        )
        request_fingerprint = "c" * 64

        started = self.store.begin_parameter_analysis_run(
            parameter_set.parameter_set_id,
            request_fingerprint=request_fingerprint,
            prompt_version="prompt-v1",
            glossary_version="glossary-v1",
            model="analysis-model",
            now=T0 + timedelta(minutes=2),
        )
        replay_started = self.store.begin_parameter_analysis_run(
            parameter_set.parameter_set_id,
            request_fingerprint=request_fingerprint,
            prompt_version="prompt-v1",
            glossary_version="glossary-v1",
            model="analysis-model",
            now=T0 + timedelta(minutes=3),
        )

        self.assertTrue(started.should_execute)
        self.assertFalse(replay_started.should_execute)
        self.assertEqual(
            started.record.analysis_run_id,
            replay_started.record.analysis_run_id,
        )
        self.assertIsNone(
            self.store.find_completed_parameter_analysis_run(
                parameter_set.parameter_set_id,
                request_fingerprint,
            )
        )

        failed = self.store.fail_parameter_analysis_run(
            started.record.analysis_run_id,
            error="provider timeout",
            usage={"prompt_tokens": 25},
            now=T0 + timedelta(minutes=4),
        )
        self.assertEqual("failed", failed.status)
        retry = self.store.begin_parameter_analysis_run(
            parameter_set.parameter_set_id,
            request_fingerprint=request_fingerprint,
            prompt_version="prompt-v1",
            glossary_version="glossary-v1",
            model="analysis-model",
            now=T0 + timedelta(minutes=5),
        )
        self.assertTrue(retry.should_execute)
        self.assertEqual(2, retry.record.attempt_number)
        completed = self.store.complete_parameter_analysis_run(
            retry.record.analysis_run_id,
            analysis={"valuable_parameters": ["Maximum power"]},
            usage={"prompt_tokens": 20, "completion_tokens": 5},
            now=T0 + timedelta(minutes=6),
        )
        self.assertEqual("completed", completed.status)
        self.assertEqual(
            completed,
            self.store.find_completed_parameter_analysis_run(
                parameter_set.parameter_set_id,
                request_fingerprint,
            ),
        )
        completed_replay = self.store.begin_parameter_analysis_run(
            parameter_set.parameter_set_id,
            request_fingerprint=request_fingerprint,
            prompt_version="prompt-v1",
            glossary_version="glossary-v1",
            model="analysis-model",
            now=T0 + timedelta(minutes=7),
        )
        self.assertFalse(completed_replay.should_execute)
        self.assertEqual(
            completed.analysis_run_id,
            completed_replay.record.analysis_run_id,
        )
        self.assertEqual(
            ["failed", "completed"],
            [
                run.status
                for run in self.store.parameter_analysis_run_history(
                    parameter_set.parameter_set_id
                )
            ],
        )

    def test_parameter_analysis_begin_is_concurrency_safe(self):
        candidate = self._publish_refresh_candidate()
        parameter_set = self.store.record_verified_parameter_set(
            candidate,
            pdf_url="https://example.com/datasheet.pdf",
            pdf_sha256="a" * 64,
            parameters=[],
            extractor_version="extractor-v1",
            validation_policy_fingerprint="b" * 64,
        )
        other = state.StateStore(self.path)

        def begin(store):
            return store.begin_parameter_analysis_run(
                parameter_set.parameter_set_id,
                request_fingerprint="c" * 64,
                prompt_version="prompt-v1",
                glossary_version="glossary-v1",
                model="analysis-model",
                now=T0 + timedelta(minutes=2),
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(begin, (self.store, other)))
        finally:
            other.close()

        self.assertEqual(1, sum(result.should_execute for result in results))
        self.assertEqual(
            1,
            len({result.record.analysis_run_id for result in results}),
        )

    def test_content_refresh_validates_parameter_and_analysis_provenance(self):
        candidate = self._publish_refresh_candidate("P-1")
        other_candidate = self._publish_refresh_candidate(
            "P-2",
            now=T0 + timedelta(minutes=1),
        )
        parameter_set = self.store.record_verified_parameter_set(
            candidate,
            pdf_url="https://example.com/p-1.pdf",
            pdf_sha256="a" * 64,
            parameters=[{"name": "Power", "value": "10 kW"}],
            extractor_version="extractor-v1",
            validation_policy_fingerprint="b" * 64,
        )
        other_parameter_set = self.store.record_verified_parameter_set(
            other_candidate,
            pdf_url="https://example.com/p-2.pdf",
            pdf_sha256="d" * 64,
            parameters=[],
            extractor_version="extractor-v1",
            validation_policy_fingerprint="e" * 64,
        )
        started = self.store.begin_parameter_analysis_run(
            parameter_set.parameter_set_id,
            request_fingerprint="c" * 64,
            prompt_version="prompt-v1",
            glossary_version="glossary-v1",
            model="analysis-model",
        )

        with self.assertRaisesRegex(state.StateError, "not completed"):
            self.store.record_content_refresh(
                candidate,
                1,
                wiki_action="updated",
                fact_diagnostics=candidate.fact_diagnostics,
                parameter_set_id=parameter_set.parameter_set_id,
                analysis_run_id=started.record.analysis_run_id,
            )
        with self.assertRaisesRegex(state.StateError, "does not belong"):
            self.store.record_content_refresh(
                candidate,
                1,
                wiki_action="updated",
                fact_diagnostics=candidate.fact_diagnostics,
                parameter_set_id=other_parameter_set.parameter_set_id,
            )

        other_started = self.store.begin_parameter_analysis_run(
            other_parameter_set.parameter_set_id,
            request_fingerprint="f" * 64,
            prompt_version="prompt-v1",
            glossary_version="glossary-v1",
            model="analysis-model",
        )
        other_completed = self.store.complete_parameter_analysis_run(
            other_started.record.analysis_run_id,
            analysis={"valuable_parameters": []},
            usage={},
        )
        with self.assertRaisesRegex(state.StateError, "parameter set"):
            self.store.record_content_refresh(
                candidate,
                1,
                wiki_action="updated",
                fact_diagnostics=candidate.fact_diagnostics,
                parameter_set_id=parameter_set.parameter_set_id,
                analysis_run_id=other_completed.analysis_run_id,
            )

        completed = self.store.complete_parameter_analysis_run(
            started.record.analysis_run_id,
            analysis={"valuable_parameters": ["Power"]},
            usage={},
        )
        event = self.store.record_content_refresh(
            candidate,
            1,
            wiki_action="updated",
            fact_diagnostics=candidate.fact_diagnostics,
            parameter_set_id=parameter_set.parameter_set_id,
            analysis_run_id=completed.analysis_run_id,
        )

        self.assertEqual(parameter_set.parameter_set_id, event.parameter_set_id)
        self.assertEqual(completed.analysis_run_id, event.analysis_run_id)
        self.assertEqual(
            [event],
            self.store.content_refresh_event_history("P-1"),
        )

    def test_synced_outcome_can_record_current_content_schema(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("publisher", now=T0)

        self.store.record_outcome(
            lease,
            "synced",
            content_schema_version=3,
            now=T0 + timedelta(seconds=1),
        )

        current = self.store.get_product("P-1")
        self.assertEqual(3, current.content_schema_version)
        self.assertEqual([], self.store.content_refresh_candidates(3))

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

    def test_content_quarantine_waits_for_annual_or_source_refresh(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("quarantine-worker", now=T0)

        outcome = self.store.record_outcome(
            lease,
            "content_quarantined",
            now=T0,
        )

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

    def test_lease_next_fairly_interleaves_mature_backoff_and_defers_synced(self):
        for product_id in [f"D-{index}" for index in range(8)]:
            self.store.upsert_product(product(product_id), now=T0)
        for product_id in ("B-1", "B-2", "S-1"):
            self.store.upsert_product(product(product_id), now=T0)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE products
                SET status = 'backoff'
                WHERE product_id IN ('B-1', 'B-2')
                """
            )
            connection.execute(
                """
                UPDATE products
                SET status = 'synced'
                WHERE product_id = 'S-1'
                """
            )

        leased_ids: list[str] = []
        for index in range(10):
            lease = self.store.lease_next(
                f"fair-worker-{index}",
                now=T0 + timedelta(seconds=index),
            )
            self.assertIsNotNone(lease)
            leased_ids.append(lease.product_id)
            self.store.record_outcome(
                lease,
                "synced",
                now=T0 + timedelta(seconds=index, microseconds=1),
            )
            if index == 4:
                # The fairness phase belongs to durable attempt history, not
                # process memory.
                self.store.close()
                self.store = state.StateStore(self.path)

        self.assertEqual(
            [
                "D-0",
                "D-1",
                "D-2",
                "D-3",
                "B-1",
                "D-4",
                "D-5",
                "D-6",
                "D-7",
                "B-2",
            ],
            leased_ids,
        )
        refresh = self.store.lease_next(
            "refresh-worker",
            now=T0 + timedelta(seconds=20),
        )
        self.assertEqual("S-1", refresh.product_id)

    def test_selective_requeue_uses_latest_attempt_cutover_and_preserves_audit(self):
        self.store.upsert_product(product("ACTIVE"), now=T0 - timedelta(hours=2))
        active_previous = self.store.lease_next(
            "active-old",
            now=T0 - timedelta(hours=2),
        )
        self.store.record_outcome(
            active_previous,
            "ai_error",
            error="old provider failure",
            now=T0 - timedelta(hours=2),
        )
        active_lease = self.store.lease_next(
            "active-current",
            lease_seconds=21_600,
            now=T0 - timedelta(minutes=30),
        )
        self.assertEqual("ACTIVE", active_lease.product_id)

        attempts = (
            ("OLD-INVALID", "invalid_decision", T0, "old contract"),
            (
                "OLD-AI",
                "ai_error",
                T0 + timedelta(minutes=1),
                "old prompt",
            ),
            (
                "OTHER",
                "no_datasheet",
                T0 + timedelta(minutes=2),
                "not selected",
            ),
            (
                "NEW-INVALID",
                "invalid_decision",
                T0 + timedelta(hours=2),
                "new contract",
            ),
        )
        for product_id, outcome, attempted_at, error in attempts:
            self.store.upsert_product(product(product_id), now=attempted_at)
            lease = self.store.lease_next(
                f"worker-{product_id}",
                now=attempted_at,
            )
            self.assertEqual(product_id, lease.product_id)
            self.store.record_outcome(
                lease,
                outcome,
                error=error,
                now=attempted_at + timedelta(seconds=1),
            )

        requeue_at = T0 + timedelta(days=1)
        self.assertEqual(
            1,
            self.store.requeue_products(
                {"invalid_decision", "ai_error"},
                reason="prompt-v3 evidence-policy-v2 cutover",
                attempted_after=T0 - timedelta(hours=3),
                attempted_before=T0 + timedelta(hours=1),
                limit=1,
                now=requeue_at,
            ),
        )
        self.assertEqual("due", self.store.get_product("OLD-INVALID").status)
        self.assertEqual(
            "invalid_decision",
            self.store.get_product("OLD-INVALID").last_outcome,
        )
        self.assertEqual(
            "old contract",
            self.store.get_product("OLD-INVALID").last_error,
        )
        self.assertEqual(
            1,
            self.store.requeue_products(
                ("invalid_decision", "ai_error"),
                reason="prompt-v3 evidence-policy-v2 cutover",
                attempted_after=T0 - timedelta(hours=3),
                attempted_before=T0 + timedelta(hours=1),
                limit=10,
                now=requeue_at,
            ),
        )
        self.assertEqual("due", self.store.get_product("OLD-AI").status)
        self.assertEqual("backoff", self.store.get_product("OTHER").status)
        self.assertEqual(
            "backoff",
            self.store.get_product("NEW-INVALID").status,
        )
        self.assertEqual("leased", self.store.get_product("ACTIVE").status)
        self.assertEqual(
            0,
            self.store.requeue_products(
                ("invalid_decision", "ai_error"),
                reason="prompt-v3 evidence-policy-v2 cutover",
                attempted_after=T0 - timedelta(hours=3),
                attempted_before=T0 + timedelta(hours=1),
                now=requeue_at,
            ),
        )
        self.assertEqual(1, len(self.store.attempt_history("OLD-INVALID")))

        with self.assertRaisesRegex(TypeError, "iterable"):
            self.store.requeue_products(
                "ai_error",
                reason="invalid outcomes container",
            )
        with self.assertRaisesRegex(ValueError, "non-empty"):
            self.store.requeue_products(("ai_error",), reason=" ")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.store.requeue_products(
                ("ai_error",),
                reason="cutover",
                attempted_after=datetime(2026, 1, 1),
            )
        with self.assertRaisesRegex(ValueError, "earlier"):
            self.store.requeue_products(
                ("ai_error",),
                reason="cutover",
                attempted_after=T0,
                attempted_before=T0,
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

    def test_global_research_usage_counts_known_unknown_and_uncertain_actions(self):
        evidence_url = "https://manufacturer.example/panel.pdf"
        self.store.upsert_product(product("R-KNOWN"), now=T0)
        known = self.store.lease_next(
            "known-worker",
            lease_seconds=20_000,
            now=T0,
        )
        search_fingerprint = state.research_request_fingerprint(
            "search",
            {"queries": ["known"]},
        )
        self.store.begin_research_action(
            known,
            round_number=0,
            action="search",
            request_fingerprint=search_fingerprint,
            now=T0,
        )
        self.store.finish_research_action(
            known,
            round_number=0,
            action="search",
            request_fingerprint=search_fingerprint,
            status="completed",
            result_summary={
                "queries": ["known"],
                "candidate_urls": [evidence_url],
            },
            credits=3,
            now=T0 + timedelta(seconds=1),
        )
        extract_fingerprint = state.research_request_fingerprint(
            "extract",
            {"urls": [evidence_url]},
        )
        self.store.begin_research_action(
            known,
            round_number=0,
            action="extract",
            request_fingerprint=extract_fingerprint,
            now=T0 + timedelta(seconds=2),
        )
        self.store.finish_research_action(
            known,
            round_number=0,
            action="extract",
            request_fingerprint=extract_fingerprint,
            status="completed",
            result_summary={
                "submitted_urls": [evidence_url],
                "successful_urls": [evidence_url],
            },
            credits=1,
            now=T0 + timedelta(seconds=3),
        )

        self.store.upsert_product(
            product("R-UNCERTAIN"),
            now=T0 + timedelta(hours=1),
        )
        uncertain = self.store.lease_next(
            "uncertain-worker",
            lease_seconds=20_000,
            now=T0 + timedelta(hours=1),
        )
        uncertain_fingerprint = state.research_request_fingerprint(
            "search",
            {"queries": ["uncertain"]},
        )
        self.store.begin_research_action(
            uncertain,
            round_number=0,
            action="search",
            request_fingerprint=uncertain_fingerprint,
            now=T0 + timedelta(hours=1),
        )
        self.store.finish_research_action(
            uncertain,
            round_number=0,
            action="search",
            request_fingerprint=uncertain_fingerprint,
            status="uncertain",
            error="response status unknown",
            now=T0 + timedelta(hours=1, seconds=1),
        )

        self.store.upsert_product(
            product("LEGACY"),
            now=T0 + timedelta(hours=2),
        )
        legacy = self.store.lease_next(
            "legacy-worker",
            lease_seconds=20_000,
            now=T0 + timedelta(hours=2),
        )
        self.store.begin_search(legacy, now=T0 + timedelta(hours=2))
        self.store.finish_search(
            legacy,
            [evidence_url],
            {"credits": 2},
            now=T0 + timedelta(hours=2, seconds=1),
        )
        self.store.begin_extract(
            legacy,
            [evidence_url],
            now=T0 + timedelta(hours=2, seconds=2),
        )

        usage = self.store.research_usage_between(
            start=T0,
            end=T0 + timedelta(days=1),
        )
        self.assertEqual(5, usage["actions"])
        self.assertEqual(6.0, usage["known_credits"])
        self.assertEqual(2, usage["unknown_credit_actions"])
        self.assertEqual(1, usage["uncertain_actions"])
        self.assertEqual(2, usage["unknown_or_uncertain_actions"])
        self.assertEqual(
            {
                "actions": 3,
                "known_credits": 5.0,
                "unknown_credit_actions": 1,
                "uncertain_actions": 1,
                "unknown_or_uncertain_actions": 1,
            },
            usage["by_action"]["search"],
        )
        self.assertEqual(
            {
                "actions": 2,
                "known_credits": 1.0,
                "unknown_credit_actions": 1,
                "uncertain_actions": 0,
                "unknown_or_uncertain_actions": 1,
            },
            usage["by_action"]["extract"],
        )
        narrow = self.store.research_usage_between(
            start=T0,
            end=T0 + timedelta(minutes=30),
        )
        self.assertEqual(2, narrow["actions"])
        self.assertEqual(4.0, narrow["known_credits"])

        with self.assertRaisesRegex(TypeError, "datetime"):
            self.store.research_usage_between(
                start="2026-01-01",  # type: ignore[arg-type]
                end=T0,
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.store.research_usage_between(
                start=datetime(2026, 1, 1),
                end=T0,
            )
        with self.assertRaisesRegex(ValueError, "earlier"):
            self.store.research_usage_between(start=T0, end=T0)

    def test_ai_research_summary_accepts_only_bounded_numeric_metadata(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next(
            "ai-metadata-worker",
            lease_seconds=3600,
            now=T0,
        )

        def reserve(round_number: int) -> str:
            fingerprint = state.research_request_fingerprint(
                "ai",
                {"round": round_number},
            )
            self.store.begin_research_action(
                lease,
                round_number=round_number,
                action="ai",
                request_fingerprint=fingerprint,
                now=T0 + timedelta(seconds=round_number),
            )
            return fingerprint

        accepted_fingerprint = reserve(0)
        accepted = self.store.finish_research_action(
            lease,
            round_number=0,
            action="ai",
            request_fingerprint=accepted_fingerprint,
            status="completed",
            result_summary={
                "action_type": "final",
                "finish_reasons": ["STOP", "length"],
                "usage_totals": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "reasoning_tokens": 10,
                    "total_tokens": 150,
                },
            },
            credits=0,
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(
            ["stop", "length"],
            accepted.result_summary["finish_reasons"],
        )
        self.assertEqual(
            150,
            accepted.result_summary["usage_totals"]["total_tokens"],
        )

        invalid_fingerprint = reserve(1)
        invalid_summaries = (
            {"finish_reasons": ["stop", "stop", "stop"]},
            {"finish_reasons": ["provider said: done"]},
            {"usage_totals": {"provider_text": 1}},
            {"usage_totals": {"prompt_tokens": "120"}},
            {
                "usage_totals": {
                    "prompt_tokens": state.MAX_RESEARCH_AI_TOKEN_USAGE + 1
                }
            },
            {"usage_totals": {"prompt_tokens": {"value": 120}}},
            {"error_category": "DECISION_CONTRACT"},
            {"error_category": "unknown_contract"},
        )
        for summary in invalid_summaries:
            with self.subTest(summary=summary), self.assertRaises(
                (TypeError, ValueError)
            ):
                self.store.finish_research_action(
                    lease,
                    round_number=1,
                    action="ai",
                    request_fingerprint=invalid_fingerprint,
                    status="completed",
                    result_summary={
                        "action_type": "final",
                        **summary,
                    },
                    credits=0,
                    now=T0 + timedelta(seconds=2),
                )
        failed = self.store.finish_research_action(
            lease,
            round_number=1,
            action="ai",
            request_fingerprint=invalid_fingerprint,
            status="failed",
            result_summary={
                "provider_fingerprint": "a" * 64,
                "provider_requests": 1,
                "error_type": "AIInvalidOutputError",
                "error_category": "action_contract",
            },
            credits=0,
            error="invalid output after repair",
            now=T0 + timedelta(seconds=2),
        )
        self.assertEqual(
            "action_contract",
            failed.result_summary["error_category"],
        )

    def test_provider_rejection_accessors_return_true_finish_and_expiry(self):
        exa_fingerprint = "b" * 64
        ai_fingerprint = "c" * 64
        window = timedelta(hours=1)

        def record_rejection(
            product_id,
            action,
            provider_fingerprint,
            http_status,
            finished_at,
        ):
            started_at = finished_at - timedelta(seconds=1)
            self.store.upsert_product(
                product(product_id),
                now=started_at,
            )
            lease = self.store.lease_next(
                f"worker-{product_id}",
                lease_seconds=7200,
                now=started_at,
            )
            request_fingerprint = state.research_request_fingerprint(
                action,
                {"product_id": product_id},
            )
            self.store.begin_research_action(
                lease,
                round_number=0,
                action=action,
                request_fingerprint=request_fingerprint,
                now=started_at,
            )
            result = self.store.finish_research_action(
                lease,
                round_number=0,
                action=action,
                request_fingerprint=request_fingerprint,
                status="failed",
                result_summary={
                    "provider_fingerprint": provider_fingerprint,
                    "provider_requests": 1,
                    "http_status": http_status,
                    "error_type": (
                        "AIHTTPError" if action == "ai" else "ExaHTTPError"
                    ),
                },
                credits=0,
                error="definitive provider rejection",
                now=finished_at,
            )
            return result

        search_finished = T0 + timedelta(seconds=10)
        extract_finished = T0 + timedelta(seconds=20)
        ai_finished = T0 + timedelta(seconds=30)
        search = record_rejection(
            "P-SEARCH",
            "search",
            exa_fingerprint,
            429,
            search_finished,
        )
        extract = record_rejection(
            "P-EXTRACT",
            "extract",
            exa_fingerprint,
            403,
            extract_finished,
        )
        record_rejection(
            "P-AI",
            "ai",
            ai_fingerprint,
            503,
            ai_finished,
        )
        quota_finished = T0 + timedelta(seconds=25)
        quota_started = quota_finished - timedelta(seconds=1)
        self.store.upsert_product(
            product("P-QUOTA"),
            now=quota_started,
        )
        quota_lease = self.store.lease_next(
            "worker-P-QUOTA",
            lease_seconds=7200,
            now=quota_started,
        )
        quota_request = state.research_request_fingerprint(
            "search",
            {"product_id": "P-QUOTA"},
        )
        self.store.begin_research_action(
            quota_lease,
            round_number=0,
            action="search",
            request_fingerprint=quota_request,
            now=quota_started,
        )
        self.store.finish_research_action(
            quota_lease,
            round_number=0,
            action="search",
            request_fingerprint=quota_request,
            status="failed",
            result_summary={
                "provider_fingerprint": exa_fingerprint,
                "provider_requests": 1,
                "error_type": "ExaQuotaExhaustedError",
            },
            credits=0,
            error="monthly provider quota exhausted",
            now=quota_finished,
        )
        self.assertEqual(exa_fingerprint, search.result_summary[
            "provider_fingerprint"
        ])
        self.assertEqual(403, extract.result_summary["http_status"])

        exa_event = self.store.recent_exa_provider_rejection(
            exa_fingerprint,
            http_statuses={403, 429},
            within=window,
            now=T0 + timedelta(minutes=1),
        )
        self.assertEqual("extract", exa_event.action)
        self.assertEqual("failed", exa_event.research_status)
        self.assertEqual(403, exa_event.http_status)
        self.assertEqual("ExaHTTPError", exa_event.error_type)
        self.assertEqual(extract_finished, exa_event.finished_at)
        self.assertEqual(extract_finished + window, exa_event.expires_at)

        search_event = self.store.recent_provider_rejection(
            exa_fingerprint,
            actions=("search",),
            http_statuses=(429,),
            within=window,
            now=T0 + timedelta(minutes=1),
        )
        self.assertEqual("search", search_event.action)
        self.assertEqual(search_finished, search_event.finished_at)

        quota_event = self.store.recent_exa_provider_error(
            exa_fingerprint,
            error_types={"ExaQuotaExhaustedError"},
            within=window,
            now=T0 + timedelta(minutes=1),
        )
        self.assertEqual("search", quota_event.action)
        self.assertEqual("failed", quota_event.research_status)
        self.assertEqual("ExaQuotaExhaustedError", quota_event.error_type)
        self.assertIsNone(quota_event.http_status)
        self.assertEqual(quota_finished, quota_event.finished_at)
        self.assertEqual(quota_finished + window, quota_event.expires_at)
        self.assertIsNone(
            self.store.recent_provider_error(
                exa_fingerprint,
                actions=("extract",),
                error_types={"ExaQuotaExhaustedError"},
                within=window,
                now=T0 + timedelta(minutes=1),
            )
        )

        ai_event = self.store.recent_ai_provider_rejection_event(
            ai_fingerprint,
            http_statuses=(503,),
            within=window,
            now=ai_finished + window - timedelta(microseconds=1),
        )
        self.assertEqual(ai_finished, ai_event.finished_at)
        self.assertEqual(ai_finished + window, ai_event.expires_at)
        self.assertEqual(
            503,
            self.store.recent_ai_provider_rejection(
                ai_fingerprint,
                http_statuses=(503,),
                within=window,
                now=ai_finished + timedelta(minutes=1),
            ),
        )
        self.assertIsNone(
            self.store.recent_ai_provider_rejection_event(
                ai_fingerprint,
                http_statuses=(503,),
                within=window,
                now=ai_finished + window,
            )
        )
        self.assertIsNone(
            self.store.recent_ai_provider_rejection_event(
                ai_fingerprint,
                http_statuses=(503,),
                within=window,
                now=ai_finished - timedelta(microseconds=1),
            )
        )
        with self.assertRaisesRegex(TypeError, "iterable"):
            self.store.recent_provider_rejection(
                exa_fingerprint,
                actions="search",
                http_statuses=(429,),
                within=window,
            )
        with self.assertRaisesRegex(ValueError, "100 to 599"):
            self.store.recent_exa_provider_rejection(
                exa_fingerprint,
                http_statuses=(99,),
                within=window,
            )
        with self.assertRaisesRegex(ValueError, "positive"):
            self.store.recent_exa_provider_error(
                exa_fingerprint,
                error_types={"ExaQuotaExhaustedError"},
                within=timedelta(0),
            )

    def test_search_provider_metadata_is_strictly_bounded(self):
        self.store.upsert_product(product(), now=T0)
        lease = self.store.lease_next("search-metadata-worker", now=T0)
        request_fingerprint = state.research_request_fingerprint(
            "search",
            {"queries": ["panel datasheet"]},
        )
        self.store.begin_research_action(
            lease,
            round_number=0,
            action="search",
            request_fingerprint=request_fingerprint,
            now=T0,
        )
        invalid_summaries = (
            {"provider_fingerprint": "not-a-fingerprint"},
            {"http_status": 99},
            {"http_status": True},
            {"error_type": "unsafe error type"},
        )
        for summary in invalid_summaries:
            with self.subTest(summary=summary), self.assertRaises(
                (TypeError, ValueError)
            ):
                self.store.finish_research_action(
                    lease,
                    round_number=0,
                    action="search",
                    request_fingerprint=request_fingerprint,
                    status="failed",
                    result_summary=summary,
                    credits=0,
                    error="provider rejection",
                    now=T0 + timedelta(seconds=1),
                )

        finished = self.store.finish_research_action(
            lease,
            round_number=0,
            action="search",
            request_fingerprint=request_fingerprint,
            status="failed",
            result_summary={
                "provider_fingerprint": "d" * 64,
                "http_status": 429,
                "error_type": "ExaHTTPError",
            },
            credits=0,
            error="provider rejection",
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(429, finished.result_summary["http_status"])

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
        cases = (
            ("AIInvalidOutputError", "action_contract"),
            ("AIInvalidOutputError", "invalid_json"),
            ("AIResponseError", None),
        )
        for index, (error_type, category) in enumerate(cases):
            product_id = f"P-AI-{index}"
            started_at = T0 + timedelta(seconds=index * 10)
            self.store.upsert_product(
                product(product_id=product_id),
                now=started_at,
            )
            lease = self.store.lease_next(
                f"worker-{index}",
                now=started_at,
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
                now=started_at,
            )
            summary = {
                "provider_fingerprint": provider_fingerprint,
                "provider_requests": 2,
                "error_type": error_type,
            }
            if category is not None:
                summary["error_category"] = category
            self.store.finish_research_action(
                lease,
                round_number=0,
                action="ai",
                request_fingerprint=fingerprint,
                status="uncertain",
                result_summary=summary,
                error="invalid output after repair",
                now=started_at + timedelta(seconds=1),
            )
            self.store.record_outcome(
                lease,
                "ai_error",
                error="invalid output after repair",
                now=started_at + timedelta(seconds=2),
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
        self.assertEqual(
            1,
            self.store.recent_ai_provider_error_products(
                provider_fingerprint,
                error_types={"AIInvalidOutputError"},
                categories={"action_contract"},
                within=timedelta(hours=1),
                now=T0 + timedelta(minutes=1),
            ),
        )
        self.assertEqual(
            1,
            self.store.recent_ai_provider_error_products(
                provider_fingerprint,
                error_types={"AIResponseError"},
                within=timedelta(hours=1),
                now=T0 + timedelta(minutes=1),
            ),
        )
        self.assertEqual(
            0,
            self.store.recent_ai_provider_error_products(
                provider_fingerprint,
                error_types={"AIResponseError"},
                categories={"action_contract"},
                within=timedelta(hours=1),
                now=T0 + timedelta(minutes=1),
            ),
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.store.recent_ai_provider_error_products(
                provider_fingerprint,
                error_types={"AIInvalidOutputError"},
                categories={"invented_category"},
                within=timedelta(hours=1),
                now=T0 + timedelta(minutes=1),
            )

    def test_ai_provider_error_count_filters_before_high_volume_count(self):
        target_fingerprint = "1" * 64
        noise_fingerprint = "f" * 64
        future_fingerprint = "2" * 64
        for product_id in ("TARGET", "NOISE", "FUTURE"):
            self.store.upsert_product(product(product_id), now=T0)

        target_finished = T0 + timedelta(seconds=1)
        observed_at = T0 + timedelta(minutes=30)
        future_finished = observed_at + timedelta(seconds=1)

        def insert_action(
            connection,
            *,
            product_id,
            sequence,
            provider_fingerprint,
            finished_at,
        ):
            attempt = connection.execute(
                """
                INSERT INTO attempts (
                    product_id, source_hash, lease_token, worker_id,
                    started_at, lease_until, finished_at, outcome
                ) VALUES (?, ?, ?, 'audit-worker', ?, ?, ?, 'ai_error')
                """,
                (
                    product_id,
                    f"source-{product_id}",
                    f"audit-token-{sequence}",
                    state._time_text(finished_at - timedelta(seconds=1)),
                    state._time_text(finished_at + timedelta(minutes=1)),
                    state._time_text(finished_at),
                ),
            )
            connection.execute(
                """
                INSERT INTO research_actions (
                    attempt_id, round_number, action, status,
                    request_fingerprint, started_at, finished_at,
                    result_summary_json, credits, error
                ) VALUES (?, 0, 'ai', 'failed', ?, ?, ?, ?, 0, ?)
                """,
                (
                    int(attempt.lastrowid),
                    f"{sequence:064x}"[-64:],
                    state._time_text(finished_at - timedelta(seconds=1)),
                    state._time_text(finished_at),
                    (
                        '{"error_category":"action_contract",'
                        '"error_type":"AIInvalidOutputError",'
                        f'"provider_fingerprint":"{provider_fingerprint}"'
                        "}"
                    ),
                    "invalid output after repair",
                ),
            )

        with sqlite3.connect(self.path) as connection:
            insert_action(
                connection,
                product_id="TARGET",
                sequence=1,
                provider_fingerprint=target_fingerprint,
                finished_at=target_finished,
            )
            # More than 1000 newer rows from another provider would mask the
            # target if filtering happened after ORDER/LIMIT.
            for sequence in range(2, 1003):
                insert_action(
                    connection,
                    product_id="NOISE",
                    sequence=sequence,
                    provider_fingerprint=noise_fingerprint,
                    finished_at=T0 + timedelta(seconds=2),
                )
            insert_action(
                connection,
                product_id="FUTURE",
                sequence=1003,
                provider_fingerprint=future_fingerprint,
                finished_at=future_finished,
            )

        self.assertEqual(
            1,
            self.store.recent_ai_provider_error_products(
                target_fingerprint,
                error_types={"AIInvalidOutputError"},
                categories={"action_contract"},
                within=timedelta(hours=1),
                now=observed_at,
            ),
        )
        self.assertEqual(
            0,
            self.store.recent_ai_provider_error_products(
                future_fingerprint,
                error_types={"AIInvalidOutputError"},
                within=timedelta(hours=1),
                now=observed_at,
            ),
        )
        self.assertEqual(
            0,
            self.store.recent_ai_provider_error_products(
                target_fingerprint,
                error_types={"AIInvalidOutputError"},
                categories={"invalid_json"},
                within=timedelta(hours=1),
                now=observed_at,
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

    def test_content_failure_streak_counts_alternating_outcomes_per_revision(self):
        self.store.upsert_product(product(), now=T0)
        current = T0
        for outcome in (
            "no_datasheet",
            "ambiguous",
            "insufficient_identity",
        ):
            lease = self.store.lease_next("content-worker", now=current)
            result = self.store.record_outcome(
                lease,
                outcome,
                now=current,
            )
            current = result.next_run_at

        self.assertEqual(3, self.store.content_failure_streak("P-1"))
        self.assertEqual(
            1,
            self.store.content_failure_streak(
                "P-1",
                outcomes=("insufficient_identity",),
            ),
        )
        self.store.upsert_product(
            product(
                name="Panel revision B",
                updated_at=current + timedelta(days=1),
            ),
            now=current + timedelta(days=1),
        )
        self.assertEqual(0, self.store.content_failure_streak("P-1"))
        with self.assertRaises(state.UnknownProductError):
            self.store.content_failure_streak("MISSING")

    def test_requeue_starts_new_content_failure_epoch_and_audits_reason(self):
        self.store.upsert_product(product(), now=T0)
        current = T0
        latest_attempt_id = 0
        latest_finished_at = T0
        for index in range(6):
            lease = self.store.lease_next(
                f"content-worker-{index}",
                now=current,
            )
            latest_attempt_id = lease.attempt_id
            latest_finished_at = current + timedelta(seconds=1)
            result = self.store.record_outcome(
                lease,
                "invalid_decision",
                now=latest_finished_at,
            )
            current = result.next_run_at

        self.assertEqual(6, self.store.content_failure_streak("P-1"))
        self.assertEqual(6, len(self.store.attempt_history("P-1")))

        requeued_at = latest_finished_at + timedelta(minutes=1)
        self.assertEqual(
            1,
            self.store.requeue_products(
                ("invalid_decision",),
                reason="  prompt-v4   evidence-policy-v3  ",
                now=requeued_at,
            ),
        )
        self.assertEqual(0, self.store.content_failure_streak("P-1"))
        self.assertEqual(6, len(self.store.attempt_history("P-1")))
        self.assertEqual(
            latest_attempt_id,
            self.store.get_product(
                "P-1"
            ).content_failure_cutoff_attempt_id,
        )

        events = self.store.requeue_event_history("P-1")
        self.assertEqual(1, len(events))
        self.assertEqual("P-1", events[0].product_id)
        self.assertEqual(latest_attempt_id, events[0].cutoff_attempt_id)
        self.assertEqual("backoff", events[0].previous_status)
        self.assertEqual("invalid_decision", events[0].previous_outcome)
        self.assertEqual(
            "prompt-v4 evidence-policy-v3",
            events[0].reason,
        )
        self.assertEqual(requeued_at, events[0].requeued_at)
        self.assertIsNone(events[0].attempted_after)
        self.assertIsNone(events[0].attempted_before)

        new_lease = self.store.lease_next(
            "content-worker-new-policy",
            now=requeued_at,
        )
        self.store.record_outcome(
            new_lease,
            "no_datasheet",
            now=requeued_at + timedelta(seconds=1),
        )
        self.assertEqual(1, self.store.content_failure_streak("P-1"))
        self.assertEqual(7, len(self.store.attempt_history("P-1")))
        self.assertEqual(1, len(self.store.requeue_event_history(limit=1)))

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
                product_columns = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA table_info(products)"
                    )
                }
                requeue_indexes = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA index_list(requeue_events)"
                    )
                }
                refresh_columns = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA table_info(content_refresh_events)"
                    )
                }
                analysis_indexes = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA index_list(parameter_analysis_runs)"
                    )
                }
                retirement_indexes = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA index_list(page_retirements)"
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
            self.assertIn("requeue_events", tables)
            self.assertIn("content_refresh_events", tables)
            self.assertIn("verified_parameter_sets", tables)
            self.assertIn("parameter_analysis_runs", tables)
            self.assertIn("page_retirements", tables)
            self.assertIn(
                "content_failure_cutoff_attempt_id",
                product_columns,
            )
            self.assertIn("leased_from_status", product_columns)
            self.assertIn("leased_from_next_run_at", product_columns)
            self.assertIn("content_schema_version", product_columns)
            self.assertIn("requeue_events_product_idx", requeue_indexes)
            self.assertIn("parameter_set_id", refresh_columns)
            self.assertIn("analysis_run_id", refresh_columns)
            self.assertIn(
                "parameter_analysis_runs_started_idx",
                analysis_indexes,
            )
            self.assertIn(
                "parameter_analysis_runs_completed_idx",
                analysis_indexes,
            )
            self.assertTrue(
                {
                    "page_retirements_product_idx",
                    "page_retirements_active_product_idx",
                    "page_retirements_active_path_idx",
                    "page_retirements_active_page_id_idx",
                }.issubset(retirement_indexes)
            )
            legacy_product = migrated.get_product("LEGACY")
            self.assertEqual("due", legacy_product.leased_from_status)
            self.assertEqual(T0, legacy_product.leased_from_next_run_at)

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

    def test_migrates_v10_refresh_events_with_empty_parameter_links(self):
        legacy_path = Path(self.tempdir.name) / "legacy-v10.sqlite3"
        timestamp = "2026-01-01T12:00:00.000000Z"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            "CREATE TABLE products (product_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            """
            CREATE TABLE attempts (
                attempt_id INTEGER PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(product_id),
                source_hash TEXT NOT NULL
            )
            """
        )
        for statement in state._CREATE_CONTENT_REFRESH_EVENT_SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO products (product_id) VALUES ('LEGACY')"
        )
        connection.execute(
            """
            INSERT INTO attempts (attempt_id, product_id, source_hash)
            VALUES (1, 'LEGACY', 'legacy-source')
            """
        )
        connection.execute(
            """
            INSERT INTO content_refresh_events (
                product_id,
                source_attempt_id,
                previous_content_schema_version,
                content_schema_version,
                wiki_action,
                fact_diagnostics_json,
                refreshed_at
            ) VALUES ('LEGACY', 1, 0, 1, 'updated', ?, ?)
            """,
            (
                (
                    '{"complete":true,"proposed":0,"retained":0,'
                    '"rejected":0,"rejection_reasons":{}}'
                ),
                timestamp,
            ),
        )
        connection.execute("PRAGMA user_version = 10")
        connection.commit()
        connection.close()

        migrated = state.StateStore(legacy_path)
        try:
            self.assertEqual(state.SCHEMA_VERSION, migrated.schema_version)
            events = migrated.content_refresh_event_history("LEGACY")
            self.assertEqual(1, len(events))
            self.assertIsNone(events[0].parameter_set_id)
            self.assertIsNone(events[0].analysis_run_id)
        finally:
            migrated.close()

    def test_migrates_v12_page_retirements_to_synced_basis(self):
        legacy_path = Path(self.tempdir.name) / "legacy-v12.sqlite3"
        timestamp = "2026-01-01T12:00:00.000000Z"
        connection = sqlite3.connect(legacy_path)
        for statement in state._CREATE_PAGE_RETIREMENT_SCHEMA:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO page_retirements (
                product_id, source_attempt_id, source_hash,
                retired_content_schema_version, wiki_path, wiki_page_id,
                page_content_sha256, backup_sha256, backup_reference,
                reason, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "LEGACY",
                1,
                "1" * 64,
                3,
                "products/legacy",
                99,
                "2" * 64,
                "3" * 64,
                "legacy-pages.json",
                "pre-v13-retirement",
                timestamp,
            ),
        )
        connection.execute("PRAGMA user_version = 12")
        connection.commit()
        connection.close()

        migrated = state.StateStore(legacy_path)
        try:
            self.assertEqual(state.SCHEMA_VERSION, migrated.schema_version)
            with sqlite3.connect(legacy_path) as migrated_connection:
                basis = migrated_connection.execute(
                    "SELECT basis FROM page_retirements WHERE retirement_id = 1"
                ).fetchone()[0]
            self.assertEqual("synced_publication", basis)
            self.assertEqual(
                "synced_publication",
                migrated.page_retirement_history()[0].basis,
            )
        finally:
            migrated.close()

    def test_migrates_v7_requeue_policy_boundary_to_current_schema(self):
        legacy_path = Path(self.tempdir.name) / "legacy-v7.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            """
            CREATE TABLE products (
                product_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'due',
                next_run_at TEXT NOT NULL
                    DEFAULT '2026-01-01T12:00:00.000000Z'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE research_actions (
                action_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                result_summary_json TEXT,
                credits REAL,
                error TEXT,
                finished_at TEXT
            )
            """
        )
        legacy_rows = (
            (
                1,
                "ai",
                "uncertain",
                '{"error_type":"AIInvalidOutputError","provider_requests":1}',
                None,
                "legacy invalid output",
                "2026-01-01T12:00:01.000000Z",
            ),
            (
                2,
                "ai",
                "uncertain",
                '{"error_type":"AIInvalidOutputError","provider_requests":2}',
                2.5,
                "legacy invalid output with known credits",
                "2026-01-01T12:00:02.000000Z",
            ),
            (
                3,
                "ai",
                "uncertain",
                '{"error_type":"AINetworkError","provider_requests":1}',
                None,
                "connection reset after request",
                "2026-01-01T12:00:03.000000Z",
            ),
            (
                4,
                "ai",
                "uncertain",
                '{"error_type":"AIResponseError","provider_requests":1}',
                None,
                "legacy generic response error",
                "2026-01-01T12:00:04.000000Z",
            ),
            (
                5,
                "ai",
                "uncertain",
                "{malformed",
                None,
                "corrupt legacy summary",
                "2026-01-01T12:00:05.000000Z",
            ),
            (
                6,
                "search",
                "uncertain",
                '{"error_type":"AIInvalidOutputError"}',
                None,
                "unrelated action",
                "2026-01-01T12:00:06.000000Z",
            ),
        )
        connection.executemany(
            """
            INSERT INTO research_actions (
                action_id, action, status, result_summary_json,
                credits, error, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            legacy_rows,
        )
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
        connection.close()

        migrated = state.StateStore(legacy_path)
        try:
            self.assertEqual(state.SCHEMA_VERSION, migrated.schema_version)
            migrated_connection = sqlite3.connect(legacy_path)
            try:
                product_columns = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA table_info(products)"
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
                indexes = {
                    row[1]
                    for row in migrated_connection.execute(
                        "PRAGMA index_list(requeue_events)"
                    )
                }
                action_rows = migrated_connection.execute(
                    """
                    SELECT
                        action_id, action, status, result_summary_json,
                        credits, error, finished_at
                    FROM research_actions
                    ORDER BY action_id
                    """
                ).fetchall()
            finally:
                migrated_connection.close()
            self.assertIn(
                "content_failure_cutoff_attempt_id",
                product_columns,
            )
            self.assertIn("leased_from_status", product_columns)
            self.assertIn("leased_from_next_run_at", product_columns)
            self.assertIn("requeue_events", tables)
            self.assertIn("requeue_events_product_idx", indexes)
            self.assertEqual("failed", action_rows[0][2])
            self.assertEqual(0.0, action_rows[0][4])
            self.assertEqual(legacy_rows[0][3], action_rows[0][3])
            self.assertEqual(legacy_rows[0][5], action_rows[0][5])
            self.assertEqual(legacy_rows[0][6], action_rows[0][6])
            self.assertEqual("failed", action_rows[1][2])
            self.assertEqual(2.5, action_rows[1][4])
            for row, legacy in zip(action_rows[2:], legacy_rows[2:]):
                self.assertEqual(legacy, tuple(row))
        finally:
            migrated.close()

    def test_migrates_v8_active_lease_with_conservative_due_snapshot(self):
        legacy_path = Path(self.tempdir.name) / "legacy-v8.sqlite3"
        original_next_run = "2026-01-01T11:30:00.000000Z"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            """
            CREATE TABLE products (
                product_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                next_run_at TEXT NOT NULL,
                content_failure_cutoff_attempt_id
                    INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO products (product_id, status, next_run_at)
            VALUES (?, ?, ?)
            """,
            (
                ("ACTIVE", "leased", original_next_run),
                ("RUNNABLE", "backoff", original_next_run),
            ),
        )
        connection.execute("PRAGMA user_version = 8")
        connection.commit()
        connection.close()

        migrated = state.StateStore(legacy_path)
        try:
            self.assertEqual(state.SCHEMA_VERSION, migrated.schema_version)
            migrated_connection = sqlite3.connect(legacy_path)
            try:
                rows = migrated_connection.execute(
                    """
                    SELECT
                        product_id,
                        leased_from_status,
                        leased_from_next_run_at
                    FROM products
                    ORDER BY product_id
                    """
                ).fetchall()
            finally:
                migrated_connection.close()
            self.assertEqual(
                ("ACTIVE", "due", original_next_run),
                rows[0],
            )
            self.assertEqual(
                ("RUNNABLE", None, None),
                rows[1],
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
