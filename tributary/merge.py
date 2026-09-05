"""Three-way merge of two diverged branch `Snapshot`s against their common
ancestor.

Merges are the point where two people's independent DDL history has to be
reconciled into one schema that then gets *executed against a real,
populated table*. A merge that silently resolves a real disagreement --
picking one side because it happened to iterate first, say -- is worse than
one that refuses, because the person whose change was discarded finds out
from the data, not from a warning.

**Object maps, not change lists.** Replaying and reconciling two *change
lists* (from `diff.py`) was considered and rejected: change lists are
order-dependent, and a rename on one side against a modify on the other is
close to impossible to reconcile correctly from an ordered sequence of
edits. Instead, each of `base`/`ours`/`theirs` is flattened to a map keyed by
`ObjectPath` (`object_map`), and every path is classified independently by
comparing the three sides' values at that path. This is order-independent,
and every conflict class in the classification table below falls directly
out of one three-way string/dict comparison.

Object paths: `("users",)` the table itself, `("users", "col", "email")`,
`("users", "con", "users_pkey")`, `("users", "idx", "ix_users_email")`.

Classification per path (`ours`/`theirs` relative to `base`):

| ours               | theirs             | result                        |
|--------------------|--------------------|-------------------------------|
| changed            | unchanged          | take ours                     |
| unchanged          | changed            | take theirs                   |
| changed identically|                    | no-op, not a conflict         |
| changed differently|                    | conflict: modify/modify       |
| dropped            | modified           | conflict: drop/modify         |
| dropped            | dropped            | no-op, not a conflict         |
| added              | added, different   | conflict: add/add             |

**Two design details that matter:**

1. **A table-root path carries only the table's own identity**
   (`{"name": ...}`), never its columns/constraints/indexes. If it carried
   full contents, *every* column-level change would also register as a
   change to the table path, which would make every column edit conflict
   with any other side's column edit at the table level too -- on top of
   whatever the column-level classification already (correctly) decided.
   Table presence/absence has to be judged independently of what changed
   inside it; `_classify_table` below does that by comparing the raw
   `Table` objects only when presence itself disagrees between the two
   sides (see its docstring).

2. **A conflict at a table path suppresses reporting of conflicts beneath
   it.** If one side drops a 30-column table and the other modifies it, the
   honest report is *one* `drop/modify` conflict on `("users",)` -- not that
   conflict plus 30 more underneath for each column. A UI listing 31
   conflicts for a single disagreement is unusable: the user cannot tell
   which one is the real decision. `three_way` classifies every table-root
   path *first*, and any table found to conflict has every path beneath it
   skipped entirely in the second pass.

`Change`, `Table`, and `Snapshot` carry `dict` fields and are unhashable at
runtime despite being frozen dataclasses -- nothing in this module puts one
in a `set()` or uses one as a dict key. `ObjectPath` (`tuple[str, ...]`) is
what gets hashed, as map keys throughout. Table/Column/Constraint/Index
values *are* compared with plain `==`, which only needs equality, not
hashability.

Snapshots arrive already canonicalised (see `diff.py`'s docstring for the
same ruling) -- nothing here re-normalises a type or a default; values are
compared as stored.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from tributary import store
from tributary.model import Column, Conflict, Constraint, Index, ObjectPath, Snapshot, Table

_CTOR_BY_KIND = {"col": Column, "con": Constraint, "idx": Index}


def object_map(snap: Snapshot) -> dict[ObjectPath, dict]:
    """Flatten `snap` to `{ObjectPath: value}` at table/column/constraint/index
    granularity, for order-independent three-way comparison.

    The table-root entry (`(name,)`) is deliberately identity-only -- see
    the module docstring, design detail 1 -- so it never reacts to a change
    in the table's contents, only to the table itself appearing or not.
    """
    m: dict[ObjectPath, dict] = {}
    for tname, table in snap.tables.items():
        m[(tname,)] = {"name": table.name}
        for cname, col in table.columns.items():
            m[(tname, "col", cname)] = asdict(col)
        for cname, con in table.constraints.items():
            m[(tname, "con", cname)] = asdict(con)
        for iname, idx in table.indexes.items():
            m[(tname, "idx", iname)] = asdict(idx)
    return m


@dataclass(frozen=True)
class MergeResult:
    merged: Snapshot
    conflicts: list[Conflict] = field(default_factory=list)


# --- classification --------------------------------------------------------

_CONFLICT = object()  # sentinel: distinct from any real value, including None


def _classify(base_val, ours_val, theirs_val):
    """Classify one child (column/constraint/index) path's three values.

    Returns the winning value (a dict, or `None` meaning "absent/dropped in
    the merge result") or the `_CONFLICT` sentinel. This is exactly the
    table in the module docstring: identical values on both sides (whether
    that's "both changed the same way" or "both dropped it") never
    conflict; a change on exactly one side always wins outright, dropped or
    not; anything left over -- both sides touched it, and disagree -- is a
    conflict.
    """
    if ours_val == theirs_val:
        return ours_val
    if ours_val == base_val:
        return theirs_val
    if theirs_val == base_val:
        return ours_val
    return _CONFLICT


def _child_conflict_kind(base_val, ours_val, theirs_val) -> str:
    if base_val is None:
        return "add/add"
    if ours_val is None or theirs_val is None:
        return "drop/modify"
    return "modify/modify"


def _classify_table(name: str, base: Snapshot, ours: Snapshot, theirs: Snapshot) -> tuple[str | None, bool]:
    """Decide a table's presence in the merge, independent of its contents.

    Returns `(conflict_kind, present)`. `conflict_kind` is `None` unless the
    table itself is in dispute, in which case it is always `"drop/modify"`
    -- the only way a table-root path can conflict, given design detail 1,
    is one side dropping the table outright while the other side kept it
    *and changed something underneath it* (an "add/add" at the table root
    would require the table's own value to differ between two adds, but
    that value is identity-only, so two adds of the same name are never
    distinguishable at this level -- any real disagreement there falls out
    at the column/constraint/index level instead).

    When `conflict_kind` is `None`, `present` says whether the table
    survives into the merged snapshot; its actual contents are assembled
    separately in `three_way`, from whichever child paths resolve without
    conflict.
    """
    b = base.tables.get(name)
    o = ours.tables.get(name)
    t = theirs.tables.get(name)

    if (o is not None) == (t is not None):
        # Presence agrees on both sides -- both keep it, both dropped it, or
        # neither side ever had it. Any content disagreement is exclusively
        # a matter for the column/constraint/index paths below.
        return None, o is not None

    # Exactly one side still has the table; the other dropped it.
    surviving = o if o is not None else t
    if b is None:
        # Never existed in base either -- one side simply added it.
        return None, True
    if surviving == b:
        # The surviving side never touched it -- a clean, uncontested drop.
        return None, False
    # The side that kept it also changed it, while the other deleted it
    # outright. Neither outcome is safe to pick silently.
    return "drop/modify", False


def _full_table(snap: Snapshot, name: str) -> dict | None:
    """The *full* (not identity-only) dict for a table, for a table-level
    `Conflict`'s payload -- `resolve` needs the whole structure to
    reconstruct whichever side gets chosen, not just its name.
    """
    table = snap.tables.get(name)
    return asdict(table) if table is not None else None


def three_way(base: Snapshot, ours: Snapshot, theirs: Snapshot) -> MergeResult:
    """Merge `ours` and `theirs`, both descended from `base`.

    Pass 1 classifies every table-root path. Any table found to conflict is
    recorded once (payload: the full table on each side, for `resolve`) and
    added to `conflicted_tables`; every path beneath it is then skipped
    entirely in pass 2 (design detail 2 -- one conflict, not one plus N).

    Pass 2 classifies every remaining column/constraint/index path via
    `object_map` + `_classify`.

    `resolved` accumulates every non-conflicting path's winning value
    (`None` meaning "absent"), from both passes, and is assembled into the
    merged `Snapshot` by `_build_snapshot`.
    """
    conflicts: list[Conflict] = []
    resolved: dict[ObjectPath, dict | None] = {}
    conflicted_tables: set[str] = set()

    table_names = set(base.tables) | set(ours.tables) | set(theirs.tables)
    for name in sorted(table_names):
        kind, present = _classify_table(name, base, ours, theirs)
        path: ObjectPath = (name,)
        if kind is not None:
            conflicts.append(Conflict(
                path=path,
                kind=kind,
                base=_full_table(base, name),
                ours=_full_table(ours, name),
                theirs=_full_table(theirs, name),
            ))
            conflicted_tables.add(name)
        else:
            resolved[path] = {"name": name} if present else None

    base_map, ours_map, theirs_map = object_map(base), object_map(ours), object_map(theirs)
    child_paths = {p for p in set(base_map) | set(ours_map) | set(theirs_map) if len(p) > 1}

    for path in sorted(child_paths):
        if path[0] in conflicted_tables:
            continue  # suppressed: the table-level conflict already covers it
        b, o, t = base_map.get(path), ours_map.get(path), theirs_map.get(path)
        outcome = _classify(b, o, t)
        if outcome is _CONFLICT:
            conflicts.append(Conflict(path=path, kind=_child_conflict_kind(b, o, t), base=b, ours=o, theirs=t))
        else:
            resolved[path] = outcome

    return MergeResult(merged=_build_snapshot(resolved), conflicts=conflicts)


def _build_snapshot(resolved: dict[ObjectPath, dict | None]) -> Snapshot:
    """Assemble a `Snapshot` from every non-conflicting path's winning value.

    A table appears at all only if its table-root path resolved present;
    within a surviving table, each child path whose value isn't `None`
    reconstructs the corresponding `Column`/`Constraint`/`Index`.
    """
    table_names = {path[0] for path, val in resolved.items() if len(path) == 1 and val is not None}
    tables: dict[str, Table] = {name: Table(name=name) for name in table_names}

    for path, val in resolved.items():
        if len(path) == 1 or val is None:
            continue
        tname, kind, oname = path
        if tname not in tables:
            continue
        target = {"col": tables[tname].columns, "con": tables[tname].constraints, "idx": tables[tname].indexes}[kind]
        target[oname] = _CTOR_BY_KIND[kind](**val)

    return Snapshot(tables=tables)


# --- resolve -----------------------------------------------------------------

def resolve(result: MergeResult, choices: dict[str, str]) -> Snapshot:
    """Apply human choices to every conflict in `result`, returning the
    final `Snapshot`.

    `choices` keys are `"/".join(path)`; values are `"ours"` or `"theirs"`.
    Every conflict must have a choice -- silently defaulting an unresolved
    conflict to one side is exactly the failure this engine exists to
    prevent, so a missing choice raises `ValueError` rather than picking
    anything.
    """
    missing = sorted("/".join(c.path) for c in result.conflicts if "/".join(c.path) not in choices)
    if missing:
        raise ValueError(f"unresolved conflicts, no choice given for: {', '.join(missing)}")

    tables: dict[str, Table] = {
        name: Table(name=t.name, columns=dict(t.columns), constraints=dict(t.constraints), indexes=dict(t.indexes))
        for name, t in result.merged.tables.items()
    }

    for conflict in result.conflicts:
        key = "/".join(conflict.path)
        side = choices[key]
        if side not in ("ours", "theirs"):
            raise ValueError(f"invalid choice {side!r} for {key!r}: expected 'ours' or 'theirs'")
        chosen = conflict.ours if side == "ours" else conflict.theirs
        tname = conflict.path[0]

        if len(conflict.path) == 1:
            if chosen is None:
                tables.pop(tname, None)
            else:
                tables[tname] = _table_from_dict(chosen)
            continue

        _, kind, oname = conflict.path
        table = tables.setdefault(tname, Table(name=tname))
        target = {"col": table.columns, "con": table.constraints, "idx": table.indexes}[kind]
        if chosen is None:
            target.pop(oname, None)
        else:
            target[oname] = _CTOR_BY_KIND[kind](**chosen)

    return Snapshot(tables=tables)


def _table_from_dict(d: dict) -> Table:
    """Reconstruct a full `Table` from a table-level `Conflict`'s payload
    (the `asdict(Table)` a chosen side carries -- see `_full_table`).
    """
    return Table(
        name=d["name"],
        columns={k: Column(**v) for k, v in d.get("columns", {}).items()},
        constraints={k: Constraint(**v) for k, v in d.get("constraints", {}).items()},
        indexes={k: Index(**v) for k, v in d.get("indexes", {}).items()},
    )


# --- merge_base ----------------------------------------------------------

def merge_base(conn, ours_head: str, theirs_head: str) -> str | None:
    """Find the lowest common ancestor of two branch heads.

    Intersects `store.ancestors` of both heads and returns the first element
    of the ours-ordered list (newest-first) present in both -- i.e. the most
    recent commit both histories share. Returns `None` when there is no
    common ancestor (two independent histories) rather than raising --
    callers decide what a merge with no common ancestor means, this
    function only answers the DAG question.
    """
    ours_chain = store.ancestors(conn, ours_head)
    theirs_chain = set(store.ancestors(conn, theirs_head))
    for cid in ours_chain:
        if cid in theirs_chain:
            return cid
    return None
