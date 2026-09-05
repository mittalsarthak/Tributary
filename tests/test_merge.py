"""Tests for Task 7's three-way merge engine.

One test per conflict class from the task brief, plus (fix round 1, after
code review):

- an explicit "exactly one conflict" test for table-level suppression (a
  disagreement one level up must not also be reported once per column
  underneath it -- see `merge.py`'s module docstring for why a 31-conflict
  report for one disagreement is unusable),
- constraint- and index-path coverage: every test above this point exercises
  only columns, so a constraint-added-on-one-side-only and an
  index-modified-differently-on-both-sides case are covered separately, in
  both directions of `resolve`, so a `Constraint`/`Index` reconstruction bug
  in `_build_snapshot`/`_table_from_dict` would actually be caught,
- a direct test of table-level `resolve()` in both directions -- the
  write-side counterpart of the table-level suppression logic, verified by
  actual column comparison, not just presence/absence of the table key,
- a merge-base test against a real Postgres-backed commit DAG (via the `ws`
  fixture from `conftest.py`), since `merge_base` walks real commit rows,
  plus a divergent-histories case asserting `None` rather than an exception.

`Change`, `Table`, and `Snapshot` are unhashable at runtime (frozen
dataclasses with `dict` fields) -- nothing here puts one in a `set()` or uses
one as a dict key; `ObjectPath` string tuples are what get hashed.
"""

import pytest
from psycopg.types.json import Jsonb

from tributary import store
from tributary.merge import merge_base, three_way, resolve
from tributary.model import Column, Constraint, Index, Snapshot, Table


def snap(**coltypes):
    return Snapshot({"users": Table("users", columns={
        c: Column(c, t, True, None, i + 1)
        for i, (c, t) in enumerate(coltypes.items())
    })})


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


def test_table_level_conflict_is_reported_exactly_once():
    """A 30-column table dropped on one side and modified on the other must
    surface as exactly one conflict (on the table path itself), never one
    conflict per column underneath it -- see the "two design details that
    matter" section of merge.py's module docstring.
    """
    wide_columns = {f"c{i}": Column(f"c{i}", "int8", True, None, i) for i in range(30)}
    base   = Snapshot({"users": Table("users", columns=dict(wide_columns))})
    ours   = Snapshot({})
    theirs = Snapshot({"users": Table("users", columns={
        **wide_columns,
        "extra": Column("extra", "text", True, None, 31),
    })})
    r = three_way(base, ours, theirs)
    assert len(r.conflicts) == 1
    assert r.conflicts[0].kind == "drop/modify"
    assert r.conflicts[0].path == ("users",)


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


def test_merge_base_finds_the_lowest_common_ancestor(ws):
    root = store.commit(ws, "main", "root", ops=[])
    b = store.create_branch(ws, "feature-x")
    ws.execute(f'ALTER TABLE "{b.schema_name}".users ADD COLUMN a text')
    ours = store.commit(ws, "feature-x", "ours", ops=[])
    ws.execute("ALTER TABLE main.users ADD COLUMN b text")
    theirs = store.commit(ws, "main", "theirs", ops=[])
    assert merge_base(ws, ours, theirs) == root


# --- fix round 1: constraint/index paths ------------------------------------
# Every test above exercises only columns. `object_map` and `_build_snapshot`
# treat "con"/"idx" paths through the same machinery as "col" paths, but
# nothing above actually proves that -- a `Constraint`/`Index` reconstruction
# bug in `_table_from_dict` (e.g. dropping the `columns` tuple, or forgetting
# `unique`/`method`/`predicate`) would currently go unnoticed.

def test_constraint_added_on_one_side_only_auto_merges():
    pk = Constraint(name="users_pkey", kind="p", definition="PRIMARY KEY (id)", columns=("id",))
    base = Snapshot({"users": Table("users", columns={"id": Column("id", "int8", True, None, 1)})})
    ours = Snapshot({"users": Table(
        "users",
        columns={"id": Column("id", "int8", True, None, 1)},
        constraints={"users_pkey": pk},
    )})
    theirs = base
    r = three_way(base, ours, theirs)
    assert r.conflicts == []
    assert r.merged.tables["users"].constraints["users_pkey"] == pk


def test_index_modified_differently_on_both_sides_conflicts_and_resolves():
    base_idx = Index(name="ix_users_email", definition="ix on (email)", columns=("email",))
    ours_idx = Index(
        name="ix_users_email", definition="unique ix on (email)", columns=("email",), unique=True,
    )
    theirs_idx = Index(
        name="ix_users_email", definition="ix on (email, created_at)", columns=("email", "created_at"),
    )

    def with_index(idx: Index) -> Snapshot:
        return Snapshot({"users": Table(
            "users",
            columns={"id": Column("id", "int8", True, None, 1)},
            indexes={"ix_users_email": idx},
        )})

    base, ours, theirs = with_index(base_idx), with_index(ours_idx), with_index(theirs_idx)
    r = three_way(base, ours, theirs)
    assert [c.kind for c in r.conflicts] == ["modify/modify"]
    assert r.conflicts[0].path == ("users", "idx", "ix_users_email")

    kept_theirs = resolve(r, {"users/idx/ix_users_email": "theirs"})
    assert kept_theirs.tables["users"].indexes["ix_users_email"] == theirs_idx

    kept_ours = resolve(r, {"users/idx/ix_users_email": "ours"})
    assert kept_ours.tables["users"].indexes["ix_users_email"] == ours_idx


# --- fix round 1: table-level resolve() --------------------------------------
# Table-level suppression (build side) already has a dedicated test; this is
# its write-side counterpart -- resolving a table-root `drop/modify` conflict
# in both directions, verified by actual column comparison rather than mere
# presence/absence of the table key.

def test_resolve_table_level_conflict_both_directions():
    base = Snapshot({"users": Table("users", columns={
        "id": Column("id", "int8", True, None, 1),
        "x": Column("x", "text", True, None, 2),
    })})
    ours = Snapshot({})
    theirs = Snapshot({"users": Table("users", columns={
        "id": Column("id", "int8", True, None, 1),
        "x": Column("x", "text", True, None, 2),
        "y": Column("y", "int4", True, None, 3),
    })})
    r = three_way(base, ours, theirs)
    assert [c.kind for c in r.conflicts] == ["drop/modify"]

    dropped = resolve(r, {"users": "ours"})
    assert "users" not in dropped.tables

    kept = resolve(r, {"users": "theirs"})
    assert set(kept.tables["users"].columns) == {"id", "x", "y"}
    assert kept.tables["users"].columns["id"] == Column("id", "int8", True, None, 1)
    assert kept.tables["users"].columns["x"] == Column("x", "text", True, None, 2)
    assert kept.tables["users"].columns["y"] == Column("y", "int4", True, None, 3)


# --- fix round 1: merge_base on divergent histories --------------------------
# The branch that matters when someone merges across genuinely unrelated
# histories: returning `None` versus raising is a contract Task 11 depends on.
# A second, wholly disconnected commit is fabricated directly via
# `_tributary.branches`/`commits` rows (bypassing `store.create_branch`, which
# always links a new branch to an existing one's HEAD) so its ancestor chain
# shares nothing with `main`'s.

def test_merge_base_returns_none_for_disjoint_histories(ws):
    root = store.commit(ws, "main", "root", ops=[])
    ours = store.commit(ws, "main", "ours", ops=[])

    row = ws.execute(
        "INSERT INTO _tributary.branches (name, schema_name) "
        "VALUES ('unrelated', 'unrelated_ghost_schema') RETURNING id"
    ).fetchone()
    branch_id = row[0]
    crow = ws.execute(
        "INSERT INTO _tributary.commits (branch_id, parent_id, message, author, snapshot, ops) "
        "VALUES (%s, NULL, %s, %s, %s, %s) RETURNING id",
        (branch_id, "unrelated root", "system", Jsonb(Snapshot({}).to_json()), Jsonb([])),
    ).fetchone()
    theirs = str(crow[0])

    assert merge_base(ws, ours, theirs) is None
