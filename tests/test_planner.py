from tributary.planner import plan, classify, is_binary_coercible, preflight_sql
from tributary.model import (Safety, TableStats, AddColumn, DropColumn, RenameColumn,
                             AlterColumnType, SetNotNull, AddConstraint, CreateIndex,
                             Column, Constraint, Index, CreateTable, Table, DropTable,
                             DropIndex)

BIG = {"events": TableStats(rows=52_000_000, bytes=5_200_000_000)}


# --- Step 1: classification -------------------------------------------------

def test_add_nullable_column_is_metadata_only():
    c = AddColumn("events", Column("note", "text", True, None, 9))
    assert classify(c, BIG["events"]) == Safety.SAFE_METADATA


def test_add_column_with_constant_default_is_metadata_only_on_pg11_plus():
    c = AddColumn("events", Column("tier", "int4", False, "0", 9))
    assert classify(c, BIG["events"]) == Safety.SAFE_METADATA


def test_drop_column_is_metadata_only():
    assert classify(DropColumn("events", "note"), BIG["events"]) == Safety.SAFE_METADATA


def test_rename_is_metadata_only():
    assert classify(RenameColumn("events", "a", "b"), BIG["events"]) == Safety.SAFE_METADATA


def test_int_to_bigint_is_a_rewrite():
    c = AlterColumnType("events", "id", "int4", "int8")
    assert classify(c, BIG["events"]) == Safety.REWRITE


def test_varchar_widening_is_not_a_rewrite():
    assert is_binary_coercible("varchar(50)", "varchar(100)") is True
    c = AlterColumnType("events", "name", "varchar(50)", "varchar(100)")
    assert classify(c, BIG["events"]) == Safety.SAFE_METADATA


def test_varchar_to_text_is_not_a_rewrite():
    assert is_binary_coercible("varchar(50)", "text") is True


def test_varchar_narrowing_is_a_rewrite():
    assert is_binary_coercible("varchar(100)", "varchar(50)") is False


def test_create_index_is_lock_heavy():
    c = CreateIndex("events", Index("ix", "CREATE INDEX ix ON events (ts)", ("ts",)))
    assert classify(c, BIG["events"]) == Safety.LOCK_HEAVY


def test_set_not_null_is_lock_heavy():
    assert classify(SetNotNull("events", "ts"), BIG["events"]) == Safety.LOCK_HEAVY


# --- Step 2: rewrites --------------------------------------------------------

def sqls(p):
    return [s.sql for s in p.steps]


def test_create_index_is_rewritten_to_concurrently_outside_a_transaction():
    c = CreateIndex("events", Index("ix", "CREATE INDEX ix ON events USING btree (ts)", ("ts",)))
    p = plan([c], BIG, "main")
    step = next(s for s in p.steps if s.kind == "index_concurrent")
    assert "CONCURRENTLY" in step.sql
    assert step.transactional is False


def test_add_check_constraint_is_split_into_not_valid_then_validate():
    c = AddConstraint("events", Constraint("age_ok", "c", "CHECK (age >= 0)", ("age",)))
    p = plan([c], BIG, "main")
    assert any("NOT VALID" in s for s in sqls(p))
    assert any("VALIDATE CONSTRAINT" in s for s in sqls(p))
    idx_nv = next(i for i, s in enumerate(sqls(p)) if "NOT VALID" in s)
    idx_v = next(i for i, s in enumerate(sqls(p)) if "VALIDATE CONSTRAINT" in s)
    assert idx_nv < idx_v


def test_add_foreign_key_is_split_the_same_way():
    c = AddConstraint("events", Constraint(
        "fk_u", "f", "FOREIGN KEY (user_id) REFERENCES users(id)", ("user_id",)))
    p = plan([c], BIG, "main")
    assert any("NOT VALID" in s for s in sqls(p))
    assert any("VALIDATE CONSTRAINT" in s for s in sqls(p))


def test_set_not_null_uses_a_validated_check_to_skip_the_scan():
    p = plan([SetNotNull("events", "ts")], BIG, "main")
    joined = " | ".join(sqls(p))
    assert "IS NOT NULL" in joined and "NOT VALID" in joined
    assert "VALIDATE CONSTRAINT" in joined
    assert "SET NOT NULL" in joined
    # the scaffolding check is cleaned up afterwards
    assert "DROP CONSTRAINT" in joined


def test_rewriting_retype_becomes_a_shadow_column_backfill():
    p = plan([AlterColumnType("events", "id", "int4", "int8")], BIG, "main")
    kinds = [s.kind for s in p.steps]
    assert "backfill" in kinds and "swap" in kinds
    joined = " | ".join(sqls(p))
    assert "ADD COLUMN" in joined              # shadow column
    assert "TRIGGER" in joined                 # keeps writes in sync during backfill
    assert "RENAME COLUMN" in joined           # swap, not a rewrite
    assert not any("ALTER COLUMN \"id\" TYPE" in s for s in sqls(p))
    backfill = next(s for s in p.steps if s.kind == "backfill")
    assert backfill.transactional is False


def test_non_rewriting_retype_stays_a_single_plain_alter():
    p = plan([AlterColumnType("events", "name", "varchar(50)", "varchar(100)")], BIG, "main")
    assert len([s for s in p.steps if s.kind == "ddl"]) == 1
    assert "TYPE varchar(100)" in sqls(p)[0]


def test_small_table_skips_the_shadow_dance():
    small = {"events": TableStats(rows=200, bytes=16_384)}
    p = plan([AlterColumnType("events", "id", "int4", "int8")], small, "main")
    assert not any(s.kind == "backfill" for s in p.steps)


def test_every_ddl_step_is_preceded_by_a_lock_timeout():
    # R5: the brief's original assertion was an `or` of two weak clauses that can
    # pass vacuously. Replaced with a concrete assertion per the controller's
    # ruling: every step with kind == "ddl" carries lock_timeout in its own SQL.
    p = plan([AddColumn("events", Column("n", "text", True, None, 9))], BIG, "main")
    assert any(s.kind == "ddl" for s in p.steps)
    assert all("lock_timeout" in s.sql for s in p.steps if s.kind == "ddl")


def test_plan_warns_about_the_expensive_table_in_human_words():
    p = plan([AlterColumnType("events", "id", "int4", "int8")], BIG, "main")
    assert p.warnings
    joined = " ".join(p.warnings).lower()
    assert "events" in joined and ("gb" in joined or "rewrit" in joined)


# --- R12: fail safe on unknown table size -----------------------------------

def test_never_analysed_large_table_still_takes_the_safe_path():
    """R12 required test: a never-analysed table (rows=None) whose real disk
    usage is above the byte threshold must still take the safe shadow-column
    path, never the naive plain ALTER. `rows is None` must never read as
    "small" -- that misreading is the single most realistic path to the
    outage this project exists to prevent (a freshly restored, unanalysed
    multi-GB table getting a naive ALTER).
    """
    never_analysed_big = {"events": TableStats(rows=None, bytes=5_200_000_000)}
    c = AlterColumnType("events", "id", "int4", "int8")
    assert classify(c, never_analysed_big["events"]) == Safety.REWRITE

    p = plan([c], never_analysed_big, "main")
    kinds = [s.kind for s in p.steps]
    assert "backfill" in kinds and "swap" in kinds


# --- Step 3: ordering and preflight ------------------------------------------

def test_tables_are_created_before_foreign_keys_that_reference_them():
    changes = [
        AddConstraint("events", Constraint("fk_u", "f",
            "FOREIGN KEY (user_id) REFERENCES users(id)", ("user_id",))),
        CreateTable(Table("users", columns={"id": Column("id", "int8", False, None, 1)})),
    ]
    p = plan(changes, {}, "main")
    order = sqls(p)
    create_at = next(i for i, s in enumerate(order) if "CREATE TABLE" in s)
    fk_at = next(i for i, s in enumerate(order) if "FOREIGN KEY" in s)
    assert create_at < fk_at


def test_indexes_are_dropped_before_their_columns():
    p = plan([DropColumn("events", "ts"), DropIndex("events", "ix_ts")], BIG, "main")
    order = sqls(p)
    assert next(i for i, s in enumerate(order) if "DROP INDEX" in s) < \
           next(i for i, s in enumerate(order) if "DROP COLUMN" in s)


def test_preflight_probes_a_retype_for_uncastable_rows():
    sql = preflight_sql(AlterColumnType("events", "code", "text", "int4"), "main")
    assert sql and "SELECT" in sql and "code" in sql


def test_preflight_probes_set_not_null_for_existing_nulls():
    sql = preflight_sql(SetNotNull("events", "ts"), "main")
    assert sql and "IS NULL" in sql


def test_preflight_is_absent_where_nothing_can_fail():
    assert preflight_sql(DropColumn("events", "ts"), "main") is None
