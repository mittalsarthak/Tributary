"""Turn a list of `Change`s into an ordered, safety-classified `Plan`.

This is the module the project exists for. A naive planner would emit
`ddl.render(change, schema)` for every change and call it done -- and on a
multi-GB table that is exactly the outage this project exists to prevent:
`ALTER TABLE events ALTER COLUMN id TYPE bigint` takes `ACCESS EXCLUSIVE`,
queues behind whatever is already running, and then blocks every query that
arrives behind *it* for as long as the rewrite takes. One naive statement
stalls the whole database, not just the migration.

Every DDL-kind step's own SQL text is prefixed with `SET lock_timeout`, so a
migration statement never joins the lock queue silently -- it either gets
its lock inside `lock_timeout` or fails fast and loudly, instead of stalling
every query that arrives after it. This is checked concretely by
`test_every_ddl_step_is_preceded_by_a_lock_timeout`.

R12 -- the most important rule here: `TableStats.rows` is `None` for a table
that has never been `ANALYZE`d (`pg_class.reltuples` reports `-1`), and
`None` must never be read as "small". `TableStats.bytes`
(`pg_total_relation_size`) is real, exact, on-disk usage regardless of
whether the table has been analysed, so the size decision below
(`_is_large`) is driven primarily by bytes, with rows only ever able to push
a table *into* the "large" bucket, never out of it. A completely unknown
table (`stats is None`, not present in the caller's `stats` dict at all) is
treated the same way: the asymmetry is deliberate. Needlessly running the
shadow-column dance on a small table costs a slower migration the user sees
coming in the plan; skipping it on a genuinely large one costs an outage.

Rewrites, and why each is necessary (see the task brief for the full table):

- `ADD CHECK` / `ADD FOREIGN KEY` -> `... NOT VALID`, then
  `VALIDATE CONSTRAINT` as a separate step. `NOT VALID` defers the
  full-table scan out from under `ACCESS EXCLUSIVE`; `VALIDATE CONSTRAINT`
  only needs `SHARE UPDATE EXCLUSIVE`, so reads and writes continue while it
  runs.
- `ADD PRIMARY KEY` / `ADD UNIQUE`, on a large table (R21) -> build the
  backing index with `CREATE UNIQUE INDEX CONCURRENTLY` (which does not
  block writes), then adopt it with `ADD CONSTRAINT ... USING INDEX`, which
  only holds `ACCESS EXCLUSIVE` long enough to update the catalog, not for
  the whole index build. A primary key additionally requires every key
  column to already be `NOT NULL` (Postgres would otherwise do its own
  null-check scan under the adoption's exclusive lock, defeating the
  point), so that is established first, per column, via the same
  `NOT VALID` CHECK / `VALIDATE` / `SET NOT NULL` sequence `SetNotNull`
  uses (`_not_null_steps`). Below the size threshold, both fall back to a
  single plain `ADD CONSTRAINT` -- Postgres has no `NOT VALID` form for
  either, so there is nothing cheaper to defer on a table small enough that
  the direct validation is fast anyway.
- `CREATE INDEX` -> `CREATE INDEX CONCURRENTLY`, run standalone (never
  combined into the same query string as the `SET lock_timeout` that
  precedes it -- Postgres wraps a multi-statement simple-query string in an
  implicit transaction block, and `CREATE INDEX CONCURRENTLY` fails outright
  inside one, transaction-block or not). The same rewrite applies to the
  *create* half of a drop+create pair -- `diff.py` represents a modified
  index as `DropIndex` + `CreateIndex` (Postgres has no definition-altering
  `ALTER INDEX`), and the create half is exactly as lock-heavy as a
  standalone `CREATE INDEX` would be.
- `SET NOT NULL` -> scaffolding `CHECK (col IS NOT NULL) NOT VALID`,
  `VALIDATE CONSTRAINT`, then `SET NOT NULL` (PG12+ uses the already
  validated check to skip its own scan) and finally `DROP CONSTRAINT` on the
  now-redundant scaffold.
- `ALTER COLUMN TYPE`, binary-incoercible, on a large table, with a known
  primary key (see `pk_columns` below) -> the shadow-column dance (see
  `_shadow_dance`). With no usable primary key, see R19 below -- there is no
  safe batched path, and this module says so rather than pretending
  otherwise. On a small table, a plain `ALTER` is cheaper than the dance it
  would otherwise avoid (see `_is_large`'s thresholds).
- `ALTER COLUMN TYPE`, binary-coercible (`is_binary_coercible`) -> a plain
  `ALTER`, regardless of table size, since nothing needs validating.

R19 -- no unresolved placeholders in emitted SQL, ever. An earlier version
of this module put a literal `{pk}` token in the batched backfill's SQL,
intending for Task 9's executor to substitute the table's real primary-key
column before running it. That is indistinguishable, by inspection of
`Step.sql` alone, from a real, runnable statement -- the only thing standing
between it and a malformed query against production was the next person
remembering an unwritten contract. `plan()` now takes `pk_columns:
dict[str, str] | None`, mapping table name to its (single-column) primary
key, which a caller derives from its own `Snapshot` (a `Constraint` with
`kind == "p"` and one column). When the retyped table's primary key is
known, the backfill's SQL is fully resolved -- no placeholder text appears
anywhere in it. When it is not (table absent from `pk_columns`, or
`pk_columns` not supplied at all) *and* the table is large, this module
does not fabricate a batched plan it cannot actually make safe: a table
genuinely cannot be paged through by primary-key range without one. Instead
it falls back to a single plain `ALTER` (the same statement the naive
planner this project exists to replace would have emitted) and attaches a
warning explaining exactly why the safe path was unavailable, so a human
sees the tradeoff before committing rather than discovering it as an
unexplained outage. `test_no_step_sql_ever_contains_an_unresolved_placeholder`
is the regression guard for this.

Composite primary keys are out of scope for `pk_columns`'s `dict[str, str]`
shape (one column name, not a tuple) -- a caller with a composite-keyed
table should simply omit it from the map, which correctly routes it through
the same conservative no-PK path (there is no single column to batch-range
over regardless).

R20 -- the shadow-column swap must restore what it silently drops. A plain
`ALTER COLUMN ... TYPE` never touches a column's `NOT NULL`/`DEFAULT` --
it's the same physical column throughout. The shadow-column dance is
different: it creates a brand new (plain, nullable, default-less) column
and drops the old one, so if the retyped column was `NOT NULL DEFAULT 0`
and *stays* that way (only its type changes), `diff.py` emits a bare
`AlterColumnType` with no accompanying `SetNotNull`/`SetDefault` -- nothing
else in the change list would ever restore them. Worse than a cosmetic gap:
the very next diff would see the missing `NOT NULL` and emit a `SetNotNull`
to repair damage this migration caused, on a table already large enough
that the repair is itself expensive. `AlterColumnType.nullable`/`.default`
(populated by `diff.py` from the *target* column) tell `_shadow_dance` what
to restore. Restoring `NOT NULL` inside the swap's own short transaction
would force Postgres to scan the (already-populated) shadow column under
that transaction's `ACCESS EXCLUSIVE` lock, extending exactly the lock
window the whole dance exists to keep short -- so, mirroring `SetNotNull`'s
own trick, a scaffolding `CHECK` is validated on the shadow column *before*
the swap begins, and the swap's `SET NOT NULL` only pays for a catalog
update. `DEFAULT` costs nothing to restore either way (it only affects
future inserts), so it is set directly in the swap.

Ordering is topological by change *type*, not by input order: `diff.py`
already returns changes in a sane default bucket order, but this module is
the one that actually enforces it, since a plan is handed changes that may
originate from arbitrary callers (not just `diff`). Changes are bucketed
into phases -- drops-of-dependents, then table-level structure, then
column-level work, then constraint adds, then index creates -- and each
phase is stable-sorted internally so within-table ordering already
established upstream (e.g. `RenameColumn` before the `AlterColumnType` that
follows it) survives. This one rule also naturally gets the drop+create
same-named-index case right: `DropIndex` is phase 0, `CreateIndex` is phase
4, so the drop always precedes the create regardless of input order. Note
that the primary-key-adoption ordering R21 requires (`NOT NULL` established
before the index is adopted) is entirely *intra*-emission -- it is just the
order `_S` tuples are appended within `_emit_add_constraint` for a single
`AddConstraint` change -- so it does not interact with (or need to be
wedged into) this change-level topological sort at all.

`Change`/`Table`/`Snapshot` carry `dict` fields and are unhashable at
runtime despite being frozen dataclasses -- nothing in this module puts one
in a `set()` or uses one as a dict key.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from psycopg import sql

from tributary.ddl import render
from tributary.model import (
    AddColumn,
    AddConstraint,
    AlterColumnType,
    Change,
    CreateIndex,
    CreateTable,
    DropColumn,
    DropConstraint,
    DropDefault,
    DropIndex,
    DropNotNull,
    DropTable,
    Plan,
    RenameColumn,
    RenameTable,
    Safety,
    SetDefault,
    SetNotNull,
    Step,
    TableStats,
)

# --- size threshold (R12) ----------------------------------------------------

_LARGE_ROWS = 1_000_000
_LARGE_BYTES = 100 * 1024 * 1024  # 100MB


def _is_large(stats: TableStats | None) -> bool:
    """Fail safe on an unmeasured table (R12): `stats is None` -- not present
    in the caller's stats dict at all -- is even less information than a
    never-analysed `rows=None` entry, so it is treated the same way: large.

    `rows` can only push the answer *towards* "large" (a huge estimate on a
    tiny disk footprint would be strange, but erring large costs nothing
    real); `bytes` -- always exact -- is what actually decides "small".
    """
    if stats is None:
        return True
    if stats.rows is not None and stats.rows >= _LARGE_ROWS:
        return True
    return stats.bytes >= _LARGE_BYTES


def _size_desc(st: TableStats | None) -> str:
    if st is None:
        return "size unknown (never measured)"
    size_gb = st.bytes / (1024**3)
    rows_desc = f"{st.rows:,} rows" if st.rows is not None else "an unknown row count (never analysed)"
    return f"{size_gb:.1f}GB, {rows_desc}"


# --- binary coercibility ------------------------------------------------------

_TYPE_RE = re.compile(r"^(?P<base>[a-z_][a-z0-9_ ]*?)(\((?P<mod>[^()]*)\))?$")


def _parse_type(t: str) -> tuple[str, str | None]:
    m = _TYPE_RE.match(t.strip())
    if not m:
        return t.strip(), None
    return m.group("base").strip(), m.group("mod")


def is_binary_coercible(old: str, new: str) -> bool:
    """Would `old` -> `new` need a full table rewrite, or is the on-disk
    representation compatible as-is?

    Binary-coercible (no rewrite): identical types; `varchar(n) ->
    varchar(m)` where `m >= n` or `m` is absent (widening, or dropping the
    limit entirely); `varchar(n) -> text`; `text -> varchar` with no length;
    `numeric(p,s) -> numeric` with no modifier. Everything else that changes
    the base type rewrites -- in particular `varchar(100) -> varchar(50)` is
    a *narrowing* (existing values could be too long for the new limit) and
    is never treated as coercible, even though both sides are "varchar".
    """
    if old == new:
        return True

    old_base, old_mod = _parse_type(old)
    new_base, new_mod = _parse_type(new)

    if old_base == "varchar" and new_base == "varchar":
        if new_mod is None:
            return True
        if old_mod is None:
            return False  # unlimited -> limited is a narrowing
        try:
            return int(new_mod) >= int(old_mod)
        except ValueError:
            return False

    if old_base == "varchar" and new_base == "text":
        return True

    if old_base == "text" and new_base == "varchar" and new_mod is None:
        return True

    if old_base == "numeric" and new_base == "numeric" and new_mod is None:
        return True

    return False


# --- constant-default detection (R22 fix round 1, CRITICAL) ------------------

_NUMBER_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
_STRING_RE = re.compile(r"^'(?:[^']|'')*'$")
_KEYWORD_LITERALS = {"true", "false", "null"}
_CAST_RE = re.compile(r"^(?P<val>.*?)::(?P<cast>[a-zA-Z_][a-zA-Z0-9_ ]*(\([^)]*\))?)$")


def _is_constant_default(default: str) -> bool:
    """Is `default` demonstrably a literal -- a number, a quoted string,
    `TRUE`/`FALSE`/`NULL`, or a cast of one of those -- rather than a
    function call?

    This is the one place in the module that must fail *closed*: PG11+'s
    fast `ADD COLUMN ... DEFAULT` path is metadata-only only for a
    non-volatile default. `gen_random_uuid()`, `nextval(...)`, `now()`,
    `random()` and any other function call all force Postgres to compute
    and write an actual value into every existing row immediately -- a full
    table rewrite under `ACCESS EXCLUSIVE`, exactly what this project
    exists to prevent, dressed up as a plan step that would otherwise claim
    `SAFE_METADATA`. Deliberately no allowlist of "known volatile" function
    names: an allowlist fails open on every function whoever wrote it did
    not think of, and this is precisely the place where the failure mode
    of guessing wrong is an outage, not an unnecessary warning.
    """
    s = default.strip()
    m = _CAST_RE.match(s)
    if m:
        s = m.group("val").strip()
    if _NUMBER_RE.match(s):
        return True
    if _STRING_RE.match(s):
        return True
    return s.lower() in _KEYWORD_LITERALS


# --- classification ------------------------------------------------------------

def classify(change: Change, stats: TableStats | None) -> Safety:
    """How dangerous would the *naive* DDL for `change` be, at `stats`'s size?

    This does not decide what SQL `plan()` actually emits -- only how risky
    the naive form would be. `plan()` uses this to choose the rewrite.
    """
    match change:
        case CreateTable() | DropTable() | RenameTable():
            return Safety.SAFE_METADATA

        case AddColumn(column=column):
            if column.default is None:
                # Nullable with no default needs no per-row value at all.
                # NOT NULL with no default: Postgres cannot invent a value
                # for existing rows, so this cannot be metadata-only.
                return Safety.SAFE_METADATA if column.nullable else Safety.LOCK_HEAVY
            if _is_constant_default(column.default):
                # PG11+: ADD COLUMN with a *constant* default is
                # metadata-only -- Postgres stores the default once and
                # applies it lazily to old rows on read.
                return Safety.SAFE_METADATA
            # A volatile default (any function call: gen_random_uuid(),
            # nextval(...), now(), random(), ...) cannot use that fast
            # path -- Postgres must compute and write the actual value for
            # every existing row immediately, a full table rewrite under
            # ACCESS EXCLUSIVE, regardless of whether the column is
            # nullable.
            return Safety.REWRITE

        case (DropColumn() | RenameColumn() | DropNotNull() | SetDefault()
              | DropDefault() | DropConstraint() | DropIndex()):
            return Safety.SAFE_METADATA

        case AlterColumnType(old_type=old_type, new_type=new_type):
            if is_binary_coercible(old_type, new_type):
                return Safety.SAFE_METADATA
            return Safety.REWRITE if _is_large(stats) else Safety.LOCK_BRIEF

        case SetNotNull():
            return Safety.LOCK_HEAVY

        case AddConstraint():
            return Safety.LOCK_HEAVY

        case CreateIndex():
            return Safety.LOCK_HEAVY

        case _:
            raise TypeError(
                f"planner.classify: no classifier for change type {type(change).__name__!r}"
            )


# --- preflight -----------------------------------------------------------------

def preflight_sql(change: Change, schema: str) -> str | None:
    """A cheap probe that fails in milliseconds on data the migration would
    otherwise fail on only after taking a lock (or, for a rewrite, only
    after a long backfill). `None` where nothing in the data itself can
    make the change fail.
    """
    match change:
        case AlterColumnType(table=table, column=column, old_type=old_type, new_type=new_type):
            if is_binary_coercible(old_type, new_type):
                return None
            qualified = _qualified(schema, table)
            col = _ident(column)
            return (
                f"SELECT count(*) FROM {qualified} "
                f"WHERE {col} IS NOT NULL AND {col}::{new_type} IS NULL"
            )

        case SetNotNull(table=table, column=column):
            qualified = _qualified(schema, table)
            col = _ident(column)
            return f"SELECT count(*) FROM {qualified} WHERE {col} IS NULL"

        case _:
            return None


# --- SQL text helpers ------------------------------------------------------

def _ident(name: str) -> str:
    return sql.Identifier(name).as_string(None)


def _qualified(schema: str, name: str) -> str:
    return sql.Identifier(schema, name).as_string(None)


def _lt(lock_timeout: str) -> str:
    return f"SET lock_timeout = {sql.Literal(lock_timeout).as_string(None)}"


class _S(NamedTuple):
    """A `Step` without its final `seq` -- assigned once, in `plan()`, after
    every change has been emitted in its final order."""

    sql: str
    kind: str
    safety: Safety
    transactional: bool
    note: str
    table: str | None = None
    est_rows: int | None = None
    est_bytes: int | None = None


def _lock_timeout_step(lock_timeout: str, table: str | None, est_rows: int | None,
                        est_bytes: int | None, note: str) -> _S:
    """A standalone `SET lock_timeout`, its own step -- never combined into
    the same query string as the `CREATE INDEX CONCURRENTLY` (or similar)
    statement that follows it. Postgres wraps a multi-statement simple-query
    string in an implicit transaction block, and `CONCURRENTLY` operations
    fail outright inside any transaction, so the two must be sent as
    separate queries even though both run with `transactional=False`.
    """
    return _S(_lt(lock_timeout), "ddl", Safety.LOCK_HEAVY, False, note, table, est_rows, est_bytes)


# --- ordering ----------------------------------------------------------------

_PHASE = {
    DropIndex: 0,
    DropConstraint: 0,
    DropTable: 1,
    CreateTable: 1,
    RenameTable: 1,
    DropColumn: 2,
    RenameColumn: 2,
    AlterColumnType: 2,
    SetNotNull: 2,
    DropNotNull: 2,
    SetDefault: 2,
    DropDefault: 2,
    AddColumn: 2,
    AddConstraint: 3,
    CreateIndex: 4,
}


def _order(changes: list[Change]) -> list[Change]:
    """Bucket changes into dependency phases: drop dependents (indexes,
    constraints) first, then table-level structure, then column-level work,
    then constraint adds, then index creates. Sorting on `(phase, original
    index)` is a stable sort by construction, so relative order already
    established within a phase (e.g. by `diff.py`, for a single table's
    column changes) survives untouched.
    """
    indexed = list(enumerate(changes))
    indexed.sort(key=lambda pair: (_PHASE.get(type(pair[1]), 99), pair[0]))
    return [c for _, c in indexed]


def _table_name(change: Change) -> str | None:
    match change:
        case CreateTable(table=t):
            return t.name
        case DropTable(table=t):
            return t
        case RenameTable(old=old):
            return old
        case _:
            return getattr(change, "table", None)


# --- warnings ------------------------------------------------------------------

def _warn_rewrite(table: str, st: TableStats | None) -> str:
    return (
        f"{table} is {_size_desc(st)} -- retyping this column will be rewritten as a "
        f"shadow-column backfill instead of a naive ALTER TABLE, to avoid an ACCESS "
        f"EXCLUSIVE rewrite of the whole table. This migration will take longer than a "
        f"plain ALTER, but {table} stays readable and writable the entire time."
    )


def _warn_volatile_default(table: str, column: str, st: TableStats | None) -> str:
    return (
        f"{table} is {_size_desc(st)} -- adding {column!r} with a volatile default forces "
        f"Postgres to compute and write a value for every existing row immediately, a full "
        f"table rewrite under ACCESS EXCLUSIVE just like a naive retype, not the PG11+ "
        f"metadata-only fast path. Consider adding the column nullable with no default, "
        f"backfilling the value yourself, then setting the default afterwards."
    )


def _warn_no_pk_fallback(table: str, st: TableStats | None) -> str:
    return (
        f"{table} ({_size_desc(st)}) has no primary key Tributary can use for a safe "
        f"batched backfill, so this retype will run as a single ALTER TABLE under an "
        f"ACCESS EXCLUSIVE lock for however long the full rewrite takes -- there is no "
        f"way to page through the table's rows safely without one. Add a primary key to "
        f"{table} before running this migration, or schedule it for a maintenance window."
    )


# --- emission: one Change -> one or more Steps --------------------------------

def _emit_plain(change: Change, schema: str, lock_timeout: str, safety: Safety,
                 table: str | None, est_rows: int | None, est_bytes: int | None,
                 note: str) -> list[_S]:
    stmt = render(change, schema)
    return [_S(f"{_lt(lock_timeout)};\n{stmt}", "ddl", safety, True, note,
                table, est_rows, est_bytes)]


def _not_null_steps(table: str, column: str, schema: str, lock_timeout: str,
                     st: TableStats | None) -> list[_S]:
    """The full `NOT VALID` CHECK -> `VALIDATE` -> `SET NOT NULL` -> `DROP`
    scaffold sequence. Shared by `SetNotNull`'s own rewrite and by the
    `PRIMARY KEY USING INDEX` pattern (R21), which requires every key
    column to already be `NOT NULL` before the index can be adopted without
    Postgres redoing that check itself under the adoption's exclusive lock.
    """
    qualified = _qualified(schema, table)
    col = _ident(column)
    scaffold = _ident(f"{column}_trib_notnull")
    est_rows = st.rows if st is not None else None
    est_bytes = st.bytes if st is not None else None
    probe = f"SELECT count(*) FROM {qualified} WHERE {col} IS NULL"
    return [
        _S(probe, "preflight", Safety.LOCK_HEAVY, True,
           "cheap probe for existing NULLs before touching any lock; a nonzero count here "
           "means SET NOT NULL would fail outright and the migration should abort here",
           table, est_rows, est_bytes),
        _S(f"{_lt(lock_timeout)};\nALTER TABLE {qualified} ADD CONSTRAINT {scaffold} "
           f"CHECK ({col} IS NOT NULL) NOT VALID", "ddl", Safety.LOCK_HEAVY, True,
           "scaffolding CHECK, added NOT VALID so no scan happens yet",
           table, est_rows, est_bytes),
        _S(f"ALTER TABLE {qualified} VALIDATE CONSTRAINT {scaffold}", "validate",
           Safety.LOCK_HEAVY, True,
           "validates the scaffolding CHECK under SHARE UPDATE EXCLUSIVE, not ACCESS EXCLUSIVE",
           table, est_rows, est_bytes),
        _S(f"{_lt(lock_timeout)};\nALTER TABLE {qualified} ALTER COLUMN {col} SET NOT NULL",
           "ddl", Safety.LOCK_HEAVY, True,
           "PG12+ uses the already-validated CHECK to skip its own full-table scan",
           table, est_rows, est_bytes),
        _S(f"{_lt(lock_timeout)};\nALTER TABLE {qualified} DROP CONSTRAINT {scaffold}",
           "ddl", Safety.LOCK_HEAVY, True,
           "scaffolding CHECK is now redundant with the real NOT NULL and is removed",
           table, est_rows, est_bytes),
    ]


def _emit_set_not_null(change: SetNotNull, schema: str, lock_timeout: str,
                        st: TableStats | None) -> tuple[list[_S], list[str]]:
    return _not_null_steps(change.table, change.column, schema, lock_timeout, st), []


def _emit_add_constraint(change: AddConstraint, schema: str, lock_timeout: str,
                          st: TableStats | None) -> tuple[list[_S], list[str]]:
    con = change.constraint
    table = change.table
    qualified = _qualified(schema, table)
    name_ident = _ident(con.name)
    est_rows = st.rows if st is not None else None
    est_bytes = st.bytes if st is not None else None

    if con.kind in ("c", "f"):
        not_valid = f"ALTER TABLE {qualified} ADD CONSTRAINT {name_ident} {con.definition} NOT VALID"
        validate = f"ALTER TABLE {qualified} VALIDATE CONSTRAINT {name_ident}"
        return [
            _S(f"{_lt(lock_timeout)};\n{not_valid}", "ddl", Safety.LOCK_HEAVY, True,
               "added NOT VALID so the full-table scan Postgres would otherwise do under "
               "ACCESS EXCLUSIVE is deferred to a separate, lighter-locked VALIDATE step",
               table, est_rows, est_bytes),
            _S(validate, "validate", Safety.LOCK_HEAVY, True,
               "VALIDATE CONSTRAINT takes SHARE UPDATE EXCLUSIVE, not ACCESS EXCLUSIVE -- "
               "reads and writes continue while this scans existing rows",
               table, est_rows, est_bytes),
        ], []

    if con.kind in ("p", "u") and _is_large(st):
        # R21: PRIMARY KEY/UNIQUE build their backing index under ACCESS
        # EXCLUSIVE if added directly -- the whole table is blocked for the
        # build, exactly the outage this module exists to avoid. Build the
        # index CONCURRENTLY (does not block writes), then adopt it as the
        # constraint; the adoption only holds ACCESS EXCLUSIVE long enough
        # to update the catalog, not for the build itself.
        steps: list[_S] = []
        if con.kind == "p":
            # PRIMARY KEY USING INDEX requires every key column to already
            # be NOT NULL, or Postgres does its own null-check scan under
            # the adoption's exclusive lock, defeating the point. Established
            # first, per column, via the same safe sequence SetNotNull uses.
            for column in con.columns:
                steps.extend(_not_null_steps(table, column, schema, lock_timeout, st))

        idx_ident = _ident(f"{con.name}_trib_build")
        cols_sql = ", ".join(_ident(c) for c in con.columns)
        steps.append(_lock_timeout_step(lock_timeout, table, est_rows, est_bytes,
            "set standalone -- combining this with CREATE INDEX CONCURRENTLY in one query "
            "string would implicitly wrap both in a transaction, and CIC cannot run in one"))
        steps.append(_S(f"CREATE UNIQUE INDEX CONCURRENTLY {idx_ident} ON {qualified} ({cols_sql})",
            "index_concurrent", Safety.LOCK_HEAVY, False,
            "builds the backing index without holding ACCESS EXCLUSIVE for the whole build; "
            "must run outside any transaction. A cancelled run leaves an INVALID index "
            "behind -- Task 9's executor cleans that up on failure",
            table, est_rows, est_bytes))
        adopt_kind = "PRIMARY KEY" if con.kind == "p" else "UNIQUE"
        steps.append(_S(
            f"{_lt(lock_timeout)};\nALTER TABLE {qualified} ADD CONSTRAINT {name_ident} "
            f"{adopt_kind} USING INDEX {idx_ident}", "ddl", Safety.LOCK_HEAVY, True,
            "adopts the already-built index as the constraint; ACCESS EXCLUSIVE is held "
            "only long enough to update the catalog, not to build the index",
            table, est_rows, est_bytes))
        return steps, []

    # Small table, or a constraint kind with no CONCURRENTLY-index
    # equivalent: a direct validation (and, for p/u, index build) is cheap
    # enough here that there is nothing worth deferring.
    stmt = f"ALTER TABLE {qualified} ADD CONSTRAINT {name_ident} {con.definition}"
    note = ("primary key/unique constraints have no NOT VALID form in Postgres, but the "
            "table is small enough that a direct ACCESS EXCLUSIVE validation is cheap"
            if con.kind in ("p", "u") else "plain constraint add")
    return [_S(f"{_lt(lock_timeout)};\n{stmt}", "ddl", Safety.LOCK_HEAVY, True, note,
                table, est_rows, est_bytes)], []


_CIC_RE = re.compile(r"(?i)^(CREATE\s+(?:UNIQUE\s+)?INDEX\s+)")


def _emit_create_index(change: CreateIndex, schema: str, lock_timeout: str,
                        st: TableStats | None) -> tuple[list[_S], list[str]]:
    table = change.table
    definition = change.index.definition
    concurrent = _CIC_RE.sub(lambda m: m.group(1) + "CONCURRENTLY ", definition, count=1)
    est_rows = st.rows if st is not None else None
    est_bytes = st.bytes if st is not None else None

    return [
        _lock_timeout_step(lock_timeout, table, est_rows, est_bytes,
            "set standalone -- combining this with CREATE INDEX CONCURRENTLY in one query "
            "string would implicitly wrap both in a transaction, and CIC cannot run in one"),
        _S(concurrent, "index_concurrent", Safety.LOCK_HEAVY, False,
           "CONCURRENTLY builds the index without blocking writes for the whole build; must "
           "run outside any transaction. A cancelled run leaves an INVALID index behind -- "
           "Task 9's executor cleans that up on failure",
           table, est_rows, est_bytes),
    ], []


def _emit_retype_no_pk_fallback(change: AlterColumnType, schema: str, lock_timeout: str,
                                 st: TableStats | None) -> tuple[list[_S], list[str]]:
    """R19: a large table with no known primary key cannot be safely
    batch-backfilled by PK range -- that is a real limitation, not something
    to paper over with a placeholder. Falls back to the same single plain
    ALTER the small-table path uses, with a warning explaining why the safe
    path was unavailable so a human sees the tradeoff up front.
    """
    table = change.table
    est_rows = st.rows if st is not None else None
    est_bytes = st.bytes if st is not None else None
    steps: list[_S] = []

    probe = preflight_sql(change, schema)
    if probe:
        steps.append(_S(probe, "preflight", Safety.REWRITE, True,
            "cheap probe for values that would fail the cast, before taking any lock",
            table, est_rows, est_bytes))

    stmt = render(change, schema)
    steps.append(_S(f"{_lt(lock_timeout)};\n{stmt}", "ddl", Safety.REWRITE, True,
        "no primary key available for a safe batched backfill on this large table -- "
        "falls back to a single ALTER under ACCESS EXCLUSIVE; see the plan's warnings",
        table, est_rows, est_bytes))

    return steps, [_warn_no_pk_fallback(table, st)]


def _shadow_dance(change: AlterColumnType, schema: str, lock_timeout: str,
                   batch_size: int, st: TableStats | None, pk_col: str) -> tuple[list[_S], list[str]]:
    table = change.table
    column = change.column
    new_type = change.new_type
    qualified = _qualified(schema, table)
    col = _ident(column)
    shadow_name = f"{column}__trib_new"
    shadow_col = _ident(shadow_name)
    func_ident = _qualified(schema, f"{table}_{column}_trib_sync")
    trig_ident = _ident(f"{table}_{column}_trib_sync_trg")
    pk_ident = _ident(pk_col)
    est_rows = st.rows if st is not None else None
    est_bytes = st.bytes if st is not None else None

    steps: list[_S] = []

    probe = preflight_sql(change, schema)
    if probe:
        steps.append(_S(probe, "preflight", Safety.REWRITE, True,
            "cheap probe for values that would fail the cast, run before any lock is taken "
            "so a bad migration fails in milliseconds instead of after a long backfill",
            table, est_rows, est_bytes))

    steps.append(_S(
        f"{_lt(lock_timeout)};\nALTER TABLE {qualified} ADD COLUMN {shadow_col} {new_type}",
        "ddl", Safety.REWRITE, True,
        "shadow column, metadata only -- no existing row is touched yet",
        table, est_rows, est_bytes))

    trigger_stmt = (
        f"CREATE OR REPLACE FUNCTION {func_ident}() RETURNS trigger AS $trib$\n"
        f"BEGIN\n"
        f"  NEW.{shadow_col} := NEW.{col}::{new_type};\n"
        f"  RETURN NEW;\n"
        f"END;\n"
        f"$trib$ LANGUAGE plpgsql;\n"
        f"CREATE TRIGGER {trig_ident} BEFORE INSERT OR UPDATE ON {qualified} "
        f"FOR EACH ROW EXECUTE FUNCTION {func_ident}()"
    )
    steps.append(_S(f"{_lt(lock_timeout)};\n{trigger_stmt}", "ddl", Safety.REWRITE, True,
        "keeps rows written *during* the backfill in sync, so the batched UPDATE below "
        "never races a concurrent writer",
        table, est_rows, est_bytes))

    # R19: fully resolved -- pk_ident is this table's real (quoted)
    # primary-key column, supplied by the caller via pk_columns. No
    # placeholder text appears anywhere in this statement. %(cursor)s and
    # %(batch_size)s are genuine psycopg bind parameters (not template
    # text), meant to be re-supplied every iteration by the executor from
    # the last committed cursor_val and the configured batch_size.
    backfill_stmt = (
        f"{_lt(lock_timeout)};\n"
        f"WITH batch AS (\n"
        f"    SELECT {pk_ident} AS pk_val FROM {qualified}\n"
        f"    WHERE {pk_ident} > %(cursor)s\n"
        f"    ORDER BY {pk_ident} LIMIT %(batch_size)s\n"
        f")\n"
        f"UPDATE {qualified} AS t SET {shadow_col} = t.{col}::{new_type}\n"
        f"FROM batch WHERE t.{pk_ident} = batch.pk_val\n"
        f"RETURNING batch.pk_val"
    )
    steps.append(_S(backfill_stmt, "backfill", Safety.REWRITE, False,
        f"non-transactional and checkpointed: {batch_size} rows per iteration, resuming from "
        "cursor_val if a run is killed and restarted rather than starting over",
        table, est_rows, est_bytes))

    verify_stmt = f"SELECT count(*) FROM {qualified} WHERE {col} IS NOT NULL AND {shadow_col} IS NULL"
    steps.append(_S(verify_stmt, "validate", Safety.REWRITE, True,
        "must return 0 before the swap proceeds -- a nonzero count means the backfill has "
        "not yet covered every row",
        table, est_rows, est_bytes))

    # R20: restore NOT NULL/DEFAULT that the shadow column never had. NOT
    # NULL is validated on the shadow column *before* the swap (same
    # NOT VALID CHECK / VALIDATE trick as SetNotNull) so the swap's own
    # SET NOT NULL only pays for a catalog update, not a fresh scan under
    # the swap transaction's ACCESS EXCLUSIVE lock.
    notnull_scaffold = None
    if change.nullable is False:
        notnull_scaffold = _ident(f"{column}_trib_shadow_notnull")
        steps.append(_S(
            f"{_lt(lock_timeout)};\nALTER TABLE {qualified} ADD CONSTRAINT {notnull_scaffold} "
            f"CHECK ({shadow_col} IS NOT NULL) NOT VALID", "ddl", Safety.REWRITE, True,
            "restores NOT NULL on the shadow column ahead of the swap, validated here so the "
            "swap's own SET NOT NULL is metadata-only instead of rescanning under its lock",
            table, est_rows, est_bytes))
        steps.append(_S(f"ALTER TABLE {qualified} VALIDATE CONSTRAINT {notnull_scaffold}",
            "validate", Safety.REWRITE, True,
            "validates the shadow column's NOT NULL scaffold under SHARE UPDATE EXCLUSIVE",
            table, est_rows, est_bytes))

    swap_lines = [
        _lt(lock_timeout),
        f"DROP TRIGGER IF EXISTS {trig_ident} ON {qualified}",
        f"DROP FUNCTION IF EXISTS {func_ident}()",
    ]
    if change.nullable is False:
        swap_lines.append(f"ALTER TABLE {qualified} ALTER COLUMN {shadow_col} SET NOT NULL")
    if change.default is not None:
        swap_lines.append(
            f"ALTER TABLE {qualified} ALTER COLUMN {shadow_col} SET DEFAULT {change.default}")
    swap_lines.append(f"ALTER TABLE {qualified} DROP COLUMN {col}")
    swap_lines.append(f"ALTER TABLE {qualified} RENAME COLUMN {shadow_col} TO {col}")
    if notnull_scaffold is not None:
        # Constraints reference columns by attnum, not name, so this scaffold
        # -- created against the shadow column before it was renamed -- is
        # still reachable by its own stable name after the rename above.
        swap_lines.append(f"ALTER TABLE {qualified} DROP CONSTRAINT {notnull_scaffold}")
    swap_stmt = ";\n".join(swap_lines)
    steps.append(_S(swap_stmt, "swap", Safety.REWRITE, True,
        "one short transaction: drop the sync trigger/function, drop the old column, rename "
        "the shadow column into place, restoring NOT NULL/DEFAULT when the target column "
        "needs them (R20) -- both were populated by diff.py from the target column, since a "
        "plain ALTER never loses either but this shadow-column swap otherwise would",
        table, est_rows, est_bytes))

    return steps, [_warn_rewrite(table, st)]


def _emit_alter_column_type(change: AlterColumnType, schema: str, lock_timeout: str,
                             batch_size: int, safety: Safety, st: TableStats | None,
                             pk_col: str | None) -> tuple[list[_S], list[str]]:
    table = change.table
    est_rows = st.rows if st is not None else None
    est_bytes = st.bytes if st is not None else None

    if safety in (Safety.SAFE_METADATA, Safety.LOCK_BRIEF):
        steps: list[_S] = []
        if safety == Safety.LOCK_BRIEF:
            probe = preflight_sql(change, schema)
            if probe:
                steps.append(_S(probe, "preflight", safety, True,
                    "cheap probe for values that would fail the cast, before taking any lock",
                    table, est_rows, est_bytes))
            note = ("table is small enough that a brief ACCESS EXCLUSIVE lock costs less "
                    "than the shadow-column dance would")
        else:
            note = "binary-coercible retype -- no rewrite needed, metadata only"
        stmt = render(change, schema)
        steps.append(_S(f"{_lt(lock_timeout)};\n{stmt}", "ddl", safety, True, note,
                         table, est_rows, est_bytes))
        return steps, []

    if pk_col is None:
        return _emit_retype_no_pk_fallback(change, schema, lock_timeout, st)

    return _shadow_dance(change, schema, lock_timeout, batch_size, st, pk_col)


def _emit(change: Change, schema: str, lock_timeout: str, batch_size: int,
          safety: Safety, st: TableStats | None, pk_col: str | None) -> tuple[list[_S], list[str]]:
    table = _table_name(change)
    est_rows = st.rows if st is not None else None
    est_bytes = st.bytes if st is not None else None

    match change:
        case CreateTable() | DropTable() | RenameTable():
            return _emit_plain(change, schema, lock_timeout, safety, table,
                                est_rows, est_bytes, "metadata-only catalog change"), []

        case AddColumn():
            if safety == Safety.SAFE_METADATA:
                note = "nullable or has a constant default -- PG11+ makes this metadata only"
                return _emit_plain(change, schema, lock_timeout, safety, table,
                                    est_rows, est_bytes, note), []
            if safety == Safety.REWRITE:
                note = ("volatile default forces Postgres to compute and write a value for "
                        "every existing row immediately -- a full table rewrite under "
                        "ACCESS EXCLUSIVE, not the PG11+ metadata-only fast path")
                steps = _emit_plain(change, schema, lock_timeout, safety, table,
                                     est_rows, est_bytes, note)
                warnings = [_warn_volatile_default(table, change.column.name, st)] \
                    if _is_large(st) else []
                return steps, warnings
            note = ("NOT NULL with no default cannot be added metadata-only; Postgres must "
                    "validate/backfill a value for every existing row")
            return _emit_plain(change, schema, lock_timeout, safety, table,
                                est_rows, est_bytes, note), []

        case (DropColumn() | RenameColumn() | DropNotNull() | SetDefault()
              | DropDefault() | DropConstraint() | DropIndex()):
            return _emit_plain(change, schema, lock_timeout, safety, table,
                                est_rows, est_bytes, "metadata-only catalog change"), []

        case AlterColumnType():
            return _emit_alter_column_type(change, schema, lock_timeout, batch_size, safety,
                                            st, pk_col)

        case SetNotNull():
            return _emit_set_not_null(change, schema, lock_timeout, st)

        case AddConstraint():
            return _emit_add_constraint(change, schema, lock_timeout, st)

        case CreateIndex():
            return _emit_create_index(change, schema, lock_timeout, st)

        case _:
            raise TypeError(f"planner: no rewrite for change type {type(change).__name__!r}")


def _will_shadow_dance_restore(change: AlterColumnType, stats: dict[str, TableStats],
                                pk_columns: dict[str, str] | None) -> bool:
    """Predicts whether `change` will actually route through `_shadow_dance`
    (as opposed to a plain `ALTER` on the coercible, small-table, or
    no-known-PK paths) -- the *only* path that restores `nullable`/
    `default` at all. Used by `plan()`'s pre-pass (R20 dedup fix, IMPORTANT
    round-1 fix) to find every column a separately-emitted `SetNotNull`/
    `SetDefault` would be redundant against, before any change is emitted --
    so the dedup holds regardless of the two changes' relative order in the
    input list, not just diff.py's own conventional ordering.
    """
    st = stats.get(change.table) if change.table is not None else None
    if classify(change, st) != Safety.REWRITE:
        return False
    pk_col = pk_columns.get(change.table) if (pk_columns and change.table is not None) else None
    return pk_col is not None


# --- public entry point --------------------------------------------------------

def plan(changes: list[Change], stats: dict[str, TableStats], schema: str, *,
         lock_timeout: str = "3s", batch_size: int = 10_000,
         pk_columns: dict[str, str] | None = None) -> Plan:
    """Turn `changes` into an ordered, safety-classified, safety-rewritten `Plan`.

    `stats` is measured size (`introspect.table_stats`'s output), keyed by
    table name -- a table absent from it (not yet created, or simply not
    measured) is treated as unknown and, per R12, handled as if it were
    large rather than assumed small.

    `pk_columns` (R19) maps table name to its single-column primary key, for
    callers that hold a `Snapshot` and can derive it. A table missing from
    this map (or `pk_columns` not supplied at all) that needs a rewriting
    retype on a large table cannot be safely batch-backfilled by PK range --
    see `_emit_retype_no_pk_fallback` and the module docstring.

    R20 dedup (IMPORTANT round-1 fix): when one commit both retypes a column
    and separately changes its nullability/default, `diff.py` emits *both*
    an `AlterColumnType` (carrying the target `nullable`/`default`) *and* a
    standalone `SetNotNull`/`SetDefault` for the same column. If the retype
    takes the real shadow-column path, its swap already restores
    `nullable`/`default` -- the standalone change would then repeat that
    work with a second, fully redundant `NOT VALID`/`VALIDATE`/
    `SET NOT NULL`/`DROP` sequence (a genuine full-table scan under
    `VALIDATE`, proving something already proven) or a redundant
    `SET DEFAULT`. `diff.py` keeps reporting what actually changed --
    deduplicating the *work* is this module's job, not diff's, so the
    dedup lives here: a pre-pass finds every (table, column) the shadow
    dance will actually restore, and the standalone change is dropped from
    the plan for exactly those.
    """
    restored_not_null: set[tuple[str, str]] = set()
    restored_default: set[tuple[str, str]] = set()
    for change in changes:
        if isinstance(change, AlterColumnType) and _will_shadow_dance_restore(
                change, stats, pk_columns):
            if change.nullable is False:
                restored_not_null.add((change.table, change.column))
            if change.default is not None:
                restored_default.add((change.table, change.column))

    ordered = _order(changes)
    intermediate: list[_S] = []
    warnings: list[str] = []

    for change in ordered:
        table = _table_name(change)

        if isinstance(change, SetNotNull) and (table, change.column) in restored_not_null:
            continue  # already restored by this column's own retype -- see docstring
        if isinstance(change, SetDefault) and (table, change.column) in restored_default:
            continue

        st = stats.get(table) if table is not None else None
        safety = classify(change, st)
        pk_col = pk_columns.get(table) if (pk_columns and table is not None) else None
        emitted_steps, emitted_warnings = _emit(change, schema, lock_timeout, batch_size,
                                                 safety, st, pk_col)
        intermediate.extend(emitted_steps)
        warnings.extend(w for w in emitted_warnings if w)

    steps = [
        Step(i, s.sql, s.kind, s.safety, s.transactional, s.note, s.table, s.est_rows, s.est_bytes)
        for i, s in enumerate(intermediate, start=1)
    ]
    return Plan(steps=steps, warnings=warnings)
