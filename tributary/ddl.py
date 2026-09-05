"""Render a single `Change` (or a `Table`) into executable DDL text.

Every identifier -- schema, table, column, constraint, and index name -- is
quoted through `psycopg.sql.Identifier`, never an f-string or manual string
concatenation, so that a table called `order` or a column called `select`
(both legal in Postgres) render as valid, non-injectable SQL.

Type names and constraint/index definitions, by contrast, come from
`format_type` / `pg_get_constraintdef` / `pg_get_indexdef` in `introspect.py`
(or an equivalently pre-rendered string in a hand-built `Change`, as in the
planner). Those are already Postgres's own rendering of the object and are
inserted literally -- re-quoting or re-parsing them here would only risk
mangling a case `norm_type`/`pg_get_*def` already got right.

Caveat for callers that materialise structure into a *different* schema than
the one a definition was captured from (branch creation, in `store.py`):
`pg_get_indexdef` always schema-qualifies the indexed table with its source
schema, and `pg_get_constraintdef` does the same for a foreign key's
referenced table whenever that schema is not on the introspecting session's
`search_path`. Because those definitions are inserted literally here, this
module does not itself retarget that embedded schema name -- the caller is
responsible for ensuring the definitions it hands to `create_table_sql`
already point at the schema it is building into.
"""

from __future__ import annotations

from psycopg import sql

from tributary.model import (
    AddColumn,
    AddConstraint,
    AlterColumnType,
    Change,
    Column,
    CreateIndex,
    CreateTable,
    DropColumn,
    DropConstraint,
    DropDefault,
    DropIndex,
    DropNotNull,
    DropTable,
    RenameColumn,
    RenameTable,
    SetDefault,
    SetNotNull,
    Table,
)


def _ident(name: str) -> str:
    return sql.Identifier(name).as_string(None)


def _qualified(schema: str, name: str) -> str:
    return sql.Identifier(schema, name).as_string(None)


def _column_def(col: Column) -> str:
    parts = [f"{_ident(col.name)} {col.type}"]
    if not col.nullable:
        parts.append("NOT NULL")
    if col.default is not None:
        parts.append(f"DEFAULT {col.default}")
    return " ".join(parts)


def create_table_sql(table: Table, schema: str) -> str:
    """Render a full `CREATE TABLE` for `table` in `schema`, followed by its
    constraints (`ALTER TABLE ... ADD CONSTRAINT`) and indexes.

    Deliberately not `CREATE TABLE ... (LIKE other INCLUDING ALL)`: that
    Postgres shortcut does not carry foreign key constraints, and a branch
    that silently lost its foreign keys would make every subsequent diff
    against it wrong. Built column-by-column and constraint-by-constraint
    from the model instead, so the result is exactly what the model says.

    Returned as one string of `;`-separated statements, executable in a
    single `conn.execute(...)` call (no parameters -- psycopg 3 runs a
    semicolon-separated batch like that as a simple-query multi-statement).
    """
    columns = sorted(table.columns.values(), key=lambda c: c.position)
    col_lines = ",\n    ".join(_column_def(c) for c in columns)
    statements = [f"CREATE TABLE {_qualified(schema, table.name)} (\n    {col_lines}\n)"]

    for cname in sorted(table.constraints):
        constraint = table.constraints[cname]
        statements.append(
            f"ALTER TABLE {_qualified(schema, table.name)} ADD CONSTRAINT "
            f"{_ident(constraint.name)} {constraint.definition}"
        )

    for iname in sorted(table.indexes):
        statements.append(table.indexes[iname].definition)

    return ";\n".join(statements)


def render(change: Change, schema: str) -> str:
    """Render one `Change` to a single executable DDL string, targeting `schema`.

    Raises `TypeError` for anything that is not one of the 15 `Change`
    classes, rather than returning `None` or an empty string -- an unhandled
    change type is a bug in the caller (or in this module falling behind a
    new `Change` class), and must fail loudly.
    """
    match change:
        case CreateTable(table=table):
            return create_table_sql(table, schema)

        case DropTable(table=table):
            return f"DROP TABLE {_qualified(schema, table)}"

        case RenameTable(old=old, new=new):
            return f"ALTER TABLE {_qualified(schema, old)} RENAME TO {_ident(new)}"

        case AddColumn(table=table, column=column):
            return f"ALTER TABLE {_qualified(schema, table)} ADD COLUMN {_column_def(column)}"

        case DropColumn(table=table, column=column):
            return f"ALTER TABLE {_qualified(schema, table)} DROP COLUMN {_ident(column)}"

        case RenameColumn(table=table, old=old, new=new):
            return (f"ALTER TABLE {_qualified(schema, table)} RENAME COLUMN "
                    f"{_ident(old)} TO {_ident(new)}")

        case AlterColumnType(table=table, column=column, new_type=new_type):
            return (f"ALTER TABLE {_qualified(schema, table)} ALTER COLUMN "
                    f"{_ident(column)} TYPE {new_type} USING {_ident(column)}::{new_type}")

        case SetNotNull(table=table, column=column):
            return (f"ALTER TABLE {_qualified(schema, table)} ALTER COLUMN "
                    f"{_ident(column)} SET NOT NULL")

        case DropNotNull(table=table, column=column):
            return (f"ALTER TABLE {_qualified(schema, table)} ALTER COLUMN "
                    f"{_ident(column)} DROP NOT NULL")

        case SetDefault(table=table, column=column, default=default):
            return (f"ALTER TABLE {_qualified(schema, table)} ALTER COLUMN "
                    f"{_ident(column)} SET DEFAULT {default}")

        case DropDefault(table=table, column=column):
            return (f"ALTER TABLE {_qualified(schema, table)} ALTER COLUMN "
                    f"{_ident(column)} DROP DEFAULT")

        case AddConstraint(table=table, constraint=constraint):
            return (f"ALTER TABLE {_qualified(schema, table)} ADD CONSTRAINT "
                    f"{_ident(constraint.name)} {constraint.definition}")

        case DropConstraint(table=table, constraint=constraint):
            return f"ALTER TABLE {_qualified(schema, table)} DROP CONSTRAINT {_ident(constraint)}"

        case CreateIndex(index=index):
            return index.definition

        case DropIndex(index=index):
            return f"DROP INDEX {_qualified(schema, index)}"

        case _:
            raise TypeError(
                f"ddl.render: no renderer for change type {type(change).__name__!r}"
            )
