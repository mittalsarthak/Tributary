import os
import uuid
import pytest
import psycopg


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
