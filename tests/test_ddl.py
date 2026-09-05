import pytest

from tributary.ddl import create_table_sql, render
from tributary.introspect import snapshot
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
    Table,
)


# --- brief's required tests ------------------------------------------------

def test_add_column_is_quoted_and_typed():
    sql = render(AddColumn("users", Column("nickname", "varchar(50)", True, None, 9)), "main")
    assert sql == 'ALTER TABLE "main"."users" ADD COLUMN "nickname" varchar(50)'


def test_add_not_null_column_with_default_emits_both():
    sql = render(AddColumn("users", Column("tier", "int4", False, "0", 9)), "main")
    assert sql == 'ALTER TABLE "main"."users" ADD COLUMN "tier" int4 NOT NULL DEFAULT 0'


def test_reserved_words_are_quoted():
    sql = render(DropColumn("order", "select"), "main")
    assert sql == 'ALTER TABLE "main"."order" DROP COLUMN "select"'


def test_rename_column_renders_as_rename_not_drop_add():
    sql = render(RenameColumn("users", "email", "email_address"), "main")
    assert "RENAME COLUMN" in sql and "DROP" not in sql


def test_alter_type_includes_using_clause():
    sql = render(AlterColumnType("users", "id", "int4", "int8"), "main")
    assert sql == ('ALTER TABLE "main"."users" ALTER COLUMN "id" '
                   'TYPE int8 USING "id"::int8')


@pytest.mark.parametrize("change", [SetNotNull("users", "email"), DropIndex("users", "ix")])
def test_every_change_renders_to_nonempty_sql(change):
    assert render(change, "main").strip()


def test_rendered_ddl_actually_executes(conn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}"."order" (id int)')
    conn.execute(render(AddColumn("order", Column("select", "text", True, None, 2)),
                        fresh_schema))
    assert "select" in snapshot(conn, fresh_schema).tables["order"].columns


# --- coverage of every remaining Change type -------------------------------

def test_drop_table_is_quoted():
    sql = render(DropTable("order"), "main")
    assert sql == 'DROP TABLE "main"."order"'


def test_rename_table_renders_rename_to():
    sql = render(RenameTable("users", "accounts"), "main")
    assert sql == 'ALTER TABLE "main"."users" RENAME TO "accounts"'


def test_drop_not_null():
    sql = render(DropNotNull("users", "email"), "main")
    assert sql == 'ALTER TABLE "main"."users" ALTER COLUMN "email" DROP NOT NULL'


def test_set_default_emits_literal_expression():
    sql = render(SetDefault("users", "tier", "0"), "main")
    assert sql == 'ALTER TABLE "main"."users" ALTER COLUMN "tier" SET DEFAULT 0'


def test_drop_default():
    sql = render(DropDefault("users", "tier"), "main")
    assert sql == 'ALTER TABLE "main"."users" ALTER COLUMN "tier" DROP DEFAULT'


def test_add_constraint_inserts_definition_literally():
    con = Constraint("age_ok", "c", "CHECK (age >= 0)", ("age",))
    sql = render(AddConstraint("users", con), "main")
    assert sql == 'ALTER TABLE "main"."users" ADD CONSTRAINT "age_ok" CHECK (age >= 0)'


def test_add_foreign_key_constraint_inserts_definition_literally():
    con = Constraint("fk_u", "f", "FOREIGN KEY (user_id) REFERENCES users(id)", ("user_id",))
    sql = render(AddConstraint("orders", con), "main")
    assert sql == ('ALTER TABLE "main"."orders" ADD CONSTRAINT "fk_u" '
                   'FOREIGN KEY (user_id) REFERENCES users(id)')


def test_drop_constraint_is_quoted():
    sql = render(DropConstraint("order", "select"), "main")
    assert sql == 'ALTER TABLE "main"."order" DROP CONSTRAINT "select"'


def test_create_index_returns_definition_literally():
    idx = Index("ix_email", 'CREATE INDEX ix_email ON "main"."users" USING btree (email)', ("email",))
    sql = render(CreateIndex("users", idx), "main")
    assert sql == 'CREATE INDEX ix_email ON "main"."users" USING btree (email)'


def test_drop_index_is_schema_qualified():
    sql = render(DropIndex("users", "ix"), "main")
    assert sql == 'DROP INDEX "main"."ix"'


def test_create_table_change_dispatches_to_create_table_sql():
    table = Table("users", columns={"id": Column("id", "int8", False, None, 1)})
    assert render(CreateTable(table), "main") == create_table_sql(table, "main")


# --- create_table_sql: columns, constraints, and indexes together ---------

def test_create_table_sql_renders_columns_in_position_order_with_quoting():
    table = Table(
        "order",
        columns={
            "id": Column("id", "int8", False, None, 1),
            "select": Column("select", "text", True, None, 2),
        },
    )
    sql = create_table_sql(table, "main")
    assert sql == (
        'CREATE TABLE "main"."order" (\n'
        '    "id" int8 NOT NULL,\n'
        '    "select" text\n'
        ')'
    )


def test_create_table_sql_appends_constraints_and_indexes():
    table = Table(
        "accounts",
        columns={"id": Column("id", "int8", False, None, 1),
                  "email": Column("email", "text", True, None, 2)},
        constraints={"accounts_pkey": Constraint("accounts_pkey", "p", "PRIMARY KEY (id)", ("id",))},
        indexes={"ix_email": Index("ix_email", 'CREATE INDEX ix_email ON "main"."accounts" (email)', ("email",))},
    )
    sql = create_table_sql(table, "main")
    statements = sql.split(";\n")
    assert statements[0].startswith('CREATE TABLE "main"."accounts" (')
    assert statements[1] == ('ALTER TABLE "main"."accounts" ADD CONSTRAINT '
                              '"accounts_pkey" PRIMARY KEY (id)')
    assert statements[2] == 'CREATE INDEX ix_email ON "main"."accounts" (email)'


# --- an unhandled change type must fail loudly, never silently ------------

def test_unsupported_change_type_raises_a_clear_error():
    class NotAChange:
        pass

    with pytest.raises(TypeError, match="NotAChange"):
        render(NotAChange(), "main")


# --- every one of the 15 Change classes must render to nonempty SQL -------

ALL_CHANGES = [
    CreateTable(Table("users", columns={"id": Column("id", "int8", False, None, 1)})),
    DropTable("users"),
    RenameTable("users", "accounts"),
    AddColumn("users", Column("nickname", "text", True, None, 2)),
    DropColumn("users", "nickname"),
    RenameColumn("users", "email", "email_address"),
    AlterColumnType("users", "id", "int4", "int8"),
    SetNotNull("users", "email"),
    DropNotNull("users", "email"),
    SetDefault("users", "tier", "0"),
    DropDefault("users", "tier"),
    AddConstraint("users", Constraint("age_ok", "c", "CHECK (age >= 0)", ("age",))),
    DropConstraint("users", "age_ok"),
    CreateIndex("users", Index("ix", "CREATE INDEX ix ON users (email)", ("email",))),
    DropIndex("users", "ix"),
]


def test_exactly_fifteen_change_types_are_exercised():
    assert len(ALL_CHANGES) == 15
    assert len({type(c) for c in ALL_CHANGES}) == 15


@pytest.mark.parametrize("change", ALL_CHANGES, ids=lambda c: type(c).__name__)
def test_all_fifteen_change_types_render_to_nonempty_sql(change):
    sql = render(change, "main")
    assert isinstance(sql, str)
    assert sql.strip()


# --- execution round-trips against a real Postgres -------------------------

def test_create_table_sql_actually_executes_with_constraints_and_indexes(conn, fresh_schema):
    table = Table(
        "accounts",
        columns={
            "id": Column("id", "int8", False, None, 1),
            "email": Column("email", "text", True, None, 2),
            "age": Column("age", "int4", True, None, 3),
        },
        constraints={
            "accounts_pkey": Constraint("accounts_pkey", "p", "PRIMARY KEY (id)", ("id",)),
            "accounts_email_key": Constraint("accounts_email_key", "u", "UNIQUE (email)", ("email",)),
            "accounts_age_check": Constraint("accounts_age_check", "c", "CHECK (age >= 0)", ("age",)),
        },
        indexes={
            "ix_accounts_email": Index(
                "ix_accounts_email",
                f'CREATE INDEX ix_accounts_email ON "{fresh_schema}"."accounts" (email)',
                ("email",),
            ),
        },
    )
    conn.execute(create_table_sql(table, fresh_schema))

    snap = snapshot(conn, fresh_schema)
    live = snap.tables["accounts"]
    assert set(live.columns) == {"id", "email", "age"}
    assert live.constraints["accounts_pkey"].kind == "p"
    assert live.constraints["accounts_email_key"].kind == "u"
    assert live.constraints["accounts_age_check"].kind == "c"
    assert "ix_accounts_email" in live.indexes


def test_create_table_sql_foreign_key_round_trip(conn, fresh_schema):
    users = Table(
        "users",
        columns={"id": Column("id", "int8", False, None, 1)},
        constraints={"users_pkey": Constraint("users_pkey", "p", "PRIMARY KEY (id)", ("id",))},
    )
    conn.execute(create_table_sql(users, fresh_schema))

    # A schema-qualified reference, as pg_get_constraintdef itself would
    # produce under the default search_path (the fresh_schema fixture is not
    # on it) -- see the "insert literally" caveat in ddl.py's module docstring.
    orders = Table(
        "orders",
        columns={
            "id": Column("id", "int8", False, None, 1),
            "user_id": Column("user_id", "int8", True, None, 2),
        },
        constraints={
            "fk_user": Constraint(
                "fk_user", "f",
                f'FOREIGN KEY (user_id) REFERENCES "{fresh_schema}".users(id)',
                ("user_id",),
            ),
        },
    )
    conn.execute(create_table_sql(orders, fresh_schema))

    live = snapshot(conn, fresh_schema).tables["orders"]
    assert live.constraints["fk_user"].kind == "f"
    assert "REFERENCES" in live.constraints["fk_user"].definition
    assert "users(id)" in live.constraints["fk_user"].definition


def test_create_table_sql_quotes_reserved_words_end_to_end(conn, fresh_schema):
    table = Table(
        "order",
        columns={
            "id": Column("id", "int8", False, None, 1),
            "select": Column("select", "text", True, None, 2),
        },
    )
    conn.execute(create_table_sql(table, fresh_schema))
    live = snapshot(conn, fresh_schema).tables["order"]
    assert set(live.columns) == {"id", "select"}
