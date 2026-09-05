"""The commit DAG: branches, commits, and structure-only materialisation.

This is where "branch" stops being a metaphor and becomes a real Postgres
schema. `_tributary.branches`/`_tributary.commits` (created by `init`, from
`sql/schema.sql`) form a git-like history: every branch points at a HEAD
commit, every commit points at its parent, and every commit carries a full
`Snapshot` of the branch's structure at that point plus the op log that
produced it. `ancestors` walks the `parent_id` chain -- Task 7's
`merge.merge_base` uses that walk to find the lowest common ancestor of two
branch heads, so the chain must be correct and ordered.

**The central design decision:** `create_branch` clones structure only, zero
rows -- it materialises a new schema from the parent branch's HEAD snapshot
by replaying `CREATE TABLE` + constraints + indexes, never by copying data.
That is what makes branching instant regardless of table size, and it is why
this module builds tables from the `Snapshot` model (via `ddl.py`) rather
than reaching for `CREATE TABLE ... LIKE ... INCLUDING ALL`, which silently
drops foreign keys.

RULING R14: `Constraint.definition` and `Index.definition` are captured
*unqualified* by `introspect.snapshot` (see that module's docstring) --
`pg_get_constraintdef`/`pg_get_indexdef` only omit the schema qualifier when
that schema is on `search_path` at read time. Replaying those definitions
against a *different* schema (materialising a branch) has the identical
requirement in reverse: `search_path` must be set to the *target* schema
before any stored definition is replayed, inside an explicit transaction
(`conn` is autocommit, so a bare `SET LOCAL` has no effect beyond the single
implicit transaction it runs in -- see `introspect.snapshot`'s docstring for
the same fix). Skipping this does not raise an error -- it silently creates
the object in whatever schema the connection's ambient `search_path` (e.g.
`main`) resolves it to instead, which is the catastrophic case this module's
tests exist to catch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from tributary.ddl import create_table_sql, render
from tributary.introspect import snapshot
from tributary.model import AddConstraint, Constraint, CreateIndex, Snapshot, Table

_SCHEMA_SQL = Path(__file__).parent / "sql" / "schema.sql"

_BRANCH_COLS = "id, name, head_commit, base_commit, schema_name, created_at"
_COMMIT_COLS = "id, branch_id, parent_id, message, author, snapshot, ops, created_at"


@dataclass(frozen=True)
class Branch:
    id: str
    name: str
    head_commit: str | None
    base_commit: str | None
    schema_name: str
    created_at: datetime


@dataclass(frozen=True)
class Commit:
    id: str
    branch_id: str
    parent_id: str | None
    message: str
    author: str
    snapshot: Snapshot
    ops: list[dict]
    created_at: datetime


# --- schema bootstrap --------------------------------------------------------

def init(conn) -> None:
    """Create the `_tributary` metadata schema. Idempotent -- every statement
    in `sql/schema.sql` is `IF NOT EXISTS`, so calling this repeatedly (e.g.
    once per test, once per app boot) is always safe.
    """
    conn.execute(_SCHEMA_SQL.read_text())


def schema_name(branch: str) -> str:
    """Map a branch name to the Postgres schema name that materialises it.

    `main` is special-cased to the literal schema `main`: `ensure_main`
    registers an *already-existing* production schema, so its schema name is
    whatever that schema is already called, not a derived `br_...` name.
    Every other branch name is sanitised into `br_<lowercase, [a-z0-9]
    runs joined by single underscores>` so an arbitrary human-typed branch
    name (`"Feature/X-1"`) always becomes a legal, unambiguous identifier.
    """
    if branch == "main":
        return "main"
    s = re.sub(r"[^a-z0-9]+", "_", branch.strip().lower()).strip("_")
    return f"br_{s}"


# --- branch/commit lookups ---------------------------------------------------

def _row_to_branch(row) -> Branch:
    bid, name, head_commit, base_commit, schema, created_at = row
    return Branch(
        id=str(bid),
        name=name,
        head_commit=str(head_commit) if head_commit is not None else None,
        base_commit=str(base_commit) if base_commit is not None else None,
        schema_name=schema,
        created_at=created_at,
    )


def _row_to_commit(row) -> Commit:
    cid, branch_id, parent_id, message, author, snap_json, ops_json, created_at = row
    return Commit(
        id=str(cid),
        branch_id=str(branch_id),
        parent_id=str(parent_id) if parent_id is not None else None,
        message=message,
        author=author,
        snapshot=Snapshot.from_json(snap_json),
        ops=ops_json,
        created_at=created_at,
    )


def _find_branch_row(conn, name: str):
    return conn.execute(
        f"SELECT {_BRANCH_COLS} FROM _tributary.branches WHERE name = %s",
        (name,),
    ).fetchone()


def _require_branch(conn, name: str) -> Branch:
    row = _find_branch_row(conn, name)
    if row is None:
        raise ValueError(f"no such branch {name!r}")
    return _row_to_branch(row)


def list_branches(conn) -> list[Branch]:
    rows = conn.execute(
        f"SELECT {_BRANCH_COLS} FROM _tributary.branches ORDER BY created_at"
    ).fetchall()
    return [_row_to_branch(row) for row in rows]


def get_commit(conn, cid: str) -> Commit:
    row = conn.execute(
        f"SELECT {_COMMIT_COLS} FROM _tributary.commits WHERE id = %s",
        (cid,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no such commit {cid!r}")
    return _row_to_commit(row)


def head(conn, branch: str) -> Commit:
    b = _require_branch(conn, branch)
    if b.head_commit is None:
        raise ValueError(f"branch {branch!r} has no commits yet")
    return get_commit(conn, b.head_commit)


def ancestors(conn, cid: str) -> list[str]:
    """Walk the `parent_id` chain from `cid` back to the root commit.

    Newest-first, inclusive of `cid` itself. Deliberately not branch-scoped:
    a feature branch's ancestor chain runs straight through the commits it
    branched from on its parent, which is exactly what Task 7's
    `merge.merge_base` needs to find a lowest common ancestor across two
    different branches' heads.
    """
    result: list[str] = []
    current: str | None = cid
    while current is not None:
        row = conn.execute(
            "SELECT id, parent_id FROM _tributary.commits WHERE id = %s",
            (current,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no such commit {current!r}")
        cur_id, parent_id = row
        result.append(str(cur_id))
        current = str(parent_id) if parent_id is not None else None
    return result


# --- commit ------------------------------------------------------------------

def commit(conn, branch: str, message: str, ops: list[dict], author: str = "you") -> str:
    """Snapshot `branch`'s live schema and record it as a new commit, then
    advance the branch's HEAD to point at it.
    """
    b = _require_branch(conn, branch)
    snap = snapshot(conn, b.schema_name)

    with conn.transaction():
        row = conn.execute(
            "INSERT INTO _tributary.commits (branch_id, parent_id, message, author, snapshot, ops) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (b.id, b.head_commit, message, author, Jsonb(snap.to_json()), Jsonb(ops)),
        ).fetchone()
        cid = str(row[0])
        conn.execute(
            "UPDATE _tributary.branches SET head_commit = %s WHERE id = %s",
            (cid, b.id),
        )
    return cid


# --- ensure_main ---------------------------------------------------------

def ensure_main(conn) -> Branch:
    """Register the (already existing) `main` schema as a branch, with an
    initial commit capturing whatever structure it currently has.

    Idempotent: a `main` branch row already present is left untouched and
    returned as-is -- this is what lets an already-populated production
    database be adopted by calling `ensure_main` on every startup, rather
    than requiring a greenfield database that has never seen `main` before.
    """
    if _find_branch_row(conn, "main") is None:
        with conn.transaction():
            conn.execute("CREATE SCHEMA IF NOT EXISTS main")
            snap = snapshot(conn, "main")

            row = conn.execute(
                "INSERT INTO _tributary.branches (name, schema_name) "
                "VALUES ('main', 'main') RETURNING id"
            ).fetchone()
            branch_id = row[0]

            crow = conn.execute(
                "INSERT INTO _tributary.commits (branch_id, parent_id, message, author, snapshot, ops) "
                "VALUES (%s, NULL, %s, %s, %s, %s) RETURNING id",
                (branch_id, "adopt existing main schema", "system", Jsonb(snap.to_json()), Jsonb([])),
            ).fetchone()

            conn.execute(
                "UPDATE _tributary.branches SET head_commit = %s WHERE id = %s",
                (crow[0], branch_id),
            )

    return _require_branch(conn, "main")


# --- create_branch / materialisation -----------------------------------------

def create_branch(conn, name: str, from_branch: str = "main") -> Branch:
    """Create `name` as a new branch off `from_branch`'s current HEAD.

    Materialises `target_schema` from the parent's HEAD-commit snapshot --
    structure only, zero rows -- inside one transaction with the new schema
    row insert, so a failure partway through never leaves an orphaned schema
    or a branch row pointing at a schema that doesn't exist.
    """
    if _find_branch_row(conn, name) is not None:
        raise ValueError(f"branch {name!r} already exists")

    parent = _require_branch(conn, from_branch)
    parent_snapshot = (
        get_commit(conn, parent.head_commit).snapshot
        if parent.head_commit is not None
        else Snapshot(tables={})
    )
    target_schema = schema_name(name)

    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(target_schema)))
        _materialise(conn, target_schema, parent_snapshot)

        row = conn.execute(
            "INSERT INTO _tributary.branches (name, head_commit, base_commit, schema_name) "
            "VALUES (%s, %s, %s, %s) RETURNING " + _BRANCH_COLS,
            (name, parent.head_commit, parent.head_commit, target_schema),
        ).fetchone()

    return _row_to_branch(row)


def delete_branch(conn, name: str) -> None:
    if name == "main":
        raise ValueError("cannot delete branch 'main'")
    b = _require_branch(conn, name)
    with conn.transaction():
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(b.schema_name))
        )
        conn.execute("DELETE FROM _tributary.branches WHERE id = %s", (b.id,))


_SEQ_DEFAULT = re.compile(
    r"""^nextval\(\s*'
        (?: (?:"[^"]+"|[A-Za-z_][\w$]*) \. )?      # optional schema qualifier
        (?P<seq>"[^"]+"|[A-Za-z_][\w$]*)
        '::regclass\s*\)$""",
    re.IGNORECASE | re.VERBOSE,
)


def _sequence_in_default(default: str | None) -> str | None:
    """Extract the sequence name from a `nextval('...'::regclass)` column
    default (what a `serial`/`bigserial` column's default renders as), or
    `None` for anything else. Needed because the sequence such a default
    calls is a real object that a `CREATE TABLE` must be able to resolve --
    it does not exist yet in a freshly materialised branch schema.
    """
    if not default:
        return None
    m = _SEQ_DEFAULT.match(default.strip())
    if not m:
        return None
    seq = m.group("seq")
    return seq[1:-1] if seq.startswith('"') else seq


def _materialise(conn, target_schema: str, snap: Snapshot) -> None:
    """Build `target_schema` from `snap`: structure only, zero rows.

    `search_path` is set to `target_schema` for the remainder of the caller's
    transaction (RULING R14) before anything else runs, because every
    subsequent statement here replays a definition (`Constraint.definition`,
    `Index.definition`, a `nextval(...)` column default) that was captured
    *unqualified*. Without this, an unqualified `CREATE INDEX ... ON users
    ...` would resolve `users` through the connection's ambient search_path
    and create the index on `main`'s live table instead of the branch's.

    Ordered in four phases, each of which must fully finish before the next
    starts:

    1. Sequences implied by `nextval(...)` column defaults (serial/bigserial
       columns) -- Postgres resolves a DEFAULT expression's function calls
       at DDL time, so the sequence must already exist before `CREATE TABLE`
       runs, not merely by the time a row is inserted.
    2. Tables, columns only -- no constraints or indexes yet. This is what
       lets a foreign key on one table reference another table that hasn't
       been created yet in this same loop: table creation never depends on
       another table already having its constraints.
    3. Constraints, primary/unique/check before foreign -- a foreign key
       cannot be added before the unique or primary key it targets exists,
       and that target may belong to a table processed after the table
       holding the foreign key.
    4. Standalone indexes, last (nothing else depends on them).
    """
    conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(target_schema)))

    tables = list(snap.tables.values())

    for table in tables:
        for col in table.columns.values():
            seq = _sequence_in_default(col.default)
            if seq:
                conn.execute(
                    sql.SQL("CREATE SEQUENCE IF NOT EXISTS {}").format(
                        sql.Identifier(target_schema, seq)
                    )
                )

    for table in tables:
        bare = Table(name=table.name, columns=table.columns)
        conn.execute(create_table_sql(bare, target_schema))

    for table in tables:
        for col in table.columns.values():
            seq = _sequence_in_default(col.default)
            if seq:
                conn.execute(
                    sql.SQL("ALTER SEQUENCE {} OWNED BY {}").format(
                        sql.Identifier(target_schema, seq),
                        sql.Identifier(target_schema, table.name, col.name),
                    )
                )

    all_constraints: list[tuple[str, Constraint]] = [
        (table.name, table.constraints[cname])
        for table in tables
        for cname in sorted(table.constraints)
    ]
    for tname, con in sorted(all_constraints, key=lambda tc: (tc[1].kind == "f", tc[0], tc[1].name)):
        conn.execute(render(AddConstraint(table=tname, constraint=con), target_schema))

    for table in tables:
        for iname in sorted(table.indexes):
            conn.execute(render(CreateIndex(table=table.name, index=table.indexes[iname]), target_schema))
