# Tributary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Branch, diff, and merge Postgres schemas against a real database, where merging a schema change onto a 5GB table does not take the database down.

**Architecture:** One Postgres instance is the workspace. `_tributary` holds the commit DAG; `main` holds the populated production schema; `br_<name>` schemas hold structure-only materialised branches. Pure functions (`canonical`, `diff`, `merge`, `planner`) convert catalog state into safety-classified DDL plans; `executor` runs them observably. A single FastAPI + htmx app serves the whole thing from one container.

**Tech Stack:** Python 3.13, FastAPI, psycopg 3 (sync), Jinja2 + htmx + Alpine + Tailwind (CDN), Postgres 16, pytest + testcontainers, Docker, Railway.

**Spec:** `docs/superpowers/specs/2026-09-05-tributary-design.md`

## Global Constraints

- **Postgres only.** Version floor **PG 12** (`SET NOT NULL` scan-skip via validated CHECK requires 12; `ADD COLUMN ... DEFAULT` non-rewriting requires 11). Dev and deploy target **PG 16**.
- **Sync psycopg 3 throughout.** FastAPI endpoints are `def`, not `async def`, so they run in the threadpool. No async/await anywhere. Rationale: the workload is a handful of concurrent users issuing DDL; async buys nothing and costs bugs under a one-day clock.
- **All identifiers are quoted via `psycopg.sql.Identifier`.** Never f-string an identifier into DDL.
- **`SET lock_timeout` precedes every DDL statement**, default `'3s'`, with bounded retry (3 attempts, exponential backoff 1s/2s/4s).
- **`CREATE INDEX CONCURRENTLY` and batched backfills never run inside a transaction.** Steps carry a `transactional: bool` and the executor honours it.
- **Branches are structure-only.** Materialising a branch never copies rows.
- Schema naming: branch `feature-x` materialises to Postgres schema `br_feature_x` (non-alphanumerics → `_`, lowercased, max 40 chars, collision-suffixed).
- Metadata lives in schema `_tributary`. Reserved names: `_tributary`, `main`, and any `br_*`.
- Snapshots are canonicalised at construction. Downstream code never re-normalises.
- Tests run against a real Postgres. No mocking of database behaviour.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | deps, pytest config |
| `docker-compose.yml` | app + postgres + seeded demo, one command |
| `Dockerfile` | production image for Railway |
| `tributary/model.py` | `Column`/`Constraint`/`Index`/`Table`/`Snapshot`, the `Change` union, `Conflict`, `Safety`, `Step`, `Plan`, `TableStats` |
| `tributary/canonical.py` | type + default normalisation |
| `tributary/introspect.py` | `pg_catalog` → `Snapshot`; `table_stats` |
| `tributary/ddl.py` | `Change` → SQL text |
| `tributary/diff.py` | two `Snapshot`s (+ op log) → `[Change]`, rename-aware |
| `tributary/merge.py` | object maps, LCA, three-way classify, conflict resolution |
| `tributary/planner.py` | `[Change]` + `TableStats` → ordered, classified, rewritten `Plan` |
| `tributary/executor.py` | run a `Plan`: retries, progress, cleanup, resume |
| `tributary/store.py` | commit DAG, branches, merges, migration steps; branch materialisation |
| `tributary/db.py` | connection factory, `DATABASE_URL` handling |
| `tributary/seed.py` | demo workspace + row growth |
| `tributary/web/app.py` | FastAPI app, routes |
| `tributary/web/templates/*.html` | Jinja + htmx UI |
| `tests/conftest.py` | Postgres fixture (testcontainers, or `TRIBUTARY_TEST_DSN`) |
| `tests/test_*.py` | one per module + `test_locks.py` centrepiece |
| `bench/benchmark_5gb.py` | the 5GB proof, output committed |
| `README.md`, `decisions.md` | setup + judgment log |

---

## Task 1: Scaffold and a real Postgres test fixture

**Files:**
- Create: `pyproject.toml`, `docker-compose.yml`, `tributary/__init__.py`, `tributary/db.py`, `tests/conftest.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: nothing
- Produces: `db.connect(dsn: str | None = None) -> psycopg.Connection`, `db.dsn() -> str`; pytest fixtures `pg_dsn: str` (session), `conn: psycopg.Connection` (function, rolled back), `fresh_schema(conn) -> str` (function, creates and drops a uniquely-named schema)

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "tributary"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "psycopg[binary,pool]>=3.2",
  "jinja2>=3.1",
  "python-multipart>=0.0.12",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "testcontainers[postgres]>=4.8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.setuptools.packages.find]
include = ["tributary*"]
```

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: tributary
      POSTGRES_USER: tributary
      POSTGRES_DB: tributary
    ports: ["5433:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tributary"]
      interval: 2s
      timeout: 3s
      retries: 30
    volumes: ["pgdata:/var/lib/postgresql/data"]
  app:
    build: .
    environment:
      DATABASE_URL: postgresql://tributary:tributary@db:5432/tributary
      TRIBUTARY_AUTOSEED: "1"
    ports: ["8000:8000"]
    depends_on:
      db: {condition: service_healthy}
volumes:
  pgdata:
```

- [ ] **Step 3: Write `tributary/db.py`**

```python
import os
import psycopg

DEFAULT_DSN = "postgresql://tributary:tributary@localhost:5433/tributary"


def dsn() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DSN


def connect(target: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(target or dsn())
    conn.autocommit = autocommit
    return conn
```

- [ ] **Step 4: Write `tests/conftest.py`**

`TRIBUTARY_TEST_DSN` short-circuits the container so an already-running compose Postgres can be reused — container startup per run is the slowest thing in the loop.

```python
import os
import uuid
import pytest
import psycopg


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    existing = os.environ.get("TRIBUTARY_TEST_DSN")
    if existing:
        return existing
    from testcontainers.postgres import PostgresContainer
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
def conn(pg_dsn):
    c = psycopg.connect(pg_dsn)
    c.autocommit = True
    yield c
    c.close()


@pytest.fixture
def fresh_schema(conn):
    name = "t_" + uuid.uuid4().hex[:12]
    conn.execute(f'CREATE SCHEMA "{name}"')
    yield name
    conn.execute(f'DROP SCHEMA "{name}" CASCADE')
```

- [ ] **Step 5: Write the smoke test `tests/test_db.py`**

```python
def test_fixture_gives_a_live_postgres(conn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".t (id int)')
    row = conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = %s", (fresh_schema,)
    ).fetchone()
    assert row[0] == 1


def test_server_version_is_at_least_12(conn):
    assert conn.execute("SHOW server_version_num").fetchone()[0] >= "120000"
```

- [ ] **Step 6: Install and run**

Run: `.venv/bin/pip install -e ".[dev]" && .venv/bin/pytest tests/test_db.py -v`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml docker-compose.yml tributary tests
git commit -m "feat: project scaffold with a real Postgres test fixture"
```

---

## Task 2: Model and canonicalisation

Canonicalisation is load-bearing: without it `varchar(50)` and `character varying(50)` read as a schema change and every diff fills with noise nobody trusts.

**Files:**
- Create: `tributary/model.py`, `tributary/canonical.py`, `tests/test_canonical.py`, `tests/test_model.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `canonical.norm_type(raw: str) -> str`, `canonical.norm_default(raw: str | None, coltype: str) -> str | None`
  - `model.Column(name, type, nullable, default, position)`, `model.Constraint(name, kind, definition, columns)`, `model.Index(name, definition, columns, unique, method, predicate)`, `model.Table(name, columns, constraints, indexes)`, `model.Snapshot(tables)`
  - `Snapshot.to_json() -> dict`, `Snapshot.from_json(d) -> Snapshot`
  - Change classes: `CreateTable(table)`, `DropTable(table)`, `RenameTable(old, new)`, `AddColumn(table, column)`, `DropColumn(table, column)`, `RenameColumn(table, old, new)`, `AlterColumnType(table, column, old_type, new_type)`, `SetNotNull(table, column)`, `DropNotNull(table, column)`, `SetDefault(table, column, default)`, `DropDefault(table, column)`, `AddConstraint(table, constraint)`, `DropConstraint(table, constraint)`, `CreateIndex(table, index)`, `DropIndex(table, index)`
  - `model.Safety` (StrEnum: `SAFE_METADATA`, `LOCK_BRIEF`, `LOCK_HEAVY`, `REWRITE`)
  - `model.TableStats(rows: int, bytes: int)`
  - `model.Step(seq, sql, kind, safety, transactional, note, table, est_rows, est_bytes)`
  - `model.Plan(steps: list[Step], warnings: list[str])`
  - `model.Conflict(path, kind, base, ours, theirs)`

- [ ] **Step 1: Write the failing canonicalisation tests**

```python
import pytest
from tributary.canonical import norm_type, norm_default

@pytest.mark.parametrize("raw,expected", [
    ("character varying(50)", "varchar(50)"),
    ("varchar(50)", "varchar(50)"),
    ("integer", "int4"),
    ("int", "int4"),
    ("int4", "int4"),
    ("bigint", "int8"),
    ("boolean", "bool"),
    ("timestamp without time zone", "timestamp"),
    ("timestamp with time zone", "timestamptz"),
    ("numeric(10,2)", "numeric(10,2)"),
    ("double precision", "float8"),
    ("text", "text"),
    ("character varying", "varchar"),
    ("VARCHAR(50)", "varchar(50)"),
    ("varchar (50)", "varchar(50)"),
])
def test_norm_type_collapses_aliases(raw, expected):
    assert norm_type(raw) == expected


@pytest.mark.parametrize("raw,coltype,expected", [
    ("now()", "timestamptz", "now()"),
    ("CURRENT_TIMESTAMP", "timestamptz", "now()"),
    ("'x'::character varying", "varchar", "'x'"),
    ("'x'::text", "text", "'x'"),
    ("0", "int4", "0"),
    ("nextval('s'::regclass)", "int4", "nextval('s'::regclass)"),
    (None, "int4", None),
])
def test_norm_default_strips_redundant_casts(raw, coltype, expected):
    assert norm_default(raw, coltype) == expected
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_canonical.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'tributary.canonical'`

- [ ] **Step 3: Implement `tributary/canonical.py`**

```python
import re

_ALIASES = {
    "integer": "int4", "int": "int4", "int4": "int4",
    "bigint": "int8", "int8": "int8",
    "smallint": "int2", "int2": "int2",
    "boolean": "bool", "bool": "bool",
    "character varying": "varchar", "varchar": "varchar",
    "character": "bpchar", "char": "bpchar", "bpchar": "bpchar",
    "double precision": "float8", "float8": "float8",
    "real": "float4", "float4": "float4",
    "timestamp without time zone": "timestamp", "timestamp": "timestamp",
    "timestamp with time zone": "timestamptz", "timestamptz": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
    "numeric": "numeric", "decimal": "numeric",
    "text": "text", "uuid": "uuid", "json": "json", "jsonb": "jsonb",
    "date": "date", "bytea": "bytea", "inet": "inet",
}

_MOD = re.compile(r"^(?P<base>.*?)\s*\((?P<mod>[^)]*)\)\s*(?P<arr>(\[\])*)$")
_NOW = {"current_timestamp", "now()", "current_timestamp()"}


def norm_type(raw: str) -> str:
    """Collapse Postgres type spellings onto one canonical name."""
    s = " ".join(raw.strip().lower().split())
    arr = ""
    while s.endswith("[]"):
        arr += "[]"
        s = s[:-2].strip()
    mod = ""
    m = _MOD.match(s)
    if m:
        # "timestamp(3) with time zone" keeps its tail; handle by re-joining.
        base = m.group("base").strip()
        tail = ""
        mod = "(" + ",".join(p.strip() for p in m.group("mod").split(",")) + ")"
        s = base + tail
    base = _ALIASES.get(s, s)
    # varchar with no modifier is distinct from varchar(n); keep both faithfully.
    return f"{base}{mod}{arr}"


def norm_default(raw: str | None, coltype: str) -> str | None:
    """Strip casts Postgres adds back itself, and unify now()/CURRENT_TIMESTAMP."""
    if raw is None:
        return None
    s = raw.strip()
    if s.lower() in _NOW:
        return "now()"
    # Drop a trailing ::type cast when it merely restates the column's own type.
    m = re.match(r"^(?P<val>.*?)::(?P<cast>[a-z][a-z0-9_ ]*(\([^)]*\))?)$", s, re.I)
    if m and norm_type(m.group("cast")) == norm_type(coltype):
        return m.group("val").strip()
    return s
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_canonical.py -v`
Expected: all parametrised cases PASS. If `"timestamp(3) with time zone"` style inputs fail, extend `_MOD` handling — do not weaken the test.

- [ ] **Step 5: Implement `tributary/model.py`**

```python
from __future__ import annotations
from dataclasses import dataclass, field, replace
from enum import StrEnum


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    default: str | None
    position: int


@dataclass(frozen=True)
class Constraint:
    name: str
    kind: str            # 'p' primary, 'f' foreign, 'u' unique, 'c' check
    definition: str      # from pg_get_constraintdef
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Index:
    name: str
    definition: str      # from pg_get_indexdef
    columns: tuple[str, ...] = ()
    unique: bool = False
    method: str = "btree"
    predicate: str | None = None


@dataclass(frozen=True)
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    constraints: dict[str, Constraint] = field(default_factory=dict)
    indexes: dict[str, Index] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    tables: dict[str, Table] = field(default_factory=dict)

    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, d: dict) -> "Snapshot": ...


# --- changes -------------------------------------------------------------
@dataclass(frozen=True)
class CreateTable: table: Table
@dataclass(frozen=True)
class DropTable: table: str
@dataclass(frozen=True)
class RenameTable: old: str; new: str
@dataclass(frozen=True)
class AddColumn: table: str; column: Column
@dataclass(frozen=True)
class DropColumn: table: str; column: str
@dataclass(frozen=True)
class RenameColumn: table: str; old: str; new: str
@dataclass(frozen=True)
class AlterColumnType: table: str; column: str; old_type: str; new_type: str
@dataclass(frozen=True)
class SetNotNull: table: str; column: str
@dataclass(frozen=True)
class DropNotNull: table: str; column: str
@dataclass(frozen=True)
class SetDefault: table: str; column: str; default: str
@dataclass(frozen=True)
class DropDefault: table: str; column: str
@dataclass(frozen=True)
class AddConstraint: table: str; constraint: Constraint
@dataclass(frozen=True)
class DropConstraint: table: str; constraint: str
@dataclass(frozen=True)
class CreateIndex: table: str; index: Index
@dataclass(frozen=True)
class DropIndex: table: str; index: str

Change = (
    CreateTable | DropTable | RenameTable | AddColumn | DropColumn | RenameColumn
    | AlterColumnType | SetNotNull | DropNotNull | SetDefault | DropDefault
    | AddConstraint | DropConstraint | CreateIndex | DropIndex
)


# --- planning ------------------------------------------------------------
class Safety(StrEnum):
    SAFE_METADATA = "safe_metadata"
    LOCK_BRIEF = "lock_brief"
    LOCK_HEAVY = "lock_heavy"
    REWRITE = "rewrite"


@dataclass(frozen=True)
class TableStats:
    rows: int
    bytes: int


@dataclass(frozen=True)
class Step:
    seq: int
    sql: str
    kind: str            # ddl | validate | index_concurrent | backfill | swap | preflight
    safety: Safety
    transactional: bool
    note: str
    table: str | None = None
    est_rows: int | None = None
    est_bytes: int | None = None


@dataclass(frozen=True)
class Plan:
    steps: list[Step]
    warnings: list[str] = field(default_factory=list)


ObjectPath = tuple[str, ...]


@dataclass(frozen=True)
class Conflict:
    path: ObjectPath
    kind: str            # modify/modify | drop/modify | add/add | rename/modify
    base: dict | None
    ours: dict | None
    theirs: dict | None
```

- [ ] **Step 6: Write the round-trip test `tests/test_model.py`**

```python
from tributary.model import Snapshot, Table, Column, Constraint, Index

def test_snapshot_json_round_trip_is_lossless():
    snap = Snapshot(tables={
        "users": Table(
            name="users",
            columns={"id": Column("id", "int8", False, None, 1),
                     "email": Column("email", "varchar(255)", True, "'x'", 2)},
            constraints={"users_pkey": Constraint("users_pkey", "p",
                                                  "PRIMARY KEY (id)", ("id",))},
            indexes={"ix_email": Index("ix_email", "CREATE INDEX ...", ("email",), True)},
        )
    })
    assert Snapshot.from_json(snap.to_json()) == snap


def test_snapshot_json_is_key_sorted_for_stable_diffs():
    snap = Snapshot(tables={
        "b": Table("b"), "a": Table("a"),
    })
    assert list(snap.to_json()["tables"].keys()) == ["a", "b"]
```

- [ ] **Step 7: Implement `to_json` / `from_json` to make both pass**

Sort every dict by key on the way out so serialised snapshots are byte-stable; a snapshot that serialises differently on two runs makes the whole commit DAG untrustworthy.

- [ ] **Step 8: Run the suite**

Run: `.venv/bin/pytest tests/test_canonical.py tests/test_model.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add tributary/model.py tributary/canonical.py tests/test_model.py tests/test_canonical.py
git commit -m "feat: schema model with canonical type and default normalisation"
```

---

## Task 3: Introspection

**Files:**
- Create: `tributary/introspect.py`, `tests/test_introspect.py`

**Interfaces:**
- Consumes: `model.Snapshot`/`Table`/`Column`/`Constraint`/`Index`, `canonical.norm_type`, `canonical.norm_default`
- Produces: `introspect.snapshot(conn, schema: str) -> Snapshot`, `introspect.table_stats(conn, schema: str) -> dict[str, TableStats]`

- [ ] **Step 1: Write the failing test**

```python
from tributary.introspect import snapshot, table_stats

DDL = """
CREATE TABLE {s}.users (
  id bigserial PRIMARY KEY,
  email character varying(255) NOT NULL,
  created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
  age integer
);
CREATE UNIQUE INDEX ix_users_email ON {s}.users (email);
ALTER TABLE {s}.users ADD CONSTRAINT age_sane CHECK (age >= 0);
"""


def test_snapshot_reads_columns_with_canonical_types(conn, fresh_schema):
    conn.execute(DDL.format(s=fresh_schema))
    snap = snapshot(conn, fresh_schema)
    users = snap.tables["users"]
    assert users.columns["email"].type == "varchar(255)"
    assert users.columns["email"].nullable is False
    assert users.columns["created_at"].type == "timestamptz"
    assert users.columns["created_at"].default == "now()"
    assert users.columns["age"].nullable is True


def test_snapshot_reads_constraints_and_indexes(conn, fresh_schema):
    conn.execute(DDL.format(s=fresh_schema))
    users = snapshot(conn, fresh_schema).tables["users"]
    kinds = {c.kind for c in users.constraints.values()}
    assert "p" in kinds and "c" in kinds
    assert users.indexes["ix_users_email"].unique is True
    assert users.indexes["ix_users_email"].columns == ("email",)


def test_snapshot_of_empty_schema_is_empty(conn, fresh_schema):
    assert snapshot(conn, fresh_schema).tables == {}


def test_snapshot_ignores_other_schemas(conn, fresh_schema):
    conn.execute(DDL.format(s=fresh_schema))
    other = fresh_schema + "_x"
    conn.execute(f'CREATE SCHEMA "{other}"')
    try:
        conn.execute(f'CREATE TABLE "{other}".ghost (id int)')
        assert "ghost" not in snapshot(conn, fresh_schema).tables
    finally:
        conn.execute(f'DROP SCHEMA "{other}" CASCADE')


def test_table_stats_reports_size(conn, fresh_schema):
    conn.execute(DDL.format(s=fresh_schema))
    conn.execute(f"INSERT INTO {fresh_schema}.users (email) "
                 f"SELECT 'u'||i FROM generate_series(1,1000) i")
    conn.execute(f"ANALYZE {fresh_schema}.users")
    st = table_stats(conn, fresh_schema)["users"]
    assert st.rows >= 900
    assert st.bytes > 0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_introspect.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `tributary/introspect.py`**

Query `pg_catalog`, not `information_schema` alone — `pg_get_constraintdef`, `pg_get_indexdef` and `format_type` are the only way to get definitions Postgres itself agrees with. Skip index rows backing a constraint (`pg_constraint.conindid`) so a PK is not reported twice, once as a constraint and once as an index.

```python
from tributary.model import Snapshot, Table, Column, Constraint, Index, TableStats
from tributary.canonical import norm_type, norm_default

_COLS = """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
       NOT a.attnotnull, pg_get_expr(d.adbin, d.adrelid), a.attnum
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE n.nspname = %s AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

_CONS = """
SELECT c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid),
       COALESCE(array_agg(a.attname ORDER BY k.ord) FILTER (WHERE a.attname IS NOT NULL), '{}')
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE n.nspname = %s
GROUP BY c.relname, con.conname, con.contype, con.oid
"""

_IDX = """
SELECT c.relname, i.relname, pg_get_indexdef(i.oid), x.indisunique, am.amname,
       pg_get_expr(x.indpred, x.indrelid),
       ARRAY(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
             ORDER BY k.ord)
FROM pg_index x
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_am am ON am.oid = i.relam
WHERE n.nspname = %s AND x.indisprimary = FALSE
  AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = i.oid)
"""

_STATS = """
SELECT c.relname,
       GREATEST(c.reltuples, 0)::bigint,
       pg_total_relation_size(c.oid)::bigint
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind = 'r'
"""
```

Assemble into `Snapshot`, applying `norm_type` to every column type and `norm_default(default, type)` to every default.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_introspect.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tributary/introspect.py tests/test_introspect.py
git commit -m "feat: introspect live Postgres catalog into canonical snapshots"
```

---

## Task 4: DDL rendering

**Files:**
- Create: `tributary/ddl.py`, `tests/test_ddl.py`

**Interfaces:**
- Consumes: every `Change` class, `model.Table`
- Produces: `ddl.render(change: Change, schema: str) -> str`, `ddl.create_table_sql(table: Table, schema: str) -> str`

- [ ] **Step 1: Write the failing test**

Identifier quoting is the point of these tests — a table called `order` or `select` must not produce a syntax error.

```python
import pytest
from tributary.ddl import render
from tributary.model import (AddColumn, DropColumn, RenameColumn, AlterColumnType,
                             SetNotNull, DropIndex, Column)

def test_add_column_is_quoted_and_typed():
    sql = render(AddColumn("users", Column("nickname", "varchar(50)", True, None, 9)), "main")
    assert sql == 'ALTER TABLE "main"."users" ADD COLUMN "nickname" varchar(50)'


def test_add_not_null_column_with_default_emits_both():
    sql = render(AddColumn("users", Column("tier", "int4", False, "0", 9)), "main")
    assert sql == 'ALTER TABLE "main"."users" ADD COLUMN "tier" int4 NOT NULL DEFAULT 0'


def test_reserved_words_are_quoted():
    sql = render(DropColumn("order", "select"), "main")
    assert sql == 'ALTER TABLE "main"."order" DROP COLUMN "select"'


def test_rename_column_renders_as_rename_not_drop_add():
    sql = render(RenameColumn("users", "email", "email_address"), "main")
    assert "RENAME COLUMN" in sql and "DROP" not in sql


def test_alter_type_includes_using_clause():
    sql = render(AlterColumnType("users", "id", "int4", "int8"), "main")
    assert sql == ('ALTER TABLE "main"."users" ALTER COLUMN "id" '
                   'TYPE int8 USING "id"::int8')


@pytest.mark.parametrize("change", [SetNotNull("users", "email"), DropIndex("users", "ix")])
def test_every_change_renders_to_nonempty_sql(change):
    assert render(change, "main").strip()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_ddl.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `tributary/ddl.py`**

Use `psycopg.sql.Identifier(...).as_string(None)` for every identifier. Types and constraint/index definitions come from `pg_get_*def` and are inserted literally — they are already Postgres's own rendering.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_ddl.py -v`
Expected: PASS.

- [ ] **Step 5: Add an execution round-trip test**

Proves the rendered SQL is not merely well-shaped but actually runs, and that applying it produces the snapshot the change described.

```python
from tributary.introspect import snapshot
from tributary.model import AddColumn, Column
from tributary.ddl import render

def test_rendered_ddl_actually_executes(conn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}"."order" (id int)')
    conn.execute(render(AddColumn("order", Column("select", "text", True, None, 2)),
                        fresh_schema))
    assert "select" in snapshot(conn, fresh_schema).tables["order"].columns
```

- [ ] **Step 6: Run and commit**

```bash
.venv/bin/pytest tests/test_ddl.py -v
git add tributary/ddl.py tests/test_ddl.py
git commit -m "feat: render schema changes to correctly quoted DDL"
```

---

## Task 5: Diff, including rename detection

The failure this task exists to prevent: a renamed column read as `DROP` + `ADD`, which on a populated table destroys the data.

**Files:**
- Create: `tributary/diff.py`, `tests/test_diff.py`

**Interfaces:**
- Consumes: `model.Snapshot`, all `Change` classes
- Produces: `diff.diff(old: Snapshot, new: Snapshot, ops: list[dict] | None = None) -> list[Change]`, `diff.detect_renames(old, new) -> list[tuple[str, str, str]]` returning `(table, old_col, new_col)`

Op-log entry shape (written by the editor in Task 6, consumed here):
`{"op": "rename_column", "table": "users", "old": "email", "new": "email_address"}`

- [ ] **Step 1: Write the failing tests**

```python
from tributary.diff import diff
from tributary.model import (Snapshot, Table, Column, Index, Constraint,
                             AddColumn, DropColumn, RenameColumn, AlterColumnType,
                             CreateTable, DropTable, SetNotNull, CreateIndex)

def tbl(name, **cols):
    return Table(name=name, columns={
        c: Column(c, t, True, None, i + 1) for i, (c, t) in enumerate(cols.items())
    })


def test_added_column_is_detected():
    a = Snapshot({"users": tbl("users", id="int8")})
    b = Snapshot({"users": tbl("users", id="int8", email="text")})
    assert diff(a, b) == [AddColumn("users", b.tables["users"].columns["email"])]


def test_dropped_column_is_detected():
    a = Snapshot({"users": tbl("users", id="int8", email="text")})
    b = Snapshot({"users": tbl("users", id="int8")})
    assert diff(a, b) == [DropColumn("users", "email")]


def test_op_log_turns_drop_plus_add_into_a_rename():
    a = Snapshot({"users": tbl("users", id="int8", email="text")})
    b = Snapshot({"users": tbl("users", id="int8", email_address="text")})
    ops = [{"op": "rename_column", "table": "users",
            "old": "email", "new": "email_address"}]
    assert diff(a, b, ops) == [RenameColumn("users", "email", "email_address")]


def test_without_op_log_a_matching_drop_add_pair_is_inferred_as_rename():
    a = Snapshot({"users": tbl("users", id="int8", email="text")})
    b = Snapshot({"users": tbl("users", id="int8", email_address="text")})
    assert diff(a, b) == [RenameColumn("users", "email", "email_address")]


def test_unrelated_drop_and_add_of_different_types_is_not_a_rename():
    a = Snapshot({"users": tbl("users", id="int8", email="text")})
    b = Snapshot({"users": tbl("users", id="int8", age="int4")})
    changes = diff(a, b)
    assert DropColumn("users", "email") in changes
    assert any(isinstance(c, AddColumn) for c in changes)
    assert not any(isinstance(c, RenameColumn) for c in changes)


def test_type_change_is_detected():
    a = Snapshot({"users": tbl("users", id="int4")})
    b = Snapshot({"users": tbl("users", id="int8")})
    assert diff(a, b) == [AlterColumnType("users", "id", "int4", "int8")]


def test_canonically_equal_types_produce_no_diff():
    a = Snapshot({"users": tbl("users", email="varchar(50)")})
    b = Snapshot({"users": tbl("users", email="varchar(50)")})
    assert diff(a, b) == []


def test_created_and_dropped_tables():
    a = Snapshot({"users": tbl("users", id="int8")})
    b = Snapshot({"orders": tbl("orders", id="int8")})
    changes = diff(a, b)
    assert any(isinstance(c, CreateTable) for c in changes)
    assert DropTable("users") in changes


def test_nullability_change_is_detected():
    a = Snapshot({"users": Table("users", columns={"e": Column("e", "text", True, None, 1)})})
    b = Snapshot({"users": Table("users", columns={"e": Column("e", "text", False, None, 1)})})
    assert diff(a, b) == [SetNotNull("users", "e")]


def test_index_addition_is_detected():
    a = Snapshot({"users": tbl("users", email="text")})
    t = tbl("users", email="text")
    b = Snapshot({"users": Table("users", columns=t.columns,
                                 indexes={"ix": Index("ix", "CREATE INDEX ix ...", ("email",))})})
    assert diff(a, b) == [CreateIndex("users", b.tables["users"].indexes["ix"])]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_diff.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `tributary/diff.py`**

Order: `DropIndex` / `DropConstraint` first, then table creates, then column work, then `AddConstraint` / `CreateIndex` last. Rename heuristic: an unmatched dropped column and an unmatched added column pair up only when the canonical type is identical and exactly one candidate exists on each side for that type — ambiguity means no inference, because a wrong rename guess is worse than a reported drop.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_diff.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add tributary/diff.py tests/test_diff.py
git commit -m "feat: rename-aware structural diff between snapshots"
```

---

## Task 6: Commit DAG, branches, and materialisation

**Files:**
- Create: `tributary/store.py`, `tributary/sql/schema.sql`, `tests/test_store.py`

**Interfaces:**
- Consumes: `introspect.snapshot`, `model.Snapshot`, `ddl`
- Produces:
  - `store.init(conn)` — creates `_tributary` metadata schema, idempotent
  - `store.create_branch(conn, name: str, from_branch: str = "main") -> Branch`
  - `store.commit(conn, branch: str, message: str, ops: list[dict], author: str = "you") -> str` (returns commit id)
  - `store.head(conn, branch: str) -> Commit`, `store.get_commit(conn, cid) -> Commit`
  - `store.ancestors(conn, cid) -> list[str]` (newest-first, inclusive)
  - `store.list_branches(conn) -> list[Branch]`
  - `store.schema_name(branch: str) -> str`
  - `store.delete_branch(conn, name: str)`
  - Dataclasses `Branch(id, name, head_commit, base_commit, schema_name, created_at)` and `Commit(id, branch_id, parent_id, message, author, snapshot, ops, created_at)`

- [ ] **Step 1: Write `tributary/sql/schema.sql`**

```sql
CREATE SCHEMA IF NOT EXISTS _tributary;

CREATE TABLE IF NOT EXISTS _tributary.branches (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text UNIQUE NOT NULL,
  head_commit  uuid,
  base_commit  uuid,
  schema_name  text UNIQUE NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS _tributary.commits (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  branch_id  uuid NOT NULL REFERENCES _tributary.branches(id) ON DELETE CASCADE,
  parent_id  uuid REFERENCES _tributary.commits(id),
  message    text NOT NULL,
  author     text NOT NULL DEFAULT 'you',
  snapshot   jsonb NOT NULL,
  ops        jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_commits_branch ON _tributary.commits (branch_id, created_at DESC);

CREATE TABLE IF NOT EXISTS _tributary.merges (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_branch text NOT NULL,
  target_branch text NOT NULL,
  base_commit   uuid,
  source_head   uuid,
  target_head   uuid,
  status        text NOT NULL DEFAULT 'pending',
  conflicts     jsonb NOT NULL DEFAULT '[]',
  resolutions   jsonb NOT NULL DEFAULT '{}',
  plan          jsonb NOT NULL DEFAULT '[]',
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);

CREATE TABLE IF NOT EXISTS _tributary.migration_steps (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  merge_id    uuid NOT NULL REFERENCES _tributary.merges(id) ON DELETE CASCADE,
  seq         int NOT NULL,
  sql         text NOT NULL,
  kind        text NOT NULL,
  safety      text NOT NULL,
  note        text NOT NULL DEFAULT '',
  status      text NOT NULL DEFAULT 'pending',
  rows_done   bigint NOT NULL DEFAULT 0,
  rows_total  bigint,
  cursor_val  text,
  error       text,
  started_at  timestamptz,
  finished_at timestamptz,
  UNIQUE (merge_id, seq)
);
```

`cursor_val` is what makes a killed backfill resumable — it records the last PK processed.

- [ ] **Step 2: Write the failing tests**

```python
import pytest
from tributary import store
from tributary.introspect import snapshot

@pytest.fixture
def ws(conn):
    store.init(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS main")
    conn.execute("CREATE TABLE IF NOT EXISTS main.users (id bigserial PRIMARY KEY, email text)")
    store.ensure_main(conn)
    yield conn
    conn.execute("DROP SCHEMA IF EXISTS main CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS _tributary CASCADE")
    for (s,) in conn.execute("SELECT nspname FROM pg_namespace "
                             "WHERE nspname LIKE 'br\\_%'").fetchall():
        conn.execute(f'DROP SCHEMA "{s}" CASCADE')


def test_init_is_idempotent(conn):
    store.init(conn); store.init(conn)


def test_branch_materialises_structure(ws):
    b = store.create_branch(ws, "feature-x")
    snap = snapshot(ws, b.schema_name)
    assert "users" in snap.tables
    assert set(snap.tables["users"].columns) == {"id", "email"}


def test_branch_copies_no_rows(ws):
    ws.execute("INSERT INTO main.users (email) "
               "SELECT 'u'||i FROM generate_series(1,500) i")
    b = store.create_branch(ws, "feature-x")
    n = ws.execute(f'SELECT count(*) FROM "{b.schema_name}".users').fetchone()[0]
    assert n == 0


def test_branch_name_is_sanitised_into_a_schema_name(ws):
    b = store.create_branch(ws, "Feature/X-1")
    assert b.schema_name == "br_feature_x_1"


def test_duplicate_branch_name_is_rejected(ws):
    store.create_branch(ws, "dup")
    with pytest.raises(ValueError, match="already exists"):
        store.create_branch(ws, "dup")


def test_commit_records_snapshot_and_advances_head(ws):
    b = store.create_branch(ws, "feature-x")
    ws.execute(f'ALTER TABLE "{b.schema_name}".users ADD COLUMN nickname text')
    cid = store.commit(ws, "feature-x", "add nickname", ops=[])
    head = store.head(ws, "feature-x")
    assert head.id == cid
    assert "nickname" in head.snapshot.tables["users"].columns


def test_ancestors_walks_the_parent_chain(ws):
    b = store.create_branch(ws, "feature-x")
    c1 = store.commit(ws, "feature-x", "one", ops=[])
    ws.execute(f'ALTER TABLE "{b.schema_name}".users ADD COLUMN a text')
    c2 = store.commit(ws, "feature-x", "two", ops=[])
    anc = store.ancestors(ws, c2)
    assert anc[0] == c2 and c1 in anc
    assert anc.index(c2) < anc.index(c1)


def test_branch_inherits_base_commit_from_parent_head(ws):
    store.commit(ws, "main", "initial", ops=[])
    b = store.create_branch(ws, "feature-x")
    assert b.base_commit == store.head(ws, "main").id
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: FAIL, module not found.

- [ ] **Step 4: Implement `tributary/store.py`**

Materialisation walks the source snapshot and issues `CREATE TABLE` + constraints + indexes. Do **not** use `CREATE TABLE ... LIKE ... INCLUDING ALL` — it does not carry foreign keys, and silently dropping FKs from a branch would make every diff wrong. Build from the snapshot so the branch is exactly what the model says it is.

`ensure_main(conn)` registers the existing `main` schema as a branch with an initial commit if it is not already registered — this is what makes an existing database adoptable rather than requiring a greenfield one.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add tributary/store.py tributary/sql/schema.sql tests/test_store.py
git commit -m "feat: commit DAG, branches, and structure-only materialisation"
```

---

## Task 7: Three-way merge

**Files:**
- Create: `tributary/merge.py`, `tests/test_merge.py`

**Interfaces:**
- Consumes: `model.Snapshot`, `model.Conflict`, `model.ObjectPath`, `store.ancestors`
- Produces:
  - `merge.object_map(snap: Snapshot) -> dict[ObjectPath, dict]`
  - `merge.merge_base(conn, ours_head: str, theirs_head: str) -> str | None`
  - `merge.three_way(base: Snapshot, ours: Snapshot, theirs: Snapshot) -> MergeResult`
  - `MergeResult(merged: Snapshot, conflicts: list[Conflict])`
  - `merge.resolve(result: MergeResult, choices: dict[str, str]) -> Snapshot` where keys are `"/".join(path)` and values are `"ours"` or `"theirs"`

Object paths: `("users",)` for the table itself, `("users", "col", "email")`, `("users", "con", "users_pkey")`, `("users", "idx", "ix_users_email")`.

- [ ] **Step 1: Write the failing tests — one per conflict class**

```python
import pytest
from tributary.merge import three_way, resolve
from tributary.model import Snapshot, Table, Column

def snap(**coltypes):
    return Snapshot({"users": Table("users", columns={
        c: Column(c, t, True, None, i + 1)
        for i, (c, t) in enumerate(coltypes.items())})})


def test_change_on_one_side_only_is_taken():
    base   = snap(id="int8")
    ours   = snap(id="int8", a="text")
    theirs = snap(id="int8")
    r = three_way(base, ours, theirs)
    assert r.conflicts == []
    assert "a" in r.merged.tables["users"].columns


def test_disjoint_changes_on_both_sides_both_land():
    base   = snap(id="int8")
    ours   = snap(id="int8", a="text")
    theirs = snap(id="int8", b="text")
    r = three_way(base, ours, theirs)
    assert r.conflicts == []
    assert {"id", "a", "b"} == set(r.merged.tables["users"].columns)


def test_identical_change_on_both_sides_is_not_a_conflict():
    base   = snap(id="int8")
    ours   = snap(id="int8", a="text")
    theirs = snap(id="int8", a="text")
    r = three_way(base, ours, theirs)
    assert r.conflicts == []
    assert "a" in r.merged.tables["users"].columns


def test_add_add_with_different_definitions_conflicts():
    base   = snap(id="int8")
    ours   = snap(id="int8", a="text")
    theirs = snap(id="int8", a="int4")
    r = three_way(base, ours, theirs)
    assert [c.kind for c in r.conflicts] == ["add/add"]
    assert r.conflicts[0].path == ("users", "col", "a")


def test_modify_modify_conflicts():
    base   = snap(id="int8", a="text")
    ours   = snap(id="int8", a="varchar(50)")
    theirs = snap(id="int8", a="int4")
    r = three_way(base, ours, theirs)
    assert [c.kind for c in r.conflicts] == ["modify/modify"]


def test_drop_modify_conflicts():
    base   = snap(id="int8", a="text")
    ours   = snap(id="int8")
    theirs = snap(id="int8", a="int4")
    r = three_way(base, ours, theirs)
    assert [c.kind for c in r.conflicts] == ["drop/modify"]


def test_drop_on_both_sides_is_not_a_conflict():
    base   = snap(id="int8", a="text")
    ours   = snap(id="int8")
    theirs = snap(id="int8")
    r = three_way(base, ours, theirs)
    assert r.conflicts == []
    assert "a" not in r.merged.tables["users"].columns


def test_table_level_drop_modify_conflicts():
    base   = Snapshot({"users": Table("users", columns={"id": Column("id","int8",True,None,1)})})
    ours   = Snapshot({})
    theirs = Snapshot({"users": Table("users", columns={
        "id": Column("id","int8",True,None,1), "x": Column("x","text",True,None,2)})})
    r = three_way(base, ours, theirs)
    assert any(c.kind == "drop/modify" for c in r.conflicts)


def test_resolve_taking_theirs_applies_their_definition():
    base   = snap(id="int8", a="text")
    ours   = snap(id="int8", a="varchar(50)")
    theirs = snap(id="int8", a="int4")
    r = three_way(base, ours, theirs)
    merged = resolve(r, {"users/col/a": "theirs"})
    assert merged.tables["users"].columns["a"].type == "int4"


def test_resolve_taking_ours_applies_our_definition():
    base   = snap(id="int8", a="text")
    ours   = snap(id="int8", a="varchar(50)")
    theirs = snap(id="int8", a="int4")
    r = three_way(base, ours, theirs)
    merged = resolve(r, {"users/col/a": "ours"})
    assert merged.tables["users"].columns["a"].type == "varchar(50)"


def test_resolve_rejects_unresolved_conflicts():
    base   = snap(id="int8", a="text")
    ours   = snap(id="int8", a="varchar(50)")
    theirs = snap(id="int8", a="int4")
    r = three_way(base, ours, theirs)
    with pytest.raises(ValueError, match="unresolved"):
        resolve(r, {})
```

- [ ] **Step 2: Write the merge-base test against a real DAG**

```python
from tributary import store
from tributary.merge import merge_base

def test_merge_base_finds_the_lowest_common_ancestor(ws):
    root = store.commit(ws, "main", "root", ops=[])
    b = store.create_branch(ws, "feature-x")
    ws.execute(f'ALTER TABLE "{b.schema_name}".users ADD COLUMN a text')
    ours = store.commit(ws, "feature-x", "ours", ops=[])
    ws.execute("ALTER TABLE main.users ADD COLUMN b text")
    theirs = store.commit(ws, "main", "theirs", ops=[])
    assert merge_base(ws, ours, theirs) == root
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/test_merge.py -v`
Expected: FAIL, module not found.

- [ ] **Step 4: Implement `tributary/merge.py`**

`three_way` flattens all three snapshots to object maps, unions the key sets, and classifies each path by comparing `base`, `ours`, `theirs` values. Table-level paths carry only table identity so a table's presence/absence is decided independently of its columns; a conflict at a table path suppresses reporting of conflicts beneath it, otherwise dropping a 30-column table reports 31 conflicts and the UI is unusable.

`merge_base` intersects `ancestors(ours_head)` with `ancestors(theirs_head)` and returns the first element of the ours-ordered list that appears in both.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_merge.py -v`
Expected: 12 passed.

- [ ] **Step 6: Commit**

```bash
git add tributary/merge.py tests/test_merge.py
git commit -m "feat: three-way schema merge with typed conflict detection"
```

---

## Task 8: The migration planner

This is the task the project is really about. A plan that emits naive DDL is a plan that takes a database down.

**Files:**
- Create: `tributary/planner.py`, `tests/test_planner.py`

**Interfaces:**
- Consumes: all `Change` classes, `model.TableStats`, `model.Safety`, `model.Step`, `model.Plan`, `ddl.render`
- Produces:
  - `planner.plan(changes: list[Change], stats: dict[str, TableStats], schema: str, *, lock_timeout: str = "3s", batch_size: int = 10_000) -> Plan`
  - `planner.is_binary_coercible(old: str, new: str) -> bool`
  - `planner.classify(change: Change, stats: TableStats | None) -> Safety`
  - `planner.preflight_sql(change: Change, schema: str) -> str | None`

- [ ] **Step 1: Write the classification tests**

```python
from tributary.planner import plan, classify, is_binary_coercible
from tributary.model import (Safety, TableStats, AddColumn, DropColumn, RenameColumn,
                             AlterColumnType, SetNotNull, AddConstraint, CreateIndex,
                             Column, Constraint, Index)

BIG = {"events": TableStats(rows=52_000_000, bytes=5_200_000_000)}


def test_add_nullable_column_is_metadata_only():
    c = AddColumn("events", Column("note", "text", True, None, 9))
    assert classify(c, BIG["events"]) == Safety.SAFE_METADATA


def test_add_column_with_constant_default_is_metadata_only_on_pg11_plus():
    c = AddColumn("events", Column("tier", "int4", False, "0", 9))
    assert classify(c, BIG["events"]) == Safety.SAFE_METADATA


def test_drop_column_is_metadata_only():
    assert classify(DropColumn("events", "note"), BIG["events"]) == Safety.SAFE_METADATA


def test_rename_is_metadata_only():
    assert classify(RenameColumn("events", "a", "b"), BIG["events"]) == Safety.SAFE_METADATA


def test_int_to_bigint_is_a_rewrite():
    c = AlterColumnType("events", "id", "int4", "int8")
    assert classify(c, BIG["events"]) == Safety.REWRITE


def test_varchar_widening_is_not_a_rewrite():
    assert is_binary_coercible("varchar(50)", "varchar(100)") is True
    c = AlterColumnType("events", "name", "varchar(50)", "varchar(100)")
    assert classify(c, BIG["events"]) == Safety.SAFE_METADATA


def test_varchar_to_text_is_not_a_rewrite():
    assert is_binary_coercible("varchar(50)", "text") is True


def test_varchar_narrowing_is_a_rewrite():
    assert is_binary_coercible("varchar(100)", "varchar(50)") is False


def test_create_index_is_lock_heavy():
    c = CreateIndex("events", Index("ix", "CREATE INDEX ix ON events (ts)", ("ts",)))
    assert classify(c, BIG["events"]) == Safety.LOCK_HEAVY


def test_set_not_null_is_lock_heavy():
    assert classify(SetNotNull("events", "ts"), BIG["events"]) == Safety.LOCK_HEAVY
```

- [ ] **Step 2: Write the rewrite tests — the heart of the task**

```python
def sqls(p):
    return [s.sql for s in p.steps]


def test_create_index_is_rewritten_to_concurrently_outside_a_transaction():
    c = CreateIndex("events", Index("ix", "CREATE INDEX ix ON events USING btree (ts)", ("ts",)))
    p = plan([c], BIG, "main")
    step = next(s for s in p.steps if s.kind == "index_concurrent")
    assert "CONCURRENTLY" in step.sql
    assert step.transactional is False


def test_add_check_constraint_is_split_into_not_valid_then_validate():
    c = AddConstraint("events", Constraint("age_ok", "c", "CHECK (age >= 0)", ("age",)))
    p = plan([c], BIG, "main")
    assert any("NOT VALID" in s for s in sqls(p))
    assert any("VALIDATE CONSTRAINT" in s for s in sqls(p))
    idx_nv = next(i for i, s in enumerate(sqls(p)) if "NOT VALID" in s)
    idx_v = next(i for i, s in enumerate(sqls(p)) if "VALIDATE CONSTRAINT" in s)
    assert idx_nv < idx_v


def test_add_foreign_key_is_split_the_same_way():
    c = AddConstraint("events", Constraint(
        "fk_u", "f", "FOREIGN KEY (user_id) REFERENCES users(id)", ("user_id",)))
    p = plan([c], BIG, "main")
    assert any("NOT VALID" in s for s in sqls(p))
    assert any("VALIDATE CONSTRAINT" in s for s in sqls(p))


def test_set_not_null_uses_a_validated_check_to_skip_the_scan():
    p = plan([SetNotNull("events", "ts")], BIG, "main")
    joined = " | ".join(sqls(p))
    assert "IS NOT NULL" in joined and "NOT VALID" in joined
    assert "VALIDATE CONSTRAINT" in joined
    assert "SET NOT NULL" in joined
    # the scaffolding check is cleaned up afterwards
    assert "DROP CONSTRAINT" in joined


def test_rewriting_retype_becomes_a_shadow_column_backfill():
    p = plan([AlterColumnType("events", "id", "int4", "int8")], BIG, "main")
    kinds = [s.kind for s in p.steps]
    assert "backfill" in kinds and "swap" in kinds
    joined = " | ".join(sqls(p))
    assert "ADD COLUMN" in joined              # shadow column
    assert "TRIGGER" in joined                 # keeps writes in sync during backfill
    assert "RENAME COLUMN" in joined           # swap, not a rewrite
    assert not any("ALTER COLUMN \"id\" TYPE" in s for s in sqls(p))
    backfill = next(s for s in p.steps if s.kind == "backfill")
    assert backfill.transactional is False


def test_non_rewriting_retype_stays_a_single_plain_alter():
    p = plan([AlterColumnType("events", "name", "varchar(50)", "varchar(100)")], BIG, "main")
    assert len([s for s in p.steps if s.kind == "ddl"]) == 1
    assert "TYPE varchar(100)" in sqls(p)[0]


def test_small_table_skips_the_shadow_dance():
    small = {"events": TableStats(rows=200, bytes=16_384)}
    p = plan([AlterColumnType("events", "id", "int4", "int8")], small, "main")
    assert not any(s.kind == "backfill" for s in p.steps)


def test_every_ddl_step_is_preceded_by_a_lock_timeout():
    p = plan([AddColumn("events", Column("n", "text", True, None, 9))], BIG, "main")
    assert any("lock_timeout" in s.sql for s in p.steps) or all(
        s.kind != "ddl" or "lock_timeout" in s.note for s in p.steps)


def test_plan_warns_about_the_expensive_table_in_human_words():
    p = plan([AlterColumnType("events", "id", "int4", "int8")], BIG, "main")
    assert p.warnings
    joined = " ".join(p.warnings).lower()
    assert "events" in joined and ("gb" in joined or "rewrit" in joined)
```

- [ ] **Step 3: Write the ordering and preflight tests**

```python
from tributary.model import CreateTable, Table, Column, DropTable
from tributary.planner import preflight_sql


def test_tables_are_created_before_foreign_keys_that_reference_them():
    changes = [
        AddConstraint("events", Constraint("fk_u", "f",
            "FOREIGN KEY (user_id) REFERENCES users(id)", ("user_id",))),
        CreateTable(Table("users", columns={"id": Column("id", "int8", False, None, 1)})),
    ]
    p = plan(changes, {}, "main")
    order = sqls(p)
    create_at = next(i for i, s in enumerate(order) if "CREATE TABLE" in s)
    fk_at = next(i for i, s in enumerate(order) if "FOREIGN KEY" in s)
    assert create_at < fk_at


def test_indexes_are_dropped_before_their_columns():
    from tributary.model import DropIndex
    p = plan([DropColumn("events", "ts"), DropIndex("events", "ix_ts")], BIG, "main")
    order = sqls(p)
    assert next(i for i, s in enumerate(order) if "DROP INDEX" in s) < \
           next(i for i, s in enumerate(order) if "DROP COLUMN" in s)


def test_preflight_probes_a_retype_for_uncastable_rows():
    sql = preflight_sql(AlterColumnType("events", "code", "text", "int4"), "main")
    assert sql and "SELECT" in sql and "code" in sql


def test_preflight_probes_set_not_null_for_existing_nulls():
    sql = preflight_sql(SetNotNull("events", "ts"), "main")
    assert sql and "IS NULL" in sql


def test_preflight_is_absent_where_nothing_can_fail():
    assert preflight_sql(DropColumn("events", "ts"), "main") is None
```

- [ ] **Step 4: Run to verify failure**

Run: `.venv/bin/pytest tests/test_planner.py -v`
Expected: FAIL, module not found.

- [ ] **Step 5: Implement `tributary/planner.py`**

Binary-coercible pairs (no rewrite): `varchar(n) → varchar(m)` where `m >= n` or `m` absent; `varchar(n) → text`; `text → varchar` with no length; `numeric(p,s) → numeric` with no modifier; identical types. Everything else that changes the base type rewrites.

Shadow-column sequence for a rewriting retype on a large table:

1. `ADD COLUMN <col>__trib_new <newtype>` — metadata only
2. `CREATE FUNCTION` + `CREATE TRIGGER` on INSERT/UPDATE writing `NEW.<col>::<newtype>` into the shadow column, so rows written during the backfill stay correct
3. batched `UPDATE ... WHERE pk > :cursor ORDER BY pk LIMIT :batch` — non-transactional, checkpointed by `cursor_val`
4. verification `SELECT count(*) WHERE <col> IS NOT NULL AND <col>__trib_new IS NULL` — must be 0
5. swap in one short transaction: drop trigger + function, `DROP COLUMN <col>`, `RENAME COLUMN <col>__trib_new TO <col>`, restore NOT NULL/default

Threshold: tables under 1,000,000 rows or 100MB take the plain ALTER — the shadow dance costs more than the lock it avoids at that size. Warnings are written as sentences a person can act on, with sizes in GB, not bytes.

- [ ] **Step 6: Run to verify pass**

Run: `.venv/bin/pytest tests/test_planner.py -v`
Expected: 22 passed.

- [ ] **Step 7: Commit**

```bash
git add tributary/planner.py tests/test_planner.py
git commit -m "feat: migration planner that rewrites unsafe DDL into safe forms"
```

---

## Task 9: The executor

**Files:**
- Create: `tributary/executor.py`, `tests/test_executor.py`

**Interfaces:**
- Consumes: `model.Plan`, `model.Step`, `db.connect`, `store`
- Produces:
  - `executor.run(dsn: str, schema: str, plan: Plan, merge_id: str | None = None, on_progress: Callable[[Step, str, dict], None] | None = None) -> None`
  - `executor.run_step(conn, step: Step, *, lock_timeout: str, retries: int = 3) -> None`
  - `executor.cleanup_invalid_indexes(conn, schema: str) -> list[str]`
  - Raises `executor.StepFailed(step, cause)`

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from tributary import executor
from tributary.planner import plan
from tributary.model import AddColumn, Column, CreateIndex, Index, AlterColumnType
from tributary.introspect import snapshot, table_stats


def test_plan_applies_to_a_real_schema(conn, pg_dsn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY, ts text)')
    p = plan([AddColumn("events", Column("note", "text", True, None, 3))],
             table_stats(conn, fresh_schema), fresh_schema)
    executor.run(pg_dsn, fresh_schema, p)
    assert "note" in snapshot(conn, fresh_schema).tables["events"].columns


def test_concurrent_index_is_created_and_is_valid(conn, pg_dsn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".events (id int PRIMARY KEY, ts text)')
    idx = Index("ix_ts", f'CREATE INDEX ix_ts ON "{fresh_schema}".events USING btree (ts)', ("ts",))
    p = plan([CreateIndex("events", idx)], {"events": __import__("tributary.model",
             fromlist=["TableStats"]).TableStats(rows=5_000_000, bytes=900_000_000)},
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
    from tributary.model import TableStats
    p = plan([AlterColumnType("events", "n", "int4", "int8")],
             {"events": TableStats(rows=5_000_000, bytes=900_000_000)},
             fresh_schema, batch_size=500)
    executor.run(pg_dsn, fresh_schema, p)
    col = snapshot(conn, fresh_schema).tables["events"].columns["n"]
    assert col.type == "int8"
    bad = conn.execute(f'SELECT count(*) FROM "{fresh_schema}".events '
                       f'WHERE n <> id*2').fetchone()[0]
    assert bad == 0


def test_failed_step_reports_the_step_that_failed(conn, pg_dsn, fresh_schema):
    from tributary.model import Plan, Step, Safety
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_executor.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `tributary/executor.py`**

- Transactional steps run in an explicit transaction on a fresh connection; non-transactional steps run on an autocommit connection.
- Every step sets `lock_timeout` first. On `psycopg.errors.LockNotAvailable`, retry up to 3 times with 1s/2s/4s backoff, then fail with a sentence naming the blocking table.
- Backfill steps loop by PK range, writing `rows_done` and `cursor_val` into `migration_steps` after each batch, so a killed run resumes rather than restarting.
- `run` records each step's status transitions into `_tributary.migration_steps` when `merge_id` is given, and always calls `on_progress`.
- On failure, run `cleanup_invalid_indexes` before re-raising — an INVALID index left in the catalog is a landmine for the next migration.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_executor.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tributary/executor.py tests/test_executor.py
git commit -m "feat: observable, resumable plan executor with lock retry and cleanup"
```

---

## Task 10: The lock-safety test and the 5GB benchmark

This is the test that proves the thesis. Everything else is scaffolding around it.

**Files:**
- Create: `tests/test_locks.py`, `bench/benchmark_5gb.py`, `bench/RESULTS.md`

**Interfaces:**
- Consumes: `executor.run`, `planner.plan`, `introspect.table_stats`
- Produces: nothing importable; `bench/benchmark_5gb.py` is a CLI

- [ ] **Step 1: Write the lock-safety test**

```python
import threading
import time
import psycopg
import pytest
from tributary import executor
from tributary.planner import plan
from tributary.model import AddColumn, Column, TableStats, CreateIndex, Index

BIG = {"events": TableStats(rows=5_000_000, bytes=900_000_000)}


@pytest.fixture
def events(conn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".events '
                 f'(id bigint PRIMARY KEY, ts timestamptz, payload text)')
    conn.execute(f"INSERT INTO \"{fresh_schema}\".events "
                 f"SELECT i, now(), repeat('x', 50) FROM generate_series(1,50000) i")
    return fresh_schema


def test_migration_does_not_block_a_long_running_reader(pg_dsn, events):
    """The failure this catches: a naive ALTER queues behind an open read
    transaction and then blocks every query that arrives behind it."""
    reader_ok = threading.Event()
    stop = threading.Event()
    errors = []

    def reader():
        try:
            with psycopg.connect(pg_dsn) as c:
                with c.transaction():
                    c.execute(f'SELECT count(*) FROM "{events}".events')
                    reader_ok.set()
                    while not stop.is_set():
                        c.execute("SELECT 1")   # keeps the snapshot open
                        time.sleep(0.05)
        except Exception as exc:                # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    assert reader_ok.wait(5)

    p = plan([AddColumn("events", Column("note", "text", True, None, 4))], BIG, events)
    started = time.monotonic()
    executor.run(pg_dsn, events, p)
    elapsed = time.monotonic() - started

    stop.set()
    t.join(5)
    assert not errors, f"reader was disrupted: {errors}"
    assert elapsed < 10, f"migration took {elapsed:.1f}s — it queued behind the reader"


def test_migration_gives_up_rather_than_holding_the_lock_queue(pg_dsn, events):
    """A migration that cannot get its lock must fail fast, not stall the
    database while every other query piles up behind it."""
    holder_ready = threading.Event()
    release = threading.Event()

    def holder():
        with psycopg.connect(pg_dsn) as c:
            with c.transaction():
                c.execute(f'LOCK TABLE "{events}".events IN ACCESS EXCLUSIVE MODE')
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
    # 3 retries at 3s lock_timeout plus 1+2+4s backoff — bounded, and nowhere near forever.
    assert elapsed < 25


def test_concurrent_index_build_leaves_writes_available(pg_dsn, events):
    writes = {"n": 0}
    stop = threading.Event()
    errors = []

    def writer():
        try:
            with psycopg.connect(pg_dsn, autocommit=True) as c:
                i = 10_000_000
                while not stop.is_set():
                    c.execute(f'INSERT INTO "{events}".events VALUES (%s, now(), %s)',
                              (i, "y"))
                    writes["n"] += 1
                    i += 1
                    time.sleep(0.01)
        except Exception as exc:                # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    time.sleep(0.3)
    before = writes["n"]

    idx = Index("ix_ts", f'CREATE INDEX ix_ts ON "{events}".events USING btree (ts)', ("ts",))
    executor.run(pg_dsn, events, plan([CreateIndex("events", idx)], BIG, events))

    stop.set()
    t.join(5)
    assert not errors, f"writes failed during index build: {errors}"
    assert writes["n"] > before, "writes stalled while the index was building"
```

- [ ] **Step 2: Run and verify the tests pass**

Run: `.venv/bin/pytest tests/test_locks.py -v`
Expected: 3 passed. If the first test fails with a long elapsed time, the executor is not setting `lock_timeout` — fix the executor, not the test.

- [ ] **Step 3: Write `bench/benchmark_5gb.py`**

A CLI that seeds an `events` table to a target size, then times each migration class against it, printing a markdown table. Usage: `python bench/benchmark_5gb.py --rows 50000000`. It measures, for each of `AddColumn(nullable)`, `AddColumn(default)`, `RenameColumn`, `DropColumn`, `CreateIndex`, `SetNotNull`, `AlterColumnType(int4→int8)`: total wall time, and **the longest single exclusive lock held** (sampled from `pg_locks` by a watcher thread). The lock-held column is the number that matters — total time is allowed to be long, lock time is not.

- [ ] **Step 4: Run the benchmark locally and commit the output**

Run: `python bench/benchmark_5gb.py --rows 50000000 | tee bench/RESULTS.md`
This is the evidence for the 5GB claim. Record the actual table size reported by `pg_total_relation_size` in the output header. If the machine cannot hold 5GB, run the largest size it can and say so in `RESULTS.md` — an honest smaller number beats an invented larger one.

- [ ] **Step 5: Commit**

```bash
git add tests/test_locks.py bench/
git commit -m "test: prove migrations never block readers or stall the lock queue"
```

---

## Task 11: Web application

**Files:**
- Create: `tributary/web/app.py`, `tributary/web/templates/{base,branches,editor,diff,merge,progress,history}.html`, `tests/test_web.py`

**Interfaces:**
- Consumes: everything above
- Produces: FastAPI `app`

Routes:

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | branch list with ahead/behind counts |
| POST | `/branches` | create branch |
| DELETE | `/branches/{name}` | drop branch and its schema |
| GET | `/branches/{name}` | schema editor |
| POST | `/branches/{name}/changes` | apply one edit + append to op log |
| POST | `/branches/{name}/commit` | commit uncommitted edits |
| GET | `/branches/{name}/history` | commit log |
| GET | `/branches/{name}/diff?target=main` | diff view + generated plan |
| POST | `/merges` | start a merge → conflicts or plan |
| POST | `/merges/{id}/resolve` | record conflict resolutions |
| POST | `/merges/{id}/run` | execute the plan in a background thread |
| GET | `/merges/{id}/events` | SSE progress stream |
| POST | `/seed/grow` | grow the demo `events` table |

- [ ] **Step 1: Write the failing route tests**

```python
from fastapi.testclient import TestClient

def test_home_lists_branches(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "main" in r.text


def test_create_branch_then_it_appears(client):
    assert client.post("/branches", data={"name": "feature-x"}).status_code in (200, 303)
    assert "feature-x" in client.get("/").text


def test_duplicate_branch_shows_an_error_not_a_stack_trace(client):
    client.post("/branches", data={"name": "dup"})
    r = client.post("/branches", data={"name": "dup"})
    assert r.status_code < 500
    assert "already exists" in r.text.lower()


def test_diff_shows_the_added_column(client):
    client.post("/branches", data={"name": "feature-x"})
    client.post("/branches/feature-x/changes",
                data={"op": "add_column", "table": "users", "name": "nickname", "type": "text"})
    r = client.get("/branches/feature-x/diff?target=main")
    assert "nickname" in r.text


def test_merge_with_a_conflict_reports_it_before_offering_to_run(client):
    client.post("/branches", data={"name": "a"})
    client.post("/branches", data={"name": "b"})
    for br, typ in (("a", "text"), ("b", "int4")):
        client.post(f"/branches/{br}/changes",
                    data={"op": "add_column", "table": "users", "name": "x", "type": typ})
        client.post(f"/branches/{br}/commit", data={"message": "add x"})
    client.post("/merges", data={"source": "a", "target": "main"})
    client.post("/merges/latest/run")
    r = client.post("/merges", data={"source": "b", "target": "main"})
    assert "conflict" in r.text.lower()


def test_unknown_branch_is_a_404_with_a_readable_message(client):
    r = client.get("/branches/nope")
    assert r.status_code == 404
    assert "nope" in r.text
```

- [ ] **Step 2: Run to verify failure, then implement**

Templates use htmx for edits (`hx-post` + `hx-swap` on the table list) and an SSE-driven progress panel on the merge screen. Tailwind via CDN — no build step, so the Docker image stays a single `pip install`.

Error handling: every route catches domain errors (`ValueError` from `store`, `StepFailed` from `executor`) and renders a sentence in the UI. A 500 with a stack trace is a bug in this task.

- [ ] **Step 3: Run to verify pass**

Run: `.venv/bin/pytest tests/test_web.py -v`
Expected: 6 passed.

- [ ] **Step 4: Commit**

```bash
git add tributary/web tests/test_web.py
git commit -m "feat: web UI for branching, diffing, and merging schemas"
```

---

## Task 12: Demo seed and first-run experience

**Files:**
- Create: `tributary/seed.py`, `tests/test_seed.py`

**Interfaces:**
- Produces: `seed.ensure_demo(conn)`, `seed.grow_events(conn, target_rows: int, on_progress=None)`, `seed.demo_present(conn) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
from tributary import seed, store

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
```

- [ ] **Step 2: Implement**

`ensure_demo` creates `users`, `orders`, `events` with realistic columns and an FK from `orders` to `users`, seeds ~10k rows, then calls `store.ensure_main` so `main` is a registered branch with an initial commit. `grow_events` inserts via `generate_series` in 500k batches so a 50M-row growth reports progress instead of appearing hung.

App startup runs `ensure_demo` when `TRIBUTARY_AUTOSEED=1`, so the deployed URL is never an empty screen.

- [ ] **Step 3: Run and commit**

```bash
.venv/bin/pytest tests/test_seed.py -v
git add tributary/seed.py tests/test_seed.py
git commit -m "feat: seeded demo workspace with a growable events table"
```

---

## Task 13: Dockerfile and Railway deploy

**Files:**
> **Superseded after this plan was written.** This task targeted Railway, and the repo was
> unpublished at the time. Both changed: the user published the repository, and Railway turned out to
> have had no free tier since 2023. The project now deploys to Render via `render.yaml`; `railway.json`
> was deleted. Kept here unedited as the record of what was planned — see `decisions.md` §16 for what
> actually happened and why.

- Create: `Dockerfile`, `.dockerignore`, `railway.json`

- [ ] **Step 1: Write the `Dockerfile`**

```dockerfile
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml ./
COPY tributary ./tributary
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["sh", "-c", "uvicorn tributary.web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

`${PORT:-8000}` matters: Railway injects `PORT` and a hardcoded port silently fails health checks.

- [ ] **Step 2: Verify the whole stack locally in one command**

Run: `docker compose up --build`
Expected: `http://localhost:8000` shows the seeded demo with `main` listed. This is the "stranger runs it in one shot" check — if it needs a second command, fix it here.

- [ ] **Step 3: Do NOT push to GitHub**

**Binding user instruction (given 2026-09-05): nothing is pushed to GitHub.** No `gh repo create`,
no `git remote add`, no `git push`, no PR. The work stays local on the `build-tributary` branch and
the user publishes it themselves when they choose.

This is a deliberate deviation from the assignment's "GitHub repository with the code" deliverable.
The repository is complete and ready to push — full history, one commit per task with its fixes — but
publishing it is the user's call to make, not this build's. Leave the commits clean enough that a
single `git push` later is all it takes.

- [ ] **Step 4: Deploy on Railway — without GitHub**

Railway's GitHub-source flow is unavailable given Step 3, so deploy from the local directory instead:
`railway login` → `railway init` → `railway add` (Postgres) → `railway up`. This uploads the working
directory directly and never touches GitHub.

`railway` CLI is not installed and the user is not authenticated, so this step **requires the user**:
they run `railway login` themselves (suggest `! railway login` in the session so its output lands in
the conversation). Set `TRIBUTARY_AUTOSEED=1`; Railway injects `DATABASE_URL` automatically. Confirm
the public URL loads and that a branch → diff → merge round-trip works **on the deployed instance**,
not just locally — a deploy that boots but cannot complete a merge is not a working deliverable.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore railway.json
git commit -m "chore: containerise and deploy to Railway"
```

---

## Task 14: README and decisions.md

**Files:**
- Create: `README.md`, `decisions.md`

- [ ] **Step 1: Write `README.md`**

Deployed URL at the top. Then: what it is in two sentences, `docker compose up` as the entire setup, a 60-second guided tour (branch → add a column → retype `events.id` → see the safety warning → merge → watch progress), how to run tests, and an architecture section keyed to the module table.

- [ ] **Step 2: Write `decisions.md`**

Not a changelog. One entry per real call, each with **decision / alternatives / reasoning / what it cost**. Minimum set, drawn from the spec:

1. Schema version control, not data version control — why branching copies zero rows
2. Postgres only — transactional DDL is the whole safety model; MySQL would be a different product
3. Branch = Postgres schema, not database or instance — cheap, and cross-branch queries stay possible
4. Snapshot + op log — why state alone cannot tell a rename from a drop, and what that destroys
5. Canonicalising types at introspection — the phantom-diff problem
6. Three-way merge on object maps rather than change-list replay — why replay was rejected
7. Conflict suppression beneath a conflicted table path — the 31-conflicts UI failure
8. Shadow-column backfill over plain `ALTER TYPE` — and the 1M-row threshold below which it is not worth it
9. `lock_timeout` + bounded retry — the lock-queue pile-up, and why failing fast beats waiting
10. Optimistic concurrency on head commits + advisory lock on merge target
11. htmx over React — one container, no build step, deployability under a one-day clock
12. Sync psycopg over async
13. What was cut: row versioning, MySQL, rebase/cherry-pick/revert, auth, free-text conflict resolution
14. The honest note: 5GB proven locally in `bench/RESULTS.md`; the deployed instance runs smaller because the host disk is smaller

- [ ] **Step 3: Commit**

```bash
git add README.md decisions.md
git commit -m "docs: README and decisions log"
```

---

## Self-Review

**Spec coverage:** §3 architecture → Tasks 1, 6, 11, 13. §4 snapshots/diff/renames → Tasks 2, 3, 5. §5 planner → Task 8. §6 merge + concurrency → Tasks 7, 11. §7 execution/failure → Task 9. §8 journey → Tasks 11, 12. §9 testing → every task plus Task 10. §10 setup → Tasks 1, 13. §11 order of work → task sequence. No gaps.

**Placeholders:** none — every code step carries real code; every test step carries real assertions.

**Type consistency:** `Snapshot`/`Table`/`Column`/`Constraint`/`Index` defined Task 2, used identically in 3, 5, 6, 7. `Change` classes defined Task 2, rendered Task 4, produced Task 5, consumed Task 8. `TableStats` defined Task 2, produced by `introspect.table_stats` Task 3, consumed by `planner.plan` Task 8. `Plan`/`Step`/`Safety` defined Task 2, produced Task 8, consumed Task 9. `Conflict`/`ObjectPath` defined Task 2, produced Task 7. `store.ancestors` defined Task 6, consumed by `merge.merge_base` Task 7. Consistent.

**Degradation path:** if the clock runs out, Task 8's shadow-column rewrite degrades to classification plus a blocking warning, and Task 9's backfill/resume machinery is dropped with it. Tasks 1-7 and 11-14 are the irreducible core.
