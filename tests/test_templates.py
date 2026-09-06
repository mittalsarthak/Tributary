"""Static checks on the htmx wiring in the templates.

These exist because of a bug that every one of the other 244 tests missed: every
CTA in the app blanked the page in a real browser, while the server returned a
perfectly valid 200 the whole time.

The cause was `hx-select="body"`. htmx 1.x decides a response is a "full page"
with `/<body/.test(resp)`, and then parses it via
`DOMParser.parseFromString(...)` returning `responseDoc.body` — so the fragment
htmx holds *is* the body element. `hx-select` then runs
`fragment.querySelectorAll("body")`, which searches that body's *descendants*.
A body never contains another body, so it matched zero nodes, and swapping zero
nodes into `hx-target="body"` with `hx-swap="outerHTML"` erased the page.

The TestClient suite could not catch this: it asserts on response text and never
executes htmx, so the swap semantics were entirely untested while the server was
always right.
"""

from pathlib import Path

import pytest

TEMPLATES = sorted((Path(__file__).parent.parent / "tributary" / "web" / "templates").glob("*.html"))


def _templates_with(needle: str) -> list[str]:
    return [t.name for t in TEMPLATES if needle in t.read_text()]


def test_there_are_templates_to_check():
    """Guard against this whole file silently passing because the glob broke."""
    assert TEMPLATES, "no templates found - the glob path is wrong"


def test_no_template_selects_body_from_an_htmx_response():
    """`hx-select="body"` can never match, and blanks the page when it doesn't.

    See the module docstring: htmx hands `hx-select` a fragment that already *is*
    the response's body, so selecting "body" from it matches nothing.
    """
    offenders = _templates_with('hx-select="body"')
    assert offenders == [], (
        f'hx-select="body" found in {offenders}. It matches zero nodes against a '
        "full-page response and erases the swap target. Select an element *inside* "
        'the body instead, e.g. hx-select="main".'
    )


def test_no_template_swaps_the_body_element_itself():
    """Swapping `<body>` via outerHTML discards the element the CDN scripts booted against."""
    offenders = [
        t.name
        for t in TEMPLATES
        if 'hx-target="body"' in t.read_text() and 'hx-swap="outerHTML"' in t.read_text()
    ]
    assert offenders == [], (
        f'hx-target="body" with hx-swap="outerHTML" found in {offenders}. '
        'Target the inner content region instead (hx-target="main").'
    )


def test_error_responses_are_swapped_in_so_the_user_can_see_them():
    """A 4xx must still render, or every validation error fails silently.

    htmx decides whether to swap with, at htmx.js:3542:

        var shouldSwap = xhr.status >= 200 && xhr.status < 400 && xhr.status !== 204

    So a 400 is fetched and thrown away. The app deliberately returns 4xx with a
    rendered error banner ("errors are sentences, not stack traces") — and without
    a `htmx:beforeSwap` handler flipping `shouldSwap`, every one of those banners
    is discarded and the user sees a button that does nothing at all.

    Returning 200 for errors would also make them visible, and is the wrong fix:
    it lies to the browser, to curl, and to the tests that assert on status codes.
    """
    base = (Path(__file__).parent.parent / "tributary" / "web" / "templates" / "base.html").read_text()
    assert "htmx:beforeSwap" in base, (
        "base.html has no htmx:beforeSwap handler, so htmx discards every 4xx/5xx "
        "response body and validation errors fail silently in the browser."
    )
    assert "shouldSwap" in base, (
        "the htmx:beforeSwap handler must set detail.shouldSwap = true for error "
        "statuses; listening without overriding it changes nothing."
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_every_htmx_select_targets_an_element_that_exists_inside_a_body(template):
    """A selector htmx can actually match: anything but the body wrapper itself."""
    text = template.read_text()
    for line in text.splitlines():
        if "hx-select=" not in line:
            continue
        selector = line.split('hx-select="', 1)[1].split('"', 1)[0]
        assert selector not in {"body", "html"}, (
            f"{template.name}: hx-select=\"{selector}\" cannot match — htmx's fragment "
            f"is the body itself, so only its descendants are selectable."
        )


def test_progress_scripts_live_inside_the_element_htmx_selects():
    """The EventSource script must survive the swap that renders the page.

    merge.html runs a merge with `hx-select="[data-merge-id]"`, so htmx keeps only
    that element from the response. progress.html had its `<script>` as a *sibling*
    of that div, so the swap dropped it: no EventSource was ever created and the
    page sat on "Running..." forever while the merge had already finished.

    Same shape as the `hx-select="body"` bug — a selector that quietly excludes the
    behaviour it needs. The server was right both times.
    """
    progress = (
        Path(__file__).parent.parent / "tributary" / "web" / "templates" / "progress.html"
    ).read_text()

    # Anchor at the *opening tag* that carries the attribute, not the attribute
    # itself — otherwise the wrapper's own `<div` is left out of the nesting count.
    anchor = progress.rindex("<div", 0, progress.index("data-merge-id="))
    script_at = progress.index("<script")

    # Is the [data-merge-id] element still open where the script begins? Count the
    # div nesting between the two: depth >= 1 means the script is inside it.
    span = progress[anchor:script_at]
    depth = span.count("<div") - span.count("</div>")

    assert depth >= 1, (
        "progress.html's <script> sits outside the [data-merge-id] element (nesting "
        f"depth {depth} at the script). htmx's hx-select=\"[data-merge-id]\" keeps only "
        "that element, so the script is discarded, no EventSource is created, and the "
        'page shows "Running..." forever for a merge that already finished.'
    )
