"""Demo workspace: seed data and the growable `events` table.

Tributary's central claim is that it classifies each DDL operation against
the *measured* size of the table it touches and rewrites unsafe operations
into safe multi-step forms. An empty first screen proves nothing -- an
evaluator needs a schema realistic enough to exercise every feature this
project handles (a `bigserial` primary key, a foreign key, a unique
constraint, a standalone index, a `NOT NULL` column with a default, a
`timestamptz`), and an `events` table they can grow on demand to watch the
planner's safety classification change from "instant, metadata only" to
"this will rewrite a multi-GB table" in front of them.

`ensure_demo` builds that schema -- idempotently, since the app calls it on
every startup -- and registers it with `store.ensure_main` so it behaves
like any other Tributary-managed database from the first request onward.

`grow_events` is the growth lever. It inserts in `_BATCH_SIZE`-row batches
via `INSERT ... SELECT ... FROM generate_series(...)` rather than one giant
statement, so a 50M-row growth reports progress instead of holding one
multi-minute transaction with no feedback, and it finishes with `ANALYZE`.
That last step is not cosmetic: `table_stats` (introspect.py) reports
`rows=None` -- "never analysed", the deliberate fail-safe for "unknown,
assume large" -- until a table has been analysed at least once. Skipping it
would leave the demo's `events` table permanently unmeasured, so the
planner's next classification of it would reflect ignorance of the new size
rather than an actual measurement.
"""

from __future__ import annotations

from psycopg import sql

from tributary import store
from tributary.introspect import snapshot

_SCHEMA = "main"

# Rows per `INSERT ... SELECT ... FROM generate_series` batch in
# `grow_events`. Module-level (read fresh on every loop iteration, not
# captured at import time) so tests can shrink it via monkeypatch to
# exercise the batching/progress-reporting logic without inserting anywhere
# near this many rows.
_BATCH_SIZE = 500_000

_DEMO_TABLES = ("users", "orders", "events")


def _ident(name: str) -> str:
    return sql.Identifier(name).as_string(None)


def _qualified(name: str) -> str:
    return sql.Identifier(_SCHEMA, name).as_string(None)


def demo_present(conn) -> bool:
    """Whether the demo's tables already exist in the `main` schema.

    Safe to call before `main` (or `_tributary`) exists at all: `snapshot`
    filters strictly by schema name and returns an empty result for a
    schema that isn't there yet rather than raising, so this never needs a
    prior `store.init`/`ensure_demo` call to be answered correctly.
    """
    tables = snapshot(conn, _SCHEMA).tables
    return set(_DEMO_TABLES) <= set(tables)


def _create_schema_objects(conn) -> None:
    """Create `main`'s demo tables, constraints, and indexes.

    Every statement is `IF NOT EXISTS` (or, for the inline constraints on
    `CREATE TABLE`, protected by the same `IF NOT EXISTS` on the table
    itself), so a second call is a complete no-op against an
    already-created schema -- required for `ensure_demo`'s idempotency.
    """
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(_SCHEMA)))

    # users: bigserial PK, NOT NULL + UNIQUE column, NOT NULL column with a
    # default, a timestamptz.
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_qualified("users")} (
            {_ident("id")} bigserial PRIMARY KEY,
            {_ident("email")} text NOT NULL,
            {_ident("full_name")} text,
            {_ident("created_at")} timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT {_ident("users_email_key")} UNIQUE ({_ident("email")})
        )
        """
    )

    # orders: FK to users, NOT NULL column with a default, a standalone index.
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_qualified("orders")} (
            {_ident("id")} bigserial PRIMARY KEY,
            {_ident("user_id")} bigint NOT NULL REFERENCES {_qualified("users")} ({_ident("id")}),
            {_ident("status")} text NOT NULL DEFAULT 'pending',
            {_ident("total_cents")} integer NOT NULL DEFAULT 0,
            {_ident("created_at")} timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {_ident('orders_user_id_idx')} "
        f"ON {_qualified('orders')} ({_ident('user_id')})"
    )

    # events: the table the demo's "grow" buttons target. No FK to keep
    # `grow_events`'s batched inserts free of per-row FK lookups at scale --
    # a deliberate, realistic modelling choice for a high-volume event log.
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_qualified("events")} (
            {_ident("id")} bigserial PRIMARY KEY,
            {_ident("user_id")} bigint,
            {_ident("event_type")} text NOT NULL DEFAULT 'view',
            {_ident("payload")} jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            {_ident("created_at")} timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {_ident('events_created_at_idx')} "
        f"ON {_qualified('events')} ({_ident('created_at')})"
    )


def _table_row_count(conn, table: str) -> int:
    (n,) = conn.execute(f"SELECT count(*) FROM {_qualified(table)}").fetchone()
    return n


def _seed_rows(conn) -> None:
    """Seed ~10k rows total, but only into tables found empty.

    Checking each table's count first (rather than, say, an `ON CONFLICT
    DO NOTHING` upsert) is what makes a second `ensure_demo` call a genuine
    no-op instead of a same-size-but-different-rows re-seed.
    """
    if _table_row_count(conn, "users") == 0:
        conn.execute(
            f"""
            INSERT INTO {_qualified("users")} ({_ident("email")}, {_ident("full_name")})
            SELECT 'user' || s || '@example.com', 'Demo User ' || s
            FROM generate_series(1, 500) AS s
            """
        )

    if _table_row_count(conn, "orders") == 0:
        conn.execute(
            f"""
            INSERT INTO {_qualified("orders")}
                ({_ident("user_id")}, {_ident("status")}, {_ident("total_cents")})
            SELECT (s % 500) + 1,
                   (ARRAY['pending', 'paid', 'shipped', 'cancelled'])[1 + (s % 4)],
                   (s * 137) % 100000
            FROM generate_series(1, 2000) AS s
            """
        )

    if _table_row_count(conn, "events") == 0:
        conn.execute(
            f"""
            INSERT INTO {_qualified("events")}
                ({_ident("user_id")}, {_ident("event_type")}, {_ident("created_at")})
            SELECT (s % 500) + 1,
                   (ARRAY['view', 'click', 'purchase', 'signup'])[1 + (s % 4)],
                   now() - (s::text || ' seconds')::interval
            FROM generate_series(1, 7500) AS s
            """
        )


def ensure_demo(conn) -> None:
    """Build (or confirm) the demo workspace. Idempotent.

    The app calls this on every startup (`TRIBUTARY_AUTOSEED=1`), so a
    second call must never duplicate rows or re-register `main`:
    `store.init` and `store.ensure_main` are already idempotent by
    contract, every DDL statement issued here is `IF NOT EXISTS`, and row
    seeding only inserts into a table found empty.

    Order matters: the schema and its ~10k rows are created *before*
    `store.ensure_main` runs, so `main`'s initial commit snapshot captures
    the fully-seeded structure rather than an empty schema.

    Analyses all three tables at the end so the demo's very first
    `table_stats` read reflects a real measurement rather than the "never
    analysed" fail-safe `None` -- the same reasoning `grow_events` follows.
    """
    store.init(conn)
    _create_schema_objects(conn)
    _seed_rows(conn)

    for table in _DEMO_TABLES:
        conn.execute(f"ANALYZE {_qualified(table)}")

    store.ensure_main(conn)


def grow_events(conn, target_rows: int, on_progress=None) -> None:
    """Grow `main.events` to (at least) `target_rows` total rows.

    Expressed as a target, not a delta, because the callers are UI buttons
    ("grow to 10M / 50M") that name a destination size; a no-op, returning
    without error, when the table already meets or exceeds `target_rows`.

    Inserts in `_BATCH_SIZE`-row batches via `INSERT ... SELECT ... FROM
    generate_series(...)` instead of one enormous statement, so a 50M-row
    growth gives feedback throughout instead of holding one giant
    transaction for minutes with no signal that anything is happening.
    `on_progress(rows_done, target_rows)` is called after each batch when
    given.

    Ends with `ANALYZE` on `events` -- required, not optional; see the
    module docstring for why an unanalysed table would make the demo show
    the fail-safe path instead of the real classification.
    """
    done = _table_row_count(conn, "events")
    if done >= target_rows:
        return

    n_users = _table_row_count(conn, "users") or 1

    while done < target_rows:
        batch = min(_BATCH_SIZE, target_rows - done)
        conn.execute(
            f"""
            INSERT INTO {_qualified("events")}
                ({_ident("user_id")}, {_ident("event_type")}, {_ident("created_at")})
            SELECT ((s + %(offset)s) %% %(n_users)s) + 1,
                   (ARRAY['view', 'click', 'purchase', 'signup'])[1 + ((s + %(offset)s) %% 4)],
                   now() - ((s + %(offset)s)::text || ' seconds')::interval
            FROM generate_series(1, %(batch)s) AS s
            """,
            {"offset": done, "n_users": n_users, "batch": batch},
        )
        done += batch
        if on_progress is not None:
            on_progress(done, target_rows)

    conn.execute(f"ANALYZE {_qualified('events')}")
