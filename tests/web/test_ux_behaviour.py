"""What the page scripts and the stylesheet do for keyboard, screen-reader and mouse users.

Tests never start a browser, so these read the sources in ``budgetapp/web/static`` and check
that each behaviour is wired where it has to be. The behaviours themselves were checked in
headless Chrome against a demo.
"""

from __future__ import annotations

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
