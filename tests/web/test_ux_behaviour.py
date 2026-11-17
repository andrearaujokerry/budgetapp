"""What the page scripts and the stylesheet do for keyboard, screen-reader and mouse users.

Tests never start a browser, so these read the sources in ``budgetapp/web/static`` and check
that each behaviour is wired where it has to be. The behaviours themselves were checked in
headless Chrome against a demo.
"""

from __future__ import annotations

import hashlib
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


def test_charts_wait_for_the_text_face():
    """Canvas text isn't redrawn when a web font arrives, so charts are drawn once Newsreader has
    loaded, and in it."""
    js = source("js/app.js")
    assert 'cssVar("--font-text")' in function_body(js, "readTheme")
    assert "doc.fonts.load(" in function_body(js, "textFontReady")
    assert "textFontReady()" in function_body(js, "renderCharts")


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


def themes() -> tuple[dict[str, str], dict[str, str]]:
    """The light and dark colour tokens (dark falls back to light for tokens it doesn't set)."""
    css = re.sub(r"/\*.*?\*/", "", source("css/app.css"), flags=re.S)
    light = tokens(re.search(r"^:root \{(.*?)^\}", css, re.M | re.S).group(1))
    dark_block = r"@media screen and \(prefers-color-scheme: dark\) \{\s*:root \{(.*?)\}"
    return light, {**light, **tokens(re.search(dark_block, css, re.S).group(1))}


def test_field_borders_and_off_switches_stand_out_in_both_themes():
    """WCAG 1.4.11: a field's border and an off switch's track reach 3:1 against the card and
    the page, and the white knob 3:1 against the track, in light and dark."""
    for name, theme in zip(("light", "dark"), themes(), strict=True):
        for token in ("--control-border", "--switch-off"):
            for behind in ("--surface", "--bg", "--control-bg"):
                ratio = contrast(theme[token], theme[behind])
                assert ratio >= 3.0, f"{name} {token} on {behind}: {ratio:.2f}:1"
        assert contrast("#ffffff", theme["--switch-off"]) >= 3.0, f"{name} switch knob"


def test_a_switched_on_switch_keeps_its_knob_visible_in_both_themes():
    """The accent is ink, black in light mode and white in dark mode, and fills a switched-on
    switch, so the knob takes --accent-contrast there instead of staying white."""
    rules = css_rules(source("css/app.css"))
    knob = [body for sel, body in rules if sel == ".switch:checked::before"]
    assert any("background: var(--accent-contrast)" in body for body in knob)
    for name, theme in zip(("light", "dark"), themes(), strict=True):
        ratio = contrast(theme["--accent-contrast"], theme["--accent"])
        assert ratio >= 3.0, f"{name} knob on a switched-on switch: {ratio:.2f}:1"


def test_links_in_running_text_are_underlined():
    """Links are ink like the words around them, so a link inside a sentence needs its underline
    (component links such as buttons and table rows style themselves)."""
    plain = [body for sel, body in css_rules(source("css/app.css")) if sel == "a:not([class])"]
    assert any("text-decoration: underline" in body for body in plain)


def test_the_text_face_ships_with_the_app():
    """Newsreader is served from static/vendor, never fetched from another site, and each file
    matches the checksum VERSIONS.md records for it."""
    css = source("css/app.css")
    assert all(u.startswith(("data:", "../vendor/")) for u in re.findall(r'url\("([^"]+)"\)', css))
    assert '--font-text: "Newsreader",' in css
    versions = source("vendor/VERSIONS.md")
    faces = re.findall(r"@font-face \{(.*?)\}", css, re.S)
    assert len(faces) == 3  # latin, latin-ext, and the latin italic for pending amounts
    for face in faces:
        assert 'font-family: "Newsreader";' in face
        name = re.search(r'url\("\.\./vendor/([^"]+)"\)', face).group(1)
        digest = hashlib.sha256((STATIC / "vendor" / name).read_bytes()).hexdigest()
        assert f"| `{name}` |" in versions and f"`{digest}`" in versions, name
    assert (STATIC / "vendor" / "LICENSE-newsreader.txt").is_file()


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


def declarations(css: str, selector: str) -> list[str]:
    """The declarations of every rule in ``css`` whose selector list names ``selector``."""
    return [body for sel, body in css_rules(css) if selector in (s.strip() for s in sel.split(","))]


def test_the_trends_grid_keeps_names_inside_their_sticky_column_on_phones():
    """ux-1: the months of Trends' Every month grid scroll under its sticky first column. On a
    phone that column is 8.5rem wide, so a long category badge ends in an ellipsis inside the
    cell's padding (it is as wide as the cell's content at most, never a fixed width), the Net
    row's note takes a line of its own, and the cell clips anything else: nothing it holds
    prints over the amounts at rest or while they scroll under it."""
    css = source("css/pages/trends.css")
    phones = narrow_rules(css)
    names = declarations(phones, ".trends-grid .trends-grid__name")
    assert any("overflow: hidden" in body for body in names)
    assert any("display: block" in body for body in declarations(phones, ".trends-grid__note"))
    badges = [body for sel, body in css_rules(css) if ".trends-grid__name .cat" in sel]
    assert not any(re.search(r"max-width:\s*[\d.]+(?:rem|px)", body) for body in badges)
    assert any("max-width: 100%" in body for body in declarations(source("css/app.css"), ".cat"))


# --------------------------------------------------------------------------- forms.html


def test_a_checkbox_with_help_is_named_by_its_label_alone():
    """ux-7: the <label> of a checkbox or switch wraps its help too (one click target, the
    layout Settings and the dialogs use), so without more the help would be read twice: as part
    of the name and again as the description. aria-labelledby names the control by its label
    text alone; aria-describedby reads the help once, as its description."""
    from budgetapp.web.templating import templates

    source = '{% import "components/forms.html" as form %}'
    for macro in ("checkbox_field", "switch_field"):
        call = f'{{{{ form.{macro}("x", "Also apply", help="Edits stay.", id="t") }}}}'
        html = templates.env.from_string(source + call).render()
        control = re.search(r"<input[^>]*>", html).group(0)
        assert 'aria-labelledby="t-label"' in control and 'aria-describedby="t-help"' in control
        assert re.search(r'<span id="t-label">Also apply</span>', html), html
        assert '<span class="check__help" id="t-help">Edits stay.</span>' in html
        assert html.index("</label>") > html.index('id="t-help"')  # still one click target
    plain = templates.env.from_string(source + '{{ form.checkbox_field("x", "Also", id="t") }}')
    assert "aria-labelledby" not in plain.render() and "aria-describedby" not in plain.render()


# --------------------------------------------------------------------------- shapes and motion
# The owner asked for no rounded shapes anywhere (squares and pointed polygons only), a
# text-only sidebar and motion like a product page that is never in the way (README "Shapes"
# and "Motion"). These pin the rules so a later change can't quietly bring a curve back.

TEMPLATES = Path(web.__file__).parent / "templates"
#: The page-motion keyframes (app.css section 7); every one is off under reduced motion.
PAGE_MOTION = ("page-out", "arrive", "reveal", "title-away", "swap-in", "fade-in", "fade-out")


def stylesheets() -> dict[str, str]:
    """app.css and every page stylesheet, comments removed."""
    paths = [STATIC / "css" / "app.css", *sorted((STATIC / "css" / "pages").glob("*.css"))]
    comments = re.compile(r"/\*.*?\*/", re.S)
    return {p.name: comments.sub("", p.read_text(encoding="utf-8")) for p in paths}


def blocks(css: str, prelude: str) -> list[str]:
    """The insides of every ``<prelude> { … }`` block (nested braces included)."""
    out = []
    for match in re.finditer(re.escape(prelude) + r"\s*\{", css):
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[match.end() : i - 1])
    return out


def test_every_corner_is_square():
    """No border-radius but 0 in the app's own CSS (no px, no 50%, no pill), the radius tokens
    are 0, and a reset squares what browsers round by default (buttons, fields, selects)."""
    for name, css in stylesheets().items():
        for prop, value in re.findall(r"(border(?:-[a-z]+)*-radius)\s*:\s*([^;}]+)", css):
            assert value.strip() == "0", f"{name}: {prop}: {value.strip()}"
        for token, value in re.findall(r"(--radius-[\w-]+)\s*:\s*([^;}]+)", css):
            assert value.strip() == "0", f"{name}: {token}: {value.strip()}"
        assert not re.search(r"\b(?:circle|ellipse)\(", css), name  # no round clip-paths either
    reset = [body for sel, body in css_rules(source("css/app.css")) if sel.startswith("*,")]
    assert any("border-radius: 0" in body for body in reset)
    lists = declarations(source("css/app.css"), "ul")
    assert any("list-style-type: square" in body for body in lists)  # bullets too


def test_round_marks_are_squares_and_radios_diamonds():
    """Dots are squares (equal sides, no radius); a radio is a turned square, so it never looks
    like a checkbox, and so is each colour in the category picker; the switch is a square track
    whose square knob slides (it still takes --accent-contrast on an ink track)."""
    css = source("css/app.css")
    for selector in (".cat__dot", ".pill__dot"):
        body = " ".join(declarations(css, selector))
        width, height = re.search(r"width: (\S+);", body), re.search(r"height: (\S+);", body)
        assert width and height and width.group(1) == height.group(1), selector
    radio = declarations(css, '.check input[type="radio"]')
    assert any("transform: rotate(45deg)" in body for body in radio)
    check = declarations(css, '.check input[type="checkbox"]:not(.switch)::before')
    assert any("clip-path: polygon(" in body for body in check)  # an angular check mark
    swatch = declarations(source("css/pages/categories.css"), ".swatch-option__dot")
    assert any("transform: rotate(45deg)" in body for body in swatch)
    knob = " ".join(declarations(css, ".switch:checked::before"))
    assert "background: var(--accent-contrast)" in knob and "translateX(" in knob


def test_icons_are_angular():
    """Icons draw with square caps and mitred joins, and no glyph has a circle, an ellipse, a
    rounded rect or an arc or curve command; nor does any other SVG the app draws (the brand
    mark, the favicon, the select chevrons)."""
    css = source("css/app.css")
    icon = " ".join(declarations(css, ".icon"))
    assert "stroke-linecap: square" in icon and "stroke-linejoin: miter" in icon
    comments = re.compile(r"\{#.*?#\}", re.S)
    templates = TEMPLATES.rglob("*.html")
    svgs = {p.name: comments.sub("", p.read_text(encoding="utf-8")) for p in templates}
    svgs |= {p.name: p.read_text(encoding="utf-8") for p in (STATIC / "img").glob("*.svg")}
    svgs["app.css chevrons"] = " ".join(re.findall(r'url\("data:image/svg\+xml,([^"]+)"\)', css))
    assert "i-home" in svgs["icons.html"] and svgs["app.css chevrons"]
    for name, text in svgs.items():
        assert not re.search(r"<circle|<ellipse|%3Ccircle|%3Cellipse", text), name
        assert not re.search(r"""\s(?:rx|ry)=["']""", text), name
        assert "round" not in " ".join(re.findall(r"stroke-line(?:cap|join)[=:]\s*\S+", text)), name
        for d in re.findall(r"""\sd=["']([^"']*)["']""", text):
            assert not re.search(r"[AaCcSsQqTt]", d), f"{name}: a curve in {d!r}"


def test_the_sidebar_nav_is_text_only(client):
    """The nav, in the sidebar and in the phone menu, draws no icon next to a page name; the
    current page is marked by its ink text and a 2px bar (.nav__marker), never a filled
    background; and the sync status in the top bar is a flat label, not a pill."""
    from budgetapp.web.templating import NAV_ITEMS

    assert all(set(item) == {"key", "label", "href", "group"} for item in NAV_ITEMS)
    html = client.get("/").text
    navs = re.findall(r'<nav class="nav" aria-label="Main">.*?</nav>', html, re.S)
    assert len(navs) == 2  # sidebar + phone menu
    for nav in navs:
        assert "<svg" not in nav and "icon" not in nav
        current = re.search(r'<a class="nav__link" href="/" aria-current="page">(.*?)</a>', nav)
        marker = '<span class="nav__marker" aria-hidden="true"></span>'
        assert current and marker in current.group(1)
    footer = re.search(r'<div class="sidebar__footer">(.*?)</div>', html, re.S).group(1)
    assert "<svg" not in footer
    css = source("css/app.css")
    current_link = " ".join(declarations(css, '.nav__link[aria-current="page"]'))
    assert "background" not in current_link and "color: var(--text)" in current_link
    assert "background" not in " ".join(declarations(css, ".nav__link:hover"))
    pill = " ".join(declarations(css, ".pill"))
    assert "border" not in pill and "background" not in pill


def test_flat_surfaces_keep_shadows_for_overlays_only():
    """Cards, tiles, buttons and the filter bar are flat (a hairline border, no shadow); only
    what floats above the page has one: popovers, tooltips, dialogs, toasts, the phone menu."""
    css = source("css/app.css")
    for selector in (".card", ".stat", ".btn", ".filter-bar", ".segmented"):
        shadows = re.findall(r"box-shadow:\s*([^;]+);", " ".join(declarations(css, selector)))
        assert set(shadows) <= {"none"}, selector  # (print says none)
    assert "--shadow-sm" not in css and "--shadow-xs" not in css
    for selector in (".dialog", ".toast", ".mobile-nav__panel"):
        assert "box-shadow: var(--shadow-lg)" in " ".join(declarations(css, selector)), selector


def test_charts_use_square_marks_and_straight_lines():
    """Lines are straight (tension 0) with mitred joins and flat ends; points, tooltip keys and
    bar legend keys are squares, never rounded or round; bars and tooltips have square corners.
    (Doughnuts and pies are the UI kit's only.)"""
    js = source("js/app.js")
    build = function_body(js, "buildConfig")
    assert "out.tension = 0;" in build and 'out.borderJoinStyle = "miter";' in build
    assert 'out.borderCapStyle = "butt";' in build and "out.borderRadius = 0;" in build
    assert build.count('out.pointStyle = "rect";') == 2  # line points, bar tooltip keys
    assert "cornerRadius: 0," in build
    assert 'pointStyle: type === "line" ? "line" : "rect",' in build
    assert 'it.pointStyle = isLine ? "line" : "rect";' in function_body(js, "legendLabels")
    assert not re.search(r"rectRounded|\"round\"|'round'|pointStyle[^,;]*circle", js)


def test_page_changes_are_view_transitions_that_keep_the_chrome_still():
    """A cross-document View Transition (also for Back and Forward), only without reduced
    motion; the sidebar, the bars at the top and the notices keep their own names and do not
    animate; the top bar is named itself (a named ancestor would stop its frosted blur); and
    clicks pass through the transition."""
    css = re.sub(r"/\*.*?\*/", "", source("css/app.css"), flags=re.S)
    allowed = blocks(css, "@media (prefers-reduced-motion: no-preference)")
    assert any(re.search(r"@view-transition\s*\{\s*navigation: auto;", b) for b in allowed)
    assert css.count("@view-transition") == 1
    named = r"^([^\s{}][^{}\n]*?) \{ view-transition-name: ([\w-]+); \}"
    names = dict(re.findall(named, css, re.M))
    assert names[".sidebar"] == "sidebar" and names[".topbar"] == "topbar"
    assert {".mobile-bar", ".env-bar", ".notices", ".topbar__title"} <= set(names)
    assert ".masthead" not in names
    still = " ".join(sel for sel, body in css_rules(css) if "animation: none" in body)
    for name in ("sidebar", "mobile-bar", "env-bar", "topbar", "notices"):
        assert f"::view-transition-group({name})" in still, name
    assert any("pointer-events: none" in body for body in declarations(css, "::view-transition"))


def test_every_page_motion_is_off_with_reduced_motion():
    """Each rule that runs a page-motion animation (or a scroll-driven one) is covered by the
    reduced-motion block, which switches them off with animation: none; the bars stop filling;
    and app.js asks reducedMotion() before drawing charts in, holding bars and fading swaps."""
    css = re.sub(r"/\*.*?\*/", "", source("css/app.css"), flags=re.S)
    reduced = "\n".join(blocks(css, "@media (prefers-reduced-motion: reduce)"))
    off = " ".join(sel for sel, body in css_rules(reduced) if "animation: none !important" in body)
    for covered in (".content > *", ".content > * > *", ".card", ".stat", ".page-header__title"):
        assert covered in off, covered
    assert "[data-swapped]" in off and "::view-transition-group(*)" in off
    bars = " ".join(body for sel, body in css_rules(reduced) if ".progress__fill" in sel)
    assert "transition: none !important" in bars and "transform: none !important" in bars
    outside = css
    for block in blocks(css, "@media (prefers-reduced-motion: reduce)"):
        outside = outside.replace(block, "")
    assert set(PAGE_MOTION) <= set(re.findall(r"@keyframes ([\w-]+)", css))
    markers = (".content >", ".card", ".stat", ".page-header__title", "[data-swapped]")
    markers += ("::view-transition",)
    for selector, body in css_rules(outside):
        animated = re.findall(r"animation(?:-name)?:\s*([^;]+);", body)
        uses = {n for value in animated for n in re.findall(r"[\w-]+", value) if n in PAGE_MOTION}
        if uses or "animation-timeline" in body:
            assert any(m in selector for m in markers), f"{selector} is not switched off"
    js = source("js/app.js")
    assert "reducedMotion()" in function_body(js, "renderChart")
    assert "reducedMotion()" in function_body(js, "waitsForView")
    assert "reducedMotion()" in function_body(js, "watchBars")
    assert "reducedMotion()" in listener(js, "htmx:afterSwap")


def test_charts_draw_in_when_first_seen_and_swaps_fade_in():
    """A chart is drawn when its canvas first comes into view (IntersectionObserver), and only
    then draws in; what HTMX swaps in is marked with an attribute (htmx resets class and style
    when it settles), never for polling or the toast stack; the arrival hands over to swaps
    through .is-arrived."""
    js = source("js/app.js")
    assert "new IntersectionObserver(" in js
    assert "waitsForView(canvas)" in function_body(js, "renderCharts")
    render = function_body(js, "renderChart")
    assert "!canvas._chartDrawn" in render and "startDrawIn(chart)" in render
    swap = listener(js, "htmx:afterSwap")
    assert 'setAttribute("data-swapped"' in swap and "isQuiet(d.elt)" in swap and "#flash" in swap
    assert 'classList.add("is-arrived")' in function_body(js, "markArrived")
    assert "flashSwapped(el)" in listener(js, "htmx:afterSettle")  # saved rows still glow


def test_templates_set_only_css_variables_inline():
    """Inline style="" attributes only pass values into CSS variables (--value, --cat-color, …):
    no styling crept into a template (and <style> elements are refused by test_security)."""
    jinja = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
    for path in TEMPLATES.rglob("*.html"):
        for value in re.findall(r'\sstyle="([^"]*)"', path.read_text(encoding="utf-8")):
            for declaration in jinja.sub("", value).split(";"):
                declaration = declaration.strip()
                assert not declaration or declaration.startswith("--"), f"{path.name}: {value}"
