"""Canonicalisation of Postgres type and default spellings.

Postgres reports the same type or default expression under several different
spellings depending on where the query comes from (``information_schema`` vs
``pg_catalog``, casing, whitespace). Snapshots must normalise these onto one
canonical spelling *at construction time* so that two snapshots of an
identical schema always compare equal — everything downstream (diff, merge,
plan) assumes this has already happened exactly once, here.
"""

import re

_ALIASES = {
    "integer": "int4", "int": "int4", "int4": "int4",
    "bigint": "int8", "int8": "int8",
    "smallint": "int2", "int2": "int2",
    "boolean": "bool", "bool": "bool",
    "character varying": "varchar", "varchar": "varchar",
    "character": "bpchar", "char": "bpchar", "bpchar": "bpchar",
    "double precision": "float8", "float8": "float8",
    "real": "float4", "float4": "float4",
    "timestamp without time zone": "timestamp", "timestamp": "timestamp",
    "timestamp with time zone": "timestamptz", "timestamptz": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
    "numeric": "numeric", "decimal": "numeric",
    "text": "text", "uuid": "uuid", "json": "json", "jsonb": "jsonb",
    "date": "date", "bytea": "bytea", "inet": "inet",
}

# Splits "<base>(<mod>) <tail>" — e.g. "varchar(50)", "numeric(10,2)", or
# "timestamp(3) with time zone" where a modifier is sandwiched between the
# base type name and a trailing qualifier. `tail` is captured (not discarded)
# so that e.g. "with time zone" survives and the base+tail combination can
# still be looked up in _ALIASES.
_MOD = re.compile(r"^(?P<base>.*?)\s*\((?P<mod>[^)]*)\)\s*(?P<tail>.*)$")
_NOW = {"current_timestamp", "now()", "current_timestamp()"}


def norm_type(raw: str) -> str:
    """Collapse Postgres type spellings onto one canonical name.

    A type with no modifier (``varchar``) is a genuinely different type from
    one with a modifier (``varchar(50)``) and the two are never collapsed
    together.
    """
    s = " ".join(raw.strip().lower().split())
    arr = ""
    while s.endswith("[]"):
        arr += "[]"
        s = s[:-2].strip()
    m = _MOD.match(s)
    if m:
        base = m.group("base").strip()
        mod = "(" + ",".join(p.strip() for p in m.group("mod").split(",")) + ")"
        tail = m.group("tail").strip()
        # Re-join the base with its tail (e.g. "timestamp" + "with time
        # zone") before alias lookup, so the modifier in between does not
        # sever the two halves of the type name and silently drop the tail.
        lookup = f"{base} {tail}".strip() if tail else base
        canon = _ALIASES.get(lookup, lookup)
        return f"{canon}{mod}{arr}"
    canon = _ALIASES.get(s, s)
    return f"{canon}{arr}"


def norm_default(raw: str | None, coltype: str) -> str | None:
    """Strip casts Postgres adds back itself, and unify now()/CURRENT_TIMESTAMP."""
    if raw is None:
        return None
    s = raw.strip()
    if s.lower() in _NOW:
        return "now()"
    # Drop a trailing ::type cast when it merely restates the column's own type.
    m = re.match(r"^(?P<val>.*?)::(?P<cast>[a-z][a-z0-9_ ]*(\([^)]*\))?)$", s, re.I)
    if m and norm_type(m.group("cast")) == norm_type(coltype):
        return m.group("val").strip()
    return s
