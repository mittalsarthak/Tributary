from tributary.planner import plan, classify, is_binary_coercible, preflight_sql
from tributary.model import (Safety, TableStats, AddColumn, DropColumn, RenameColumn,
                             AlterColumnType, SetNotNull, SetDefault, AddConstraint, CreateIndex,
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
    # R19: plan() now needs to know the table's primary key to emit a fully
    # resolved batched backfill (no {pk}-style placeholder in Step.sql, see
    # the R19 tests below) -- pk_columns is new surface added by that
    # ruling, after this test was originally written, so it is supplied here
    # to keep exercising the full shadow-column dance rather than the
    # no-known-PK fallback. Every original assertion below is unchanged.
    p = plan([AlterColumnType("events", "id", "int4", "int8")], BIG, "main",
             pk_columns={"events": "id"})
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
    p = plan([AlterColumnType("events", "id", "int4", "int8")], BIG, "main",
             pk_columns={"events": "id"})
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

    p = plan([c], never_analysed_big, "main", pk_columns={"events": "id"})
    kinds = [s.kind for s in p.steps]
    assert "backfill" in kinds and "swap" in kinds


# --- CRITICAL fix round 1: volatile ADD COLUMN defaults are not metadata-only

def test_volatile_defaults_are_not_metadata_only_on_a_large_table():
    # PG11+'s fast ADD COLUMN path only skips the rewrite for a *constant*
    # default; a volatile one (any function call) forces Postgres to
    # compute and write a real value into every existing row immediately --
    # a full table rewrite under ACCESS EXCLUSIVE. Deliberately no allowlist
    # of "known volatile" function names here (see _is_constant_default's
    # docstring) -- these three are just representative examples, not the
    # full set the fix is supposed to catch.
    for default in ["gen_random_uuid()", "nextval('events_id_seq'::regclass)", "random()"]:
        c = AddColumn("events", Column("x", "uuid", False, default, 9))
        assert classify(c, BIG["events"]) != Safety.SAFE_METADATA, default


def test_literal_defaults_stay_metadata_only():
    cases = [
        Column("tier", "int4", False, "0", 9),
        Column("code", "text", False, "'x'", 9),
        Column("flag", "bool", False, "TRUE", 9),
        Column("label", "text", False, "'x'::text", 9),
    ]
    for column in cases:
        c = AddColumn("events", column)
        assert classify(c, BIG["events"]) == Safety.SAFE_METADATA, column.default


def test_volatile_default_on_a_large_table_produces_a_warning():
    c = AddColumn("events", Column("id2", "uuid", False, "gen_random_uuid()", 9))
    p = plan([c], BIG, "main")
    assert p.warnings
    joined = " ".join(p.warnings).lower()
    assert "events" in joined


def test_volatile_default_on_a_small_table_emits_no_warning():
    # The classification (rewrite-equivalent) doesn't depend on size, but
    # the *warning* -- like every other size-triggered warning in this
    # module -- only fires when the table is actually large enough to
    # matter; a handful of rows costs nothing to rewrite.
    small = {"events": TableStats(rows=200, bytes=16_384)}
    c = AddColumn("events", Column("id2", "uuid", False, "gen_random_uuid()", 9))
    p = plan([c], small, "main")
    assert p.warnings == []


# --- IMPORTANT fix round 1: don't double-restore NOT NULL/DEFAULT -----------

def test_combined_retype_and_nullability_and_default_change_dedupes_the_restoration():
    """A single commit that retypes a column *and* separately changes its
    nullability and default emits AlterColumnType (carrying the target
    nullable/default, R20) *and* standalone SetNotNull/SetDefault changes
    for that same column -- diff.py reports both, honestly. On a large
    table with a known PK, the shadow dance already restores NOT NULL
    (validated on the shadow column before the swap) and DEFAULT (set
    directly in the swap); the separately-emitted SetNotNull/SetDefault
    must not repeat that as a second, fully redundant NOT VALID/VALIDATE/
    SET NOT NULL/DROP sequence -- that VALIDATE is a real full-table scan
    proving something already proven.
    """
    changes = [
        AlterColumnType("events", "id", "int4", "int8", nullable=False, default="0"),
        SetNotNull("events", "id"),
        SetDefault("events", "id", "0"),
    ]
    p = plan(changes, BIG, "main", pk_columns={"events": "id"})

    kinds = [s.kind for s in p.steps]
    assert "backfill" in kinds and "swap" in kinds

    # Exactly two VALIDATE CONSTRAINT steps: the shadow dance's own
    # backfill-verification count-check and its NOT NULL scaffold
    # validation. A third (from a non-deduped standalone SetNotNull) would
    # be the redundant full-table scan this fix removes.
    validate_steps = [s for s in p.steps if s.kind == "validate"]
    assert len(validate_steps) == 2

    set_default_occurrences = [s for s in sqls(p) if "SET DEFAULT" in s]
    assert len(set_default_occurrences) == 1


def test_no_pk_fallback_still_needs_the_separate_not_null_restoration():
    # With no known PK, the retype falls back to a plain ALTER, which never
    # touches nullability/default at all -- so here the standalone
    # SetNotNull is NOT redundant and must still run in full.
    changes = [
        AlterColumnType("events", "id", "int4", "int8", nullable=False, default="0"),
        SetNotNull("events", "id"),
    ]
    p = plan(changes, BIG, "main")  # no pk_columns -> conservative fallback
    validate_steps = [s for s in p.steps if s.kind == "validate"]
    assert len(validate_steps) == 1  # SetNotNull's own VALIDATE CONSTRAINT


def test_small_table_retype_still_needs_the_separate_not_null_restoration():
    # The small-table (LOCK_BRIEF) path is a plain ALTER too -- it never
    # restores nullability/default either, so the standalone SetNotNull
    # must not be deduped here.
    small = {"events": TableStats(rows=200, bytes=16_384)}
    changes = [
        AlterColumnType("events", "id", "int4", "int8", nullable=False, default="0"),
        SetNotNull("events", "id"),
    ]
    p = plan(changes, small, "main", pk_columns={"events": "id"})
    validate_steps = [s for s in p.steps if s.kind == "validate"]
    assert len(validate_steps) == 1


def test_unrelated_columns_are_never_deduped_against_each_other():
    # A SetNotNull on a *different* column than the one being retyped must
    # never be swallowed by the dedup -- it only matches on (table, column).
    changes = [
        AlterColumnType("events", "id", "int4", "int8", nullable=False, default="0"),
        SetNotNull("events", "other_col"),
    ]
    p = plan(changes, BIG, "main", pk_columns={"events": "id"})
    validate_steps = [s for s in p.steps if s.kind == "validate"]
    # 2 from the shadow dance (verify + NOT NULL scaffold) + 1 from
    # other_col's own, unrelated SetNotNull.
    assert len(validate_steps) == 3


# --- R19: eliminate the {pk} placeholder entirely ---------------------------

def test_known_pk_produces_a_fully_resolved_backfill_with_no_placeholder():
    c = AlterColumnType("events", "id", "int4", "int8")
    p = plan([c], BIG, "main", pk_columns={"events": "id"})
    backfill = next(s for s in p.steps if s.kind == "backfill")
    assert "{" not in backfill.sql
    assert '"id"' in backfill.sql


def test_missing_pk_falls_back_to_plain_alter_with_a_warning_instead_of_a_placeholder():
    c = AlterColumnType("events", "id", "int4", "int8")
    p = plan([c], BIG, "main")  # no pk_columns at all
    assert not any(s.kind == "backfill" for s in p.steps)
    assert not any(s.kind == "swap" for s in p.steps)
    assert not any("{" in s.sql for s in p.steps)
    joined = " ".join(p.warnings).lower()
    assert "events" in joined and "primary key" in joined


def test_table_absent_from_pk_columns_also_falls_back_conservatively():
    c = AlterColumnType("events", "id", "int4", "int8")
    # pk_columns is supplied but doesn't mention "events" -- same as absent.
    p = plan([c], BIG, "main", pk_columns={"other_table": "id"})
    assert not any(s.kind == "backfill" for s in p.steps)
    assert not any("{" in s.sql for s in p.steps)


def test_no_step_sql_ever_contains_an_unresolved_placeholder():
    changes = [
        AlterColumnType("events", "id", "int4", "int8"),          # PK known
        AlterColumnType("orders", "amount", "int4", "numeric"),    # PK unknown
    ]
    stats = {
        "events": TableStats(rows=52_000_000, bytes=5_200_000_000),
        "orders": TableStats(rows=10_000_000, bytes=2_000_000_000),
    }
    p = plan(changes, stats, "main", pk_columns={"events": "id"})
    assert not any("{" in s.sql for s in p.steps)


# --- R20: the shadow-column swap must restore NOT NULL/DEFAULT --------------

def test_shadow_dance_restores_not_null_and_default():
    c = AlterColumnType("events", "id", "int4", "int8", nullable=False, default="0")
    p = plan([c], BIG, "main", pk_columns={"events": "id"})
    joined = " | ".join(sqls(p))
    assert "SET NOT NULL" in joined
    assert "SET DEFAULT" in joined
    # restored on the shadow column, validated before the swap so the swap's
    # own SET NOT NULL is metadata-only rather than a fresh scan under lock
    assert any("NOT VALID" in s and "trib_new" in s for s in sqls(p))
    swap = next(s for s in p.steps if s.kind == "swap")
    assert "SET NOT NULL" in swap.sql
    assert "SET DEFAULT" in swap.sql


def test_plain_alter_path_never_needed_to_restore_anything():
    # A plain ALTER COLUMN TYPE never drops NOT NULL/DEFAULT in the first
    # place -- it's the same physical column throughout -- so nullable/
    # default on the change must not trigger any extra SET NOT NULL/SET
    # DEFAULT step on either the binary-coercible or small-table path.
    coercible = AlterColumnType("events", "name", "varchar(50)", "varchar(100)",
                                 nullable=False, default="'x'")
    p1 = plan([coercible], BIG, "main")
    assert not any("SET NOT NULL" in s for s in sqls(p1))
    assert not any("SET DEFAULT" in s for s in sqls(p1))

    small = {"events": TableStats(rows=200, bytes=16_384)}
    non_coercible_small = AlterColumnType("events", "id", "int4", "int8",
                                           nullable=False, default="0")
    p2 = plan([non_coercible_small], small, "main")
    assert not any("SET NOT NULL" in s for s in sqls(p2))
    assert not any("SET DEFAULT" in s for s in sqls(p2))


# --- R21: PRIMARY KEY/UNIQUE on a large table use the concurrent-index pattern

def test_large_table_unique_constraint_builds_concurrently_then_adopts_it():
    c = AddConstraint("events", Constraint("uq_email", "u", "UNIQUE (email)", ("email",)))
    p = plan([c], BIG, "main")
    idx_step = next(s for s in p.steps if s.kind == "index_concurrent")
    assert "CONCURRENTLY" in idx_step.sql
    assert idx_step.transactional is False
    assert any("USING INDEX" in s and "UNIQUE" in s for s in sqls(p))


def test_small_table_unique_constraint_stays_a_single_plain_add_constraint():
    small = {"events": TableStats(rows=200, bytes=16_384)}
    c = AddConstraint("events", Constraint("uq_email", "u", "UNIQUE (email)", ("email",)))
    p = plan([c], small, "main")
    assert not any(s.kind == "index_concurrent" for s in p.steps)
    assert len([s for s in p.steps if s.kind == "ddl"]) == 1
    assert any("ADD CONSTRAINT" in s and "UNIQUE" in s for s in sqls(p))


def test_large_table_primary_key_establishes_not_null_before_adopting_the_index():
    c = AddConstraint("events", Constraint("pk_events", "p", "PRIMARY KEY (id)", ("id",)))
    p = plan([c], BIG, "main")
    order = sqls(p)
    set_nn_at = next(i for i, s in enumerate(order) if "SET NOT NULL" in s)
    using_idx_at = next(i for i, s in enumerate(order)
                        if "USING INDEX" in s and "PRIMARY KEY" in s)
    assert set_nn_at < using_idx_at
    idx_step = next(s for s in p.steps if s.kind == "index_concurrent")
    assert idx_step.transactional is False


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
