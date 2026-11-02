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


def tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def contrast(a: str, b: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_field_borders_and_off_switches_stand_out_in_both_themes():
    """WCAG 1.4.11: a field's border and an off switch's track reach 3:1 against the card and
    the page, and the white knob 3:1 against the track, in light and dark."""
    css = re.sub(r"/\*.*?\*/", "", source("css/app.css"), flags=re.S)
    light = tokens(re.search(r"^:root \{(.*?)^\}", css, re.M | re.S).group(1))
    dark_block = r"@media screen and \(prefers-color-scheme: dark\) \{\s*:root \{(.*?)\}"
    dark = {**light, **tokens(re.search(dark_block, css, re.S).group(1))}
    for name, theme in (("light", light), ("dark", dark)):
        for token in ("--control-border", "--switch-off"):
            for behind in ("--surface", "--bg", "--control-bg"):
                ratio = contrast(theme[token], theme[behind])
                assert ratio >= 3.0, f"{name} {token} on {behind}: {ratio:.2f}:1"
        assert contrast("#ffffff", theme["--switch-off"]) >= 3.0, f"{name} switch knob"


def test_swapped_results_are_announced_through_their_live_region():
    """A filter or the period arrows change results silently for screen readers; app.js copies
    the new [data-status] text into the region's <p role="status" data-status-for="<id>">."""
    js = source("js/app.js")
    assert "announceStatus(" in listener(js, "htmx:afterSettle")
    body = function_body(js, "announceStatus")
    assert "data-status-for" in body and "[data-status]" in body and "textContent" in body
    assert "announceResults" not in source("js/transactions.js")  # one mechanism for every page


def narrow_rules(css: str) -> str:
    """The text of every ``@media (max-width: 599.98px)`` (phones) and ``899.98px`` (phones and
    narrow windows) block in a stylesheet."""
    out = []
    for match in re.finditer(r"@media \(max-width: (?:599|899)\.98px\) \{", css):
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[match.end() : i])
    return "\n".join(out)


def test_names_wrap_on_phones_instead_of_being_cut():
    """Below 600px category names and Plaid and Amex labels, and below 900px rule texts, wrap
    rather than end in an ellipsis after a few letters, and the rules table fits the window."""
    categories = narrow_rules(source("css/pages/categories.css"))
    for selector in (".categories-table .cat__name", ".mapping-table .table__primary"):
        assert selector in categories
    rules = narrow_rules(source("css/pages/rules.css"))
    assert ".rules-table .table__primary" in rules and ".rules-table .table__secondary" in rules
    assert "white-space: normal" in categories and "white-space: normal" in rules
    assert re.search(r"\.table-wrap > \.rules-table \{\s*min-width: 0;", rules)
