from tributary.model import Snapshot, Table, Column, Constraint, Index


def test_snapshot_json_round_trip_is_lossless():
    snap = Snapshot(tables={
        "users": Table(
            name="users",
            columns={"id": Column("id", "int8", False, None, 1),
                     "email": Column("email", "varchar(255)", True, "'x'", 2)},
            constraints={"users_pkey": Constraint("users_pkey", "p",
                                                  "PRIMARY KEY (id)", ("id",))},
            indexes={"ix_email": Index("ix_email", "CREATE INDEX ...", ("email",), True)},
        )
    })
    assert Snapshot.from_json(snap.to_json()) == snap


def test_snapshot_json_is_key_sorted_for_stable_diffs():
    snap = Snapshot(tables={
        "b": Table("b"), "a": Table("a"),
    })
    assert list(snap.to_json()["tables"].keys()) == ["a", "b"]
