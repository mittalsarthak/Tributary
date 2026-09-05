import tributary.db as db


def test_db_module_imports_and_exposes_its_interface():
    """Guards against a syntax error in db.py going unnoticed.

    Nothing imported this module until the web app and the deploy path did, so a
    stray indent on line 1 sat committed and green through two reviews. The suite
    should notice a module that cannot be imported at all.
    """
    assert callable(db.dsn)
    assert callable(db.connect)


def test_dsn_prefers_database_url_over_the_default(monkeypatch):
    """DATABASE_URL is what Railway injects — if the override silently failed,
    the deployed app would quietly talk to the local default instead."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@example.invalid:5432/x")
    assert db.dsn() == "postgresql://u:p@example.invalid:5432/x"


def test_dsn_falls_back_to_the_default_when_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert db.dsn() == db.DEFAULT_DSN


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
