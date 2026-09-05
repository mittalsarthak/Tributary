from tributary.diff import detect_renames, diff
from tributary.model import (
    AddColumn,
    AddConstraint,
    AlterColumnType,
    Column,
    Constraint,
    CreateIndex,
    CreateTable,
    DropColumn,
    DropConstraint,
    DropDefault,
    DropIndex,
    DropNotNull,
    DropTable,
    Index,
    RenameColumn,
    RenameTable,
    SetDefault,
    SetNotNull,
    Snapshot,
    Table,
)


def tbl(name, **cols):
    return Table(name=name, columns={
        c: Column(c, t, True, None, i + 1) for i, (c, t) in enumerate(cols.items())
    })


# --- brief's required tests (verbatim) --------------------------------------

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
    # R20: AlterColumnType now carries the *target* column's nullable/default
    # (both True/None here, since tbl() builds nullable columns with no
    # default) so a rewriting retype's shadow-column swap knows what to
    # restore. tbl()'s columns are always nullable=True, default=None.
    assert diff(a, b) == [
        AlterColumnType("users", "id", "int4", "int8", nullable=True, default=None)
    ]


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


# --- R9: the op log must do something the heuristic genuinely cannot -------

def test_rename_plus_retype_in_the_same_commit_needs_the_op_log_not_just_the_heuristic():
    """A column renamed *and* retyped in one commit is the case the
    heuristic cannot solve on its own: it only pairs candidates of
    identical canonical type, so `email text` -> `email_address
    varchar(255)` looks like an unrelated drop and add to it. Only the op
    log -- which declares rename intent directly -- can turn this into
    RenameColumn + AlterColumnType. If this test passed with the op-log
    mechanism deleted, the mechanism would not be earning its place.
    """
    a = Snapshot({"users": tbl("users", id="int8", email="text")})
    b = Snapshot({"users": tbl("users", id="int8", email_address="varchar(255)")})
    ops = [{"op": "rename_column", "table": "users",
            "old": "email", "new": "email_address"}]

    without_ops = diff(a, b)
    assert DropColumn("users", "email") in without_ops
    assert any(
        isinstance(c, AddColumn) and c.column.name == "email_address"
        for c in without_ops
    )
    assert not any(isinstance(c, RenameColumn) for c in without_ops)

    with_ops = diff(a, b, ops)
    # R20: AlterColumnType carries the target column's nullable/default
    # (nullable=True, default=None, per tbl()'s always-nullable columns).
    assert with_ops == [
        RenameColumn("users", "email", "email_address"),
        AlterColumnType("users", "email_address", "text", "varchar(255)",
                         nullable=True, default=None),
    ]


# --- rename heuristic: ambiguity must refuse to guess -----------------------

def test_ambiguous_same_type_candidates_are_not_inferred_as_a_rename():
    a = Snapshot({"users": tbl("users", id="int8", first_name="text", last_name="text")})
    b = Snapshot({"users": tbl("users", id="int8", given_name="text", family_name="text")})
    changes = diff(a, b)
    assert DropColumn("users", "first_name") in changes
    assert DropColumn("users", "last_name") in changes
    assert any(isinstance(c, AddColumn) and c.column.name == "given_name" for c in changes)
    assert any(isinstance(c, AddColumn) and c.column.name == "family_name" for c in changes)
    assert not any(isinstance(c, RenameColumn) for c in changes)


# --- detect_renames as its own interface ------------------------------------

def test_detect_renames_returns_table_old_new_tuples():
    a = Snapshot({"users": tbl("users", id="int8", email="text")})
    b = Snapshot({"users": tbl("users", id="int8", email_address="text")})
    assert detect_renames(a, b) == [("users", "email", "email_address")]


def test_detect_renames_refuses_ambiguous_same_type_candidates():
    a = Snapshot({"users": tbl("users", first_name="text", last_name="text")})
    b = Snapshot({"users": tbl("users", given_name="text", family_name="text")})
    assert detect_renames(a, b) == []


# --- R16: table renames are the same failure one level up ------------------
#
# A renamed table read as DropTable + CreateTable does not rename anything
# executed against a real, populated table -- it drops the table and every
# row in it, then creates an empty one under the new name. Same argument as
# the column case, applied to tables; RenameTable must be preferred whenever
# the op log declares the intent.

def test_op_log_turns_table_drop_plus_create_into_a_rename_table():
    a = Snapshot({"users": tbl("users", id="int8")})
    b = Snapshot({"accounts": tbl("accounts", id="int8")})
    ops = [{"op": "rename_table", "old": "users", "new": "accounts"}]
    changes = diff(a, b, ops)
    assert changes == [RenameTable("users", "accounts")]
    assert not any(isinstance(c, DropTable) for c in changes)
    assert not any(isinstance(c, CreateTable) for c in changes)


def test_table_rename_and_column_addition_in_the_same_commit_is_rename_plus_add_column():
    a = Snapshot({"users": tbl("users", id="int8")})
    b = Snapshot({"accounts": tbl("accounts", id="int8", email="text")})
    ops = [{"op": "rename_table", "old": "users", "new": "accounts"}]
    changes = diff(a, b, ops)
    assert changes == [
        RenameTable("users", "accounts"),
        AddColumn("accounts", b.tables["accounts"].columns["email"]),
    ]


def test_structurally_identical_table_drop_and_create_without_op_log_is_never_guessed_as_a_rename():
    """Pins the deliberate refusal to infer table renames heuristically
    (R16): even though `accounts` is byte-for-byte the same shape as the
    dropped `users`, with no op log declaring intent, diff must report an
    honest DropTable + CreateTable and never invent a RenameTable -- unlike
    a single column, there is no cheap way to be confident two tables with
    an identical column set are "the same table" rather than an unrelated
    coincidence.
    """
    a = Snapshot({"users": tbl("users", id="int8")})
    b = Snapshot({"accounts": tbl("accounts", id="int8")})
    changes = diff(a, b)
    assert DropTable("users") in changes
    assert any(isinstance(c, CreateTable) and c.table.name == "accounts" for c in changes)
    assert not any(isinstance(c, RenameTable) for c in changes)


# --- ordering: drops before table work before column work before creates ---

def test_change_ordering_is_drops_then_tables_then_columns_then_creates():
    a = Snapshot({"users": Table(
        "users",
        columns={"id": Column("id", "int8", False, None, 1),
                 "old_col": Column("old_col", "text", True, None, 2)},
        constraints={"ck_old": Constraint("ck_old", "c", "CHECK (id > 0)")},
        indexes={"ix_old": Index("ix_old", "CREATE INDEX ix_old ON users (old_col)", ("old_col",))},
    )})
    b = Snapshot({
        "users": Table(
            "users",
            columns={"id": Column("id", "int8", False, None, 1)},
            constraints={"ck_new": Constraint("ck_new", "c", "CHECK (id > 1)")},
            indexes={"ix_new": Index("ix_new", "CREATE INDEX ix_new ON users (id)", ("id",))},
        ),
        "orders": tbl("orders", id="int8"),
    })
    changes = diff(a, b)
    kinds = [type(c).__name__ for c in changes]

    drop_positions = [i for i, k in enumerate(kinds) if k in ("DropIndex", "DropConstraint")]
    table_positions = [i for i, k in enumerate(kinds) if k in ("DropTable", "CreateTable")]
    column_positions = [i for i, k in enumerate(kinds) if k == "DropColumn"]
    create_positions = [i for i, k in enumerate(kinds) if k in ("AddConstraint", "CreateIndex")]

    assert drop_positions and table_positions and column_positions and create_positions
    assert max(drop_positions) < min(table_positions)
    assert max(table_positions) < min(column_positions)
    assert max(column_positions) < min(create_positions)


# --- final fix wave: cheap coverage gaps (item 4) ---------------------------

def test_table_rename_plus_column_rename_logged_under_the_old_table_name():
    """R16 + a column rename together in one commit, with the column rename
    logged under the table's *pre*-rename name -- the order a real editor
    session would actually produce it in (the column was renamed while the
    table was still called `users`; the table itself was renamed afterward,
    as a separate op). `_diff_columns`'s op-log lookup tries
    `(new_table, old_col)` first and falls back to `(old_table, old_col)`
    (diff.py:275) -- this exercises that fallback specifically.
    """
    a = Snapshot({"users": tbl("users", id="int8", email="text")})
    b = Snapshot({"accounts": tbl("accounts", id="int8", email_address="text")})
    ops = [
        {"op": "rename_column", "table": "users", "old": "email", "new": "email_address"},
        {"op": "rename_table", "old": "users", "new": "accounts"},
    ]
    changes = diff(a, b, ops)
    assert changes == [
        RenameTable("users", "accounts"),
        RenameColumn("accounts", "email", "email_address"),
    ]


def test_drop_not_null_is_detected():
    a = Snapshot({"users": Table("users", columns={"e": Column("e", "text", False, None, 1)})})
    b = Snapshot({"users": Table("users", columns={"e": Column("e", "text", True, None, 1)})})
    assert diff(a, b) == [DropNotNull("users", "e")]


def test_set_default_is_detected():
    a = Snapshot({"users": Table("users", columns={"e": Column("e", "text", True, None, 1)})})
    b = Snapshot({"users": Table("users", columns={"e": Column("e", "text", True, "'x'", 1)})})
    assert diff(a, b) == [SetDefault("users", "e", "'x'")]


def test_drop_default_is_detected():
    a = Snapshot({"users": Table("users", columns={"e": Column("e", "text", True, "'x'", 1)})})
    b = Snapshot({"users": Table("users", columns={"e": Column("e", "text", True, None, 1)})})
    assert diff(a, b) == [DropDefault("users", "e")]
