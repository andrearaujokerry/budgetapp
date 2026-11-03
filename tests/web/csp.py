"""What in a template or a rendered page would break under the app's CSP (ARCHITECTURE §9).

``csp_problems(text)`` lists inline ``<script>``/``<style>`` elements, scripts from anywhere but
``/static/`` and Plaid Link, ``on*=`` event attributes, ``hx-on`` and ``javascript:`` URLs.

* Template source (the default): Jinja comments and the insides of ``{% … %}`` / ``{{ … }}`` are
  code, not markup, so they are ignored; markup between tags is still checked.
* Rendered HTML (``rendered=True``): the page exactly as served, which also catches attributes a
  macro built from keyword arguments. Feature tests can check their pages this way:
  ``assert csp_problems(response.text, rendered=True) == []``.

Event attributes are looked for among a tag's attribute names only: text inside a quoted
attribute value (``title="&lt;img onerror=…&gt;"``, user text correctly escaped) is not one.
"""

from __future__ import annotations

import re

from budgetapp.web.security import PLAID_LINK_SCRIPT

_JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.S)
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b([^>]*)>", re.I)
#: A start tag, whose quoted attribute values may hold ``>``.
_TAG = re.compile(r"""<[a-zA-Z](?:[^>"']|"[^"]*"|'[^']*')*>?""")
_QUOTED = re.compile(r""""[^"]*"|'[^']*'""")
_EVENT_ATTR = re.compile(r"\son[a-z]+\s*=", re.I)


def _has_event_attribute(text: str) -> bool:
    """An ``on*=`` attribute in any tag (attribute values blanked first)."""
    return any(_EVENT_ATTR.search(_QUOTED.sub('""', tag.group(0))) for tag in _TAG.finditer(text))


def csp_problems(source: str, *, rendered: bool = False) -> list[str]:
    """What in ``source`` would break under the CSP (inline JS/CSS, event handlers)."""
    text = source if rendered else _JINJA.sub("", source)
    problems = []
    for match in _SCRIPT_OR_STYLE.finditer(text):
        name, attrs = match.group(1).lower(), match.group(2)
        if name == "style":
            problems.append("a <style> element (put CSS in static/css/pages/<page>.css)")
            continue
        if re.search(r'\btype="application/json"', attrs):
            continue
        src = re.search(r'\bsrc="([^"]*)"', attrs)
        if src is None:
            problems.append("an inline <script> (put JS in static/js/<page>.js)")
        elif not (src.group(1).startswith("/static/") or src.group(1) == PLAID_LINK_SCRIPT):
            problems.append(f"a script from {src.group(1)} (only /static/ and Plaid Link)")
    if _has_event_attribute(text):
        problems.append("an on*= event attribute (use data-* and static JS)")
    if re.search(r"hx-on|hx_on", text):
        problems.append("hx-on (allowEval is off; use static JS)")
    if "javascript:" in text.lower():
        problems.append("a javascript: URL")
    return problems
