"""Lock-safety tests -- the evidence for Tributary's central claim: a schema
migration on a large table will not take the database down.

Everything else in this project is scaffolding around the property proven
here. A reviewer who reads nothing else should be able to read this file
and see the property demonstrated against a real, live Postgres --
`pg_dsn`/`conn`/`fresh_schema` from `tests/conftest.py`. Lock behaviour is
never mocked; it cannot be meaningfully mocked.

RULING R4 (this task's dispatch overrides the brief here): the brief's
first test opened one read transaction, held it open for the whole test,
and asserted the migration finished in under 10 seconds anyway. That cannot
happen and should not: a reader holding ACCESS SHARE for the life of its
transaction conflicts with ADD COLUMN's ACCESS EXCLUSIVE for as long as
that transaction stays open, so the *correct* executor exhausts its
retries and fails -- the tempting way to make that test pass is deleting
`lock_timeout`, which would destroy the exact property this project exists
to demonstrate. A long-lived idle-in-transaction reader is a production bug
in the caller, not a case Tributary should be designed around.

The three tests below state the honest property instead:

- `test_migration_does_not_starve_short_lived_readers` models realistic
  OLTP traffic -- a reader issuing short transactions in a loop, never
  holding one open -- and asserts *both* halves: the migration completes
  promptly, and no individual read is starved (tracked as per-query
  latency, not merely "the reader thread survived").
- `test_migration_gives_up_rather_than_holding_the_lock_queue` holds an
  ACCESS EXCLUSIVE lock hostage and asserts the migration fails *bounded*
  -- lock_timeout retries with backoff -- rather than hanging forever.
- `test_concurrent_index_build_leaves_writes_available` hammers inserts
  from a background thread while `CREATE INDEX CONCURRENTLY` runs, and
  asserts writes genuinely continued (a count comparison), not merely that
  the writer thread didn't crash.

Together: a migration either gets its lock quickly or gives up quickly; it
never stalls the queue behind it.
"""

import threading
import time

import psycopg
import pytest
from psycopg import sql

from tributary import executor
from tributary.model import AddColumn, Column, CreateIndex, Index, TableStats
from tributary.planner import plan

# A table classified as *large* by the planner (comfortably over
# `_LARGE_BYTES`, tributary/planner.py's 100MB threshold) without paying to
# actually build one that size -- R12 and the planner module both make
# `TableStats` a first-class input to `plan()` for exactly this reason:
# forcing the large-table classification while keeping real row counts (and
# so, test runtime) modest.
BIG = {"events": TableStats(rows=5_000_000, bytes=900_000_000)}


@pytest.fixture
def events(conn, fresh_schema):
    events_ident = sql.Identifier(fresh_schema, "events")
    conn.execute(sql.SQL(
        "CREATE TABLE {} (id bigint PRIMARY KEY, ts timestamptz, payload text)"
    ).format(events_ident))
    conn.execute(sql.SQL(
        "INSERT INTO {} SELECT i, now(), repeat('x', 50) FROM generate_series(1, 50000) i"
    ).format(events_ident))
    return fresh_schema


def test_migration_does_not_starve_short_lived_readers(pg_dsn, events):
    """The failure this catches: a migration that queues behind normal OLTP
    traffic and then blocks every read that arrives behind *it* -- even
    though no single reader ever holds a transaction open.

    The reader loop runs one short (begin, read, commit) transaction after
    another, back to back, with no sleep between them -- deliberately
    maximising contention. Postgres's ACCESS EXCLUSIVE request for the
    migration's ADD COLUMN only ever has to wait for whichever single read
    is *currently* in flight (a few milliseconds), and once its request is
    queued it holds priority over reads that arrive after it (Postgres's
    lock manager is a fair FIFO queue) -- so it is neither starved by this
    traffic, nor a starver of it. Both halves of the property are asserted:
    total migration time, and the worst single read latency observed
    anywhere during the run.
    """
    stop = threading.Event()
    ready = threading.Event()
    errors: list[Exception] = []
    latencies: list[float] = []

    query = sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(events, "events"))

    def reader():
        try:
            with psycopg.connect(pg_dsn) as c:
                while not stop.is_set():
                    started = time.monotonic()
                    with c.transaction():
                        c.execute(query)
                    latencies.append(time.monotonic() - started)
                    ready.set()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    assert ready.wait(5), "reader never completed a single query"

    p = plan([AddColumn("events", Column("note", "text", True, None, 4))], BIG, events)
    started = time.monotonic()
    executor.run(pg_dsn, events, p)
    elapsed = time.monotonic() - started

    stop.set()
    t.join(5)

    assert not errors, f"reader was disrupted: {errors}"
    assert elapsed < 10, f"migration took {elapsed:.2f}s -- it queued behind read traffic"
    assert latencies, "reader recorded no completed queries at all -- it never ran"

    worst = max(latencies)
    # "Close to the migration's duration": a starved read shows up as a
    # latency on the same order as `elapsed` (or worse, the full
    # lock_timeout). The floor keeps this from being flaky on a fast box --
    # a metadata-only ALTER can finish in tens of milliseconds, and demanding
    # every read be proportionally faster than that would be measuring
    # scheduler noise, not lock contention.
    budget = max(1.0, elapsed * 0.5)
    assert worst < budget, (
        f"a read blocked for {worst:.2f}s while the migration took {elapsed:.2f}s total "
        f"(budget was {budget:.2f}s) -- a read queued behind the migration"
    )


def test_migration_gives_up_rather_than_holding_the_lock_queue(pg_dsn, events):
    """A migration that cannot get its lock must fail fast, not stall the
    database while every other query piles up behind it."""
    holder_ready = threading.Event()
    release = threading.Event()

    def holder():
        with psycopg.connect(pg_dsn) as c:
            with c.transaction():
                c.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE")
                          .format(sql.Identifier(events, "events")))
                holder_ready.set()
                release.wait(20)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert holder_ready.wait(5)

    p = plan([AddColumn("events", Column("note2", "text", True, None, 5))], BIG, events)
    started = time.monotonic()
    with pytest.raises(executor.StepFailed):
        executor.run(pg_dsn, events, p)
    elapsed = time.monotonic() - started
    release.set()
    t.join(5)
    # 3 retries at 3s lock_timeout plus 1+2+4s backoff -- bounded, and
    # nowhere near the 20s the holder is willing to wait. If this ever hits
    # 20+s instead, `lock_timeout` stopped being enforced -- fix the
    # executor, not this number.
    assert elapsed < 25


def test_concurrent_index_build_leaves_writes_available(pg_dsn, events):
    """Hammer inserts in a background thread while `CREATE INDEX
    CONCURRENTLY` runs; assert no write errored *and* that writes genuinely
    continued during the build -- a count comparison, not merely that the
    writer thread survived.
    """
    writes = {"n": 0}
    stop = threading.Event()
    errors: list[Exception] = []

    insert = sql.SQL("INSERT INTO {} VALUES (%s, now(), %s)").format(
        sql.Identifier(events, "events")
    )

    def writer():
        try:
            with psycopg.connect(pg_dsn, autocommit=True) as c:
                i = 10_000_000
                while not stop.is_set():
                    c.execute(insert, (i, "y"))
                    writes["n"] += 1
                    i += 1
                    time.sleep(0.01)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    time.sleep(0.3)
    before = writes["n"]

    index_ddl = sql.SQL("CREATE INDEX ix_ts ON {} USING btree (ts)").format(
        sql.Identifier(events, "events")
    ).as_string(None)
    idx = Index("ix_ts", index_ddl, ("ts",))
    executor.run(pg_dsn, events, plan([CreateIndex("events", idx)], BIG, events))

    stop.set()
    t.join(5)
    assert not errors, f"writes failed during index build: {errors}"
    assert writes["n"] > before, "writes stalled while the index was building"
