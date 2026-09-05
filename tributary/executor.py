"""Run a `Plan` (Task 8's output) against a real, live Postgres database.

Task 8's planner rewrote unsafe DDL into safe multi-step forms; this module
is where that care either survives contact with a real database or is
wasted. Three properties matter more than anything else here:

1. **A migration either gets its lock quickly or gives up quickly.** Every
   statement that can take a heavy lock has `lock_timeout` enforced on the
   connection before it runs (both because the planner already embeds
   `SET lock_timeout` in most step SQL, and, defensively, because this
   module also sets it explicitly -- see `_infer_lock_timeout`). On
   `psycopg.errors.LockNotAvailable` it retries up to `retries` times with
   1s/2s/4s backoff, then fails with a sentence naming the table -- it never
   silently queues behind whatever else is running, and never stalls the
   queue of statements behind *it*.

2. **A killed run resumes, it does not restart.** When `merge_id` is given,
   every step's status transition is persisted to
   `_tributary.migration_steps`; a step already marked `done` is skipped on
   the next `run()`, and a backfill step's `rows_done`/`cursor_val` are
   checkpointed after every batch, so re-running the same plan against the
   same `merge_id` continues from where it left off rather than reprocessing
   (or, worse, redoing DDL that already succeeded) from zero.

3. **RULING R14 -- `search_path` before replaying a stored definition.**
   `Index.definition` (and `Constraint.definition`) are captured *unqualified*
   by `introspect.snapshot` (that module's docstring). A `CreateIndex` step
   built from a stored `Index` replays that unqualified text verbatim
   (`planner._emit_create_index`), so if the executing connection's
   `search_path` is not pointed at the target schema first, an unqualified
   `CREATE INDEX ix ON events ...` resolves through whatever the connection's
   *default* search_path is -- potentially `main`'s live table, not the
   branch being migrated. Every step here sets `search_path` to the target
   schema before executing, defensively, whether or not that particular
   step's SQL is known to contain a stored definition.

   The subtlety: `SET LOCAL search_path` (used for transactional steps,
   inside their own transaction, and auto-reverted by that transaction
   ending) has no effect outside an explicit transaction, and
   `CREATE INDEX CONCURRENTLY` cannot run *inside* one. Non-transactional
   steps therefore get a plain session-level `SET search_path`, captured
   (`SHOW search_path`) and restored in a `finally` -- including when the
   statement fails -- so a failed concurrent index build never leaves the
   shared non-transactional connection pointed at the wrong schema for
   whatever step runs next.

A note on `run_step`'s signature: the brief (predating R14 and the
checkpointing requirements) specified `run_step(conn, step, *, lock_timeout,
retries=3)`. Implementing R14 and per-batch checkpointing correctly requires
`run_step` to know the target `schema`, and the batched backfill loop to
report per-batch progress somewhere `run()` can persist it -- neither fits
that literal signature. `run_step` here is `run_step(conn, step, *, schema,
lock_timeout, retries=3, on_batch=None, resume_from=None, batch_size=10_000)`.
This is safe to extend: nothing outside this module calls `run_step`
directly (Task 9's own brief and Task 10's brief both call only
`executor.run`, `executor.cleanup_invalid_indexes`, and `executor.StepFailed`
-- confirmed by reading both). `run()`'s own signature -- the one every other
task actually imports -- matches the required interface exactly, with two
additional optional keyword-only knobs (`batch_size`, `retries`) that default
to sensible values and change nothing for an existing caller.
"""

from __future__ import annotations

import re
import time
from typing import Callable

import psycopg
from psycopg import errors, sql

from tributary.model import Plan, Step

_DEFAULT_LOCK_TIMEOUT = "3s"
_DEFAULT_BATCH_SIZE = 10_000
_DEFAULT_RETRIES = 3


class StepFailed(Exception):
    """Raised when a `Step` cannot be applied. Carries the failing step and
    the underlying cause, so a caller (and a human) can see exactly which
    step broke and why -- a sentence, not a bare stack trace.
    """

    def __init__(self, step: Step, cause: BaseException):
        self.step = step
        self.cause = cause
        table_desc = f"table {step.table!r}" if step.table else "an unnamed target"
        super().__init__(
            f"step {step.seq} ({step.kind}) against {table_desc} failed: {cause}"
        )


# --- cleanup: INVALID indexes left behind by a failed/cancelled CIC --------

_INVALID_INDEXES_SQL = """
SELECT i.relname
FROM pg_index x
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND NOT x.indisvalid
"""


def cleanup_invalid_indexes(conn, schema: str) -> list[str]:
    """Find and drop every INVALID index in `schema`.

    A cancelled or failed `CREATE INDEX CONCURRENTLY` leaves an INVALID
    index in the catalog -- it consumes the index's name (a retry of the
    same `CREATE INDEX CONCURRENTLY ix_x ...` would fail with "relation
    already exists") and is never used by the planner or by Postgres's query
    planner. `run()` calls this before re-raising on any step failure, and
    the non-transactional retry loop calls it between attempts of a failed
    `index_concurrent` step so a retry can recreate the index cleanly.

    Plain `DROP INDEX` (not CONCURRENTLY): an invalid index was never
    finished being built, so dropping it is a catalog-only operation
    regardless of table size -- there is nothing to defer here the way
    building one has to be deferred.
    """
    rows = conn.execute(_INVALID_INDEXES_SQL, (schema,)).fetchall()
    names = [r[0] for r in rows]
    for name in names:
        stmt = sql.SQL("DROP INDEX {}").format(sql.Identifier(schema, name))
        if conn.autocommit:
            conn.execute(stmt)
        else:
            with conn.transaction():
                conn.execute(stmt)
    return names


# --- step SQL execution helpers ---------------------------------------------

def _is_count_check(sql_text: str) -> bool:
    """Is this step's SQL a bare `SELECT count(*) ...` probe rather than a
    DDL/DML statement? `preflight` steps always are; `validate` steps
    sometimes are (the shadow-column dance's post-backfill row-count check),
    and sometimes aren't (a real `ALTER TABLE ... VALIDATE CONSTRAINT`).
    """
    return sql_text.strip().upper().startswith("SELECT")


def _execute_step_sql(conn, step: Step) -> None:
    """Execute `step`'s SQL, honouring its shape.

    A count-check step (`preflight`, or a `validate` step whose SQL is a
    bare `SELECT`) is not just executed and forgotten -- its whole purpose
    is to fail the migration *before* any lock is taken (preflight) or
    *before* an unsafe swap proceeds (the shadow-dance's post-backfill
    check) when the count comes back nonzero. Some probes (e.g. a retype's
    "would this cast fail" check) instead raise directly from Postgres when
    the cast itself is invalid -- that is caught by the generic exception
    handling in the transactional/non-transactional retry wrappers, not
    here.
    """
    if _is_count_check(step.sql):
        row = conn.execute(step.sql).fetchone()
        count = row[0] if row else 0
        if count:
            raise RuntimeError(
                f"{step.kind} check failed on {step.table!r}: {count} existing row(s) "
                f"would violate this migration -- {step.note}"
            )
        return
    conn.execute(step.sql)


def _lock_timeout_literal(conn, autocommit: bool, lock_timeout: str) -> None:
    stmt = sql.SQL("SET {} lock_timeout = {}").format(
        sql.SQL("" if autocommit else "LOCAL"), sql.Literal(lock_timeout)
    )
    conn.execute(stmt)


def _search_path_literal(conn, autocommit: bool, schema: str) -> None:
    stmt = sql.SQL("SET {} search_path TO {}").format(
        sql.SQL("" if autocommit else "LOCAL"), sql.Identifier(schema)
    )
    conn.execute(stmt)


# --- transactional steps -----------------------------------------------------

def _run_transactional(conn, step: Step, *, schema: str, lock_timeout: str, retries: int) -> None:
    delays = [2 ** i for i in range(retries)]
    for attempt in range(retries + 1):
        try:
            with conn.transaction():
                _search_path_literal(conn, autocommit=False, schema=schema)
                _lock_timeout_literal(conn, autocommit=False, lock_timeout=lock_timeout)
                _execute_step_sql(conn, step)
            return
        except errors.LockNotAvailable as exc:
            if attempt < retries:
                time.sleep(delays[attempt])
                continue
            raise StepFailed(
                step,
                RuntimeError(
                    f"could not acquire a lock on {step.table!r} after "
                    f"{retries + 1} attempts (lock_timeout={lock_timeout})"
                ),
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: every
            # non-lock failure fails the step immediately, no retry.
            raise StepFailed(step, exc) from exc


# --- non-transactional steps (CREATE INDEX CONCURRENTLY, the standalone -----
# --- SET lock_timeout step that precedes it) --------------------------------

def _run_nontransactional(conn, step: Step, *, schema: str, lock_timeout: str, retries: int) -> None:
    """R14: `search_path` is set at the session level (no explicit
    transaction is open -- none can be, `CREATE INDEX CONCURRENTLY` cannot
    run inside one) and *always* restored in `finally`, on every exit path
    including a raised exception, so a failed concurrent index build never
    leaves this shared connection pointed at the wrong schema for the next
    step or the next call.
    """
    prev_search_path = conn.execute("SHOW search_path").fetchone()[0]
    _search_path_literal(conn, autocommit=True, schema=schema)
    try:
        _lock_timeout_literal(conn, autocommit=True, lock_timeout=lock_timeout)
        delays = [2 ** i for i in range(retries)]
        for attempt in range(retries + 1):
            try:
                _execute_step_sql(conn, step)
                return
            except errors.LockNotAvailable as exc:
                if step.kind == "index_concurrent":
                    cleanup_invalid_indexes(conn, schema)
                if attempt < retries:
                    time.sleep(delays[attempt])
                    continue
                raise StepFailed(
                    step,
                    RuntimeError(
                        f"could not acquire a lock on {step.table!r} after "
                        f"{retries + 1} attempts (lock_timeout={lock_timeout})"
                    ),
                ) from exc
            except Exception as exc:  # noqa: BLE001
                if step.kind == "index_concurrent":
                    cleanup_invalid_indexes(conn, schema)
                raise StepFailed(step, exc) from exc
    finally:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.SQL(prev_search_path)))


# --- backfill: batched, checkpointed --------------------------------------

_BACKFILL_TARGET_RE = re.compile(
    r'SELECT\s+("(?:[^"]|"")+")\s+AS\s+pk_val\s+FROM\s+((?:"(?:[^"]|"")+"\.)?"(?:[^"]|"")+")',
    re.IGNORECASE,
)
_LEADING_LOCK_TIMEOUT_RE = re.compile(r"^\s*SET\s+lock_timeout\s*=\s*'[^']*'\s*;\s*\n", re.IGNORECASE)


def _backfill_target(sql_text: str) -> tuple[str, str]:
    """Pull the (already-quoted) primary-key column and qualified table out
    of a backfill step's SQL. `planner._shadow_dance` always emits this
    exact shape (`SELECT {pk} AS pk_val FROM {qualified} ...`), so this is
    parsing our own trusted, planner-produced text, not arbitrary input.
    """
    m = _BACKFILL_TARGET_RE.search(sql_text)
    if not m:
        raise ValueError(
            "executor: could not find the primary-key column/table in a backfill "
            "step's SQL -- planner.py's backfill statement shape may have changed"
        )
    return m.group(1), m.group(2)


def _strip_leading_lock_timeout(sql_text: str) -> str:
    """The backfill statement's SQL text is `SET lock_timeout = '...';\\n<the
    parametrised WITH...UPDATE...RETURNING statement>`. psycopg cannot run a
    multi-statement string together with bind parameters (`cannot insert
    multiple commands into a prepared statement`), so the lock_timeout is set
    separately (`_lock_timeout_literal`, session-level, same connection) and
    only the parametrised remainder is executed per batch.
    """
    return _LEADING_LOCK_TIMEOUT_RE.sub("", sql_text, count=1)


def _run_backfill(
    conn,
    step: Step,
    *,
    schema: str,
    lock_timeout: str,
    retries: int,
    batch_size: int,
    on_batch: Callable[[int, str | None], None] | None,
    resume_from: tuple[int, str | None] | None,
) -> None:
    """Loop the batched backfill to completion, checkpointing after every
    batch via `on_batch` (rows_done, cursor_val).

    Cursor initialisation: with no checkpoint to resume from, the first
    cursor value must be a real value strictly less than every existing
    primary key (the fixed `WHERE pk > %(cursor)s` shape cannot be rewritten
    per-call to special-case "no cursor yet" -- `pk > NULL` is never true in
    SQL, so a bare `None` would silently skip every row). `(min(pk) - 1)`,
    computed by Postgres itself rather than guessed in Python, is exact for
    any primary key type that supports `-` against an integer literal --
    every real PK batched by this project (serial/bigserial/int/bigint) --
    and, on an empty table, `min(pk)` is `NULL`, so the first batch's
    `pk > NULL` naturally returns zero rows and the loop ends immediately,
    with no separate empty-table special case needed.
    """
    prev_search_path = conn.execute("SHOW search_path").fetchone()[0]
    _search_path_literal(conn, autocommit=True, schema=schema)
    try:
        _lock_timeout_literal(conn, autocommit=True, lock_timeout=lock_timeout)

        # Only failures from *our own* SQL (parsing the step, finding the
        # starting cursor, running a batch) become StepFailed. `on_batch`
        # below is the caller's own progress/checkpoint hook (`run()`'s
        # closure calls the caller's `on_progress`) -- an exception raised
        # from *there* is the caller's signal (e.g. a simulated kill, or a
        # real crash), not a step failure, and must propagate untouched
        # rather than being relabelled.
        try:
            pk_ident, table_ident = _backfill_target(step.sql)
            batch_sql = _strip_leading_lock_timeout(step.sql)

            if resume_from is not None and (resume_from[0] or resume_from[1] is not None):
                rows_done, cursor = resume_from
            else:
                row = conn.execute(
                    f"SELECT (min({pk_ident}) - 1)::text FROM {table_ident}"
                ).fetchone()
                cursor = row[0]
                rows_done = 0
        except Exception as exc:  # noqa: BLE001
            raise StepFailed(step, exc) from exc

        delays = [2 ** i for i in range(retries)]
        while True:
            rows = None
            for attempt in range(retries + 1):
                try:
                    rows = conn.execute(
                        batch_sql, {"cursor": cursor, "batch_size": batch_size}
                    ).fetchall()
                    break
                except errors.LockNotAvailable as exc:
                    if attempt < retries:
                        time.sleep(delays[attempt])
                        continue
                    raise StepFailed(
                        step,
                        RuntimeError(
                            f"could not acquire a lock on {step.table!r} after "
                            f"{retries + 1} attempts (lock_timeout={lock_timeout})"
                        ),
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    raise StepFailed(step, exc) from exc

            if not rows:
                return
            cursor = rows[-1][0]
            rows_done += len(rows)
            if on_batch is not None:
                on_batch(rows_done, str(cursor) if cursor is not None else None)
    finally:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.SQL(prev_search_path)))


# --- public per-step primitive ------------------------------------------------

def run_step(
    conn,
    step: Step,
    *,
    schema: str,
    lock_timeout: str,
    retries: int = _DEFAULT_RETRIES,
    on_batch: Callable[[int, str | None], None] | None = None,
    resume_from: tuple[int, str | None] | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> None:
    """Run one `Step` to completion on `conn`.

    `conn` must already be the right kind for `step.transactional` -- a
    plain (non-autocommit) connection for a transactional step, an
    autocommit connection for a non-transactional one (`CREATE INDEX
    CONCURRENTLY` cannot run inside a transaction at all). `run()` is
    responsible for choosing and reusing the right one across the whole
    plan -- reuse matters for non-transactional steps in particular, since
    the standalone `SET lock_timeout` step that precedes a
    `CREATE INDEX CONCURRENTLY` only has an effect on the *next* statement if
    both run on the same session.
    """
    if step.kind == "backfill":
        _run_backfill(
            conn, step, schema=schema, lock_timeout=lock_timeout, retries=retries,
            batch_size=batch_size, on_batch=on_batch, resume_from=resume_from,
        )
        return
    if step.transactional:
        _run_transactional(conn, step, schema=schema, lock_timeout=lock_timeout, retries=retries)
    else:
        _run_nontransactional(conn, step, schema=schema, lock_timeout=lock_timeout, retries=retries)


# --- lock_timeout inference (there is no run()-level parameter for it; it --
# --- is recovered from whatever the plan itself embedded) -------------------

_EMBEDDED_LOCK_TIMEOUT_RE = re.compile(r"SET\s+lock_timeout\s*=\s*'([^']*)'", re.IGNORECASE)


def _infer_lock_timeout(plan: Plan) -> str:
    for step in plan.steps:
        m = _EMBEDDED_LOCK_TIMEOUT_RE.search(step.sql)
        if m:
            return m.group(1)
    return _DEFAULT_LOCK_TIMEOUT


# --- _tributary.migration_steps bookkeeping ---------------------------------

def _load_checkpoint(conn, merge_id: str, seq: int) -> dict | None:
    row = conn.execute(
        "SELECT status, rows_done, cursor_val FROM _tributary.migration_steps "
        "WHERE merge_id = %s AND seq = %s",
        (merge_id, seq),
    ).fetchone()
    if row is None:
        return None
    status, rows_done, cursor_val = row
    return {"status": status, "rows_done": rows_done or 0, "cursor_val": cursor_val}


def _safety_value(safety) -> str:
    return safety.value if hasattr(safety, "value") else str(safety)


def _record_step(
    conn, merge_id: str, step: Step, *, status: str, rows_done: int = 0,
    cursor_val: str | None = None, error: str | None = None,
) -> None:
    finished = status in ("done", "failed")
    conn.execute(
        """
        INSERT INTO _tributary.migration_steps
            (merge_id, seq, sql, kind, safety, note, status, rows_done, rows_total,
             cursor_val, error, started_at, finished_at)
        VALUES (%(merge_id)s, %(seq)s, %(sql)s, %(kind)s, %(safety)s, %(note)s, %(status)s,
                %(rows_done)s, %(rows_total)s, %(cursor_val)s, %(error)s, now(),
                CASE WHEN %(finished)s THEN now() ELSE NULL END)
        ON CONFLICT (merge_id, seq) DO UPDATE SET
            status = EXCLUDED.status,
            rows_done = EXCLUDED.rows_done,
            rows_total = COALESCE(_tributary.migration_steps.rows_total, EXCLUDED.rows_total),
            cursor_val = EXCLUDED.cursor_val,
            error = EXCLUDED.error,
            finished_at = CASE WHEN %(finished)s THEN now()
                               ELSE _tributary.migration_steps.finished_at END
        """,
        {
            "merge_id": merge_id,
            "seq": step.seq,
            "sql": step.sql,
            "kind": step.kind,
            "safety": _safety_value(step.safety),
            "note": step.note,
            "status": status,
            "rows_done": rows_done,
            "rows_total": step.est_rows,
            "cursor_val": cursor_val,
            "error": error,
            "finished": finished,
        },
    )


# --- public entry point --------------------------------------------------------

def run(
    dsn: str,
    schema: str,
    plan: Plan,
    merge_id: str | None = None,
    on_progress: Callable[[Step, str, dict], None] | None = None,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    retries: int = _DEFAULT_RETRIES,
) -> None:
    """Run every step of `plan` against `schema` on the database at `dsn`.

    Two connections are opened for the whole run and reused across every
    step of the appropriate kind: `tx_conn` (a plain connection, one
    explicit transaction per transactional step) and `nontx_conn` (a single
    autocommit connection shared by every non-transactional step -- both a
    backfill's batches and a `CREATE INDEX CONCURRENTLY` together with the
    standalone `SET lock_timeout` step that precedes it, since the latter's
    effect only carries over if both run on the same session).

    When `merge_id` is given, every step's status transition is persisted to
    `_tributary.migration_steps`; a step already `done` (from an earlier,
    killed run against the same `merge_id`) is skipped rather than redone,
    and a `backfill` step resumes from its last checkpointed
    `rows_done`/`cursor_val` instead of restarting.

    On any step failure, `cleanup_invalid_indexes` runs before the
    `StepFailed` is re-raised -- a failed `CREATE INDEX CONCURRENTLY` leaves
    an INVALID index behind that would otherwise block the next attempt from
    reusing its name.
    """

    def progress(step: Step, status: str, info: dict | None = None) -> None:
        if on_progress is not None:
            on_progress(step, status, info or {})

    lock_timeout = _infer_lock_timeout(plan)

    tx_conn = psycopg.connect(dsn)
    tx_conn.autocommit = False
    nontx_conn = psycopg.connect(dsn)
    nontx_conn.autocommit = True

    try:
        for step in plan.steps:
            existing = _load_checkpoint(nontx_conn, merge_id, step.seq) if merge_id else None
            if existing is not None and existing["status"] == "done":
                progress(step, "done", {"resumed": True})
                continue

            progress(step, "running", {})
            if merge_id is not None:
                _record_step(
                    nontx_conn, merge_id, step, status="running",
                    rows_done=existing["rows_done"] if existing else 0,
                    cursor_val=existing["cursor_val"] if existing else None,
                )

            conn = tx_conn if step.transactional else nontx_conn
            resume_from = None
            if step.kind == "backfill" and existing is not None:
                resume_from = (existing["rows_done"], existing["cursor_val"])

            last_batch = {
                "rows_done": existing["rows_done"] if existing else 0,
                "cursor_val": existing["cursor_val"] if existing else None,
            }

            def on_batch(rows_done: int, cursor_val: str | None, step: Step = step) -> None:
                last_batch["rows_done"] = rows_done
                last_batch["cursor_val"] = cursor_val
                if merge_id is not None:
                    _record_step(
                        nontx_conn, merge_id, step, status="running",
                        rows_done=rows_done, cursor_val=cursor_val,
                    )
                progress(step, "running", {"rows_done": rows_done, "cursor": cursor_val})

            try:
                run_step(
                    conn, step, schema=schema, lock_timeout=lock_timeout, retries=retries,
                    on_batch=on_batch, resume_from=resume_from, batch_size=batch_size,
                )
            except StepFailed as exc:
                if merge_id is not None:
                    _record_step(
                        nontx_conn, merge_id, step, status="failed",
                        rows_done=last_batch["rows_done"], cursor_val=last_batch["cursor_val"],
                        error=str(exc.cause),
                    )
                cleanup_invalid_indexes(nontx_conn, schema)
                progress(step, "failed", {"error": str(exc.cause)})
                raise

            if merge_id is not None:
                _record_step(
                    nontx_conn, merge_id, step, status="done",
                    rows_done=last_batch["rows_done"], cursor_val=last_batch["cursor_val"],
                )
            progress(step, "done", {})
    finally:
        tx_conn.close()
        nontx_conn.close()
