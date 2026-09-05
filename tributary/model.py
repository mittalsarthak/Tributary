from __future__ import annotations

from dataclasses import dataclass, field
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
    definition: str       # from pg_get_constraintdef
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Index:
    name: str
    definition: str       # from pg_get_indexdef
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

    def to_json(self) -> dict:
        return {
            "tables": {
                tname: _table_to_json(table)
                for tname, table in sorted(self.tables.items())
            }
        }

    @classmethod
    def from_json(cls, d: dict) -> "Snapshot":
        return cls(
            tables={
                tname: _table_from_json(tdata)
                for tname, tdata in d["tables"].items()
            }
        )


def _table_to_json(table: Table) -> dict:
    return {
        "name": table.name,
        "columns": {
            cname: _column_to_json(col)
            for cname, col in sorted(table.columns.items())
        },
        "constraints": {
            cname: _constraint_to_json(con)
            for cname, con in sorted(table.constraints.items())
        },
        "indexes": {
            iname: _index_to_json(idx)
            for iname, idx in sorted(table.indexes.items())
        },
    }


def _table_from_json(d: dict) -> Table:
    return Table(
        name=d["name"],
        columns={cname: _column_from_json(cd) for cname, cd in d["columns"].items()},
        constraints={cname: _constraint_from_json(cd) for cname, cd in d["constraints"].items()},
        indexes={iname: _index_from_json(idata) for iname, idata in d["indexes"].items()},
    )


def _column_to_json(col: Column) -> dict:
    return {
        "name": col.name,
        "type": col.type,
        "nullable": col.nullable,
        "default": col.default,
        "position": col.position,
    }


def _column_from_json(d: dict) -> Column:
    return Column(
        name=d["name"],
        type=d["type"],
        nullable=d["nullable"],
        default=d["default"],
        position=d["position"],
    )


def _constraint_to_json(con: Constraint) -> dict:
    return {
        "name": con.name,
        "kind": con.kind,
        "definition": con.definition,
        "columns": list(con.columns),
    }


def _constraint_from_json(d: dict) -> Constraint:
    return Constraint(
        name=d["name"],
        kind=d["kind"],
        definition=d["definition"],
        columns=tuple(d["columns"]),
    )


def _index_to_json(idx: Index) -> dict:
    return {
        "name": idx.name,
        "definition": idx.definition,
        "columns": list(idx.columns),
        "unique": idx.unique,
        "method": idx.method,
        "predicate": idx.predicate,
    }


def _index_from_json(d: dict) -> Index:
    return Index(
        name=d["name"],
        definition=d["definition"],
        columns=tuple(d["columns"]),
        unique=d["unique"],
        method=d["method"],
        predicate=d["predicate"],
    )


# --- changes -------------------------------------------------------------
@dataclass(frozen=True)
class CreateTable:
    table: Table


@dataclass(frozen=True)
class DropTable:
    table: str


@dataclass(frozen=True)
class RenameTable:
    old: str
    new: str


@dataclass(frozen=True)
class AddColumn:
    table: str
    column: Column


@dataclass(frozen=True)
class DropColumn:
    table: str
    column: str


@dataclass(frozen=True)
class RenameColumn:
    table: str
    old: str
    new: str


@dataclass(frozen=True)
class AlterColumnType:
    table: str
    column: str
    old_type: str
    new_type: str


@dataclass(frozen=True)
class SetNotNull:
    table: str
    column: str


@dataclass(frozen=True)
class DropNotNull:
    table: str
    column: str


@dataclass(frozen=True)
class SetDefault:
    table: str
    column: str
    default: str


@dataclass(frozen=True)
class DropDefault:
    table: str
    column: str


@dataclass(frozen=True)
class AddConstraint:
    table: str
    constraint: Constraint


@dataclass(frozen=True)
class DropConstraint:
    table: str
    constraint: str


@dataclass(frozen=True)
class CreateIndex:
    table: str
    index: Index


@dataclass(frozen=True)
class DropIndex:
    table: str
    index: str


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
    kind: str             # ddl | validate | index_concurrent | backfill | swap | preflight
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
    kind: str              # modify/modify | drop/modify | add/add | rename/modify
    base: dict | None
    ours: dict | None
    theirs: dict | None
