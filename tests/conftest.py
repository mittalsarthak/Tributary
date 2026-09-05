import os
import uuid
import pytest
import psycopg
from psycopg import sql

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


def _drop_tributary_schemas(conn) -> None:
    """Drop every Tributary-managed schema: `main`, `_tributary`, and any
    `br_*` branch schema. Shared by the `client` fixture below (setup *and*
    teardown, so a test's own failure never leaks branches into the next
    one) -- same pattern `ws`'s teardown (above) and `test_seed.py`'s
    autouse fixture already use.
    """
    conn.execute("DROP SCHEMA IF EXISTS main CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS _tributary CASCADE")
    for (s,) in conn.execute("SELECT nspname FROM pg_namespace "
                              "WHERE nspname LIKE 'br\\_%'").fetchall():
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(s)))


@pytest.fixture
def client(conn, pg_dsn, monkeypatch):
    """A FastAPI `TestClient` wired to the test Postgres container, with a
    freshly seeded demo workspace (Task 11, ruling R7).

    `DATABASE_URL`/`TRIBUTARY_AUTOSEED=1` are set *before* the app's ASGI
    lifespan starts, so `with TestClient(app) as c:` -- which runs the
    app's real startup handler -- both points `tributary.db.dsn()` at this
    test's database and doubles as a live regression test of the
    `TRIBUTARY_AUTOSEED` startup wiring itself: if that wiring silently
    stopped calling `store.init`/`seed.ensure_demo`, every test using this
    fixture would fail immediately (no `main` branch to find).

    Every Tributary-managed schema is dropped before *and* after, so each
    test starts from a clean slate regardless of what a previous test
    created (branches, merges, ...) -- required because `tributary.web.app`
    holds its uncommitted-edit/merge state in module-level dicts that are
    only reset by the app's own startup handler, which runs fresh on every
    `with TestClient(app)` block here.
    """
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    monkeypatch.setenv("TRIBUTARY_AUTOSEED", "1")
    _drop_tributary_schemas(conn)

    from fastapi.testclient import TestClient
    from tributary.web.app import app

    with TestClient(app) as test_client:
        yield test_client

    _drop_tributary_schemas(conn)


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
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(s)))
