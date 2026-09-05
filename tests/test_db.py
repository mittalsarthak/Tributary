def test_fixture_gives_a_live_postgres(conn, fresh_schema):
    conn.execute(f'CREATE TABLE "{fresh_schema}".t (id int)')
    row = conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = %s", (fresh_schema,)
    ).fetchone()
    assert row[0] == 1


def test_server_version_is_at_least_12(conn):
    version_num = conn.execute("SHOW server_version_num").fetchone()[0]
    assert int(version_num) >= 120000
