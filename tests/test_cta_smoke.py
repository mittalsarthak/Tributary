"""Every CTA in the UI, exercised end to end against the invariant that matters.

Why this file exists: the same bug was reported three times in a row, each time as
a different button, because each was fixed individually instead of the class being
audited. The failures were never identical -- `hx-select="body"` matching nothing,
4xx bodies being discarded, a script excluded from its own fragment, a 500 whose
21-byte body got swapped into `<main>` -- but the *symptom* was always the same:
a blank content area, while the server looked fine in the logs.

So this asserts one invariant across every CTA the templates can fire:

    a CTA response must never be a 5xx, and must always carry a `<main>` with real
    content in it

because htmx swaps the response into `<main>`. If a response is a bare error string
or lacks `<main>`, the swap empties the page -- which is what the user sees, every
time, regardless of which underlying bug caused it.

The endpoint list below is derived from every `hx-post`/`hx-delete`/`hx-get` in
`tributary/web/templates/`. `test_every_template_cta_is_covered_here` fails if a new
one is added without a case here, so this audit cannot silently go stale.
"""

import re

import pytest

from tributary.web import app as web_app

MIN_MAIN_TEXT = 40  # a real page; an error string or empty swap is far below this


def _main_text(html: str) -> str:
    m = re.search(r"<main[^>]*>(.*)</main>", html, re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()


def _assert_swappable(r, what: str) -> None:
    """The invariant: never a 5xx, and always a page htmx can swap without blanking."""
    assert r.status_code < 500, f"{what}: server error {r.status_code} -- body: {r.text[:200]!r}"
    text = _main_text(r.text)
    assert text, f"{what}: response has no <main>; htmx would swap emptiness into the page"
    assert len(text) >= MIN_MAIN_TEXT, (
        f"{what}: <main> holds only {len(text)} chars ({text!r}) -- too thin to be a "
        f"real page, so the swap would leave the content area effectively blank"
    )


def _seed_branch(client, name: str, *, commit: bool = True) -> None:
    client.post("/branches", data={"name": name})
    client.post(f"/branches/{name}/changes",
                data={"op": "add_column", "table": "users", "name": f"{name}_c", "type": "text"})
    if commit:
        client.post(f"/branches/{name}/commit", data={"message": f"{name} change"})


def _merge_id(html: str) -> str:
    m = re.search(r'data-merge-id="([^"]+)"', html)
    assert m, "no data-merge-id in response"
    return m.group(1)


# --- one case per CTA ---------------------------------------------------------

def test_cta_home(client):
    _assert_swappable(client.get("/"), "GET / (grow-poll target)")


def test_cta_create_branch(client):
    _assert_swappable(client.post("/branches", data={"name": "ctanew"}), "POST /branches")


def test_cta_create_duplicate_branch_is_a_readable_page(client):
    client.post("/branches", data={"name": "ctadup"})
    r = client.post("/branches", data={"name": "ctadup"})
    _assert_swappable(r, "POST /branches (duplicate)")
    assert "already exists" in r.text.lower()


def test_cta_apply_change(client):
    client.post("/branches", data={"name": "ctaedit"})
    r = client.post("/branches/ctaedit/changes",
                    data={"op": "add_column", "table": "users", "name": "c1", "type": "text"})
    _assert_swappable(r, "POST /branches/{n}/changes")


def test_cta_apply_change_with_invalid_input_is_a_readable_page(client):
    """An unquoted default is a column reference to Postgres -- the error must render."""
    client.post("/branches", data={"name": "ctabad"})
    r = client.post("/branches/ctabad/changes",
                    data={"op": "add_column", "table": "users", "name": "c2",
                          "type": "text", "default": "unquoted"})
    _assert_swappable(r, "POST /branches/{n}/changes (invalid default)")
    assert r.status_code == 400


def test_cta_commit(client):
    _seed_branch(client, "ctacommit", commit=False)
    r = client.post("/branches/ctacommit/commit", data={"message": "m"})
    _assert_swappable(r, "POST /branches/{n}/commit")


def test_cta_delete_unmerged_branch(client):
    client.post("/branches", data={"name": "ctadel"})
    _assert_swappable(client.delete("/branches/ctadel"), "DELETE /branches/{n}")


def test_cta_delete_merged_branch(client):
    """The reported case: deleting a branch after merging it."""
    _seed_branch(client, "ctadelm")
    mid = _merge_id(client.post("/merges", data={"source": "ctadelm", "target": "main"}).text)
    client.post(f"/merges/{mid}/run")
    _wait_for(mid)
    _assert_swappable(client.delete("/branches/ctadelm"), "DELETE /branches/{n} (merged)")


def test_cta_delete_main_is_refused_readably(client):
    _assert_swappable(client.delete("/branches/main"), "DELETE /branches/main")


def test_cta_create_merge(client):
    _seed_branch(client, "ctamerge")
    r = client.post("/merges", data={"source": "ctamerge", "target": "main"})
    _assert_swappable(r, "POST /merges")


def test_cta_run_merge(client):
    _seed_branch(client, "ctarun")
    mid = _merge_id(client.post("/merges", data={"source": "ctarun", "target": "main"}).text)
    r = client.post(f"/merges/{mid}/run")
    _assert_swappable(r, "POST /merges/{id}/run")
    _wait_for(mid)


def test_cta_resolve_merge(client):
    for br, typ in (("ctac1", "text"), ("ctac2", "int4")):
        client.post("/branches", data={"name": br})
        client.post(f"/branches/{br}/changes",
                    data={"op": "add_column", "table": "users", "name": "clash", "type": typ})
        client.post(f"/branches/{br}/commit", data={"message": "clash"})
    mid = _merge_id(client.post("/merges", data={"source": "ctac1", "target": "main"}).text)
    client.post(f"/merges/{mid}/run")
    _wait_for(mid)

    r2 = client.post("/merges", data={"source": "ctac2", "target": "main"})
    mid2 = _merge_id(r2.text)
    paths = re.findall(r'name="path" value="([^"]+)"', r2.text)
    assert paths, "expected a conflict to resolve"
    data = {"path": paths}
    data.update({f"side_{i}": "ours" for i in range(len(paths))})
    _assert_swappable(client.post(f"/merges/{mid2}/resolve", data=data),
                      "POST /merges/{id}/resolve")


def test_cta_grow_events(client):
    r = client.post("/seed/grow", data={"target_rows": 10500})
    _assert_swappable(r, "POST /seed/grow")
    _wait_for_grow()


# --- the audit cannot go stale ------------------------------------------------

def test_every_template_cta_is_covered_here():
    """Fail if a new CTA appears in a template with no case in this file."""
    from pathlib import Path

    tpl_dir = Path(__file__).parent.parent / "tributary" / "web" / "templates"
    found = set()
    for t in tpl_dir.glob("*.html"):
        for verb, url in re.findall(r'hx-(post|delete|get)="([^"]*)"', t.read_text()):
            # normalise Jinja expressions to a stable shape
            found.add(f"{verb}:{re.sub(r'{{[^}]*}}', '{}', url)}")

    covered = {
        "get:/",
        "post:/branches",
        "post:/branches/{}/changes",
        "post:/branches/{}/commit",
        "delete:/branches/{}",
        "post:/merges",
        "post:/merges/{}/resolve",
        "post:/merges/{}/run",
        "post:/seed/grow",
    }
    missing = found - covered
    assert not missing, (
        f"CTAs present in templates but not exercised by this audit: {sorted(missing)}. "
        f"Add a case -- this file exists because the same blank-page class of bug was "
        f"reported three times as three different buttons."
    )


# --- helpers shared with test_web -------------------------------------------

def _wait_for(mid: str, timeout: float = 10.0) -> None:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with web_app._LOCK:
            st = web_app._merges.get(mid)
            if st is not None and st.status in ("done", "failed"):
                return
        time.sleep(0.05)
    pytest.fail(f"merge {mid!r} did not finish in time")


def _wait_for_grow(timeout: float = 20.0) -> None:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with web_app._grow_lock:
            if not web_app._grow_state["running"]:
                return
        time.sleep(0.05)
    pytest.fail("grow thread did not finish in time")
