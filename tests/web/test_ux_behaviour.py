"""What the page scripts and the stylesheet do for keyboard, screen-reader and mouse users.

Tests never start a browser, so these read the sources in ``budgetapp/web/static`` and check
that each behaviour is wired where it has to be. The behaviours themselves were checked in
headless Chrome against a demo.
"""

from __future__ import annotations

import re
from pathlib import Path

from budgetapp import web

STATIC = Path(web.__file__).parent / "static"


def source(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def listener(text: str, event: str) -> str:
    """The body of the ``doc.addEventListener("<event>", …)`` call (the first one)."""
    start = text.index(f'addEventListener("{event}"')
    return text[start : text.index("\n  });", start)]


# --------------------------------------------------------------------------- budgets.js


def test_leaving_a_suggested_amount_saves_it():
    """Suggest only fills a budget row's field. Leaving the field saves it like a typed
    amount (the form is submitted); only Esc puts the saved amount back."""
    js = source("js/budgets.js")
    leave = listener(js, "focusout")
    assert "requestSubmit()" in leave
    assert "revert(" not in leave
    keys = listener(js, "keydown")
    assert '"Escape"' in keys and "revert(" in keys


# --------------------------------------------------------------------------- app.js


def function_body(text: str, name: str) -> str:
    start = text.index(f"function {name}(")
    return text[start : text.index("\n  }\n", start)]


def test_a_form_returned_with_errors_shows_its_first_problem():
    """After a 422 swap (e.g. Settings saved from its bottom Save button), focus moves to the
    first invalid field, or the error banner, and it is scrolled into view."""
    js = source("js/app.js")
    settle = listener(js, "htmx:afterSettle")
    assert "status === 422" in settle and "focusFormError(" in settle
    body = function_body(js, "focusFormError")
    assert '[aria-invalid="true"]' in body and '[role="alert"]' in body
    assert ".focus(" in body and "scrollIntoView(" in body


def test_focus_lost_in_a_swap_goes_to_the_nearest_container():
    """When the focused control is gone after a swap (its row left the list, Sync now became
    "Syncing…"), focus goes to the nearest surviving container with an id (its heading or
    first control), not to <body>."""
    js = source("js/app.js")
    assert "rememberFocus(" in listener(js, "htmx:beforeRequest")
    assert "restoreFocus(" in listener(js, "htmx:afterSettle")
    remember = function_body(js, "rememberFocus")
    assert "doc.activeElement" in remember and "parentElement" in remember
    restore = function_body(js, "restoreFocus")
    assert "getElementById(" in restore and ".focus(" in restore
    assert '"h1, h2, h3"' in js and "doc.body" in restore


def test_a_failed_save_puts_the_stored_value_back():
    """A save that fails (5xx, another 4xx, the app unreachable) changes nothing, so the select,
    switch or amount that triggered it shows the stored value again instead of the new one."""
    js = source("js/app.js")
    for event in ("htmx:responseError", "htmx:sendError"):
        handler = listener(js, event)
        assert "restoreControls(" in handler, event
        assert '!== "get"' in handler, event  # a failed search or filter changes no control
    body = function_body(js, "restoreControls")
    assert "defaultSelected" in body and "defaultChecked" in body and "defaultValue" in body
    assert "change" in body  # only controls that save on change, never a whole typed-in form


def test_a_failed_load_does_not_say_nothing_was_saved():
    handler = listener(source("js/app.js"), "htmx:responseError")
    server = [line for line in handler.splitlines() if "status >= 500" in line]
    assert len(server) == 2
    saving = next(line for line in server if '!== "get"' in line)
    loading = next(line for line in server if '!== "get"' not in line)
    assert "Nothing was saved" in saving
    assert "saved" not in loading and "try again" in loading


def test_the_mobile_menu_never_hides_keyboard_focus():
    """Escape closes the menu and puts focus on its Menu button (not on <body>); tabbing out of
    the open menu closes it, so the panel never covers the control that has focus."""
    js = source("js/app.js")
    start = js.index('if (evt.key === "Escape")')
    escape = js[start : js.index("return;", start)]
    assert "details.mobile-nav[open]" in escape
    assert "contains(doc.activeElement)" in escape and 'querySelector("summary")' in escape
    leave = [
        js[i : js.index("\n  });", i)]
        for i in range(len(js))
        if js.startswith('addEventListener("focusout"', i)
    ]
    assert any("details.mobile-nav[open]" in body and "relatedTarget" in body for body in leave)


# --------------------------------------------------------------------------- app.css


def css_rules(css: str) -> list[tuple[str, str]]:
    """``(selector, declarations)`` of every rule, media blocks flattened (no nesting)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        (sel.split("{")[-1].strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    ]


def test_keyboard_focus_never_lands_under_a_sticky_bar_or_table_header():
    """Scrolling a focused control into view leaves room for the top bar (and the Sandbox or
    demo bar above it), and for a table header that sticks under it."""
    rules = css_rules(source("css/app.css"))
    envbar = [body for sel, body in rules if sel.startswith("html:has(.shell.has-envbar)")]
    assert any("scroll-padding-top" in body and "--envbar-h" in body for body in envbar)
    sticky_rows = [
        body
        for sel, body in rules
        if ".table-wrap.is-fit" in sel and "tbody" in sel and "scroll-margin-top" in body
    ]
    assert sticky_rows
