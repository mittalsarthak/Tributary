"""Structural diff between two canonicalised `Snapshot`s.

The failure this module exists to prevent: a naive diff sees a renamed
column as `DROP a; ADD b`. Run against a populated table, that does not
rename anything -- it destroys the data in `a` and creates an empty `b`
beside it. `RENAME COLUMN` preserves the data and is instant. Telling the
two apart is the whole point of this module.

Snapshots are assumed already canonicalised (`canonical.norm_type` /
`norm_default` have already run, in `introspect.snapshot`) -- this module
never re-normalises anything. Two canonically-equal types or defaults are
already byte-equal strings by the time they reach here, so plain `==` is
correct throughout.

Renames are resolved two ways, in order of trust:

1. **Op log.** An entry `{"op": "rename_column", "table": ..., "old": ...,
   "new": ...}`, written by a human (via the Task 6/11 editor) declaring
   intent explicitly. This is authoritative and is not required to satisfy
   the heuristic below -- a rename can change the column's type in the same
   commit (`email text` -> `email_address varchar(255)`), which the
   heuristic below could never infer, because it matches on identical type.
2. **Heuristic.** With no op log (or for whatever the op log didn't cover),
   an unmatched dropped column pairs with an unmatched added column *only*
   when their canonical type is identical *and* each is the sole unmatched
   candidate of that type on its side. Ambiguity -- two candidates of the
   same type on either side -- means no inference at all: both are reported
   as plain drops/adds. A wrong guess is strictly worse than an honest,
   visible drop-and-add: it either silently destroys data under a
   rename-shaped mask, or (worse) renames the wrong column. The user can
   see and correct a drop-and-add; they cannot un-guess a bad rename that
   already ran.

`Change` (via `CreateTable`), `Table`, and `Snapshot` carry `dict` fields and
so are unhashable at runtime despite being frozen dataclasses -- this module
never puts one in a `set()` or uses one as a dict key. All set arithmetic
below is over column/table/constraint/index *names* (plain strings), with
the actual objects looked up afterwards.

The change list is returned in a base order later consumed by Task 8's
topological planner: index and constraint drops first (an index cannot
outlive the column it references being dropped out from under it), then
table-level creates/drops, then column-level work, then constraint and
index creates last (a `UNIQUE` constraint cannot be added before the column
it constrains exists). This is a sane default, not a proof of validity --
the planner is the one that actually enforces dependency order.
"""

from __future__ import annotations

from collections import defaultdict

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
    SetDefault,
    SetNotNull,
    Snapshot,
    Table,
)


def diff(old: Snapshot, new: Snapshot, ops: list[dict] | None = None) -> list[Change]:
    """Compute the list of `Change`s that turn `old` into `new`.

    `ops` is an optional op log (see module docstring for the entry shape).
    Only `"rename_column"` entries are consulted; anything else is ignored
    so this stays forward-compatible with op kinds introduced by later
    tasks that do not concern column renames.
    """
    op_renames = _op_log_renames(ops or [])

    drop_indexes: list[Change] = []
    drop_constraints: list[Change] = []
    drop_tables: list[Change] = []
    create_tables: list[Change] = []
    column_changes: list[Change] = []
    add_constraints: list[Change] = []
    create_indexes: list[Change] = []

    old_names = set(old.tables)
    new_names = set(new.tables)

    for name in sorted(old_names - new_names):
        drop_tables.append(DropTable(name))

    for name in sorted(new_names - old_names):
        create_tables.append(CreateTable(new.tables[name]))

    for name in sorted(old_names & new_names):
        old_t = old.tables[name]
        new_t = new.tables[name]

        column_changes.extend(_diff_columns(name, old_t, new_t, op_renames))

        idx_drops, idx_creates = _diff_indexes(name, old_t, new_t)
        drop_indexes.extend(idx_drops)
        create_indexes.extend(idx_creates)

        con_drops, con_adds = _diff_constraints(name, old_t, new_t)
        drop_constraints.extend(con_drops)
        add_constraints.extend(con_adds)

    return (
        drop_indexes
        + drop_constraints
        + drop_tables
        + create_tables
        + column_changes
        + add_constraints
        + create_indexes
    )


def detect_renames(old: Snapshot, new: Snapshot) -> list[tuple[str, str, str]]:
    """Infer column renames by structure alone -- no op log involved.

    This is the same conservative heuristic `diff` falls back on when no op
    log is supplied (or for whatever the op log leaves unmatched), exposed
    directly so a caller such as an editor UI can surface a rename
    suggestion to a human *before* it is committed to an op log entry.

    Returns `(table, old_column, new_column)` tuples. Ambiguous same-type
    candidates are never guessed at -- see the module docstring.
    """
    result: list[tuple[str, str, str]] = []
    for table_name in sorted(set(old.tables) & set(new.tables)):
        old_t = old.tables[table_name]
        new_t = new.tables[table_name]
        dropped = set(old_t.columns) - set(new_t.columns)
        added = set(new_t.columns) - set(old_t.columns)
        for old_name, new_name in _infer_renames(old_t.columns, new_t.columns, dropped, added):
            result.append((table_name, old_name, new_name))
    return result


# --- op log ----------------------------------------------------------------

def _op_log_renames(ops: list[dict]) -> dict[tuple[str, str], str]:
    """Index `rename_column` ops by `(table, old_column)` -> `new_column`."""
    renames: dict[tuple[str, str], str] = {}
    for op in ops:
        if op.get("op") == "rename_column":
            renames[(op["table"], op["old"])] = op["new"]
    return renames


# --- columns -----------------------------------------------------------------

def _infer_renames(
    old_cols: dict[str, Column],
    new_cols: dict[str, Column],
    dropped: set[str],
    added: set[str],
) -> list[tuple[str, str]]:
    """Pair up names from `dropped` and `added` as renames, conservatively.

    A pair is formed only when the canonical type is identical *and* it is
    the only candidate pair of that type on both sides. Two dropped columns
    and two added columns sharing a type are never paired, even though the
    types "line up" -- which one renamed to which is genuinely unknowable
    from structure alone, and guessing is worse than reporting the honest
    drop-and-add.
    """
    by_type_dropped: dict[str, list[str]] = defaultdict(list)
    for name in dropped:
        by_type_dropped[old_cols[name].type].append(name)

    by_type_added: dict[str, list[str]] = defaultdict(list)
    for name in added:
        by_type_added[new_cols[name].type].append(name)

    pairs: list[tuple[str, str]] = []
    for coltype in sorted(by_type_dropped):
        old_candidates = by_type_dropped[coltype]
        new_candidates = by_type_added.get(coltype, [])
        if len(old_candidates) == 1 and len(new_candidates) == 1:
            pairs.append((old_candidates[0], new_candidates[0]))
    return pairs


def _diff_columns(
    table_name: str,
    old_t: Table,
    new_t: Table,
    op_renames: dict[tuple[str, str], str],
) -> list[Change]:
    old_cols = old_t.columns
    new_cols = new_t.columns

    dropped = set(old_cols) - set(new_cols)
    added = set(new_cols) - set(old_cols)
    common = set(old_cols) & set(new_cols)

    # (old_name, new_name) pairs identifying "the same logical column" --
    # same name for anything untouched, differing names for a rename.
    pairs: list[tuple[str, str]] = [(name, name) for name in common]

    # Op log renames are authoritative and consume from the unmatched pool
    # first -- they do not need to satisfy the type-match heuristic below
    # (a rename may change the type in the same commit).
    for old_name in sorted(dropped):
        new_name = op_renames.get((table_name, old_name))
        if new_name is not None and new_name in added:
            pairs.append((old_name, new_name))
            dropped.discard(old_name)
            added.discard(new_name)

    # Whatever the op log didn't cover falls to the conservative heuristic.
    for old_name, new_name in _infer_renames(old_cols, new_cols, dropped, added):
        pairs.append((old_name, new_name))
        dropped.discard(old_name)
        added.discard(new_name)

    changes: list[Change] = []
    for old_name, new_name in sorted(pairs):
        old_c = old_cols[old_name]
        new_c = new_cols[new_name]

        if old_name != new_name:
            changes.append(RenameColumn(table_name, old_name, new_name))

        # Every subsequent comparison addresses the column by its *new*
        # name -- by the time an ALTER COLUMN runs, RENAME COLUMN (ordered
        # ahead of it, see module docstring) has already retargeted it.
        if old_c.type != new_c.type:
            changes.append(AlterColumnType(table_name, new_name, old_c.type, new_c.type))

        if old_c.nullable and not new_c.nullable:
            changes.append(SetNotNull(table_name, new_name))
        elif not old_c.nullable and new_c.nullable:
            changes.append(DropNotNull(table_name, new_name))

        if old_c.default != new_c.default:
            if new_c.default is None:
                changes.append(DropDefault(table_name, new_name))
            else:
                changes.append(SetDefault(table_name, new_name, new_c.default))

    for name in sorted(dropped):
        changes.append(DropColumn(table_name, name))

    for name in sorted(added):
        changes.append(AddColumn(table_name, new_cols[name]))

    return changes


# --- indexes and constraints -------------------------------------------------
# Index and Constraint have no "alter" change type: a modified one (same
# name, different definition) is a drop of the old plus a create of the new.

def _diff_indexes(table_name: str, old_t: Table, new_t: Table) -> tuple[list[Change], list[Change]]:
    old_idx = old_t.indexes
    new_idx = new_t.indexes

    drops: list[Change] = []
    creates: list[Change] = []

    for name in sorted(set(old_idx) - set(new_idx)):
        drops.append(DropIndex(table_name, name))

    for name in sorted(set(old_idx) & set(new_idx)):
        if old_idx[name] != new_idx[name]:
            drops.append(DropIndex(table_name, name))
            creates.append(CreateIndex(table_name, new_idx[name]))

    for name in sorted(set(new_idx) - set(old_idx)):
        creates.append(CreateIndex(table_name, new_idx[name]))

    return drops, creates


def _diff_constraints(table_name: str, old_t: Table, new_t: Table) -> tuple[list[Change], list[Change]]:
    old_con = old_t.constraints
    new_con = new_t.constraints

    drops: list[Change] = []
    adds: list[Change] = []

    for name in sorted(set(old_con) - set(new_con)):
        drops.append(DropConstraint(table_name, name))

    for name in sorted(set(old_con) & set(new_con)):
        if old_con[name] != new_con[name]:
            drops.append(DropConstraint(table_name, name))
            adds.append(AddConstraint(table_name, new_con[name]))

    for name in sorted(set(new_con) - set(old_con)):
        adds.append(AddConstraint(table_name, new_con[name]))

    return drops, adds
