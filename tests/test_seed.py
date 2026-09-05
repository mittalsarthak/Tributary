import pytest
from psycopg import sql

from tributary import seed, store
from tributary.introspect import snapshot, table_stats


def _q(table: str) -> str:
    """Quote a `main.<table>` reference the same way `seed.py` does --
    through `sql.Identifier`, never a raw f-string -- even where `table`
    is a hardcoded loop variable rather than external input.
    """
    return sql.Identifier("main", table).as_string(None)


@pytest.fixture(autouse=True)
def _clean_main(conn):
    """`ensure_demo`/`grow_events` always target the literal `main` schema
    (never `fresh_schema`), so guarantee every test in this module starts
    from -- and leaves behind -- a clean slate. Without this, leftover
    `main`/`_tributary` schemas from one test would silently seed the next
    test's "before" state, and (since `conn` is session-shared across the
    whole test run) could leak into other test modules' own use of `main`.
    """
    conn.execute("DROP SCHEMA IF EXISTS main CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS _tributary CASCADE")
    yield
    conn.execute("DROP SCHEMA IF EXISTS main CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS _tributary CASCADE")


# --- verbatim brief test cases -----------------------------------------------

def test_ensure_demo_creates_a_realistic_schema(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    from tributary.introspect import snapshot
    tables = snapshot(conn, "main").tables
    assert {"users", "orders", "events"} <= set(tables)
    assert "user_id" in tables["orders"].columns


def test_ensure_demo_is_idempotent(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    seed.ensure_demo(conn)
    n = conn.execute("SELECT count(*) FROM main.users").fetchone()[0]
    assert n > 0


def test_grow_events_adds_rows(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    before = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    seed.grow_events(conn, before + 5000)
    after = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    assert after >= before + 5000


# --- strengthened idempotency: exact row counts, not just ">0" --------------
# R-style regression guard: this project has shipped two defects that were
# "verified by hand" with no test (ensure_main idempotency, delete_branch).
# Calling ensure_demo twice and only checking `n > 0` would pass even if the
# second call duplicated every row. Assert the counts are byte-for-byte
# unchanged across the second call, for every seeded table.

def test_ensure_demo_second_call_does_not_duplicate_rows(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    counts_before = {
        t: conn.execute(f"SELECT count(*) FROM {_q(t)}").fetchone()[0]
        for t in ("users", "orders", "events")
    }

    seed.ensure_demo(conn)

    counts_after = {
        t: conn.execute(f"SELECT count(*) FROM {_q(t)}").fetchone()[0]
        for t in ("users", "orders", "events")
    }
    assert counts_after == counts_before


def test_ensure_demo_second_call_does_not_re_register_main(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    first_head = store.head(conn, "main").id

    seed.ensure_demo(conn)

    branches = [b for b in store.list_branches(conn) if b.name == "main"]
    assert len(branches) == 1
    assert store.head(conn, "main").id == first_head


# --- schema realism: the features the rest of the system exercises ---------

def test_ensure_demo_schema_has_bigserial_primary_keys(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    tables = snapshot(conn, "main").tables
    for name in ("users", "orders", "events"):
        pk_cols = [c for c in tables[name].columns.values() if c.name == "id"]
        assert pk_cols, f"{name} has no id column"
        assert pk_cols[0].default is not None and "nextval" in pk_cols[0].default


def test_ensure_demo_schema_has_a_foreign_key_from_orders_to_users(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    tables = snapshot(conn, "main").tables
    fks = [c for c in tables["orders"].constraints.values() if c.kind == "f"]
    assert len(fks) == 1
    assert "users" in fks[0].definition


def test_ensure_demo_schema_has_a_unique_constraint(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    tables = snapshot(conn, "main").tables
    uniques = [c for c in tables["users"].constraints.values() if c.kind == "u"]
    assert len(uniques) == 1
    assert uniques[0].columns == ("email",)


def test_ensure_demo_schema_has_a_standalone_index(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    tables = snapshot(conn, "main").tables
    assert len(tables["orders"].indexes) >= 1
    assert len(tables["events"].indexes) >= 1


def test_ensure_demo_schema_has_a_not_null_column_with_a_default(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    tables = snapshot(conn, "main").tables
    status = tables["orders"].columns["status"]
    assert status.nullable is False
    assert status.default is not None


def test_ensure_demo_schema_has_a_timestamptz_column(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    tables = snapshot(conn, "main").tables
    assert tables["events"].columns["created_at"].type == "timestamptz"


def test_ensure_demo_seeds_roughly_10k_rows(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    total = sum(
        conn.execute(f"SELECT count(*) FROM {_q(t)}").fetchone()[0]
        for t in ("users", "orders", "events")
    )
    assert 5000 <= total <= 20000


def test_ensure_demo_analyzes_the_seeded_tables(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    stats = table_stats(conn, "main")
    for t in ("users", "orders", "events"):
        assert stats[t].rows is not None


# --- demo_present -------------------------------------------------------

def test_demo_present_is_false_before_ensure_demo(conn):
    assert seed.demo_present(conn) is False


def test_demo_present_is_true_after_ensure_demo(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    assert seed.demo_present(conn) is True


# --- grow_events: target semantics, batching, progress, analyze --------

def test_grow_events_is_a_noop_when_already_at_target(conn, monkeypatch):
    store.init(conn)
    seed.ensure_demo(conn)
    before = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]

    calls = []
    monkeypatch.setattr(seed, "_BATCH_SIZE", 10)
    seed.grow_events(conn, before, on_progress=lambda done, target: calls.append(done))

    after = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    assert after == before
    assert calls == []


def test_grow_events_is_a_noop_when_target_is_below_current_count(conn, monkeypatch):
    """The below-target case, not just the exact-equal boundary.

    `target == before` passes even with a `!=` guard or a sign error; only a
    target strictly below the current count catches arithmetic that would
    compute a negative batch and either loop forever or delete rows.
    """
    store.init(conn)
    seed.ensure_demo(conn)
    before = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    assert before > 10, "demo seed should leave enough rows to undershoot"

    calls = []
    monkeypatch.setattr(seed, "_BATCH_SIZE", 10)
    seed.grow_events(conn, before - 10, on_progress=lambda done, target: calls.append(done))

    after = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    assert after == before, "growing to a smaller target must never remove rows"
    assert calls == []


def test_grow_events_batches_and_reports_progress(conn, monkeypatch):
    store.init(conn)
    seed.ensure_demo(conn)
    monkeypatch.setattr(seed, "_BATCH_SIZE", 200)
    before = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    target = before + 450  # forces 3 batches of a 200-row _BATCH_SIZE

    progress = []
    seed.grow_events(conn, target, on_progress=lambda done, tgt: progress.append((done, tgt)))

    after = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    assert after == target
    assert len(progress) == 3
    assert progress[-1] == (target, target)
    # monotonically increasing rows_done, each batch capped at _BATCH_SIZE
    prev = before
    for done, tgt in progress:
        assert tgt == target
        assert done > prev
        assert done - prev <= 200
        prev = done


def test_grow_events_works_without_on_progress(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    before = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    seed.grow_events(conn, before + 100)
    after = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    assert after >= before + 100


def test_grow_events_analyzes_afterwards(conn):
    store.init(conn)
    seed.ensure_demo(conn)
    before = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    target = before + 3000

    seed.grow_events(conn, target)

    events_rows = table_stats(conn, "main")["events"].rows
    assert events_rows is not None
    # A fresh ANALYZE's estimate must reflect the *grown* table, not the
    # stale, smaller estimate ensure_demo's own ANALYZE already produced --
    # otherwise this would pass even if grow_events never analysed at all.
    assert events_rows >= before + 2000
