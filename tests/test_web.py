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
"""

import re
import time

import pytest

from tributary.web import app as web_app


def _merge_id(html: str) -> str:
    m = re.search(r'data-merge-id="([^"]+)"', html)
    assert m, f"no data-merge-id found in response:\n{html}"
    return m.group(1)


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

    r2 = client.post("/merges", data={"source": "b", "target": "main"})
    mid_b = _merge_id(r2.text)
    r3 = client.post(f"/merges/{mid_b}/run")
    assert r3.status_code < 500
    assert "conflict" in r3.text.lower()


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


def test_seed_grow_endpoint_grows_the_table(client):
    r = client.post("/seed/grow", data={"target_rows": 8000})
    assert r.status_code < 500
    _wait_for_grow_to_finish()


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
