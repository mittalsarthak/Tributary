import pytest
from tributary.canonical import norm_type, norm_default


@pytest.mark.parametrize("raw,expected", [
    ("character varying(50)", "varchar(50)"),
    ("varchar(50)", "varchar(50)"),
    ("integer", "int4"),
    ("int", "int4"),
    ("int4", "int4"),
    ("bigint", "int8"),
    ("boolean", "bool"),
    ("timestamp without time zone", "timestamp"),
    ("timestamp with time zone", "timestamptz"),
    ("numeric(10,2)", "numeric(10,2)"),
    ("double precision", "float8"),
    ("text", "text"),
    ("character varying", "varchar"),
    ("VARCHAR(50)", "varchar(50)"),
    ("varchar (50)", "varchar(50)"),
    # R8.3: modifier + tail must not drop the tail — timestamptz/timetz stay distinct
    # from timestamp/time even when a precision modifier is present.
    ("timestamp(3) with time zone", "timestamptz(3)"),
    ("time(2) with time zone", "timetz(2)"),
])
def test_norm_type_collapses_aliases(raw, expected):
    assert norm_type(raw) == expected


@pytest.mark.parametrize("raw,coltype,expected", [
    ("now()", "timestamptz", "now()"),
    ("CURRENT_TIMESTAMP", "timestamptz", "now()"),
    ("'x'::character varying", "varchar", "'x'"),
    ("'x'::text", "text", "'x'"),
    ("0", "int4", "0"),
    ("nextval('s'::regclass)", "int4", "nextval('s'::regclass)"),
    (None, "int4", None),
])
def test_norm_default_strips_redundant_casts(raw, coltype, expected):
    assert norm_default(raw, coltype) == expected
