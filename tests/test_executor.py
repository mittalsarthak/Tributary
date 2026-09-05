"""Tests for tributary.executor: the module that actually runs a Plan
against a real, live database.

Every test here uses a real Postgres (`conn`/`pg_dsn`/`fresh_schema` from
`tests/conftest.py`) -- lock behaviour in particular cannot be mocked
meaningfully, and this module's entire reason for existing is lock/backfill
behaviour under real contention.

R22: `test_shadow_backfill_preserves_every_value` passes `pk_columns` and
asserts a `kind == "backfill"` step is actually present in the plan before
running it -- without that guard, R19's no-PK conservative fallback would
make this test pass while exercising none of the shadow-column backfill it
is named for.
"""

import threading
import time
import uuid

import psycopg
import pytest
from psycopg import sql

from tributary import executor, store
from tributary.introspect import snapshot, table_stats
from tributary.model import (
    AddColumn,
    AlterColumnType,
    Column,
    CreateIndex,
    Index,
    Plan,
    Safety,
    SetNotNull,
    Step,
    TableStats,
)
from tributary.planner import plan


def test_plan_applies_to_a_real_schema(conn, pg_dsn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY, ts text)')
    p = plan([AddColumn("events", Column("note", "text", True, None, 3))],
             table_stats(conn, fresh_schema), fresh_schema)
    executor.run(pg_dsn, fresh_schema, p)
    assert "note" in snapshot(conn, fresh_schema).tables["events"].columns


def test_concurrent_index_is_created_and_is_valid(conn, pg_dsn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY, ts text)')
    idx = Index("ix_ts", f'CREATE INDEX ix_ts ON "{fresh_schema}".events USING btree (ts)', ("ts",))
    p = plan([CreateIndex("events", idx)],
             {"events": TableStats(rows=5_000_000, bytes=900_000_000)},
             fresh_schema)
    executor.run(pg_dsn, fresh_schema, p)
    valid = conn.execute(
        "SELECT x.indisvalid FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
        "JOIN pg_class c ON c.oid = x.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND i.relname = 'ix_ts'", (fresh_schema,)).fetchone()
    assert valid == (True,)


def test_shadow_backfill_preserves_every_value(conn, pg_dsn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY, n int)')
    conn.execute(f'INSERT INTO "{fresh_schema}".events '
                 f'SELECT i, i*2 FROM generate_series(1,5000) i')
    p = plan([AlterColumnType("events", "n", "int4", "int8")],
             {"events": TableStats(rows=5_000_000, bytes=900_000_000)},
             fresh_schema, batch_size=500, pk_columns={"events": "id"})

    # R22 guard: fail loudly, not silently, if this ever stops exercising the
    # shadow-column backfill it is named for.
    assert any(s.kind == "backfill" for s in p.steps), (
        "no backfill step in the plan -- this test would otherwise pass "
        "while testing nothing about the shadow-column backfill"
    )

    executor.run(pg_dsn, fresh_schema, p, batch_size=500)
    col = snapshot(conn, fresh_schema).tables["events"].columns["n"]
    assert col.type == "int8"
    bad = conn.execute(f'SELECT count(*) FROM "{fresh_schema}".events '
                       f'WHERE n <> id*2').fetchone()[0]
    assert bad == 0


def test_failed_step_reports_the_step_that_failed(conn, pg_dsn, fresh_schema):
    bad = Plan(steps=[Step(1, 'ALTER TABLE "nope"."nope" ADD COLUMN x int', "ddl",
                           Safety.SAFE_METADATA, True, "boom")])
    with pytest.raises(executor.StepFailed) as e:
        executor.run(pg_dsn, fresh_schema, bad)
    assert e.value.step.seq == 1


def test_cleanup_removes_invalid_indexes(conn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".t (id int)')
    conn.execute(f'INSERT INTO "{fresh_schema}".t SELECT generate_series(1,10)')
    # Force an INVALID index the way a cancelled CIC leaves one behind.
    conn.execute(f'CREATE UNIQUE INDEX ix_bad ON "{fresh_schema}".t (id)')
    conn.execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
                 f"'\"{fresh_schema}\".ix_bad'::regclass")
    assert "ix_bad" in executor.cleanup_invalid_indexes(conn, fresh_schema)


def test_progress_callback_reports_each_step(conn, pg_dsn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY)')
    seen = []
    p = plan([AddColumn("events", Column("note", "text", True, None, 2))],
             table_stats(conn, fresh_schema), fresh_schema)
    executor.run(pg_dsn, fresh_schema, p,
                 on_progress=lambda s, status, info: seen.append((s.seq, status)))
    assert ("running" in [s for _, s in seen]) and ("done" in [s for _, s in seen])


# --- R14: search_path must be set before replaying a stored (unqualified) --
# --- index definition, for non-transactional (CIC) steps -------------------

def test_index_from_stored_unqualified_definition_lands_in_target_schema_not_default(
    conn, pg_dsn, fresh_schema
):
    """`Index.definition` is captured *unqualified* by `introspect.snapshot`
    (R14). If the executor fails to set `search_path` before replaying it for
    `CREATE INDEX CONCURRENTLY`, the unqualified `ON events ...` clause
    resolves through the connection's default search_path -- which, in this
    test, points at a decoy `public.events` table planted for exactly this
    reason. A broken executor would either silently index the wrong table or
    error out finding it; a correct one indexes only `fresh_schema.events`.
    """
    default_search_path = conn.execute("SHOW search_path").fetchone()[0]
    assert "public" in default_search_path, (
        "this test assumes the session's default search_path includes "
        "'public' -- adjust the decoy schema below if that ever changes"
    )

    conn.execute("DROP TABLE IF EXISTS public.events")
    conn.execute("CREATE TABLE public.events (id int PRIMARY KEY, ts text)")
    try:
        conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY, ts text)')
        conn.execute(f'CREATE INDEX ix_ts ON "{fresh_schema}".events (ts)')

        # The real R14 mechanism: introspect so the captured definition is
        # unqualified, exactly as a real diff/merge flow would hand it to
        # the planner.
        real_index = snapshot(conn, fresh_schema).tables["events"].indexes["ix_ts"]
        assert fresh_schema not in real_index.definition
        assert "public" not in real_index.definition

        conn.execute(f'DROP INDEX "{fresh_schema}".ix_ts')

        p = plan([CreateIndex("events", real_index)],
                  {"events": TableStats(rows=5_000_000, bytes=900_000_000)},
                  fresh_schema)
        executor.run(pg_dsn, fresh_schema, p)

        in_target = conn.execute(
            "SELECT 1 FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class c ON c.oid = x.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND i.relname = 'ix_ts' AND x.indisvalid",
            (fresh_schema,)).fetchone()
        assert in_target == (1,), "index was not created (validly) in the target schema"

        in_public = conn.execute(
            "SELECT 1 FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class c ON c.oid = x.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND i.relname = 'ix_ts'").fetchone()
        assert in_public is None, "index leaked into 'public' -- search_path was not set/restored"
    finally:
        conn.execute("DROP TABLE IF EXISTS public.events")


# --- checkpointed resumption -------------------------------------------------

def _make_merge_row(conn) -> str:
    store.init(conn)
    row = conn.execute(
        "INSERT INTO _tributary.merges (source_branch, target_branch, status) "
        "VALUES ('feature', 'main', 'pending') RETURNING id"
    ).fetchone()
    return row[0]


class _Killed(Exception):
    """Stands in for a hard process kill mid-run."""


def test_backfill_resumes_from_checkpoint_after_a_killed_run(conn, pg_dsn, fresh_schema):
    # `_tributary` is shared, session-wide state (other test modules' `ws`/
    # `client` fixtures create and tear it down too) -- this is the only test
    # in this module that touches it, so it cleans up after itself the same
    # way those fixtures do, rather than leaving `_tributary` (and this
    # test's merge/migration_steps rows) behind for the rest of the suite.
    try:
        merge_id = _make_merge_row(conn)

        conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY, n int)')
        conn.execute(f'INSERT INTO "{fresh_schema}".events '
                     f'SELECT i, i*2 FROM generate_series(1,5000) i')
        p = plan([AlterColumnType("events", "n", "int4", "int8")],
                 {"events": TableStats(rows=5_000_000, bytes=900_000_000)},
                 fresh_schema, batch_size=200, pk_columns={"events": "id"})
        assert any(s.kind == "backfill" for s in p.steps)

        seen_rows_done = []

        def flaky_progress(step, status, info):
            rows_done = info.get("rows_done")
            if status == "running" and rows_done:
                seen_rows_done.append(rows_done)
                if rows_done >= 1000:
                    raise _Killed()

        with pytest.raises(_Killed):
            executor.run(pg_dsn, fresh_schema, p, merge_id=merge_id,
                         on_progress=flaky_progress, batch_size=200)

        assert seen_rows_done, "no batch progress was ever reported"
        assert max(seen_rows_done) < 5000, "the fixture killed before completion; test setup is wrong"

        backfill_seq = next(s.seq for s in p.steps if s.kind == "backfill")
        checkpoint = conn.execute(
            "SELECT status, rows_done, cursor_val FROM _tributary.migration_steps "
            "WHERE merge_id = %s AND seq = %s", (merge_id, backfill_seq)
        ).fetchone()
        assert checkpoint is not None, "no checkpoint row was persisted for the killed backfill step"
        ckpt_status, ckpt_rows_done, ckpt_cursor = checkpoint
        assert ckpt_status == "running"
        assert ckpt_rows_done >= 1000
        assert ckpt_cursor is not None

        # A step that already completed before the kill must be marked done, so
        # the resumed run does not try (and fail) to redo it -- e.g. re-adding
        # the shadow column a second time.
        earlier_steps_done = conn.execute(
            "SELECT count(*) FROM _tributary.migration_steps WHERE merge_id = %s "
            "AND seq < %s AND status = 'done'", (merge_id, backfill_seq)
        ).fetchone()[0]
        assert earlier_steps_done > 0

        resumed_rows_done = []

        def resumed_progress(step, status, info):
            if status == "running" and info.get("rows_done"):
                resumed_rows_done.append(info["rows_done"])

        executor.run(pg_dsn, fresh_schema, p, merge_id=merge_id,
                     on_progress=resumed_progress, batch_size=200)

        # The genuine resumption assertion: the resumed run's first reported
        # progress continues from the checkpoint, it does not restart at ~200.
        assert resumed_rows_done[0] > ckpt_rows_done, (
            "resumed run started over from near zero instead of continuing from "
            f"the persisted checkpoint ({ckpt_rows_done} rows)"
        )

        col = snapshot(conn, fresh_schema).tables["events"].columns["n"]
        assert col.type == "int8"
        bad = conn.execute(f'SELECT count(*) FROM "{fresh_schema}".events '
                           f'WHERE n <> id*2').fetchone()[0]
        assert bad == 0

        final_status = conn.execute(
            "SELECT status FROM _tributary.migration_steps WHERE merge_id = %s AND seq = %s",
            (merge_id, backfill_seq)
        ).fetchone()[0]
        assert final_status == "done"
    finally:
        conn.execute("DROP SCHEMA IF EXISTS _tributary CASCADE")


# --- lock_timeout + retry -----------------------------------------------------

def test_migration_retries_then_fails_when_lock_cannot_be_acquired(conn, pg_dsn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY)')

    # Built *before* the holder thread starts: `table_stats` calls
    # `pg_total_relation_size`, which itself needs an AccessShareLock on the
    # table -- built after the holder grabs ACCESS EXCLUSIVE, it would
    # deadlock right here (this thread blocked in `table_stats`, the holder
    # blocked in `release.wait`, since nothing ever reaches the `release.set()`
    # below), silently resolving only once the holder's own wait timed out --
    # which made an earlier version of this test pass for the wrong reason.
    p = plan([AddColumn("events", Column("note", "text", True, None, 2))],
             table_stats(conn, fresh_schema), fresh_schema, lock_timeout="500ms")

    holder_ready = threading.Event()
    release = threading.Event()

    def holder():
        with psycopg.connect(pg_dsn) as c:
            with c.transaction():
                c.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE")
                          .format(sql.Identifier(fresh_schema, "events")))
                holder_ready.set()
                release.wait(20)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert holder_ready.wait(5)

    started = time.monotonic()
    with pytest.raises(executor.StepFailed) as e:
        executor.run(pg_dsn, fresh_schema, p)
    elapsed = time.monotonic() - started

    release.set()
    t.join(5)

    assert fresh_schema in str(e.value) or "events" in str(e.value)
    # 4 attempts (1 + 3 retries) at ~0.5s lock_timeout each, plus 1+2+4s backoff
    # between them -- bounded, but only after genuinely retrying, not instantly.
    assert 3 < elapsed < 20, f"elapsed={elapsed:.1f}s -- either failed instantly or hung"


# --- preflight aborts before any lock is taken -------------------------------

def test_preflight_aborts_set_not_null_when_nulls_exist_leaving_no_trace(
    conn, pg_dsn, fresh_schema
):
    conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY, note text)')
    conn.execute(f'INSERT INTO "{fresh_schema}".events VALUES (1, \'x\'), (2, NULL)')

    p = plan([SetNotNull("events", "note")], table_stats(conn, fresh_schema), fresh_schema)
    preflight_step = next(s for s in p.steps if s.kind == "preflight")

    with pytest.raises(executor.StepFailed) as e:
        executor.run(pg_dsn, fresh_schema, p)
    assert e.value.step.seq == preflight_step.seq

    col = snapshot(conn, fresh_schema).tables["events"].columns["note"]
    assert col.nullable is True, "SET NOT NULL must never have been attempted"
    cons = snapshot(conn, fresh_schema).tables["events"].constraints
    assert not any("notnull" in name for name in cons), (
        "the NOT NULL scaffold constraint must never have been added"
    )
