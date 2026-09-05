import pytest
from tributary import store
from tributary.introspect import snapshot


# --- brief's required tests (verbatim) --------------------------------------

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


# --- R3: ensure_main must be idempotent, and that guarantee needs a test ----
#
# The `ws` fixture already calls `store.ensure_main(conn)` once at setup; this
# test calls it again and checks that the second call left no trace -- no
# second `main` branch row, and no new commit (HEAD unchanged). Without a
# regression test for this, a future refactor of the check-then-act logic
# could silently reintroduce a duplicate-registration bug.

def test_ensure_main_is_idempotent(ws):
    first = store.ensure_main(ws)
    second = store.ensure_main(ws)

    main_branches = [b for b in store.list_branches(ws) if b.name == "main"]
    assert len(main_branches) == 1
    assert first.head_commit == second.head_commit


# --- delete_branch ------------------------------------------------------------

def test_delete_branch_removes_the_branch_row_and_its_schema(ws):
    b = store.create_branch(ws, "throwaway")
    store.delete_branch(ws, "throwaway")

    assert all(branch.name != "throwaway" for branch in store.list_branches(ws))
    (schema_exists,) = ws.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)",
        (b.schema_name,),
    ).fetchone()
    assert schema_exists is False


def test_delete_branch_refuses_to_delete_main(ws):
    with pytest.raises(ValueError):
        store.delete_branch(ws, "main")

    main_branches = [b for b in store.list_branches(ws) if b.name == "main"]
    assert len(main_branches) == 1
    (schema_exists,) = ws.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'main')"
    ).fetchone()
    assert schema_exists is True


# --- R15: the mandatory test -------------------------------------------------
#
# Materialising a branch that silently wrote its objects into `main` instead
# of the new branch schema would still make `test_branch_materialises_index_
# and_fk` (the "(a) half") pass -- the index and FK *would* exist somewhere,
# just in the wrong schema, and `main` would now also carry them. The only
# assertion that actually catches that catastrophic case is checking that
# `main`'s own index and constraint sets are byte-for-byte unchanged after
# branching off of it. Both halves live in one test so neither can be
# accidentally dropped later without the test name flagging exactly what
# guarantee was lost.

def test_branch_materialises_index_and_fk_and_leaves_main_completely_unchanged(ws):
    ws.execute(
        "CREATE TABLE main.posts ("
        "  id bigserial PRIMARY KEY,"
        "  author_id bigint REFERENCES main.users(id)"
        ")"
    )
    ws.execute("CREATE INDEX ix_users_email ON main.users (email)")
    store.commit(ws, "main", "add posts table, fk, and standalone index", ops=[])

    main_before = snapshot(ws, "main")
    indexes_before = {tname: set(t.indexes) for tname, t in main_before.tables.items()}
    constraints_before = {tname: set(t.constraints) for tname, t in main_before.tables.items()}

    b = store.create_branch(ws, "feature-y")

    # (a) the branch actually got the index and the FK.
    branch_snap = snapshot(ws, b.schema_name)
    assert "ix_users_email" in branch_snap.tables["users"].indexes
    fk_kinds = {c.kind for c in branch_snap.tables["posts"].constraints.values()}
    assert "f" in fk_kinds

    # (b) -- the assertion that actually matters: main is untouched.
    main_after = snapshot(ws, "main")
    indexes_after = {tname: set(t.indexes) for tname, t in main_after.tables.items()}
    constraints_after = {tname: set(t.constraints) for tname, t in main_after.tables.items()}
    assert indexes_after == indexes_before
    assert constraints_after == constraints_before
