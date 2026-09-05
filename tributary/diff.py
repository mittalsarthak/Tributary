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

Column renames are resolved two ways, in order of trust:

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

Table renames (R16) are the same failure one level up -- a renamed table
read as `DROP TABLE` + `CREATE TABLE` destroys every row in it, not just one
column's worth of data. `{"op": "rename_table", "old": ..., "new": ...}` in
the op log is honoured the same authoritative way column op-log renames
are: emit `RenameTable` instead of `DropTable`/`CreateTable`, then diff the
two tables' contents against each other under their real identities so a
commit that renames a table *and* changes its columns produces `RenameTable`
plus the column changes, not `RenameTable` plus a spurious full recreate.

Deliberately **no heuristic for table renames**, unlike columns. For a
column, a wrong guess costs one column's data. For a table, a wrong guess
either destroys an entire table that should have been renamed, or renames a
table that should have been dropped -- and there is no cheap way to be
confident two tables are "the same table" from shape alone the way there is
for a single scalar column: two unrelated tables with an identical column
set are common in real schemas (e.g. `orders` and `returns` might both be
`(id int8, created_at timestamptz)`), so a type/shape match carries far
less evidence at table granularity than it does at column granularity.
Op-log intent is the only signal trusted here. This asymmetry with the
column heuristic is deliberate, not an oversight.

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
    RenameTable,
    SetDefault,
    SetNotNull,
    Snapshot,
    Table,
)


def diff(old: Snapshot, new: Snapshot, ops: list[dict] | None = None) -> list[Change]:
    """Compute the list of `Change`s that turn `old` into `new`.

    `ops` is an optional op log (see module docstring for the entry shape).
    Only `"rename_column"` and `"rename_table"` entries are consulted;
    anything else is ignored so this stays forward-compatible with op kinds
    introduced by later tasks that do not concern renames.
    """
    ops = ops or []
    op_column_renames = _op_log_column_renames(ops)
    op_table_renames = _op_log_table_renames(ops)

    drop_indexes: list[Change] = []
    drop_constraints: list[Change] = []
    table_structure: list[Change] = []
    column_changes: list[Change] = []
    add_constraints: list[Change] = []
    create_indexes: list[Change] = []

    old_names = set(old.tables)
    new_names = set(new.tables)

    # Table renames: op-log only (see module docstring for why there is no
    # heuristic fallback here). A matched pair is removed from both
    # candidate pools so it is never *also* reported as a drop and a create.
    renamed_pairs: list[tuple[str, str]] = []
    for old_name, new_name in op_table_renames:
        if old_name in old_names and new_name in new_names:
            table_structure.append(RenameTable(old_name, new_name))
            renamed_pairs.append((old_name, new_name))
            old_names.discard(old_name)
            new_names.discard(new_name)

    for name in sorted(old_names - new_names):
        table_structure.append(DropTable(name))

    for name in sorted(new_names - old_names):
        table_structure.append(CreateTable(new.tables[name]))

    # Every table pair (renamed or not) still gets its contents diffed --
    # a table can be renamed *and* have its columns/constraints/indexes
    # changed in the same commit.
    table_pairs = renamed_pairs + [(name, name) for name in sorted(old_names & new_names)]

    for old_name, new_name in table_pairs:
        old_t = old.tables[old_name]
        new_t = new.tables[new_name]

        # Column/constraint/index work always executes *after* any table
        # rename above (see the return-order assembly below), so anything
        # emitted here must address the table by its post-rename identity
        # -- except a Drop*, which runs alongside the other pre-rename
        # drops and so must still address the table by its pre-rename name.
        column_changes.extend(_diff_columns(old_name, new_name, old_t, new_t, op_column_renames))

        idx_drops, idx_creates = _diff_indexes(old_name, new_name, old_t, new_t)
        drop_indexes.extend(idx_drops)
        create_indexes.extend(idx_creates)

        con_drops, con_adds = _diff_constraints(old_name, new_name, old_t, new_t)
        drop_constraints.extend(con_drops)
        add_constraints.extend(con_adds)

    return (
        drop_indexes
        + drop_constraints
        + table_structure
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

def _op_log_column_renames(ops: list[dict]) -> dict[tuple[str, str], str]:
    """Index `rename_column` ops by `(table, old_column)` -> `new_column`."""
    renames: dict[tuple[str, str], str] = {}
    for op in ops:
        if op.get("op") == "rename_column":
            renames[(op["table"], op["old"])] = op["new"]
    return renames


def _op_log_table_renames(ops: list[dict]) -> list[tuple[str, str]]:
    """Collect `rename_table` ops as `(old_table, new_table)` pairs.

    No heuristic counterpart exists for this one -- see the module
    docstring for why table renames are trusted from the op log alone.
    """
    return [(op["old"], op["new"]) for op in ops if op.get("op") == "rename_table"]


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
    old_table: str,
    new_table: str,
    old_t: Table,
    new_t: Table,
    op_renames: dict[tuple[str, str], str],
) -> list[Change]:
    """Diff one table's columns. `old_table`/`new_table` are the same string
    unless the table itself was renamed (R16) -- in which case every change
    here is emitted under `new_table`, because column-level work always
    executes after the table rename in the change list this module returns.
    """
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
    # (a rename may change the type in the same commit). Looked up under
    # both the table's old and new name since a table-rename commit's
    # column op may have been logged under either identity.
    for old_col in sorted(dropped):
        new_col = op_renames.get((new_table, old_col)) or op_renames.get((old_table, old_col))
        if new_col is not None and new_col in added:
            pairs.append((old_col, new_col))
            dropped.discard(old_col)
            added.discard(new_col)

    # Whatever the op log didn't cover falls to the conservative heuristic.
    for old_col, new_col in _infer_renames(old_cols, new_cols, dropped, added):
        pairs.append((old_col, new_col))
        dropped.discard(old_col)
        added.discard(new_col)

    changes: list[Change] = []
    for old_col, new_col in sorted(pairs):
        old_c = old_cols[old_col]
        new_c = new_cols[new_col]

        if old_col != new_col:
            changes.append(RenameColumn(new_table, old_col, new_col))

        # Every subsequent comparison addresses the column by its *new*
        # name -- by the time an ALTER COLUMN runs, RENAME COLUMN (ordered
        # ahead of it, see module docstring) has already retargeted it.
        if old_c.type != new_c.type:
            changes.append(AlterColumnType(new_table, new_col, old_c.type, new_c.type))

        if old_c.nullable and not new_c.nullable:
            changes.append(SetNotNull(new_table, new_col))
        elif not old_c.nullable and new_c.nullable:
            changes.append(DropNotNull(new_table, new_col))

        if old_c.default != new_c.default:
            if new_c.default is None:
                changes.append(DropDefault(new_table, new_col))
            else:
                changes.append(SetDefault(new_table, new_col, new_c.default))

    for name in sorted(dropped):
        changes.append(DropColumn(new_table, name))

    for name in sorted(added):
        changes.append(AddColumn(new_table, new_cols[name]))

    return changes


# --- indexes and constraints -------------------------------------------------
# Index and Constraint have no "alter" change type: a modified one (same
# name, different definition) is a drop of the old plus a create of the new.
#
# `old_table`/`new_table` differ only when the table itself was renamed
# (R16). Drops are emitted under `old_table` because DropIndex/DropConstraint
# run in the pre-rename part of the returned change list (the table still
# has its old name at that point); creates/adds are emitted under
# `new_table` because they run in the post-rename part.

def _diff_indexes(old_table: str, new_table: str, old_t: Table, new_t: Table) -> tuple[list[Change], list[Change]]:
    old_idx = old_t.indexes
    new_idx = new_t.indexes

    drops: list[Change] = []
    creates: list[Change] = []

    for name in sorted(set(old_idx) - set(new_idx)):
        drops.append(DropIndex(old_table, name))

    for name in sorted(set(old_idx) & set(new_idx)):
        if old_idx[name] != new_idx[name]:
            drops.append(DropIndex(old_table, name))
            creates.append(CreateIndex(new_table, new_idx[name]))

    for name in sorted(set(new_idx) - set(old_idx)):
        creates.append(CreateIndex(new_table, new_idx[name]))

    return drops, creates


def _diff_constraints(old_table: str, new_table: str, old_t: Table, new_t: Table) -> tuple[list[Change], list[Change]]:
    old_con = old_t.constraints
    new_con = new_t.constraints

    drops: list[Change] = []
    adds: list[Change] = []

    for name in sorted(set(old_con) - set(new_con)):
        drops.append(DropConstraint(old_table, name))

    for name in sorted(set(old_con) & set(new_con)):
        if old_con[name] != new_con[name]:
            drops.append(DropConstraint(old_table, name))
            adds.append(AddConstraint(new_table, new_con[name]))

    for name in sorted(set(new_con) - set(old_con)):
        adds.append(AddConstraint(new_table, new_con[name]))

    return drops, adds
