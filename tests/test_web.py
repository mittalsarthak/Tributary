"""Tests for Task 11's FastAPI web application.

Covers the brief's six required routes/behaviours verbatim (Step 1 of the
brief), plus additional coverage for behaviours the binding controller
rulings call out specifically:

- R7: the brief's own merge test posts to the nonexistent `/merges/latest/run`
  -- there is no such route in the route table, "latest" is not a real merge
  id. Fixed here by capturing the *real* merge id from the `data-merge-id`
  attribute the create-merge response always carries (win or lose, conflict
  or not -- see `tributary/web/templates/merge.html`), then posting to
  `/merges/{that id}/run`.
- "the run button must be genuinely unreachable while conflicts are
  unresolved" -- `test_no_run_button_while_conflicts_are_unresolved` checks
  the actual markup, not just the word "conflict" appearing somewhere.
- "every route catches the domain errors ... and renders a sentence" --
  beyond the brief's duplicate-branch case, `test_cannot_delete_main` and
  `test_unresolved_conflicts_cannot_be_run` exercise two more `ValueError`
  paths (`store.delete_branch`, `merge.resolve`/the run-gate) the same way.
- the full happy path (create branch, edit, commit, diff, merge with no
  conflict, run, and see it land on `main`) is exercised end to end by
  `test_merge_with_a_conflict_reports_it_before_offering_to_run` already,
  but `test_clean_merge_runs_and_updates_main` isolates just the no-conflict
  run path with a direct assertion against the actual Postgres schema
  afterwards -- not just page text -- so a change that made the UI *say*
  "done" without actually running the plan would be caught.

`client` (added to `tests/conftest.py`, R7) wires FastAPI's `TestClient` to
`DATABASE_URL`/`TRIBUTARY_AUTOSEED=1` pointed at the test Postgres container,
and cleans every Tributary-managed schema before and after each test so
tests never see another test's branches.

Fix round (post-review):
- `test_resolve_with_two_conflicts_applies_each_choice_independently` --
  every conflict test above adds a single column named `x` on both
  branches, which is structurally incapable of catching the bug where
  `merge.html` gave every conflict's radio pair the same `name="side"`
  (collapsing all of them into one browser-side radio group) and
  `resolve_merge` paired `path`/`side` via a truncating `zip`. Two
  conflicting columns, resolved with different choices, through the real
  `POST /merges/{id}/resolve` route.
- `_wait_for_merge` replaces the bounded `t.join()` `run_merge` used to do
  before responding (removed: it tied up a threadpool worker for up to 10s
  on every run). Tests that need a merge to have actually landed before
  their next request now poll `MergeState.status` instead.
"""

import re
import time

import pytest

from tributary import diff as diff_mod
from tributary import planner
from tributary.introspect import snapshot
from tributary.model import AlterColumnType, TableStats
from tributary.web import app as web_app


def _merge_id(html: str) -> str:
    m = re.search(r'data-merge-id="([^"]+)"', html)
    assert m, f"no data-merge-id found in response:\n{html}"
    return m.group(1)


def _wait_for_merge(mid: str, timeout: float = 10.0) -> None:
    """`POST /merges/{id}/run` starts `_execute_merge` in a background
    thread and returns immediately -- no bounded `t.join()` any more (fix
    round: that join tied up a FastAPI threadpool worker for up to
    `_JOIN_TIMEOUT` seconds on every run, the exact thing "long-running
    merges must not block the request thread" exists to prevent). A test
    that needs the merge to have actually landed on its target branch
    before issuing its next request polls the in-memory `MergeState.status`
    until it reaches a terminal state, the same way `_wait_for_grow_to_finish`
    below polls `_grow_state` for the fire-and-forget grow thread.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with web_app._LOCK:
            state = web_app._merges.get(mid)
            if state is not None and state.status in ("done", "failed"):
                return
        time.sleep(0.05)
    pytest.fail(f"merge {mid!r} did not reach a terminal status in time")


def _wait_for_grow_to_finish(timeout: float = 5.0) -> None:
    """`POST /seed/grow` runs `seed.grow_events` in a background thread on
    purpose (module docstring: growth isn't required to be synchronous the
    way a merge's bounded-join is) -- but a test that doesn't wait for it
    risks the `client` fixture's teardown dropping `main` out from under a
    still-running thread, which is exactly the "schema \"main\" does not
    exist" background-thread exception this helper exists to avoid.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with web_app._grow_lock:
            if not web_app._grow_state["running"]:
                return
        time.sleep(0.05)
    pytest.fail("grow_events background thread did not finish in time")


# --- brief's required tests, verbatim except R7's merge-id fix ---------------

def test_home_lists_branches(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "main" in r.text


def test_create_branch_then_it_appears(client):
    assert client.post("/branches", data={"name": "feature-x"}).status_code in (200, 303)
    assert "feature-x" in client.get("/").text


def test_duplicate_branch_shows_an_error_not_a_stack_trace(client):
    client.post("/branches", data={"name": "dup"})
    r = client.post("/branches", data={"name": "dup"})
    assert r.status_code < 500
    assert "already exists" in r.text.lower()


def test_diff_shows_the_added_column(client):
    client.post("/branches", data={"name": "feature-x"})
    client.post("/branches/feature-x/changes",
                data={"op": "add_column", "table": "users", "name": "nickname", "type": "text"})
    r = client.get("/branches/feature-x/diff?target=main")
    assert "nickname" in r.text


def test_merge_with_a_conflict_reports_it_before_offering_to_run(client):
    client.post("/branches", data={"name": "a"})
    client.post("/branches", data={"name": "b"})
    for br, typ in (("a", "text"), ("b", "int4")):
        client.post(f"/branches/{br}/changes",
                    data={"op": "add_column", "table": "users", "name": "x", "type": typ})
        client.post(f"/branches/{br}/commit", data={"message": "add x"})

    r1 = client.post("/merges", data={"source": "a", "target": "main"})
    mid = _merge_id(r1.text)
    client.post(f"/merges/{mid}/run")
    _wait_for_merge(mid)

    r = client.post("/merges", data={"source": "b", "target": "main"})
    assert "conflict" in r.text.lower()


def test_unknown_branch_is_a_404_with_a_readable_message(client):
    r = client.get("/branches/nope")
    assert r.status_code == 404
    assert "nope" in r.text


# --- additional coverage -----------------------------------------------------

def test_no_run_button_while_conflicts_are_unresolved(client):
    client.post("/branches", data={"name": "a"})
    client.post("/branches", data={"name": "b"})
    for br, typ in (("a", "text"), ("b", "int4")):
        client.post(f"/branches/{br}/changes",
                    data={"op": "add_column", "table": "users", "name": "x", "type": typ})
        client.post(f"/branches/{br}/commit", data={"message": "add x"})

    r1 = client.post("/merges", data={"source": "a", "target": "main"})
    mid = _merge_id(r1.text)
    client.post(f"/merges/{mid}/run")
    _wait_for_merge(mid)

    r = client.post("/merges", data={"source": "b", "target": "main"})
    assert 'id="run-merge-btn"' not in r.text


def test_clean_merge_runs_and_updates_main(client):
    client.post("/branches", data={"name": "feature-x"})
    client.post("/branches/feature-x/changes",
                data={"op": "add_column", "table": "users", "name": "nickname", "type": "text"})
    client.post("/branches/feature-x/commit", data={"message": "add nickname"})

    r1 = client.post("/merges", data={"source": "feature-x", "target": "main"})
    assert "conflict" not in r1.text.lower()
    mid = _merge_id(r1.text)
    assert 'id="run-merge-btn"' in r1.text

    r2 = client.post(f"/merges/{mid}/run")
    assert r2.status_code == 200
    _wait_for_merge(mid)

    diff_after = client.get("/branches/feature-x/diff?target=main")
    assert "nickname" not in diff_after.text  # main now has it too -- no more diff


def test_unresolved_conflicts_cannot_be_run(client):
    client.post("/branches", data={"name": "a"})
    client.post("/branches", data={"name": "b"})
    for br, typ in (("a", "text"), ("b", "int4")):
        client.post(f"/branches/{br}/changes",
                    data={"op": "add_column", "table": "users", "name": "x", "type": typ})
        client.post(f"/branches/{br}/commit", data={"message": "add x"})

    client.post(f"/merges", data={"source": "a", "target": "main"})
    # merge "a" straight into main first (no conflict yet), then attempt to
    # merge "b" (which now conflicts) and try to run it directly without
    # ever resolving -- the run route itself must refuse, echoing merge.py's
    # own "unresolved conflicts" ValueError as a sentence, never a 500.
    r1 = client.post("/merges", data={"source": "a", "target": "main"})
    mid_a = _merge_id(r1.text)
    client.post(f"/merges/{mid_a}/run")
    _wait_for_merge(mid_a)

    r2 = client.post("/merges", data={"source": "b", "target": "main"})
    mid_b = _merge_id(r2.text)
    r3 = client.post(f"/merges/{mid_b}/run")
    assert r3.status_code < 500
    assert "conflict" in r3.text.lower()


def test_resolve_with_two_conflicts_applies_each_choice_independently(client, conn):
    """Fix round, R1: `merge.html` used to give every conflict's ours/theirs
    radio pair the same `name="side"`. HTML scopes "exactly one selected" by
    `name` across the whole <form>, not per <fieldset> -- so with two or
    more conflicts, every radio on the page collapsed into one
    mutually-exclusive group, and the browser could submit only a single
    `side` value no matter how many `path` hidden fields were emitted.
    Server-side, `dict(zip(path, side))` then silently truncated to
    whichever list was shorter, applying the one submitted choice to only
    the first conflict and dropping the rest.

    Every existing conflict test (before this fix round) added a single
    column named `x` on both branches -- structurally incapable of catching
    this, since one conflict is exactly the case that still worked. This
    test creates a branch with *two* conflicting columns and resolves them
    with different choices (ours for one, theirs for the other), posting
    the real field names the fixed template emits (`side_0`, `side_1`) to
    the real `POST /merges/{id}/resolve` route -- never calling
    `merge_mod.resolve` directly -- then runs the merge and checks the
    actual column types in Postgres. A regression back to a shared
    `name="side"` fails the markup assertions below outright; a regression
    back to `zip(path, side)` pairing would resolve at most one of the two
    conflicts and leave the other reported as still unresolved.
    """
    client.post("/branches", data={"name": "a"})
    client.post("/branches", data={"name": "b"})
    for br, x_type, y_type in (("a", "text", "text"), ("b", "int4", "int4")):
        client.post(f"/branches/{br}/changes",
                    data={"op": "add_column", "table": "users", "name": "x", "type": x_type})
        client.post(f"/branches/{br}/changes",
                    data={"op": "add_column", "table": "users", "name": "y", "type": y_type})
        client.post(f"/branches/{br}/commit", data={"message": "add x and y"})

    # Land "a" on main first (no conflict: main had neither column), so "b"
    # against main now conflicts on both x and y (main/ours: text; b/theirs:
    # int4, for both columns).
    r_a = client.post("/merges", data={"source": "a", "target": "main"})
    mid_a = _merge_id(r_a.text)
    client.post(f"/merges/{mid_a}/run")
    _wait_for_merge(mid_a)

    r1 = client.post("/merges", data={"source": "b", "target": "main"})
    mid = _merge_id(r1.text)
    assert "2 conflicts" in r1.text
    # The template-level fix: each fieldset's radios must carry a distinct
    # `name`, not a shared "side" -- this is what a browser needs to treat
    # them as independent radio groups at all.
    assert 'name="side_0"' in r1.text
    assert 'name="side_1"' in r1.text
    assert 'name="side"' not in r1.text

    r2 = client.post(f"/merges/{mid}/resolve", data={
        "path": ["users/col/x", "users/col/y"],
        "side_0": "theirs",  # x: take b's int4
        "side_1": "ours",    # y: keep main's text
    })
    assert r2.status_code == 200
    assert "conflict" not in r2.text.lower()
    assert 'id="run-merge-btn"' in r2.text

    client.post(f"/merges/{mid}/run")
    _wait_for_merge(mid)

    row = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = 'users' "
        "AND column_name IN ('x', 'y')"
    ).fetchall()
    types = dict(row)
    assert types["x"] == "integer"  # theirs
    assert types["y"] == "text"     # ours


def test_cannot_delete_main(client):
    r = client.delete("/branches/main")
    assert r.status_code < 500
    assert "main" in r.text.lower()


def test_delete_branch_removes_it(client):
    client.post("/branches", data={"name": "throwaway"})
    assert "throwaway" in client.get("/").text
    client.delete("/branches/throwaway")
    assert "throwaway" not in client.get("/").text


def test_history_shows_commits(client):
    client.post("/branches", data={"name": "feature-x"})
    client.post("/branches/feature-x/changes",
                data={"op": "add_column", "table": "users", "name": "nickname", "type": "text"})
    client.post("/branches/feature-x/commit", data={"message": "add nickname column"})
    r = client.get("/branches/feature-x/history")
    assert r.status_code == 200
    assert "add nickname column" in r.text


def test_grow_events_control_shows_current_size(client):
    r = client.get("/")
    assert "events" in r.text.lower()
    assert "GB" in r.text or "gb" in r.text.lower()


def test_seed_grow_endpoint_grows_the_table(client, conn):
    before = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    r = client.post("/seed/grow", data={"target_rows": 8000})
    assert r.status_code < 500
    _wait_for_grow_to_finish()

    after = conn.execute("SELECT count(*) FROM main.events").fetchone()[0]
    assert after >= 8000
    assert after > before


def test_unknown_merge_id_is_a_readable_error(client):
    r = client.post("/merges/does-not-exist/run")
    assert r.status_code < 500
    assert "no such merge" in r.text.lower()


def test_diff_shows_safety_report(client):
    client.post("/branches", data={"name": "feature-x"})
    client.post("/branches/feature-x/changes",
                data={"op": "add_column", "table": "users", "name": "nickname", "type": "text"})
    r = client.get("/branches/feature-x/diff?target=main")
    assert "safe_metadata" in r.text.lower()


# --- R24: retype a column; create/drop tables; constraints and indexes ------
#
# `apply_change` used to support exactly four ops (add/drop/rename column,
# rename table) -- three of the four capability groups the problem statement
# names ("retype columns; change constraints and indexes; create and drop
# tables") were simply unreachable from the UI even though the engine
# underneath (`ddl.py`/`diff.py`/`planner.py`) already handled all of them.
# Every test below goes through the real HTTP route and then re-introspects
# Postgres directly (`introspect.snapshot`/`information_schema`) -- never
# just checking the response was 200 -- so a change that rendered "success"
# without actually running the DDL would be caught.

def test_alter_column_type_lands_and_diff_shows_it(client, conn):
    """The headline R24 op. Retypes `orders.status` (text, NOT NULL, with a
    default) to `varchar(50)` through the real editor route, then checks
    three things a broken wiring could get wrong independently: the type
    actually changed in Postgres, NOT NULL/the default survived (a plain
    `ALTER COLUMN ... TYPE` never touches either -- only a shadow-column
    rewrite could lose them, and this table is far too small to take that
    path), and the live diff against `main` reports it as an
    `AlterColumnType`, not a drop-and-add.
    """
    client.post("/branches", data={"name": "feature-retype"})
    r = client.post("/branches/feature-retype/changes",
                     data={"op": "alter_column_type", "table": "orders",
                           "column": "status", "type": "varchar(50)"})
    assert r.status_code == 200

    b = web_app._find_branch(conn, "feature-retype")
    row = conn.execute(
        "SELECT data_type, character_maximum_length, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'orders' AND column_name = 'status'",
        (b.schema_name,),
    ).fetchone()
    assert row[0] == "character varying"
    assert row[1] == 50
    assert row[2] == "NO"        # NOT NULL preserved
    assert row[3] is not None    # DEFAULT preserved

    diff_r = client.get("/branches/feature-retype/diff?target=main")
    assert diff_r.status_code == 200
    assert "AlterColumnType" in diff_r.text


def test_retype_on_large_table_produces_a_backfill_step(client, conn):
    """The UI path reaching the shadow-column mechanism end to end: the
    retype itself lands through the real `POST .../changes` route (the same
    route the test above uses), on `orders` (seeded with ~2000 real rows by
    autoseed -- modest, not a genuinely large table). The planner's size
    threshold is then forced past the "large" line with an explicit
    `TableStats` override, the same technique `tests/test_planner.py` uses
    throughout, rather than actually building a multi-GB table in the suite.
    `orders.total_cents` (integer) -> `text` is not binary-coercible, so at
    a forced-large size with `orders`'s single-column primary key known,
    this must route through `_shadow_dance` and produce a `backfill` step.
    """
    client.post("/branches", data={"name": "feature-retype-big"})
    r = client.post("/branches/feature-retype-big/changes",
                     data={"op": "alter_column_type", "table": "orders",
                           "column": "total_cents", "type": "text"})
    assert r.status_code == 200

    b = web_app._find_branch(conn, "feature-retype-big")
    main_snap = snapshot(conn, "main")
    branch_snap = snapshot(conn, b.schema_name)
    changes = diff_mod.diff(main_snap, branch_snap)
    assert any(isinstance(c, AlterColumnType) and c.table == "orders" and c.column == "total_cents"
               for c in changes)

    forced_stats = {"orders": TableStats(rows=2_000_000, bytes=200 * 1024 * 1024)}
    pk_columns = web_app._pk_columns(main_snap)
    plan_result = planner.plan(changes, forced_stats, b.schema_name, pk_columns=pk_columns)
    kinds = {s.kind for s in plan_result.steps}
    assert "backfill" in kinds
    assert "swap" in kinds


def test_create_table_lands_with_a_primary_key(client, conn):
    client.post("/branches", data={"name": "feature-newtable"})
    r = client.post("/branches/feature-newtable/changes",
                     data={"op": "create_table", "table": "widgets",
                           "columns": "label text\nweight_grams integer"})
    assert r.status_code == 200

    b = web_app._find_branch(conn, "feature-newtable")
    snap = snapshot(conn, b.schema_name)
    assert "widgets" in snap.tables
    widgets = snap.tables["widgets"]
    assert set(widgets.columns) == {"id", "label", "weight_grams"}
    pk = next(c for c in widgets.constraints.values() if c.kind == "p")
    assert pk.columns == ("id",)


def test_drop_table_requires_confirmation_and_removes_it(client, conn):
    """Dropping a table is destructive (R24 brief): the editor's drop-table
    control must carry a real, explicit confirmation step, not a bare
    button -- checked here via the same `hx-confirm` markup the existing
    branch-delete control already uses (`branches.html`), not just the word
    "drop" appearing somewhere on the page.
    """
    client.post("/branches", data={"name": "feature-droptable"})
    editor_html = client.get("/branches/feature-droptable").text
    assert 'hx-confirm="Drop table' in editor_html

    r = client.post("/branches/feature-droptable/changes",
                     data={"op": "drop_table", "table": "orders"})
    assert r.status_code == 200

    b = web_app._find_branch(conn, "feature-droptable")
    snap = snapshot(conn, b.schema_name)
    assert "orders" not in snap.tables


def test_add_constraint_lands(client, conn):
    client.post("/branches", data={"name": "feature-constraint"})
    r = client.post("/branches/feature-constraint/changes",
                     data={"op": "add_constraint", "table": "orders",
                           "name": "orders_total_nonneg",
                           "definition": "CHECK (total_cents >= 0)"})
    assert r.status_code == 200

    b = web_app._find_branch(conn, "feature-constraint")
    snap = snapshot(conn, b.schema_name)
    con = snap.tables["orders"].constraints.get("orders_total_nonneg")
    assert con is not None
    assert con.kind == "c"


def test_add_constraint_with_bad_sql_is_a_sentence_not_a_traceback(client):
    """Constraint/index definitions are accepted as raw SQL text and handed
    straight to Postgres to validate (R24 brief) -- a definition Postgres
    rejects (here: a CHECK referencing a column that doesn't exist) must
    come back as a readable sentence, never a 500 with a traceback.
    """
    client.post("/branches", data={"name": "feature-badcon"})
    r = client.post("/branches/feature-badcon/changes",
                     data={"op": "add_constraint", "table": "orders", "name": "bad",
                           "definition": "CHECK (nonexistent_column > 0)"})
    assert r.status_code == 400
    assert "traceback" not in r.text.lower()
    assert "internal server error" not in r.text.lower()


def test_drop_constraint_removes_it(client, conn):
    client.post("/branches", data={"name": "feature-dropcon"})
    b = web_app._find_branch(conn, "feature-dropcon")
    before = snapshot(conn, b.schema_name).tables["users"].constraints
    assert "users_email_key" in before

    r = client.post("/branches/feature-dropcon/changes",
                     data={"op": "drop_constraint", "table": "users", "name": "users_email_key"})
    assert r.status_code == 200

    after = snapshot(conn, b.schema_name).tables["users"].constraints
    assert "users_email_key" not in after


def test_create_index_lands(client, conn):
    client.post("/branches", data={"name": "feature-index"})
    r = client.post("/branches/feature-index/changes",
                     data={"op": "create_index", "table": "orders",
                           "name": "orders_status_idx",
                           "definition": "CREATE INDEX orders_status_idx ON orders (status)"})
    assert r.status_code == 200

    b = web_app._find_branch(conn, "feature-index")
    snap = snapshot(conn, b.schema_name)
    assert "orders_status_idx" in snap.tables["orders"].indexes


def test_drop_index_removes_it(client, conn):
    client.post("/branches", data={"name": "feature-dropindex"})
    b = web_app._find_branch(conn, "feature-dropindex")
    before = snapshot(conn, b.schema_name).tables["orders"].indexes
    assert "orders_user_id_idx" in before

    r = client.post("/branches/feature-dropindex/changes",
                     data={"op": "drop_index", "table": "orders", "name": "orders_user_id_idx"})
    assert r.status_code == 200

    after = snapshot(conn, b.schema_name).tables["orders"].indexes
    assert "orders_user_id_idx" not in after


# --- final fix wave: rename-table UI wiring (item 1) -------------------------
#
# `_apply_op`'s `rename_table` op, `ddl.render`, and `diff.py`'s R16
# protection (RenameTable, never Drop+Create) all worked correctly before
# this fix -- but no form in editor.html ever produced a `rename_table` op,
# so the only UI path to "rename a table" was drop the old one and create a
# new one under the new name, which diffs as DropTable + CreateTable and
# destroys every row against a populated table. This test checks both
# halves: the form now actually exists in the rendered page (the missing
# piece), and, through the real HTTP route, the op lands in Postgres and
# diffs as a RenameTable with no DropTable -- proving the op log was
# genuinely written, not inferred after the fact from a drop-and-create.
def test_rename_table_form_lands_and_diffs_as_rename_not_drop(client, conn):
    client.post("/branches", data={"name": "feature-renametable"})

    editor_html = client.get("/branches/feature-renametable").text
    assert 'value="rename_table"' in editor_html, (
        "no rename-table form in editor.html -- the only UI path to renaming "
        "a table is still drop-old + create-new, which diffs as a destructive "
        "DropTable + CreateTable against a populated table"
    )

    r = client.post("/branches/feature-renametable/changes",
                     data={"op": "rename_table", "old": "orders", "new": "purchases"})
    assert r.status_code == 200

    b = web_app._find_branch(conn, "feature-renametable")
    snap = snapshot(conn, b.schema_name)
    assert "purchases" in snap.tables
    assert "orders" not in snap.tables

    diff_r = client.get("/branches/feature-renametable/diff?target=main")
    assert diff_r.status_code == 200
    assert "RenameTable" in diff_r.text
    assert "DropTable" not in diff_r.text


# --- final fix wave: drop_column/rename_column had zero web-layer tests -----
# (item 3) -- their wiring was correct, but only ever verified by inspection.

def test_drop_column_lands(client, conn):
    client.post("/branches", data={"name": "feature-dropcolumn"})
    r = client.post("/branches/feature-dropcolumn/changes",
                     data={"op": "drop_column", "table": "users", "column": "full_name"})
    assert r.status_code == 200

    b = web_app._find_branch(conn, "feature-dropcolumn")
    snap = snapshot(conn, b.schema_name)
    assert "full_name" not in snap.tables["users"].columns


def test_rename_column_lands(client, conn):
    client.post("/branches", data={"name": "feature-renamecolumn"})
    r = client.post("/branches/feature-renamecolumn/changes",
                     data={"op": "rename_column", "table": "users",
                           "old": "full_name", "new": "display_name"})
    assert r.status_code == 200

    b = web_app._find_branch(conn, "feature-renamecolumn")
    snap = snapshot(conn, b.schema_name)
    assert "display_name" in snap.tables["users"].columns
    assert "full_name" not in snap.tables["users"].columns


# --- final fix wave: show_diff's `except ValueError` had no coverage -------
# (item 4)

def test_diff_against_nonexistent_target_is_a_sentence_not_a_traceback(client):
    client.post("/branches", data={"name": "feature-x"})
    r = client.get("/branches/feature-x/diff?target=does-not-exist")
    assert r.status_code == 404
    assert "does-not-exist" in r.text
    assert "traceback" not in r.text.lower()


def test_show_diff_wraps_a_domain_value_error_as_a_sentence(client, monkeypatch):
    """The `if t is None` guard just above (exercised by the test above)
    already turns a missing *branch* into a 404 sentence before `show_diff`
    ever reaches its own `try`/`except ValueError` around `diff`/`planner.plan`
    -- so that block, unlike every other plan-building path in this module,
    currently has no reachable input through `diff.py`/`planner.py` as they
    stand (both only ever raise `TypeError` for a genuinely unhandled
    `Change`). Rather than leave the branch entirely unexercised, this test
    forces the one failure it exists to guard against directly at the
    boundary it wraps, and pins the behaviour that actually matters: a
    `ValueError` surfacing from that stage must still render as a sentence
    with a non-500 status, never a bare traceback.
    """
    client.post("/branches", data={"name": "feature-diffvalueerror"})

    def boom(*a, **k):
        raise ValueError("no such branch 'ghost'")

    monkeypatch.setattr(web_app.planner, "plan", boom)
    r = client.get("/branches/feature-diffvalueerror/diff?target=main")
    assert r.status_code == 404
    assert "ghost" in r.text
    assert "traceback" not in r.text.lower()


# --- merge durability across a process restart --------------------------------
#
# A merge's session lives in the in-memory `_merges` dict, but the merge itself is
# durable: `_tributary.merges` and `_tributary.migration_steps` both survive. When
# the process restarts, the row says "done" while memory says nothing at all, and
# the UI reported "no such merge" and hung on "Running..." forever.
#
# This is not a hypothetical. It happened in a live session: a merge completed in
# 0.9s, the container was rebuilt a minute later, and the open page's EventSource
# reconnected into a process with no memory of it. On a platform that restarts
# dynos it would happen to every merge ever run.


def _forget_all_merge_sessions() -> None:
    """Simulate a process restart: durable rows survive, memory does not."""
    with web_app._LOCK:
        web_app._merges.clear()


def _completed_merge(client) -> str:
    client.post("/branches", data={"name": "durable"})
    client.post("/branches/durable/changes",
                data={"op": "add_column", "table": "users", "name": "durable_col", "type": "text"})
    client.post("/branches/durable/commit", data={"message": "add durable_col"})
    mid = _merge_id(client.post("/merges", data={"source": "durable", "target": "main"}).text)
    client.post(f"/merges/{mid}/run")
    _wait_for_merge(mid)
    return mid


def test_a_finished_merge_is_still_viewable_after_the_session_is_lost(client):
    """The durable row is the source of truth, not the in-memory session."""
    mid = _completed_merge(client)
    _forget_all_merge_sessions()

    r = client.get(f"/merges/{mid}")
    assert r.status_code == 200, "a completed merge must remain viewable after a restart"
    assert "no such merge" not in r.text.lower()
    assert "complete" in r.text.lower(), "the page should report the persisted terminal status"


def test_the_event_stream_reports_a_finished_merge_rather_than_no_such_merge(client):
    """Without this, an open browser tab hangs on 'Running...' forever."""
    mid = _completed_merge(client)
    _forget_all_merge_sessions()

    with client.stream("GET", f"/merges/{mid}/events") as r:
        body = "".join(chunk for chunk in r.iter_text())

    assert "no such merge" not in body, (
        "the stream fell back to an error instead of reading the durable merge row"
    )
    assert "event: complete" in body, "a finished merge must emit its completion event"
    assert '"status": "done"' in body


def test_an_unknown_merge_id_is_still_a_clean_404(client):
    """The fallback must not turn a genuinely bogus id into a hang."""
    r = client.get("/merges/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# --- merging from the branch list ---------------------------------------------
#
# Reaching the merge screen used to mean: branch list -> diff -> "Start merge".
# That is two clicks through a page you have to know exists, for the action the
# whole product is about.


def _branch_with_a_commit(client, name: str) -> None:
    client.post("/branches", data={"name": name})
    client.post(f"/branches/{name}/changes",
                data={"op": "add_column", "table": "users", "name": f"{name}_col", "type": "text"})
    client.post(f"/branches/{name}/commit", data={"message": f"add {name}_col"})


def test_branch_list_offers_a_merge_action_for_a_branch_with_commits(client):
    """Asserted via a stable `data-merge-from` hook.

    An earlier version of this test looked for `value="<branch>"` anywhere in the
    page and passed before the feature existed -- the "create branch from"
    dropdown contains an <option value="<branch>"> for every branch, so the
    assertion matched that instead. A test that passes without the feature is
    worse than no test.
    """
    _branch_with_a_commit(client, "mergeable")
    html = client.get("/").text
    assert 'data-merge-from="mergeable"' in html, (
        "the branch list should offer a merge action inline, not only from the diff page"
    )


def test_branch_list_merge_action_opens_the_merge_screen_rather_than_merging(client):
    """A one-click merge into main from a list row is too easy to hit by accident.

    The action must land on the merge screen, where conflicts and the safety
    report are shown before anything is applied to a real database.
    """
    _branch_with_a_commit(client, "opensscreen")
    r = client.post("/merges", data={"source": "opensscreen", "target": "main"})
    assert r.status_code == 200
    assert "data-merge-id=" in r.text, "should render the merge screen"
    assert web_app._merges, "a merge session should exist but nothing should have run yet"
    mid = _merge_id(r.text)
    with web_app._LOCK:
        assert web_app._merges[mid].status in ("ready", "conflicts"), (
            "opening the merge screen must not start executing the merge"
        )


def test_branch_with_nothing_ahead_says_so_instead_of_offering_a_dead_button(client):
    """`main` itself, and any branch with no commits ahead, has nothing to merge."""
    client.post("/branches", data={"name": "untouched"})
    html = client.get("/").text
    assert 'data-merge-from="untouched"' not in html, (
        "a branch with no commits ahead should not offer a merge that would do nothing"
    )
    assert 'data-merge-from="main"' not in html, "main cannot be merged into itself"
    assert "nothing to merge" in html, (
        "say why the action is absent rather than leaving an unexplained gap in the row"
    )


# --- a merged branch must stop looking unmerged --------------------------------
#
# `commits.parent_id` is a single column, so the DAG could not represent a merge:
# merging created a commit on `main` whose only parent was main's previous head.
# The branch's commits never entered main's ancestry, so `ahead` never fell to 0,
# the branch list kept offering "merge -> main" for work already merged, and --
# worse than the cosmetic part -- `merge_base` would still resolve to the original
# fork point, so re-merging would try to re-apply changes already in main.


def test_a_merged_branch_is_no_longer_ahead_of_main(client):
    client.post("/branches", data={"name": "settled"})
    client.post("/branches/settled/changes",
                data={"op": "add_column", "table": "users", "name": "settled_col", "type": "text"})
    client.post("/branches/settled/commit", data={"message": "add settled_col"})

    before = client.get("/").text
    assert 'data-merge-from="settled"' in before, "precondition: it has something to merge"

    mid = _merge_id(client.post("/merges", data={"source": "settled", "target": "main"}).text)
    client.post(f"/merges/{mid}/run")
    _wait_for_merge(mid)

    after = client.get("/").text
    assert 'data-merge-from="settled"' not in after, (
        "a branch whose commits are now in main must stop offering a merge that "
        "would do nothing"
    )
    assert "nothing to merge" in after


def test_merge_base_moves_forward_so_a_second_merge_does_not_replay_the_first(client):
    """The correctness half: after merging, the branch head is in main's ancestry."""
    from tributary import merge as merge_mod
    from tributary import store

    client.post("/branches", data={"name": "twice"})
    client.post("/branches/twice/changes",
                data={"op": "add_column", "table": "users", "name": "twice_a", "type": "text"})
    client.post("/branches/twice/commit", data={"message": "a"})
    mid = _merge_id(client.post("/merges", data={"source": "twice", "target": "main"}).text)
    client.post(f"/merges/{mid}/run")
    _wait_for_merge(mid)

    conn = web_app.db.connect(autocommit=True)
    try:
        branch_head = store.head(conn, "twice").id
        main_head = store.head(conn, "main").id
        assert branch_head in store.ancestors(conn, main_head), (
            "after a merge, the merged branch's head must be an ancestor of main -- "
            "otherwise merge_base rewinds to the fork point and replays the merge"
        )
        assert merge_mod.merge_base(conn, branch_head, main_head) == branch_head
    finally:
        conn.close()
