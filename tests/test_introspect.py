import json
import uuid

from psycopg import sql

from tributary.introspect import snapshot, table_stats

DDL = """
CREATE TABLE {s}.users (
  id bigserial PRIMARY KEY,
  email character varying(255) NOT NULL,
  created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
  age integer
);
CREATE UNIQUE INDEX ix_users_email ON {s}.users (email);
ALTER TABLE {s}.users ADD CONSTRAINT age_sane CHECK (age >= 0);
"""


def test_snapshot_reads_columns_with_canonical_types(conn, fresh_schema):
    conn.execute(DDL.format(s=fresh_schema))
    snap = snapshot(conn, fresh_schema)
    users = snap.tables["users"]
    assert users.columns["email"].type == "varchar(255)"
    assert users.columns["email"].nullable is False
    assert users.columns["created_at"].type == "timestamptz"
    assert users.columns["created_at"].default == "now()"
    assert users.columns["age"].nullable is True


def test_snapshot_reads_constraints_and_indexes(conn, fresh_schema):
    conn.execute(DDL.format(s=fresh_schema))
    users = snapshot(conn, fresh_schema).tables["users"]
    kinds = {c.kind for c in users.constraints.values()}
    assert "p" in kinds and "c" in kinds
    assert users.indexes["ix_users_email"].unique is True
    assert users.indexes["ix_users_email"].columns == ("email",)


def test_snapshot_of_empty_schema_is_empty(conn, fresh_schema):
    assert snapshot(conn, fresh_schema).tables == {}


def test_snapshot_includes_table_with_no_columns(conn, fresh_schema):
    # RULING R13: `CREATE TABLE t()` is legal Postgres. A table must be
    # seeded into the snapshot by its own existence, independent of whether
    # it has any columns, or it silently disappears from the snapshot
    # entirely -- indistinguishable, to diff, from a dropped table.
    conn.execute(f"CREATE TABLE {fresh_schema}.empty ()")
    snap = snapshot(conn, fresh_schema)
    assert "empty" in snap.tables
    empty = snap.tables["empty"]
    assert empty.columns == {}
    assert empty.constraints == {}
    assert empty.indexes == {}


def test_dropping_last_column_does_not_make_table_vanish_from_snapshot(conn, fresh_schema):
    # RULING R13 -- the real consequence this guards against: a table that
    # is present in the database but absent from the snapshot reads to
    # diff as a dropped table, and merge would issue a DropTable against a
    # real table nobody asked to drop. Losing the last column must not be
    # able to trigger that.
    conn.execute(f"CREATE TABLE {fresh_schema}.shrinking (only_col int)")
    before = snapshot(conn, fresh_schema)
    assert "shrinking" in before.tables
    assert list(before.tables["shrinking"].columns) == ["only_col"]

    conn.execute(f"ALTER TABLE {fresh_schema}.shrinking DROP COLUMN only_col")
    after = snapshot(conn, fresh_schema)
    assert "shrinking" in after.tables
    assert after.tables["shrinking"].columns == {}


def test_snapshot_ignores_other_schemas(conn, fresh_schema):
    conn.execute(DDL.format(s=fresh_schema))
    other = fresh_schema + "_x"
    conn.execute(f'CREATE SCHEMA "{other}"')
    try:
        conn.execute(f'CREATE TABLE "{other}".ghost (id int)')
        assert "ghost" not in snapshot(conn, fresh_schema).tables
    finally:
        conn.execute(f'DROP SCHEMA "{other}" CASCADE')


def test_table_stats_reports_size(conn, fresh_schema):
    conn.execute(DDL.format(s=fresh_schema))
    conn.execute(f"INSERT INTO {fresh_schema}.users (email) "
                 f"SELECT 'u'||i FROM generate_series(1,1000) i")
    conn.execute(f"ANALYZE {fresh_schema}.users")
    st = table_stats(conn, fresh_schema)["users"]
    assert st.rows >= 900
    assert st.bytes > 0


def test_table_stats_of_never_analyzed_table_is_unknown(conn, fresh_schema):
    # RULING R12: reltuples is -1 for a table that has never been ANALYZEd.
    # That must surface as rows=None (unknown), never as a confident 0 --
    # a freshly-restored, never-analysed multi-GB table reading as "0 rows"
    # would be handed a naive ALTER TABLE that takes an ACCESS EXCLUSIVE
    # lock for a full table rewrite, which is exactly the outage this
    # project exists to prevent. bytes (real disk usage) must still be
    # reported and positive regardless.
    conn.execute(f"CREATE TABLE {fresh_schema}.fresh (id int)")
    conn.execute(f"INSERT INTO {fresh_schema}.fresh SELECT * FROM generate_series(1, 50)")
    st = table_stats(conn, fresh_schema)["fresh"]
    assert st.rows is None
    assert st.bytes > 0


def test_table_stats_reports_known_rows_after_analyze(conn, fresh_schema):
    # The other half of RULING R12: unknown before ANALYZE, known (a real
    # int, not None) after. Same table as the test above, post-ANALYZE.
    conn.execute(f"CREATE TABLE {fresh_schema}.fresh (id int)")
    conn.execute(f"INSERT INTO {fresh_schema}.fresh SELECT * FROM generate_series(1, 50)")
    conn.execute(f"ANALYZE {fresh_schema}.fresh")
    st = table_stats(conn, fresh_schema)["fresh"]
    assert st.rows is not None
    assert st.rows >= 45


def test_unique_constraint_backed_index_is_not_duplicated_as_index(conn, fresh_schema):
    # A UNIQUE *constraint* (as opposed to a bare CREATE UNIQUE INDEX) is
    # backed by an implicitly-created index. That index must not also show
    # up in `indexes`, or diff/merge will try to create it twice and the
    # generated DDL will fail.
    conn.execute(f"""
        CREATE TABLE {fresh_schema}.accounts (
          id int PRIMARY KEY,
          handle text NOT NULL,
          CONSTRAINT accounts_handle_key UNIQUE (handle)
        )
    """)
    accounts = snapshot(conn, fresh_schema).tables["accounts"]
    assert accounts.constraints["accounts_handle_key"].kind == "u"
    assert "accounts_handle_key" not in accounts.indexes
    assert all("handle" not in idx.columns or name != "accounts_handle_key"
               for name, idx in accounts.indexes.items())
    # the PK's backing index must not be duplicated either.
    assert "accounts_pkey" not in accounts.indexes


def test_snapshot_is_byte_stable_across_repeated_introspection(conn, fresh_schema):
    # RULING R11(1): two introspections of the same live schema must produce
    # byte-identical JSON, or commits compare unequal for no reason and merge
    # reports phantom conflicts. `to_json` is already deterministic (sorted
    # key insertion order) -- this test must not paper over instability by
    # sorting at compare time.
    conn.execute(DDL.format(s=fresh_schema))
    first = json.dumps(snapshot(conn, fresh_schema).to_json(), sort_keys=False)
    second = json.dumps(snapshot(conn, fresh_schema).to_json(), sort_keys=False)
    assert first == second


def _build_two_table_schema(conn, s, id_type, str_type, ts_type, ts_default, extra_indexes=True):
    # A table, a standalone index, a unique constraint, and a foreign key to
    # a second table -- every place a schema-qualified definition (via
    # pg_get_indexdef / pg_get_constraintdef) could leak the source schema
    # name into the snapshot.
    conn.execute(f"""
        CREATE TABLE {s}.widgets (id {id_type} PRIMARY KEY);
        CREATE TABLE {s}.gadgets (
          id {id_type} PRIMARY KEY,
          widget_id {id_type} REFERENCES {s}.widgets(id),
          email {str_type} NOT NULL,
          status text NOT NULL DEFAULT 'active',
          created_at {ts_type} DEFAULT {ts_default},
          CONSTRAINT gadgets_email_key UNIQUE (email)
        );
        CREATE UNIQUE INDEX ix_gadgets_widget_id ON {s}.gadgets (widget_id);
    """)
    if extra_indexes:
        # RULING R17(1): a partial index and an expression index -- the two
        # shapes where pg_get_indexdef's pretty=true form rewrites the text
        # (stripping outer parens around the predicate / inside the
        # expression) rather than merely dropping the schema qualifier.
        # Folded into the same helper so the existing byte-equality
        # assertion covers them for free.
        conn.execute(f"""
            CREATE INDEX ix_gadgets_active ON {s}.gadgets (widget_id) WHERE status = 'active';
            CREATE INDEX ix_gadgets_email_status ON {s}.gadgets ((email || status));
        """)


def test_snapshot_of_identically_defined_schemas_is_byte_equal_with_no_normalisation(conn, fresh_schema):
    # RULING R14(3): replaces R11(2)'s schema-name normalisation -- which
    # was masking the real defect -- with the actual property. Two
    # separately-created schemas, identically defined (down to a standalone
    # index, a unique constraint, and a foreign key to a second table),
    # spelled with different type/default spellings, must produce
    # byte-identical snapshot JSON with NO normalisation of any kind,
    # schema name included. If they don't, diff is broken: every branch
    # would show every index and FK as "modified" purely because of the
    # schema name, and merge would report conflicts on all of them.
    schema_a = fresh_schema
    schema_b = "t_" + uuid.uuid4().hex[:12]
    conn.execute(f'CREATE SCHEMA "{schema_b}"')
    try:
        _build_two_table_schema(conn, schema_a, "integer", "character varying(255)",
                                 "timestamp with time zone", "CURRENT_TIMESTAMP")
        _build_two_table_schema(conn, f'"{schema_b}"', "int4", "varchar(255)",
                                 "timestamptz", "now()")
        json_a = json.dumps(snapshot(conn, schema_a).to_json(), sort_keys=False)
        json_b = json.dumps(snapshot(conn, schema_b).to_json(), sort_keys=False)
        assert json_a == json_b
    finally:
        conn.execute(f'DROP SCHEMA "{schema_b}" CASCADE')


def test_index_and_constraint_definitions_have_no_schema_qualifier(conn, fresh_schema):
    # RULING R14(4): pg_get_indexdef / pg_get_constraintdef must not embed
    # the schema name at all. If they did, replaying these definitions to
    # materialise a branch (Task 6/9) would create the object in the
    # SOURCE schema instead of the branch -- a branch operation silently
    # mutating the schema it branched from.
    _build_two_table_schema(conn, fresh_schema, "integer", "text",
                             "timestamptz", "now()")
    snap = snapshot(conn, fresh_schema)
    for table in snap.tables.values():
        for idx in table.indexes.values():
            assert fresh_schema not in idx.definition, idx.definition
        for con in table.constraints.values():
            assert fresh_schema not in con.definition, con.definition


def test_partial_and_expression_index_definitions_round_trip_stably(conn, fresh_schema):
    # RULING R17(2): pg_get_indexdef's pretty=true form doesn't just drop
    # the schema qualifier for a partial or expression index -- it also
    # deterministically strips the outer parens around a partial index's
    # predicate and a redundant paren layer inside an expression index. A
    # human verified by hand that this rewrite is a stable fixed point
    # (recreating the index from the rewritten text and re-introspecting
    # reproduces the same text); that must not remain a probe someone ran
    # once. Introspect an index, execute its own stored `definition` text
    # to recreate an equivalent index in a second, throwaway schema (under
    # the same search_path discipline `snapshot()` itself uses, since the
    # stored text is unqualified), re-introspect, and assert the two
    # definition strings are byte-identical -- proving both that the
    # stored text is executable DDL and that executing it round-trips.
    schema_x = fresh_schema
    _build_two_table_schema(conn, schema_x, "integer", "text", "timestamptz", "now()")
    original = snapshot(conn, schema_x).tables["gadgets"].indexes
    partial_def = original["ix_gadgets_active"].definition
    expr_def = original["ix_gadgets_email_status"].definition
    # Confirm this test is actually exercising the rewrite pretty=true
    # performs -- outer parens dropped around the predicate, and one
    # redundant paren layer dropped inside the expression (the mandatory
    # syntactic paren around an expression-index column stays, so
    # "(((email || status)))" becomes "((email || status))", not
    # "(email || status)") -- not merely comparing two copies of text that
    # was never rewritten in the first place.
    assert "WHERE (status" not in partial_def
    assert "(((email" not in expr_def

    schema_y = "t_" + uuid.uuid4().hex[:12]
    conn.execute(f'CREATE SCHEMA "{schema_y}"')
    try:
        # Same underlying table shape, but without the two indexes under
        # test -- they get recreated below straight from the stored
        # definition text.
        _build_two_table_schema(conn, f'"{schema_y}"', "integer", "text",
                                 "timestamptz", "now()", extra_indexes=False)
        with conn.transaction():
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema_y)))
            conn.execute(partial_def)
            conn.execute(expr_def)
        rebuilt = snapshot(conn, schema_y).tables["gadgets"].indexes
        assert rebuilt["ix_gadgets_active"].definition == partial_def
        assert rebuilt["ix_gadgets_email_status"].definition == expr_def
    finally:
        conn.execute(f'DROP SCHEMA "{schema_y}" CASCADE')
