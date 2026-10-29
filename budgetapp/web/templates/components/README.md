# Design system: layout, components, CSS and JS

Every page is built from `base.html`, the macros in this folder, the classes in
`static/css/app.css` and the behaviours in `static/js/app.js`. See every component live at
**`/dev/ui-kit`** (outside production; source `templates/dev/ui_kit.html`) and copy from it.

Ground rules (CSP: `script-src 'self'`, `style-src 'self'`, `style-src-attr 'unsafe-inline'`):

- No inline `<script>` (JSON data blocks `<script type="application/json">` are fine), no `on*=`
  attributes, no `hx-on`, no `javascript:` URLs, no `<style>` elements. Page JS goes in
  `static/js/<page>.js` (scripts block), page CSS in `static/css/pages/<page>.css` (head block),
  and only when a component is genuinely missing.
- `style=""` attributes are allowed, but only to pass dynamic values into CSS variables
  (`--cat-color`, `--swatch`, `--value`, `--chart-h`, `--grid-min`, `--grid-gap`, `--stack-gap`,
  `--cluster-gap`, `--truncate-w`, `--grow-min`, `--table-max-h`, `--skeleton-w`).
- Never hard-code colours, sizes or shadows: use the tokens (section 10).
- Colour is never the only signal: pair it with text or an icon (the macros do).
- Macros fail loudly on a wrong tone, variant, unlabelled inline control or non-numeric amount
  (`UndefinedError` with a message saying what to pass), so a typo breaks the page's tests instead
  of rendering the wrong colour.

---

## 1. A page in 20 lines

```jinja
{% extends "base.html" %}
{% import "components/ui.html" as ui %}
{% import "components/forms.html" as form %}
{% block title %}Budgets{% endblock %}
{% block charts %}yes{% endblock %}  {# only on pages that draw charts on first load #}
{% block head %}<link rel="stylesheet" href="/static/css/pages/budgets.css">{% endblock %}  {# optional #}

{% block content %}
{% call ui.page_header("Budgets", "October 2026 · 12 days left") %}
  {{ ui.button("Suggest amounts", icon="wand") }}
  {{ ui.button("Add budget", icon="plus", variant="primary", data_dialog_open="add-budget") }}
{% endcall %}

<div class="grid grid--4">
  {{ ui.stat("Spent", ui.money(spent, cents=false), sub="of " ~ (target|money), tone="warn") }}
</div>

{% call ui.card("Categories", flush=true) %}
  <div class="table-wrap"><table class="table">…</table></div>
{% endcall %}
{% endblock %}

{% block scripts %}<script src="/static/js/budgets.js" defer></script>{% endblock %}
```

Blocks: `title` (page name: document title and top bar; defaults to the active nav label),
`head`, `header_actions` (extra buttons in the top bar, left of the sync status), `content`,
`scripts` (always `defer`), `charts` (any non-empty text loads Chart.js up front).

Direct children of `<main class="content">` get 24px vertical rhythm automatically. Group
related blocks in `<section class="section">` (16px rhythm inside, 40px between sections) with an
optional `<div class="section__header"><div><h2 class="section__title">…</h2><p
class="section__subtitle">…</p></div>…actions…</div>`.

---

## 2. Layout contract (`base.html`)

### 2.1 Context

`web/templating.render()` adds these to **every** page, error pages included. All are optional
(missing ones fall back to safe defaults, so a bare `errors/500.html` still renders), but a
missing `env` hides the environment bar, so **every error handler must render through
`templating.render()` too** (never `TemplateResponse` with only `request`).

| Variable | Type | Used for |
| --- | --- | --- |
| `request` | Starlette `Request` | required by `TemplateResponse`; base does not read it |
| `env` | `"sandbox"` \| `"production"` \| `"test"` \| `"demo"` (see 2.2) | env bar, brand label, document title suffix, `<html data-env>` |
| `nav_items` | list of `{key, label, href, icon, group}` (dicts or objects), display order | sidebar and mobile menu; consecutive items with the same `group` form one group |
| `active_nav` | key or `None` | `aria-current="page"`, top-bar breadcrumb `Group / Title`, default title |
| `conn` | `services.status.ConnectionStatus` or `None` | sync status (pill + action), reconnect and consent banners (see 2.3) |
| `messages` | list of `{level, text}` | toasts in `#flash` (levels `success`, `info`, `warning`, `error`; also `ok`, `warn`, `danger`, `over`; anything else shows as info) |
| `app_version` | str | sidebar footer |

### 2.2 `env` values

| `env` | Env bar (sticky, above the top bar) | Brand label | `<title>` suffix |
| --- | --- | --- | --- |
| `"sandbox"` | purple, stripes: "**Sandbox** — test data. Nothing here touches your real Amex account." | Sandbox (purple) | ` (Sandbox)` |
| `"test"` | same purple bar with "**Test** — test data…" (pytest renders) | Test | ` (Test)` |
| `"demo"` | burnt orange, stripes the other way, alert icon: "**Demo data** — nothing here is real. Made-up transactions in a throwaway database; your Amex account is never touched." | Demo (orange) | ` (Demo)` |
| `"production"` | none | Live (green) | none |
| any other string | none | the value, capitalised | none |
| `None` / missing | none (never assumes Sandbox) | none | none |

The web shell passes `env="demo"` when it serves a demo database (`serve --demo <id>`). Below
900px the bars keep only their first sentence.

### 2.3 What base reads from `conn`

`conn` is `services.status.connection_status(session)`; pass `None` when it cannot be computed
(then no pill, no action and no connection banners are shown). base.html and
`components/sync_status.html` read exactly these fields, all of which exist on F1's dataclasses
(checked by rendering with real objects under `StrictUndefined`):

| Read by | Field | Type | Used for |
| --- | --- | --- | --- |
| base + partial | `conn.state` | `"healthy"` \| `"stale"` \| `"error"` \| `"needs_reconnect"` \| `"not_linked"` | pill state; `not_linked` suppresses the reconnect banner |
| base + partial | `conn.needs_reconnect` | bool (property) | reconnect banner; pill and action "Reconnect" |
| base | `conn.consent_warning` | bool (property) | consent banner |
| base | `conn.items[*].consent_warning` | bool | which Items count for the consent banner |
| base + partial | `conn.items[*].consent_days_left` | int (negative = expired) | banner tone and wording; partial: "Renew access" when any is < 0 |
| base | `conn.items[*].consent_due_at` | aware datetime | banner date: `consent_due_at.astimezone().date()` then `|date_long` |
| partial | `conn.label` | str | pill text fallback |
| partial | `conn.syncing` | bool (property) | "Syncing…" pill, polling, disabled Sync now |
| partial | `conn.stale` | bool (property) | amber "Synced 2 days ago" pill |
| partial | `conn.last_sync_success_at` | aware datetime or `None` (property) | "Synced " + `|relative_time` |
| partial | `conn.primary` | `ItemStatus` or `None` (property) | the worst Item (its fields below) |
| partial | `conn.primary.institution_name` | str or `None` | healthy tooltip "American Express via Plaid" |
| partial | `conn.primary.error_message`, `conn.primary.error_code` | str or `None` | error / reconnect tooltip |

Notices (between the top bar and the page, only rendered when there is one):

- `#reconnect-banner` (tone `over`, role alert, **Reconnect** → `/connection`) when
  `conn.needs_reconnect` and `conn.state != "not_linked"`.
- Otherwise `#consent-banner` when `conn.consent_warning`, for the Item with the fewest
  `consent_days_left` among those with `consent_warning`: days < 0 → `over` "Your Amex consent has
  expired" ("…it lapsed on Oct 25, 2026"); days = 0 → `over` "expires today"; else `warn`
  "Renew your Amex connection by Nov 20, 2026 (23 days left)." Action **Renew now** →
  `/connection`. (One Plaid update-mode session fixes both, so the consent banner waits while the
  reconnect banner shows.)
- **Not on the Connection page** (`active_nav == "connection"`): that page shows Reconnect /
  Renew consent itself (with the due date and days left), and a banner there would only link to
  the page already open.
- Below 600px a notice is one line: icon, title and button (`.banner__text` is hidden), so the
  title carries what matters (by when, days left).

### 2.4 Sync status partial (`components/sync_status.html`)

`<div id="sync-status">` holds the status pill (a link to `/connection`) **and** its action, so
the two always change together. base.html includes it in the top bar when `conn` is not `None`.
It needs only `conn` (and the `relative_time` filter).

The pill and the action are chosen independently (first match wins in each list):

| Pill: situation | State class and text |
| --- | --- |
| `conn.syncing` | `syncing` "Syncing…" (pulsing dot; the partial polls, see below) |
| `conn.needs_reconnect` (and state is not `not_linked`) | `needs_reconnect` "Reconnect needed" (tooltip: `primary.error_message` or `error_code`) |
| `conn.state == "error"` | `error` "Sync problem" (tooltip: `primary.error_message` or `error_code`) |
| `conn.state == "not_linked"` | `not_linked` "Not linked" (hollow dot) |
| `conn.state == "stale"` or `conn.stale` | `stale` (amber) "Synced 2 days ago" (or `conn.label` when never synced) |
| otherwise | `healthy` "Synced 5 min ago" (or `conn.label`; tooltip "American Express via Plaid") |

| Action: situation | Action |
| --- | --- |
| `conn.state == "not_linked"` | primary **Link account** link → `/connection` |
| `conn.needs_reconnect` | **Reconnect** link → `/connection` |
| any Item's `consent_days_left` < 0 | **Renew access** link → `/connection` |
| `conn.syncing` | disabled Sync now with a spinning icon |
| otherwise | **Sync now**: `<form method="post" action="/connection/sync">` with a button carrying `hx-post="/connection/sync" hx-target="#sync-status" hx-swap="outerHTML"`, so it also works without JS |

Route contract (SYNC package) — **both routes return this one partial**, rendered with a fresh
`conn`. Until SYNC lands, the shell's placeholder `routes/connection.py` serves both (Sync now
answers "Syncing isn't built yet." as an info toast); SYNC replaces the module and keeps the URLs:

- `POST /connection/sync`: for HTMX requests status 200 with the partial (it says "Syncing…", so
  mark the run as running before responding), optionally followed by `ui.flash_oob(...)`. If a
  sync is already running, return the partial too (never a 409: app.js would show an error
  toast). For plain form posts (no JS) redirect (303) to `/connection`: browsers send no
  `Referer` to this app (`Referrer-Policy: no-referrer`), so there is no page to go back to
  (`flash.redirect(request, "/connection", "info", "Sync started")`).
- `GET /connection/sync-status`: status 200 with the partial. While `conn.syncing` the partial
  carries `hx-get="/connection/sync-status" hx-trigger="every 4s" hx-swap="outerHTML"`; polling
  stops by itself when a response no longer says syncing. Polling errors never raise toasts.

### 2.5 Error pages

- Every error page's title is its only `<h1>` (`empty_state(..., heading=1)`); 500 and the
  generic notice page use the `warn` icon tone, so an error never looks like "Coming soon".
- `errors/404.html`, `errors/500.html`: extend base; no extra context. Never pass exception text.
- `errors/error.html`: other statuses (400/403/503, "Upgrade needed", "Keychain approval needed").
  Optional context: `status_code`, `title`, `message`, `command` (shown as copyable code, e.g.
  `./run doctor --env production`), `icon` (sprite name, default `alert`).
- Render all three through `templating.render()` so `env`, `nav_items` and `conn` are set; with an
  empty context they still render, without an env bar or nav (never a false "Sandbox" claim).

### 2.6 What the web shell must set up (`web/templating.py`)

- Filters: `money` = `budgetapp.money.format_money` (`{{ x|money }}`, `{{ x|money(signed=true) }}`;
  see 3.2), `relative_time` (aware datetime → "5 min ago", "just now", "3 h ago", "2 days ago";
  the pill prefixes "Synced "), `date_long` (`date` → "Nov 20, 2026").
- `templates.env.policies["json.dumps_kwargs"] = {"sort_keys": True, "default": str}` so `|tojson`
  (chart configs) accepts `Decimal` (it becomes a numeric string; the chart helper converts it).
- Tests: render `base.html` and `components/sync_status.html` under `StrictUndefined` with real
  `ConnectionStatus`/`ItemStatus` objects (healthy, syncing, stale, error, needs_reconnect, consent
  warning / today / expired, not linked, `None`) and every `env` value.
- `ui.flash_oob` from Python: `templates.env.get_template("components/ui.html").module.flash_oob(level, text)`
  returns `Markup` to append to an HTMX partial.

Recommended `nav_items` (icon names are from the sprite):

```python
NAV_ITEMS = [
    {"key": "dashboard", "label": "Dashboard", "href": "/", "icon": "home", "group": "Money"},
    {
        "key": "transactions",
        "label": "Transactions",
        "href": "/transactions",
        "icon": "list",
        "group": "Money",
    },
    {"key": "budgets", "label": "Budgets", "href": "/budgets", "icon": "target", "group": "Money"},
    {
        "key": "trends",
        "label": "Trends",
        "href": "/trends",
        "icon": "trending",
        "group": "Insights",
    },
    {
        "key": "recurring",
        "label": "Recurring",
        "href": "/recurring",
        "icon": "repeat",
        "group": "Insights",
    },
    {
        "key": "statements",
        "label": "Statements",
        "href": "/statements",
        "icon": "receipt",
        "group": "Insights",
    },
    {
        "key": "categories",
        "label": "Categories",
        "href": "/categories",
        "icon": "tag",
        "group": "Organize",
    },
    {"key": "rules", "label": "Rules", "href": "/rules", "icon": "wand", "group": "Organize"},
    {"key": "imports", "label": "Import", "href": "/import", "icon": "upload", "group": "Data"},
    {
        "key": "connection",
        "label": "Connection",
        "href": "/connection",
        "icon": "link",
        "group": "Data",
    },
    {
        "key": "settings",
        "label": "Settings",
        "href": "/settings",
        "icon": "settings",
        "group": "Data",
    },
]
```

Base also renders: a skip link to `#main`, the icon sprite, the sidebar (≥ 900px: brand +
environment label, grouped nav, "Private · runs only on this Mac · v{app_version}") or below 900px
a mobile bar with a CSS-only `<details>` menu, `<div id="flash" class="toast-stack">` (holds
`messages` and out-of-band flashes), `<meta name="htmx-config" content='{"includeIndicatorStyles":false}'>`,
and the scripts in this order, all `defer`: `/static/vendor/htmx.min.js`, Chart.js (only when the
`charts` block is non-empty), `/static/js/app.js`, then your `scripts` block.

---

## 3. Conventions

### 3.1 One tone vocabulary

Every macro that takes a colour meaning uses the same names:

| Tone | Meaning |
| --- | --- |
| `ok` | good / on track / credit (green) |
| `warn` | needs attention soon (amber) |
| `over` | over budget, error, danger (red) |
| `info` | neutral information (blue) |

Aliases, accepted everywhere and converted to the canonical class: `success` → `ok`,
`warning` → `warn`, `danger` → `over`, `error` → `over`. Some macros add their own extra values:

| Macro (parameter) | Accepted |
| --- | --- |
| `banner(tone)` | `info` (default) · `ok` · `warn` · `over` · `sandbox` |
| `badge(tone)` | `neutral` (default) · `accent` · `ok` · `warn` · `over` · `info` · `sandbox` · `muted` |
| `stat(tone)` | `neutral` (default) · `ok` · `warn` · `over` · `info` |
| `money(tone)` | `neutral` (plain ink, never green) · `ok` · `warn` · `over` · `muted` |
| `progress(status)` | `ok` · `warn` · `over` · `none` (default: automatic from `pct`) |
| `flash_message(level)` | lenient (levels come from Python): the four tones + aliases; anything else → `info` |

An unknown tone raises `UndefinedError: … ui.badge(): unknown tone "dangerous". Use one of: …`.
Only the canonical classes are emitted (`.badge--over`, never `.badge--danger`; `.banner--danger`
exists in CSS as an alias for hand-written markup). Connection states are not tones: see
`status_pill`.

### 3.2 Money: `|money` and `ui.money` render identically

`{{ x|money }}` (the shell builds it on `budgetapp.money.format_money`) and
`{{ ui.money(x) }}` / `{{ ui.money_text(x) }}` produce the **same text** for every `Decimal`, `int`
and `None`: half-up rounding to the cent (`ROUND_HALF_UP`), a typographic minus U+2212 for
negatives (`−$54.99`), `$`, thousands separators (`$1,234,567.89`), `+` with `signed=true`, `—` for
`None`, and the sign taken from the **rounded** value (`-0.004` → `$0.00`, unsigned, not green).

How `ui.money` achieves it without Python help: it computes the cents with integer arithmetic on
the `Decimal` itself, `floor(|amount| × 100 + ½)`, which is exactly half-up (Decimal's own
`format()` would round half-even, and floats are never involved), then formats those exact cents.
`cents=false` rounds the cent value half-up again to whole dollars (`$2,430.50` → `$2,431`, the
same as rounding `format_money`'s result). The render harness compares the two over 3,017 values
(random amounts with up to four decimals, half-cent ties, huge and negative values).

Differences in what they accept: `|money` also takes numeric strings (via `to_money`) and refuses
floats; `ui.money` refuses strings and booleans (parse with `money.to_money` first) and accepts
floats only so the UI kit can use Jinja literals — services always pass `Decimal`.

Use `ui.money` for amounts shown as amounts (tabular figures, credit colour, pending style), and
`|money` or `ui.money_text` inside sentences, labels and tooltips. Chart tooltips use
`Intl.NumberFormat` with the same U+2212 minus; pass charts cent values (two decimals).

### 3.3 Category colours

A category's `color` is stored as one of three forms, and every component accepts all three:

| Stored value | Dots, chips, swatches (`ui.color_value`) | Charts (`resolveColor` in app.js) |
| --- | --- | --- |
| `"cat-1"` … `"cat-16"` (preferred; the seeded categories use these) | `var(--cat-N)`: changes with the theme | the theme's `--cat-N` |
| `"other"` | `var(--chart-other)` (neutral grey) | `--chart-other` |
| `"#rgb"` / `"#rrggbb"` (a custom colour) | the hex, same in both themes | the hex, nudged lighter (dark) or darker (light) just enough to reach 3:1 against the card surface, so a very dark custom colour can look slightly lighter in a dark chart than in its dot |

Case and surrounding spaces are ignored. Anything else (`None`, `"red"`, `"cat-17"`) gives no
colour: dots and swatches fall back to the "other" grey, and a chart series takes "other".
**Colour follows the entity**: pass each category's own `color` (dots, `category_select`
options' `data-color`, chart datasets, HTML legends via
`style="--swatch: {{ ui.color_value(c.color) }}"`), so a category keeps its hue everywhere and when
filters change the series list. The Categories editor should offer the 16 slots as swatches and
keep a raw hex only for a custom colour.

### 3.4 Attributes and call bodies

Macros marked **attrs** turn extra keyword arguments into HTML attributes, `_` becoming `-`
(`hx_post="/x"` → `hx-post="/x"`, `data_confirm="Sure?"`); `None`/`False` are skipped, `True`
renders a bare attribute. Inline event handlers (`onclick=…`, `hx_on__click=…`) and
`javascript:` values raise an error: the CSP would block them silently in the browser. Macros marked **call** take a body via
`{% call ui.x(...) %}…{% endcall %}`. Every macro returns markup, so results can be passed into
other macros (`ui.stat("Spent", ui.money(x))`).

---

## 4. Macros: `components/ui.html` (`{% import "components/ui.html" as ui %}`)

| Macro | Purpose |
| --- | --- |
| `icon(name, label=none, size=none, class=none)` | Sprite icon. Decorative (`aria-hidden`) unless `label` is given (then `role="img"`). `size`: `xs` 12, `sm` 16, (default 18), `lg` 20, `xl` 24. |
| `money(amount, signed=false, pending=false, tone=none, cents=true, symbol="$", class=none)` | Amount in tabular figures (3.2). Plaid sign: charges `$12.34` plain; credits `−$12.34` green; `None` → muted `—`. `signed=true` prefixes `+` and turns off the automatic green (deltas). `pending=true` italic + muted + "(pending)" for screen readers. `tone` (3.1): `neutral`, `ok`, `warn`, `over`, `muted`. `cents=false` → whole dollars. |
| `money_text(amount, signed=false, cents=true, symbol="$")` | The same text without markup (sentences, aria-labels, tooltips). |
| `decimal_text(amount, places=2)` | Plain number for form values: half-up, no grouping, ASCII `-` (`1234.50`); `""` for `None`. |
| `category_badge(category, chip=false, href=none, class=none)` | Colour dot + name from `category.name`, `.color` (3.3), `.is_hidden` (muted, eye-off icon, "(hidden)"). `None` → "Uncategorized" with a hollow dot. `chip=true` pill style; `href` makes it a link. |
| `progress(pct, label=none, pace_pct=none, status=none, size=none, show_pct=true, name=none)` | Budget meter, `role="meter"` with a text value. `pct` = percent used (`Decimal`/int/float; `None` = no budget; negative, when refunds exceed charges, shows its text over an empty bar). Status is automatic: ok < 80 ≤ warn ≤ 100 < over (warn/over add an alert icon), or force it with `status`. Text is a whole percent, except next to a threshold where one decimal is rounded toward the colour shown (79.96 → "79.9%" green, 100.04 → "100.1%" red). Over 100% the bar fills, a dark tick overhanging the track marks where the budget ran out and the overspend is hatched. `pace_pct` (0–100, share of the period elapsed) draws a small caret above the bar with a notch through it (tooltip "Pace: 60% of the period has passed"). `label` sits top-left ("$415 of $500"); `show_pct=false` hides the percentage. `name` = accessible name, default the label's text, else "Budget used": give each meter its own (`name="Groceries budget"`). `size`: `sm` 6px, `lg` 10px. |
| `stat(label, value, sub=none, tone=none, icon=none, href=none, id=none)` **call** | Stat tile. `value` is usually `ui.money(..., cents=false)`; it is never truncated: inside a `.grid` or `.stack` the value's type scales with the tile (26px down to 18px). `tone` (`ok`/`warn`/`over`/`info`/`neutral`) colours the sub line and adds an icon. The optional call body renders below (e.g. `ui.progress(..., size="sm", show_pct=false, name="Monthly target used")`). `href` makes the tile a link. |
| `page_header(title, subtitle=none, back_href=none, back_label="Back")` **call** | The page `<h1>`; the call body holds action buttons (right-aligned, full width on phones). |
| `card(title=none, subtitle=none, actions=none, flush=false, footer=none, id=none, class=none)` **call** | Surface container; body from the call. `flush=true` removes body padding (tables). `actions`/`footer` take markup built with `{% set x %}…{% endset %}`. With `id`, the title gets `id="<id>-title"` and the card is labelled by it. |
| `pagination(page, pages, url, total=none, target=none, param="page", label="Pagination")` | "Page 3 of 12 · 574 results", Prev, windowed numbers, Next (phones: Prev/Next only). `url` = current URL **without** the page param (e.g. `request.url.remove_query_params("page")`); the macro appends `page=N`. With `target` (selector) links also get `hx-get`, `hx-target`, `hx-push-url="true"`. Nothing renders when `pages <= 1`. |
| `sort_th(label, key, sort, url, num=false, param="sort", target=none, first="desc")` | Sortable `<th>` with `aria-sort`. `sort` = current value (`"date"` asc, `"-date"` desc); `url` without sort/page params. First click sorts by `first`; clicking the active column flips it. `target` as for pagination. |
| `empty_state(title, message=none, icon="list", action_href=none, action_label=none, compact=false, heading=none, tone=none)` **call** | Centered icon, title, message; one primary link via `action_href`, or any buttons via the call body. Put it inside a `.card`. `heading=1`–`6` renders the title as that heading (the error pages use `heading=1`, their only `<h1>`); default `<p>`. `tone`: icon colour `warn` / `over` / `info` / `ok` (default the accent; error pages use `warn`). |
| `banner(tone, text, title=none, icon=none, action_href=none, action_label=none, id=none, role=none)` **call** | Notice. `tone` (3.1): `info`, `ok`, `warn`, `over`, `sandbox`. Bold `title` runs into `text`. One link action (danger-styled for `over`), or buttons via the call body. `role` defaults to `alert` for `over`, else `status`. |
| `flash_message(level, text)` | One toast for `#flash`. `level`: `success`/`ok`, `info`, `warning`/`warn`, `error`/`danger`/`over`; unknown → info. Errors get `role="alert"` and stay until closed (`data-persist`); others auto-dismiss after 6 s (paused on hover/focus). Close button on all. |
| `flash_oob(level, text)` | The toast wrapped in `<div hx-swap-oob="beforeend:#flash">`: append to any HTMX partial response (from Python see 2.6). |
| `status_pill(state, text=none, detail=none, href=none)` | Pill with a coloured dot: `healthy`, `syncing` (pulsing), `needs_reconnect`, `error`, `stale`, `not_linked`. `text` defaults per state; `detail` becomes the tooltip (and part of the link's accessible name). |
| `badge(text, tone=none, icon=none, title=none)` | Small label (3.1). E.g. Pending (`icon="clock"`), Payment (`muted`), Split (`info`, `icon="split"`), Rule (`accent`, `icon="wand"`). |
| `button(label, variant="secondary", size=none, icon=none, icon_only=false, href=none, type="button", spin=false, tooltip=none)` **attrs** | `<button>` (or `<a>` with `href`). `variant`: `primary`, `secondary`, `ghost`, `danger`; `size`: `sm` (others raise). `icon_only=true`: square button, `label` becomes `aria-label` + tooltip. `spin=true` spins the icon while an HTMX request from it runs. `class=` adds classes. |
| `tabs(items, active, label="Sections", target=none)` | Link tabs (`aria-current="page"`), scroll sideways when they don't fit. `items`: `[{key, label, href, count?}]`. With `target` links swap that region via HTMX and push the URL. |
| `dialog(id, title, size=none, autoopen=false)` **call** | Modal `<dialog>`. Open: any element with `data-dialog-open="<id>"`. Close: `data-dialog-close`, Esc, backdrop click, `<form method="dialog">`, or an `HX-Trigger: dialog:close` response header. Buttons go in `<div class="dialog__footer">` at the end of the body (`dialog__footer--split` for left/right groups). `size`: `sm` 420px, `lg` 780px (default 560px). `autoopen=true` opens it as soon as it appears (dialogs returned by HTMX). |
| `chart(id, config, height=280, label=none)` **call** | `<canvas data-chart="<id>" role="img">` + `<script type="application/json" id="<id>-config">`. `label` = accessible name (falls back to `config.title`). The optional call body (a short summary or a small table of the same numbers) renders in a visually hidden `<div id="<id>-desc">` that the canvas references with `aria-describedby`. See section 7. |
| `spinner(label="Loading…", size=none)` | Spinning ring with `role="status"`; `size="lg"`. |
| `loading(label="Loading…")` | Spinner + text, e.g. initial content of an `hx-trigger="load"` region. |
| `skeleton(lines=3, title=true)` | Grey placeholder lines (first loads only; refetches keep the old content dimmed instead). |
| `tip(text, icon="info", pos=none)` | Focusable info icon with a CSS tooltip. `pos`: above (default), `bottom`, `left`, `right`, `bottom-end`. |
| `kv(items)` | Definition list for detail panes: `[(term, value), …]`; `None`/`""` → `—`. Two columns; stacks (term above value) when its own container is narrower than 360px, and on phones. |
| `html_attrs(attrs, skip=())` | Helper: render a dict as attributes (the **attrs** mechanism). |
| `color_value(value)` | Helper: a category colour safe for `style=""` (3.3). |

Examples:

```jinja
{{ ui.money(t.amount, pending=t.pending) }}                         {# $86.42 / −$54.99 #}
{{ ui.money(line.remaining, tone="neutral") }}                      {# −$60.10 in plain ink #}
{{ ui.money(delta, signed=true, tone="ok" if delta < 0 else "over") }}
{{ ui.category_badge(t.category, href="/transactions?category=" ~ t.category.id) }}
{{ ui.progress(line.percent_used, label=(line.spent|money) ~ " of " ~ (line.available|money),
               pace_pct=line.pace_percent, name=line.category.name ~ " budget") }}
{% set actions %}{{ ui.button("View all", href="/transactions", size="sm", variant="ghost") }}{% endset %}
{% call ui.card("Top merchants", subtitle="October 2026", actions=actions, flush=true) %}…{% endcall %}
{{ ui.pagination(page.page, page.pages, request.url.remove_query_params("page"), total=page.total_count, target="#results") }}
{% call ui.dialog("split-dialog", "Split charge", size="lg") %}<form hx-post="…">…<div class="dialog__footer">…</div></form>{% endcall %}
{{ ui.button("Delete", icon="trash", variant="danger", hx_post="/rules/3/delete", hx_target="#rules", data_confirm="Delete this rule?") }}
```

---

## 5. Macros: `components/forms.html` (`{% import "components/forms.html" as form %}`)

Every field macro renders `<div class="field">` = `<label>` + control + error + help, wiring
`for`/`id`, `aria-describedby` and `aria-invalid`. Shared parameters:

| Parameter | Meaning |
| --- | --- |
| `name`, `label` | required |
| `id` | defaults to `f-<name>`; **pass one** when the same name appears more than once on a page (row editors, dialogs + page forms) |
| `help`, `error` | text under the control; `error` turns the control red |
| `required` / `optional` | `required` adds the attribute and a red `*`; `optional` adds "(optional)" |
| `compact` | small label + 32px control (filter bars) |
| `hide_label` | keeps the label for screen readers only |
| `wrapper_class` | extra class on the `.field` wrapper (e.g. `field--grow` in a filter bar) |
| **attrs** | extra attributes on the control (`hx_post=…`, `hx_trigger="change"`, `autofocus=true`, `min="0"`, `disabled=true`); `class=` is appended to the control's classes |

Options (`select_field`, `inline_select`, `segmented`): `[("value", "Label"), …]`, or
`[{"value": …, "label": …, "disabled": bool, "attrs": {…}}, …]`, or plain strings; optgroups:
`{"group": "Label", "options": [ … ]}`. `value` may be a list for `multiple`. Values compare as
strings, so ints and `Decimal` ids are fine.

| Macro | Purpose |
| --- | --- |
| `field(name, label, value=none, type="text", placeholder=none, id=none, help=none, error=none, required=false, optional=false, compact=false, hide_label=false, wrapper_class=none)` **attrs** | Text-like `<input>` (`text`, `email`, `number`, `search`, `password`, `month`, …). |
| `date_field(name, label, value=none, min=none, max=none, id=none, help=none, error=none, required=false, optional=false, compact=false, hide_label=false, wrapper_class=none)` **attrs** | `<input type="date">`; values may be `date` objects or ISO strings. |
| `money_field(name, label, value=none, placeholder="0.00", allow_negative=false, id=none, help=none, error=none, required=false, optional=false, compact=false, hide_label=false, wrapper_class=none)` **attrs** | `$`-prefixed input, `inputmode="decimal"` (`text` with `allow_negative`), tabular figures. Numbers render half-up with two decimals (`500.00`, via `ui.decimal_text`); strings are kept as typed (re-rendering after an error). The server parses with `money.parse_money_input`. |
| `select_field(name, label, options, value=none, placeholder=none, multiple=false, id=none, help=none, error=none, required=false, optional=false, compact=false, hide_label=false, wrapper_class=none)` **attrs** | `<select>`; `placeholder` adds a first `value=""` option ("All categories"). |
| `category_select(name, categories, value=none, label="Category", inline=false, include_hidden=false, placeholder=none, id=none, help=none, error=none, required=false, compact=false, hide_label=false, wrapper_class=none)` **attrs** | Picker from category objects (`id`, `name`, `color`, `is_hidden`). Hidden categories are omitted unless selected or `include_hidden=true` (then labelled "(hidden)"). Options carry `data-color` (3.3). `inline=true` → borderless table-cell select (max 13rem wide) with the selected category's dot; app.js recolours the dot on change. |
| `textarea_field(name, label, value=none, rows=3, placeholder=none, id=none, help=none, error=none, required=false, optional=false, hide_label=false, wrapper_class=none)` **attrs** | `<textarea>`. |
| `checkbox_field(name, label, checked=false, value="1", help=none, switch=false, id=none, disabled=false, wrapper_class=none)` **attrs** | Checkbox with the label on the right and help under it. Unchecked boxes submit nothing: treat a missing field as false. |
| `switch_field(name, label, checked=false, value="1", help=none, id=none, disabled=false, wrapper_class=none)` **attrs** | Same as `checkbox_field(…, switch=true)`: toggle with `role="switch"`. |
| `tags_field(name, label, tags=none, suggestions=none, placeholder="Add a tag…", id=none, help="Press Enter or comma to add a tag.", error=none, optional=false, hide_label=false, wrapper_class=none)` **attrs** | Tag chips + entry box. Enter or `,` adds (comma-separated paste works, case-insensitive de-dupe), Backspace in an empty box removes the last chip, blur commits pending text, × removes. Submits one `name` field per tag **plus one always-present empty `name` field**: read with `[t for t in form.getlist(name) if t.strip()]` (empty list = all removed). `suggestions` feed a `<datalist>`. Adding/removing fires `change` (autosubmit forms save). |
| `search_field(name="q", value=none, label="Search", placeholder="Search…", id=none, shortcut=true)` **attrs** | Search box with icon, marked `data-search` so `/` focuses it; shows a `/` hint. |
| `segmented(name, options, value=none, label=none, hide_label=false, help=none, wrapper_class=none)` **attrs** | Radio group drawn as a segmented control (presets: "This month / Last month / 90 days / Year / All", "Calendar month / Statement cycle"). With a `label` it renders like the other fields: `<fieldset class="field segmented-field">` with a visible `<legend class="field__label">` and `help` under it; with `hide_label=true` (or no label) just the control, the label read by screen readers only. **attrs** go on the outer `<fieldset>` (e.g. `hx_post` + `hx_trigger="change"`: radio changes bubble). In a narrow column the options stretch into rows; on phones they stay in one row that scrolls sideways. |
| `inline_select(name, options, value=none, label=none, id=none)` **attrs** | Borderless select for table cells. **`label` is required** (it becomes the `aria-label`; make it specific, e.g. "Period for Groceries"); leaving it out raises. |
| `inline_money(name, value=none, label=none, id=none, placeholder="0.00")` **attrs** | Compact right-aligned money input for table cells (budget amounts); numbers render half-up with two decimals. **`label` is required** ("Groceries budget"). Add `aria_invalid="true"` to show an error border. |
| `filter_bar(action, target=none, history="replace", autosubmit=true, label="Filters")` **call** **attrs** | GET form laid out as one row of compact controls (`role="search"`). With `target` it swaps that region: `hx-get` + `hx-target` + `hx-sync="this:replace"`, and `history` decides the address bar: `"replace"` (default) → `hx-replace-url="true"`: the URL follows the filters, so a reload or a shared link keeps them, without adding a Back step per change; `"push"` → `hx-push-url="true"` (each submit becomes a history entry); `none` → the URL is left alone. Pagination, sort headers and tabs still push (deliberate navigation). `autosubmit` submits on change, debouncing typing and date entry (400 ms). End the body with `<div class="filter-bar__actions">` for Reset / Export. Without JS it is a plain GET form with an Apply button. |

```jinja
{% call form.filter_bar("/transactions", target="#results") %}
  {{ form.search_field("q", value=f.q, placeholder="Search merchant, description or notes") }}
  {{ form.category_select("category", categories, value=f.category, placeholder="All categories", compact=true) }}
  {{ form.select_field("status", "Status", [("", "All"), ("pending", "Pending"), ("posted", "Posted")], value=f.status, compact=true) }}
  {{ form.date_field("from", "From", value=f.date_from, compact=true) }}
  <div class="filter-bar__actions">
    <a class="btn btn--ghost btn--sm" href="/transactions">Reset</a>
    {{ ui.button("Export CSV", icon="download", size="sm", href="/transactions/export?" ~ request.url.query) }}
  </div>
{% endcall %}
<div id="results">{% include "transactions/_results.html" %}</div>

{# row-level inline edit #}
{{ form.category_select("category_id", categories, t.category_id, inline=true, id="cat-" ~ t.id,
     label="Category for " ~ t.name, hx_post="/transactions/" ~ t.id ~ "/category",
     hx_trigger="change", hx_target="closest tr", hx_swap="outerHTML") }}
```

---

## 6. Tables

Every table goes in a `.table-wrap` (inside a `card(flush=true)` for full-bleed tables):

```html
<div class="table-wrap">
  <table class="table">
    <thead><tr>
      {{ ui.sort_th("Date", "date", sort, url) }}
      <th scope="col" class="col-grow">Description</th>
      <th scope="col">Category</th>
      <th scope="col" class="col-optional-md">Card member</th>
      <th scope="col" class="col-optional-lg">Tags</th>
      {{ ui.sort_th("Amount", "amount", sort, url, num=true) }}
      <th scope="col" class="shrink"><span class="sr-only">Actions</span></th>
    </tr></thead>
    <tbody><tr>
      <td class="table__date">Oct 27</td>
      <td class="col-grow"><span class="table__primary">Whole Foods</span><span class="table__secondary">WHOLEFDS MKT 10255</span></td>
      <td>…</td><td class="col-optional-md">…</td><td class="col-optional-lg">…</td>
      <td class="num">{{ ui.money(t.amount) }}</td>
      <td class="table__actions">…</td>
    </tr></tbody>
  </table>
</div>
```

- **`.table-wrap`** never lets a table push the page sideways: at every width it scrolls
  horizontally when its table is too wide. app.js measures it: `.is-fit` (the table fits; the
  wrap becomes a plain box so the sticky header follows the page under the top bar) or
  `.is-scrollable` (header sticks inside the wrap; the edge that has more columns fades:
  `.is-scrolled` / `.is-scrolled-end`). `.table-wrap--scroll` is a fixed-height scroller
  (`--table-max-h`, default 420px) with its own sticky header.
- **`.col-grow`** (on the `<th>` and every `<td>` of ONE text column: description, merchant, rule):
  the column takes the width the other columns leave and its `.table__primary` /
  `.table__secondary` lines truncate to fit, so the table fits its card at desktop widths. It
  keeps at least `--grow-min` (10rem; 8rem between 900 and 1279px; 6rem in `.table--compact`,
  `--grow-min-compact`); when even that does not fit, the wrap scrolls.
- **Column priorities** (on the `<th>` and every `<td>` of a column): `.col-optional-lg` is shown
  only from 1360px, `.col-optional-md` from 1100px, `.col-optional-sm` is hidden on phones
  (< 600px). **Never put them on Amount, the description or the actions column**: Amount must
  never be hidden.
- `.truncate` on a cell caps a text column at `--truncate-w` (280px; 180px on phones) instead.
- Phones (< 900px): tables keep a usable `min-width: 560px` and scroll inside their wrap
  (`.table--compact` tables are exempt and simply shrink).
- Cells: `.num` (right-aligned tabular numbers; Amount), `.center`, `.shrink` (as narrow as its
  content), `.table__primary`, `.table__secondary`, `.table__date`, `.table__actions`. Rows:
  `tr.is-muted` (card payments: every text in the row muted), `tr.is-pending`, `tr.is-selected`.
  Variants: `.table--compact`, `.table--bordered`, `.table--numbers` (every `td` numeric).
  `tfoot` rows are totals. Sticky `thead`, hover rows, no zebra.

---

## 7. Charts (`ui.chart` + app.js)

`{{ ui.chart("spend-trend", config, height=300, label="Spending by month, last 12 months") }}`
and `{% block charts %}yes{% endblock %}` on the page (charts that arrive later through HTMX
still work: app.js lazy-loads Chart.js from `/static/vendor/chart.umd.min.js`).

`config` (plain dict, serialised with `|tojson`):

| Key | Meaning |
| --- | --- |
| `type` | `"bar"` (default), `"line"`, `"doughnut"`, `"pie"` |
| `data.labels` | x categories (month labels, category names) |
| `data.datasets[]` | `{label, data, color?, colors?, fill?, dashed?, type?}`; `data` numbers or numeric strings (`Decimal` is fine); `color` = series colour; `colors` = per-point colours (bars: one per bar; doughnut: one per slice); line-only: `fill: true` (10% wash), `dashed: true` (targets, projections); `type` mixes a line into a bar chart |
| `format` | `"usd"` (default: ticks `$1.2K`, tooltips `$1,234.56`), `"usd0"` (whole dollars), `"percent"` (values already 0–100), `"number"` |
| `stacked` | stack bars (2px surface gaps between segments; tooltip footer shows the total) |
| `horizontal` | horizontal bars (long category names) |
| `legend` | default: shown for ≥ 2 series and for doughnuts; `false` to hide (then label marks directly or add an HTML `.legend`) |
| `sparkline` | no axes/legend/points; for tiny trend lines in stat tiles (use `height=48`) |
| `title` | accessible-name fallback |
| `options` | raw Chart.js options deep-merged last (functions are impossible in JSON; formatting comes from `format`) |

Colour values: a category's stored colour as is (`"cat-1"`…`"cat-16"`, `"other"`, `"#rrggbb"`;
3.3), or `"accent"`, `"ok"`, `"warn"`, `"over"`, `"info"`, `"muted"`, `"--any-token"` /
`"var(--any-token)"`, or another literal CSS colour. A dataset without `color` takes
`--cat-<index+1>`; there is no 17th hue (index ≥ 16 gets "other"). Past ~8 series, fold the tail
into one `"Other"` dataset with `"color": "other"`. Legend keys use the resolved series colour.

The helper reads gridline, text, tooltip and palette colours from the CSS tokens, formats USD with
`Intl.NumberFormat` (minus U+2212), re-renders after HTMX swaps (`htmx:afterSettle`, only charts
that are new or whose config changed) and redraws everything when the OS switches between light
and dark. Charts are never the only way to read a number: show the key values in text or a table
nearby (the chart's call body is read by screen readers).

---

## 8. Behaviours (`static/js/app.js`)

| Attribute / event | Behaviour |
| --- | --- |
| `data-dialog-open="<id>"` | click opens `<dialog id>` (works together with `hx-get` on the same element, e.g. load the dialog body and open it) |
| `data-dialog-close` | click closes the enclosing dialog |
| `dialog[data-dialog-autoopen]` | opened when it appears (initial load or swap) |
| `HX-Trigger: dialog:close` (response header) | closes the dialog containing the requesting element (else every open dialog) |
| `data-confirm="Question?"` | on a plain form/submit button/link: native confirm before submitting; on the element that makes an HTMX request (the form for `hx-post` forms, the button for `hx-post` buttons, or the submit button): confirm before the request |
| `form[data-autosubmit]` | submits on `change` (selects, checkboxes, radios, tags); text/search inputs and date/month inputs debounce 400 ms (typing a year fires a change per digit); Enter submits at once |
| `[data-search]` | `/` focuses it (ignored while typing in a field or when a dialog is open) |
| `[data-tags-input]` | tag chips editor (see `tags_field`) |
| `[data-category-select]` in `.inline-cat` | recolours the dot on change (from the option's `data-color`) |
| `[data-toast]` in `#flash` | ok/info/warn auto-dismiss after 6 s (paused while hovered/focused); errors (`data-persist`) stay; `[data-dismiss]` closes it (also any `[data-dismissible]` container). Toasts in the page HTML (after a POST → 303 → GET) are announced once through a live region. An identical toast already showing is not added again |
| swapped `<tr>` / `[data-flash]` | a row replaced by an HTMX swap (`hx-swap="outerHTML"` on a `<tr>`), and any swapped element marked `data-flash`, glows briefly (`.flash-in`, 1.6 s; kept under reduced motion since it is a colour fade) |
| `.table-wrap` | gets `.is-fit` or `.is-scrollable` (+ `.is-scrolled` / `.is-scrolled-end`), see section 6 |
| any HTMX request | `aria-busy="true"` on its target (dimmed to 60%) and a 2px progress bar at the top of the window; polling (`hx-trigger="every …"`) and elements with `data-quiet` stay silent |
| HTMX 4xx/5xx (except 422) | not swapped; an error toast explains (403, 404, 409, 5xx); network failure → "Can't reach the app" toast. Never for polling or `data-quiet` requests |
| HTMX 422 | **swapped**: return the re-rendered form with inline errors and status 422 |
| `details.mobile-nav` | the mobile menu closes on Escape, on a click outside and when a link is followed |

htmx config (set in app.js): `allowEval=false`, `includeIndicatorStyles=false`,
`historyCacheSize=0` (page HTML is never copied into browser storage) with
`refreshOnHistoryMiss=true` (Back/Forward after `hx-push-url`/`hx-replace-url` does a normal full
page load, so routes must render the full page for non-HTMX GETs), `selfRequestsOnly=true`, and
the response handling above. `allowScriptTags` stays on because htmx would otherwise strip the
JSON chart configs from swapped HTML; inline JS in a swap is still blocked by the CSP; never put
`<script src>` in partials.

`window.App` for page scripts: `App.toast(level, text)` (same levels as flash),
`App.renderCharts(force)`, `App.formatMoney(number)` → `"$1,234.56"`.

HTMX patterns used across pages:

- Filters: `filter_bar(..., target="#results")`; the route returns `_results.html` when
  `HX-Request` is present, the full page otherwise.
- Row edit: control with `hx_post`, `hx_target="closest tr"`, `hx_swap="outerHTML"`; respond with
  the re-rendered `<tr>` plus `ui.flash_oob("success", "Saved")`. The new row glows briefly.
- Dialog forms: `hx_post` on the `<form>`, success → updated fragment + `HX-Trigger: dialog:close`;
  validation errors → the form again with status 422.

---

## 9. Layout and utility classes

`.content` (page column, 24px rhythm), `.section` / `.section__header` / `.section__title` /
`.section__subtitle`, `.grid` (auto-fill, `--grid-min` 220px, `--grid-gap`), `.grid--2` / `--3` /
`--4`, `.grid--sidebar` (2:1), `.span-2`, `.span-all`, `.stack` (`--stack-gap`; `--xs` 4 / `--sm` 8 /
`--lg` 24 / `--xl` 40), `.cluster` (wrapping row, `--cluster-gap`, `--lg`), `.split`
(space-between row), `.form`, `.form-row` (responsive field columns), `.form-actions`, `.muted`,
`.text-secondary`, `.text-ok` / `-warn` / `-over` / `-info`, `.text-xs` / `-sm` / `-lg`, `.strong`,
`.num` (right-aligned tabular), `.tabular`, `.nowrap`, `.truncate`, `.sr-only`, `.mt-0` / `-2` /
`-4` / `-6`, `.no-print`, `.print-only`, `.legend` + `.swatch` (`--swatch`).

Breakpoints:

| Width | Changes |
| --- | --- |
| < 1360px | `.col-optional-lg` hidden |
| < 1280px | `.col-grow` minimum 8rem |
| < 1200px | `.grid--sidebar` → 1 column (its side column would drop below ~250px) |
| < 1100px | `.grid--4` → 2 columns; `.col-optional-md` hidden |
| < 900px | sidebar → mobile bar with a `<details>` menu; gutters 16px; top bar not sticky, no page title in it; `.grid--3` → 2 columns; tables `min-width: 560px` (scroll in their wrap) |
| < 600px | all grids 1 column; page-header actions full width; card padding 16px; filter-bar fields full width; pagination shows Prev/Next only; `--truncate-w` 180px; segmented controls scroll in one row; full-width toasts; `.kv` stacks; Sync now icon-only; `.col-optional-sm` hidden |

Motion respects `prefers-reduced-motion`; `prefers-contrast: more` strengthens borders and muted
text; `forced-colors` keeps meters and dots visible; print hides the sidebar, top bar, notices,
toasts, buttons, filters, pagination and tabs (the statements page prints as a clean document; add
`.no-print` / `.print-only` as needed).

---

## 10. CSS vocabulary (`static/css/app.css`)

### Tokens (CSS custom properties; light by default, dark under `prefers-color-scheme: dark`)

| Group | Tokens |
| --- | --- |
| Surfaces | `--bg` (page), `--surface` (cards), `--surface-raised` (dialogs, toasts), `--surface-sunken` (wells, table footers), `--surface-hover`, `--sidebar-bg`, `--border`, `--border-strong`, `--control-bg`, `--control-border`, `--control-border-hover`, `--track` (meter track), `--switch-off`, `--skeleton`, `--skeleton-hi` |
| Text | `--text`, `--text-secondary`, `--text-muted`, `--text-faint` (placeholders); all ≥ 4.5:1 on every surface in both themes |
| Accent | `--accent` (fills, primary buttons), `--accent-hover`, `--accent-text` (links, selected nav), `--accent-soft` (selected backgrounds), `--accent-contrast` (text on accent), `--focus` (focus outlines and focused control borders), `--focus-ring` (box-shadow, derived from `--focus`), `--danger`, `--danger-hover`, `--danger-contrast` |
| Status | for each of `ok`, `warn`, `over`, `info`, `sandbox`, `demo`: `--<s>` (fill: bars, dots), `--<s>-text`, `--<s>-soft` (background), `--<s>-border`; plus `--over-deep` (overspend hatch) |
| Charts | `--cat-1` … `--cat-16` (fixed order; validated for colour-blind separation on adjacent pairs; slots 1–8 strongest; each has a light and a dark value), `--chart-grid`, `--chart-axis`, `--chart-other`, `--chart-accent`, `--tooltip-bg`, `--tooltip-text` |
| Type | `--font-sans` (system stack), `--font-mono`, `--fs-2xs` 11 · `--fs-xs` 12 · `--fs-sm` 13 · `--fs-md` 14 (body) · `--fs-lg` 16 · `--fs-xl` 20 · `--fs-2xl` 26 · `--fs-3xl` 32, `--lh-tight`, `--lh-normal`, `--fw-medium` 500, `--fw-semibold` 600, `--fw-bold` 650 |
| Space | `--sp-1` 4 · `--sp-2` 8 · `--sp-3` 12 · `--sp-4` 16 · `--sp-5` 20 · `--sp-6` 24 · `--sp-8` 32 · `--sp-10` 40 · `--sp-12` 48 · `--sp-16` 64 |
| Radii | `--radius-xs` 4 · `--radius-sm` 6 · `--radius-md` 8 (controls) · `--radius-lg` 12 (cards) · `--radius-xl` 16 (dialogs) · `--radius-pill` |
| Shadows | `--shadow-xs`, `--shadow-sm` (cards), `--shadow-md` (popovers, tooltips), `--shadow-lg` (dialogs, toasts), `--dot-ring` |
| Layers | `--z-sticky` 10, `--z-topbar` 20, `--z-sidebar` 30, `--z-dropdown` 40, `--z-overlay` 50, `--z-dialog` 60, `--z-toast` 70, `--z-tooltip` 80 |
| Layout | `--sidebar-w` 248px, `--topbar-h`, `--envbar-h`, `--mobilebar-h`, `--content-max` 1200px, `--gutter` (32px, 16px below 900px), `--sticky-top` (offset for sticky table headers), `--scroll-fade` (table edge fade) |
| Motion | `--ease`, `--dur-fast` 120ms, `--dur` 200ms |

### Components

| Component | Classes |
| --- | --- |
| Shell | `.shell` (`.has-envbar`), `.sidebar`, `__brand`, `__footer`, `.brand`, `__mark`, `__text`, `__env` (`--sandbox` / `--demo` / `--production`), `.nav`, `__heading`, `__list`, `__link` (`aria-current`), `.mobile-bar`, `.mobile-nav`, `__panel`, `__label`, `.main-col`, `.masthead`, `.env-bar` (purple by default, used for sandbox and test; `--demo` orange; base also emits `env-bar--<env>` as a hook), `__text`, `__detail`, `.topbar`, `__inner`, `__title`, `__sep`, `__actions`, `.notices`, `.content`, `.skip-link`, `.page-progress`, `.env-badge` (`--sandbox` / `--demo` / `--production`) |
| Page header | `.page-header`, `__text`, `__eyebrow`, `__title`, `__subtitle`, `__actions` |
| Card | `.card`, `--flush`, `__header`, `__heading`, `__title`, `__subtitle`, `__actions`, `__body`, `__footer`; `a.card` hover |
| Stat tile | `.stat`, `--ok` / `--warn` / `--over` / `--info`, `__label`, `__value`, `__sub`, `__extra` |
| Amount | `.amount`, `--credit`, `--pending`, `--neutral`, `--ok` / `--warn` / `--over` / `--muted` / `--none` |
| Category | `.cat`, `--chip`, `--hidden`, `--none`, `__dot` (`--cat-color`), `__name` |
| Progress | `.progress`, `--ok` (the default look) / `--warn` / `--over` / `--none`, `--sm` / `--lg`, `__head`, `__label`, `__pct`, `__track`, `__fill`, `__overflow`, `__limit`, `__pace` (vars `--value`, `--limit`, `--pace`) |
| Table | `.table-wrap` (`--scroll`, `.is-fit`, `.is-scrollable`, `.is-scrolled`, `.is-scrolled-end`), `.table`, `--compact`, `--bordered`, `--numbers`; cells `.num`, `.center`, `.shrink`, `.truncate`, `.col-grow`, `.col-optional-lg` / `-md` / `-sm`, `.table__primary`, `.table__secondary`, `.table__date`, `.table__actions`; rows `tr.is-muted`, `tr.is-pending`, `tr.is-selected`, `.flash-in`; `.th-sort` with `aria-sort` (section 6) |
| Inline edit | `.inline-control` (select/input), `.inline-money` + `__prefix`, `.inline-cat` |
| Key/value | `.kv-wrap` (size container), `.kv` (`<dl>`) |
| Pagination | `.pagination`, `__info`, `__nav`, `__pages`, `__link` (`aria-current`, `aria-disabled`), `__gap` |
| Empty state | `.empty`, `--compact`, `__icon`, `__title`, `__message`, `__actions` |
| Banner | `.banner`, `--warn` / `--over` (alias `--danger`) / `--ok` / `--sandbox` (info is the default), `__icon`, `__body`, `__title`, `__text`, `__actions` |
| Toast | `.toast-stack` (`#flash`; `--static` for inline demos), `.toast`, `--ok` / `--warn` / `--over` (info default), `__icon`, `__text`, `__close`, `.is-leaving` |
| Status | `.sync-status` (the partial), `.sync-form`, `.sync-now__label`, `.pill`, `--healthy` / `--syncing` / `--needs_reconnect` / `--error` / `--stale` / `--not_linked`, `__dot`, `__text` |
| Buttons | `.btn` (= secondary), `--primary`, `--ghost`, `--danger`, `--sm`, `--icon`, `--block`; `.icon--spin` spins during `.htmx-request` or `.is-busy`; `.btn-group`; `disabled` / `aria-disabled="true"` |
| Forms | `.field`, `--compact`, `--grow` (in a filter bar), `__label`, `__req`, `__optional`, `__help`, `__error`; `.input`, `.select`, `.textarea`; `.input--sm`, `.select--sm`; `[aria-invalid="true"]`; `.input-affix` + `--prefix`, `__prefix`, `__suffix`; `.input--money`; `.search`; `.check`, `__text`, `__help`; `.switch`; `.chip`, `--removable`, `__remove`, `.chips`; `.tags-input`, `__entry`; `.filter-bar`, `__actions`; `.segmented`, `__option`, `.segmented-field` |
| Tabs | `.tabs`, `__tab` (`aria-current`), `__count` |
| Dialog | `.dialog`, `--sm` / `--lg`, `__header`, `__title`, `__body`, `__footer`, `__footer--split` |
| Tooltip | `data-tooltip="Text"` on any element (+ `aria-label` when it has no visible text), `data-tooltip-pos="bottom" \| "left" \| "right" \| "bottom-end"`; `.tip` icon. Tooltips use `::after`, so not on elements that already use it, and they are clipped by `overflow: hidden` ancestors. They show on keyboard focus always, and on hover only with a real pointer (touch screens would leave them stuck after a tap) |
| Badge | `.badge`, `--accent` / `--ok` / `--warn` / `--over` / `--info` / `--sandbox` / `--muted` |
| Loading | `.htmx-indicator` (hidden until the element or an ancestor has `.htmx-request`), `.spinner` (`--lg`), `.loading`, `.skeleton` (`--title`, `--block`), `.skeleton-group`, `[aria-busy="true"]` dims, `html.is-loading .page-progress` |
| Chart | `.chart` (`--chart-h`), `.legend`, `.swatch` (`--swatch`) |
| Error pages | `.error-command`, `.error-status` |
| Icons | `.icon`, `--xs` / `--sm` / `--lg` / `--xl`, `.icon-sprite` |

---

## 11. Icons (`components/icons.html`)

`{{ ui.icon("name") }}` or `<svg class="icon" aria-hidden="true"><use href="#i-name"/></svg>`.
24px stroke icons that take the current text colour:

home, list, target, trending, repeat, receipt, tag, wand, upload, download, link, settings,
refresh, search, plus, minus, x, check, alert, info, check-circle, x-circle, chevron-down,
chevron-up, chevron-left, chevron-right, arrow-up, arrow-down, sort, split, merge, pencil, trash,
eye, eye-off, calendar, bell, clock, lock, credit-card, filter, external, menu, more.

Nav icons: Dashboard home · Transactions list · Budgets target · Trends trending · Recurring
repeat · Statements receipt · Categories tag · Rules wand · Import upload · Connection link ·
Settings settings.

---

## 12. Accessibility checklist for pages

- One `<h1>` per page (`ui.page_header`), card/section titles are `<h2>`.
- Every control has a label (the form macros do it; inline controls require `label=`).
- Icon-only buttons: `ui.button(..., icon_only=true)` (sets `aria-label` and a tooltip).
- Status is never colour-only: badges, pills, meters and amounts carry text or icons.
- Give each budget meter its own `name` ("Groceries budget").
- Tables: `<th scope="col">`, numbers in `.num`, actions column `<th class="shrink"><span
  class="sr-only">Actions</span></th>`; Amount is never an optional column.
- Charts get a `label`, and the same numbers appear in text or a table nearby (or in the chart's
  call body).
- Keep focus visible: never remove outlines; `:focus-visible` and focused fields use `--focus`.
