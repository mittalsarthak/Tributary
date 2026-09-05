"""Read a live Postgres schema out of pg_catalog into a canonical Snapshot.

Every commit in the system stores a Snapshot produced here; diff and merge
compare them structurally. Canonicalisation (norm_type / norm_default) is
applied exactly once, at construction time, so nothing downstream ever needs
to re-normalise a type or default spelling.

Queries pg_catalog directly (not information_schema) so that constraint and
index definitions come from pg_get_constraintdef / pg_get_indexdef -- the
same functions Postgres itself uses to reconstruct DDL, so the definitions
are ones Postgres agrees with.

Both of those functions embed the *source* schema name in the text they
return unless the schema is in `search_path` at call time (and, for
`pg_get_indexdef` specifically, unless its `pretty` argument is also true --
see the comment on `_IDX` and on `snapshot()` below). `snapshot()` sets
`search_path` to the schema being introspected for the duration of the
catalog reads so every definition comes back unqualified: a definition that
carried the source schema name would make diff report every index and FK
as "modified" on every branch (the schema names always differ), and would
make Task 6/9's replay of that DDL create the object in the *source*
schema instead of the branch being materialised.
"""

from __future__ import annotations

from psycopg import sql

from tributary.canonical import norm_default, norm_type
from tributary.model import Column, Constraint, Index, Snapshot, Table, TableStats

_TABLES = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind = 'r'
"""

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
SELECT c.relname, i.relname, pg_get_indexdef(i.oid, 0, true), x.indisunique, am.amname,
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
# NOTE: pg_get_indexdef(oid) -- the 1-argument form -- unconditionally
# schema-qualifies the "ON <table>" clause, regardless of search_path; that
# is a hardcoded formatting choice in Postgres's ruleutils, not a
# visibility check, and no amount of search_path manipulation changes it
# (verified empirically -- see task-3-report.md). The 3-argument form
# `pg_get_indexdef(oid, 0, true)` (column_no=0, pretty=true) routes through
# the same search-path-aware naming Postgres uses for view/rule bodies, and
# *does* drop the qualifier once the schema is in search_path. That is the
# genuine "use Postgres's own machinery" mechanism for indexes; combined
# with `snapshot()` setting search_path (below), it is what makes
# Index.definition unqualified.

_STATS = """
SELECT c.relname,
       c.reltuples::bigint,
       pg_total_relation_size(c.oid)::bigint
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind = 'r'
"""


def _get_table(tables: dict[str, Table], name: str) -> Table:
    table = tables.get(name)
    if table is None:
        table = Table(name=name)
        tables[name] = table
    return table


def snapshot(conn, schema: str) -> Snapshot:
    """Introspect `schema` on `conn` into a canonical Snapshot.

    Only objects belonging to `schema` are included -- every query filters
    on `n.nspname = %s`, so nothing from another schema can leak in.

    Tables are seeded from `_TABLES` independently of their columns, before
    the columns/constraints/indexes loops run: a table with zero columns
    (`CREATE TABLE t()`, or every column later dropped) is legal Postgres
    and must still appear in the snapshot with empty `columns` -- if it
    silently disappeared instead, diff would read a real table as dropped
    and merge would issue a `DropTable` nobody asked for.

    RULING R14: `conn` is autocommit, so a bare `SET LOCAL` on it would be
    scoped to (and discarded at the end of) the single implicit transaction
    that one statement runs in, with no effect on the statements that
    follow. `conn.transaction()` opens a real, explicit transaction around
    all of the catalog reads (autocommit or not), so `SET LOCAL search_path`
    stays in effect for every query in this function and is automatically
    reverted -- by the transaction ending, whether via commit or, on an
    exception, rollback -- without any manual restore.
    """
    tables: dict[str, Table] = {}

    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))

        for (relname,) in conn.execute(_TABLES, (schema,)):
            _get_table(tables, relname)

        for relname, attname, raw_type, nullable, raw_default, attnum in conn.execute(_COLS, (schema,)):
            table = _get_table(tables, relname)
            coltype = norm_type(raw_type)
            default = norm_default(raw_default, coltype)
            table.columns[attname] = Column(
                name=attname,
                type=coltype,
                nullable=nullable,
                default=default,
                position=attnum,
            )

        for relname, conname, contype, condef, con_columns in conn.execute(_CONS, (schema,)):
            table = _get_table(tables, relname)
            table.constraints[conname] = Constraint(
                name=conname,
                kind=contype,
                definition=condef,
                columns=tuple(con_columns),
            )

        for relname, idxname, idxdef, indisunique, amname, predicate, idx_columns in conn.execute(_IDX, (schema,)):
            table = _get_table(tables, relname)
            table.indexes[idxname] = Index(
                name=idxname,
                definition=idxdef,
                columns=tuple(idx_columns),
                unique=indisunique,
                method=amname,
                predicate=predicate,
            )

    return Snapshot(tables=tables)


def table_stats(conn, schema: str) -> dict[str, TableStats]:
    """Measured size of every table in `schema`.

    `rows` (`pg_class.reltuples`) is a planner *estimate*, not a count, and
    it is `None` -- unknown, not zero -- until the table has been ANALYZEd
    at least once (Postgres reports it as -1 for a never-analysed table).
    Callers must never treat `None` as 0: a freshly-restored, never-analysed
    multi-GB table would otherwise read as "small" and be handed a naive
    `ALTER TABLE` that takes an ACCESS EXCLUSIVE lock for a full rewrite.

    `bytes` (`pg_total_relation_size`) is always exact on-disk usage --
    never an estimate, regardless of whether the table has been analysed --
    and is the signal to trust when `rows` is `None`.
    """
    stats = {}
    for relname, reltuples, nbytes in conn.execute(_STATS, (schema,)):
        stats[relname] = TableStats(
            rows=None if reltuples < 0 else reltuples,
            bytes=nbytes,
        )
    return stats
