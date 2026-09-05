import json
import uuid

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


def test_snapshot_normalises_type_spelling_across_schemas(conn, fresh_schema):
    # RULING R11(2) -- the property that actually matters: two schemas that
    # are identically defined but spell their types differently must produce
    # equal snapshots once the (necessarily different, randomly-generated)
    # schema names are normalised out. Spelling differences must not survive
    # into the snapshot.
    schema_a = fresh_schema
    schema_b = "t_" + uuid.uuid4().hex[:12]
    conn.execute(f'CREATE SCHEMA "{schema_b}"')
    try:
        conn.execute(f"""
            CREATE TABLE {schema_a}.users (
              id integer PRIMARY KEY,
              email character varying(255) NOT NULL,
              age integer,
              created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX ix_users_email ON {schema_a}.users (email);
        """)
        conn.execute(f"""
            CREATE TABLE "{schema_b}".users (
              id int4 PRIMARY KEY,
              email varchar(255) NOT NULL,
              age int4,
              created_at timestamptz DEFAULT now()
            );
            CREATE UNIQUE INDEX ix_users_email ON "{schema_b}".users (email);
        """)
        json_a = json.dumps(snapshot(conn, schema_a).to_json(), sort_keys=False)
        json_b = json.dumps(snapshot(conn, schema_b).to_json(), sort_keys=False)
        # The only difference allowed to survive is the schema name itself
        # (it leaks into index/default definitions via pg_get_indexdef /
        # pg_get_expr) -- normalise it away before comparing.
        json_a = json_a.replace(schema_a, "SCHEMA")
        json_b = json_b.replace(schema_b, "SCHEMA")
        assert json_a == json_b
    finally:
        conn.execute(f'DROP SCHEMA "{schema_b}" CASCADE')
