import os
import uuid
import pytest
import psycopg

from tributary import store


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    existing = os.environ.get("TRIBUTARY_TEST_DSN")
    if existing:
        yield existing
        return
    from testcontainers.postgres import PostgresContainer
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
def conn(pg_dsn):
    c = psycopg.connect(pg_dsn)
    c.autocommit = True
    yield c
    c.close()


@pytest.fixture
def fresh_schema(conn):
    name = "t_" + uuid.uuid4().hex[:12]
    conn.execute(f'CREATE SCHEMA "{name}"')
    yield name
    conn.execute(f'DROP SCHEMA "{name}" CASCADE')


@pytest.fixture
def ws(conn):
    """A `conn` with `_tributary` initialised and `main` adopted, carrying one
    seed table (`main.users`). Shared by Task 6's branch/commit tests and
    Task 7's merge tests (R2) -- defined here, not in a single test module,
    so both can import it.
    """
    store.init(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS main")
    conn.execute("CREATE TABLE IF NOT EXISTS main.users (id bigserial PRIMARY KEY, email text)")
    store.ensure_main(conn)
    yield conn
    conn.execute("DROP SCHEMA IF EXISTS main CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS _tributary CASCADE")
    for (s,) in conn.execute("SELECT nspname FROM pg_namespace "
                              "WHERE nspname LIKE 'br\\_%'").fetchall():
        conn.execute(f'DROP SCHEMA "{s}" CASCADE')
