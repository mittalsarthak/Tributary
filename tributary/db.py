import os
import psycopg

DEFAULT_DSN = "postgresql://tributary:tributary@localhost:5433/tributary"


def dsn() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DSN


def connect(target: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(target or dsn())
    conn.autocommit = autocommit
    return conn
