"""Tests for Task 7's three-way merge engine.

One test per conflict class from the task brief, plus:

- an explicit "exactly one conflict" test for table-level suppression (a
  disagreement one level up must not also be reported once per column
  underneath it -- see `merge.py`'s module docstring for why a 31-conflict
  report for one disagreement is unusable), and
- a merge-base test against a real Postgres-backed commit DAG (via the `ws`
  fixture from `conftest.py`), since `merge_base` walks real commit rows.

`Change`, `Table`, and `Snapshot` are unhashable at runtime (frozen
dataclasses with `dict` fields) -- nothing here puts one in a `set()` or uses
one as a dict key; `ObjectPath` string tuples are what get hashed.
"""

import pytest

from tributary import store
from tributary.merge import merge_base, three_way, resolve
from tributary.model import Snapshot, Table, Column


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
