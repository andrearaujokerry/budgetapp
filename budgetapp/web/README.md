# Web layer: how to build a page

The one page to read before building a page. The design system (layout, macros, CSS, JS
behaviours) is in [`templates/components/README.md`](templates/components/README.md); see every
component live at `/dev/ui-kit`. ARCHITECTURE §9 has the security design.

| File | What it gives you |
| --- | --- |
| `templating.py` | `render()`, `is_htmx()`, `error_page()`, the filters, `NAV_ITEMS` |
| `deps.py` | `SessionDep`, `SessionFactoryDep`, `GatewayDep`, `NotifierDep`, `SettingsDep`, `SecretStoreDep`, `FormDep` |
| `flash.py` | `redirect()`, `flash()`, `flash_oob()`, `is_local_path()` |
| `security.py` | the Host/Origin checks, security headers, CSP, `allow_plaid_link()` |
| `app.py` | `create_app()`: wiring, error pages (never import it from a route: circular) |
| `routes/<page>.py` | one module per page, each with `router = APIRouter()` (the Connection placeholder also answers `POST /connection/sync` and `GET /connection/sync-status` until SYNC replaces it) |
| `templates/<page>/` | `index.html` (full page) and `_<part>.html` partials |

Checklist for a page package:

- [ ] Replace `routes/<page>.py` and `templates/<page>/index.html` wholesale; keep the name
      `router` and the page's GET route at its nav `href`.
- [ ] Routes are `def` (not `async def`), thin, and call services; services never commit.
- [ ] Every response goes through `render()` (or `redirect()` after a change).
- [ ] Mutations are `POST`; the route calls `session.commit()` after a successful change.
- [ ] The page renders with an empty database (nothing linked, no transactions).
- [ ] No inline `<script>`/`<style>`, `on*=`, `hx-on`; page JS/CSS in your own static files.
- [ ] Tests in `tests/web/test_<page>*.py` with the `client` fixture; check your rendered pages
      with `csp_problems(response.text, rendered=True) == []` (`tests/web/csp.py`).

---

## 1. A route module

```python
"""Rules page (CAT-3, CAT-4)."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from budgetapp.services import rules  # illustrative service calls below
from budgetapp.web.deps import FormDep, SessionDep
from budgetapp.web.flash import flash_oob, redirect
from budgetapp.web.templating import is_htmx, render

router = APIRouter()


@router.get("/rules")
def rules_page(request: Request, session: SessionDep, q: str = "") -> Response:
    context = {"rules": rules.list_rules(session, q=q), "q": q}
    if is_htmx(request):  # the filter bar asked for the results only
        return render(request, "rules/_results.html", context, active_nav="rules")
    return render(request, "rules/index.html", context, active_nav="rules")


@router.post("/rules")
def create_rule(request: Request, session: SessionDep, form: FormDep) -> Response:
    try:
        rule = rules.create_rule(session, name=form.get("name", ""), text=form.get("text", ""))
    except rules.RuleValidationError as err:  # the form again, with messages
        context = {"form": form, "errors": err.errors}
        return render(request, "rules/_form.html", context, active_nav="rules", status_code=422)
    session.commit()
    return redirect(request, "/rules", "success", f"Rule “{rule.name}” saved")


@router.post("/rules/{rule_id}/delete")
def delete_rule(request: Request, rule_id: int, session: SessionDep) -> Response:
    if not rules.delete_rule(session, rule_id):
        raise HTTPException(404)
    session.commit()
    if is_htmx(request):  # swap the list, toast out of band
        context = {"rules": rules.list_rules(session)}
        return render(
            request,
            "rules/_list.html",
            context,
            active_nav="rules",
            oob=flash_oob("success", "Rule deleted"),
        )
    return redirect(request, "/rules", "success", "Rule deleted")
```

- **`def`, not `async def`.** Services and the database are synchronous; FastAPI runs `def`
  routes in a worker thread, so a slow request never blocks the others.
- **Thin routes.** Parse input, call services (`budgetapp/services/*`, which take the session
  first, never commit and return frozen dataclasses), commit, respond.
- **`active_nav`** is required on every `render()`: one of `dashboard`, `transactions`,
  `budgets`, `trends`, `recurring`, `statements`, `categories`, `rules`, `imports`, `connection`,
  `settings` (a detail page uses its parent's key), or `None`. A typo raises `ValueError`.
- **Routers.** `routes/__init__.py` lists `ALL_ROUTERS` in nav order; `create_app()` includes
  each with the schema guard. Keep all of a page's routes in its own module. A new module (not
  replacing a placeholder) must be added to `ALL_ROUTERS`, which is a shared-file change.

## 2. `render()` and the layout context

```python
render(request, template, context=None, *, active_nav, status_code=200, headers=None, oob=())
```

Renders `template` with your `context` plus the layout context below and returns an HTML
response (`Cache-Control: no-store`, `Vary: HX-Request`). Your keys win over the layout's.

| Key | Value |
| --- | --- |
| `request` | the Starlette request (`request.url`, `request.query_params` in templates) |
| `env` | `"sandbox"`, `"production"`, `"test"` (pytest) or `"demo"` (`serve --demo`): the env bar |
| `nav_items`, `active_nav` | `templating.NAV_ITEMS` and your key: the sidebar and `aria-current` |
| `conn` | `services.status.connection_status(session)`, in a short session of its own (`not_linked` when nothing is linked). Pass `conn=...` to reuse one you computed, or `conn=None` for none |
| `messages` | flash messages waiting in the cookie (full pages only), then any `messages` you pass |
| `app_version` | `budgetapp.__version__` |
| `csp_nonce` | this request's CSP nonce (only the Plaid Link script tag uses it) |

`oob`: out-of-band snippets appended after the template, e.g. `flash_oob("success", "Saved")`
(plain strings are escaped). `headers`: extra response headers, e.g.
`{"HX-Trigger": "dialog:close"}`.

`error_page(request, status, title, message=None, *, command=None, icon="alert", refresh=False)`
renders the generic notice page (`errors/error.html`: the title is the page's only `<h1>`) for
the rare case a route wants one; usually raise `HTTPException` instead (section 7).

The layout shows the reconnect and consent banners on every page except the Connection page
(`active_nav="connection"`), which must show those states and their actions itself.

**Filters** (`templating.py`; `{{ x|money }}`):

| Filter | Example |
| --- | --- |
| `money`, `money(signed=true)` | `$1,234.56`, `−$54.99`, `+$12.00`; `—` for `None` (`money.format_money`; never floats) |
| `money_signed` | `+$12.00` / `−$3.50` |
| `pct`, `pct(1)` | `85%`, `84.6%` (the value is already a percent; half-up) |
| `date_short`, `date_long`, `month_label` | `Oct 27`, `Oct 27, 2026`, `Oct 2026` (aware datetimes → the Mac's local date; ISO strings work) |
| `relative_time` | `just now`, `5 min ago`, `3 h ago`, `2 days ago`, then a date |
| `pluralize("charge")`, `pluralize("category", "categories")` | `1 charge`, `1,234 charges` |
| `tojson` | accepts `Decimal` (becomes a numeric string; the chart helper reads it) |

In amounts shown as amounts use the macro `ui.money(...)` (same text, plus tabular figures and
the credit colour); see the design-system README §3.2.

## 3. Full page or HTMX partial

- A normal GET (a link, a reload, Back/Forward) gets the **full page**: `<page>/index.html`,
  which `{% extends "base.html" %}`. htmx never restores pages from its cache here
  (`historyCacheSize=0`), so every URL a filter writes to the address bar must render fully.
- An HTMX request (`is_htmx(request)`: `HX-Request: true`) gets the **fragment** it targets:
  `<page>/_<part>.html`, which does not extend base. Include the same fragment from the full page
  so both paths render identical markup:
  `<div id="results">{% include "transactions/_results.html" %}</div>`.
- Filters: `form.filter_bar(..., target="#results")` (GET; the address bar follows with
  `hx-replace-url`). Pagination, sort headers and tabs push history.
- What app.js does with the status of an HTMX response: 2xx is swapped; **204** swaps nothing;
  **422** is swapped (return the form with its errors); other 4xx/5xx are not swapped and show an
  error toast (403, 404, 409 and 5xx have their own wording; a 404 for a POST says "That action
  isn't available", for a GET "That item no longer exists"). Polling (`hx-trigger="every …"`) and
  `data-quiet` requests never toast.
- Row edits: `hx_post=…, hx_target="closest tr", hx_swap="outerHTML"`; answer with the new
  `<tr>` (it glows briefly) plus `oob=flash_oob("success", "Saved")`.
- Dialog forms: success → the updated fragment with `headers={"HX-Trigger": "dialog:close"}`;
  errors → the form again with status 422.
- Never put `<script src>` in a partial (scripts belong to the full page). JSON data islands
  (`<script type="application/json">`) are fine in partials.

## 4. After a change: commit, then redirect or swap

```python
session.commit()  # routes own the transaction
return redirect(request, "/rules", "success", "Rule saved")  # leave the page
return render(
    request, "rules/_row.html", ctx, active_nav="rules", oob=flash_oob("success", "Saved")
)  # HTMX: swap in place
```

- `redirect(request, url, level=None, text=None)`: a plain form post gets **303 See Other**; an
  HTMX request gets **`HX-Redirect`** (htmx then loads `url` as a full page). With `level` and
  `text` the next full page shows that toast. `url` must be a path on this app (`/rules?page=2`);
  anything else raises `ValueError`.
- `flash(response, level, text)` adds a toast to any other response (several calls keep every
  message, in order). Messages travel in a short-lived signed cookie (`HttpOnly`,
  `SameSite=Strict`, 2 minutes) that the next full page shows and clears; HTMX partials leave it
  alone.
- `flash_oob(level, text)` returns the toast as an out-of-band swap for `#flash`; pass it as
  `oob=` to `render()`.
- Levels: `success`, `info`, `warning`, `error` (aliases `ok`, `warn`, `danger`, `over`; anything
  else raises). Errors stay until dismissed; the others fade after 6 s. Text is escaped.
- If the route raises after changing things, `SessionDep` rolls the whole request back. Alerts
  queued with `alerts.record_and_notify` are only sent after your `commit()`.
- **There is no `Referer`** (the app sends `Referrer-Policy: no-referrer`). To return to the page
  a form came from, post its path in a hidden field and check it:

  ```jinja
  <input type="hidden" name="next" value="{{ request.url.path }}{{ '?' ~ request.url.query if request.url.query }}">
  ```
  ```python
  target = form.get("next", "")
  return redirect(request, target if is_local_path(target) else "/transactions", "success", "Saved")
  ```

## 5. Forms and validation errors

- Build forms with the macros in `components/forms.html` (labels, `aria-describedby`, error
  text). `error=` on a field turns it red and shows the message.
- Read user-facing forms with **`form: FormDep`**: the posted form (urlencoded or multipart),
  parsed before your `def` route runs, every value as text (uploads as `UploadFile`, closed after
  the request). `form.get("name", "")`, `form.getlist("tags")` (the tags field always sends one
  empty value: `[t for t in form.getlist("tags") if t.strip()]`); unchecked checkboxes send
  nothing.
- Validate in the service (or the route), never trust the browser: money with
  `money.parse_money_input(text)` (its `ValueError` message is written for the user), dates with
  `date.fromisoformat`, ids by loading the row (`HTTPException(404)` if it is gone).
- Invalid input → render the **form again with the messages and status 422** (for a plain post
  the whole page, for HTMX the form fragment). Keep what the user typed: pass the raw strings
  back as field values.
- Typed parameters (`q: str = ""`, `page: int = 1`, `Form()` fields) are fine for values the
  app generates itself (filters, ids in paths). A malformed one gives a generic **400 page**
  (not FastAPI's JSON 422), so never use them for what the user types.
- JSON endpoints for page scripts (`fetch` from `static/js/<page>.js`): return `JSONResponse`;
  money as strings, never floats.

## 6. Security rules for routes

What the middleware enforces on every request (`security.py`, ARCHITECTURE §9):

- **Host** must be `127.0.0.1` or `localhost` (any port), else **400** (blocks DNS rebinding).
  The server only ever listens on 127.0.0.1; `serve` has no host option.
- **Requests other sites start** (Fetch Metadata, every method): when the browser says
  `Sec-Fetch-Site: cross-site` or `same-site`, only a top-level `GET` navigation passes (a link or
  bookmark: `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`); images, scripts, frames and
  `fetch` from other sites get **403**. Another website therefore can't load your page in the
  background and time it (XS-Search), e.g. `/transactions?q=…`. The app's own requests say
  `same-origin` and typed URLs `none`; curl sends nothing and is not checked.
- **Unsafe methods** (`POST`, `PUT`, `PATCH`, `DELETE`) need the app's own `Origin`
  (`http://127.0.0.1:<port>` or `http://localhost:<port>`), else **403**. Without an `Origin`
  they pass unless `Sec-Fetch-Site` says something other than `same-origin`/`none` (so curl
  works). `Origin: null` passes only with `Sec-Fetch-Site: same-origin`/`none`: because of
  `no-referrer`, browsers send `Origin: null` on the app's own plain `<form method="post">`
  submissions (checked in Chrome); htmx and `fetch` send the real origin.
- That is the CSRF protection: no tokens to add. Your part: **anything that changes data is a
  POST** (never a GET link), and GET routes change nothing.
- Every response gets the CSP below, `Referrer-Policy: no-referrer`, `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Permissions-Policy`, `Cross-Origin-Resource-Policy:
  same-origin`, and `Cache-Control: no-store` (pages, partials, redirects, exports; `/static` is
  `no-cache`). No Cross-Origin-Opener-Policy (Plaid's OAuth pop-up needs `window.opener`).
- No CORS, no login (single user, loopback only), no OpenAPI pages.
- Secrets (Plaid keys, access tokens, passwords) never go into templates, context, flash text,
  logs or exception messages. Error pages never show exception text.

## 7. Errors: what to raise, what the user sees

| In a route | The user sees |
| --- | --- |
| `raise HTTPException(404)` | the "Page not found" page (HTMX: an error toast) |
| `raise HTTPException(409, "That budget changed. Reload and try again.")` | the notice page with your sentence (any 4xx; a string `detail` is shown, so write it for the user) |
| `db.SchemaMismatch` (the router-level schema guard) | 503 "Upgrade needed" with the command: `./run live` (production), `./run migrate` (Sandbox), `./run demo create NAME` (demo) |
| `db.DatabaseUnavailable`, SQLAlchemy `OperationalError` | 503 "The database isn't responding" (`./run db start`); nothing was changed |
| `secrets.KeychainApprovalNeeded` | 503 "Keychain approval needed" (`./run doctor --env production` in production) |
| `secrets.MissingSecretError` (e.g. no Plaid keys yet) | 503 "Setup isn't finished" (`./run setup --keys-only` for Plaid keys) |
| `plaid_gateway.PlaidGatewayError` you did not handle | 502 page with Plaid's `display_message`; HTMX: an error toast, nothing swapped |
| anything else | 500 "Something went wrong" (details only in the log, redacted) |

The 503 pages send `HX-Refresh` to HTMX requests so the browser shows the fix. Handle a Plaid
error yourself when you can do better (store the error on the Item, show the Connection page's
own message). The process keeps running after any error, so the unattended production web agent
recovers once the cause is fixed.

## 8. CSP, page CSS and JS

Every page gets:

```
default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline';
img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
```

- No inline `<script>` (JSON islands `<script type="application/json">` are fine), no `<style>`
  elements, no `on*=` attributes, no `hx-on`, no `javascript:` URLs, no external URLs. `style=""`
  only to pass values into CSS variables (`--value`, `--cat-color`, …).
  `tests/web/test_security.py::test_templates_follow_the_csp` scans every template, yours too;
  the macros raise on `onclick=…`/`hx_on__…=` keyword arguments and `javascript:` values; and
  `test_every_nav_page_renders_with_the_layout` scans each nav page as rendered. Scan your other
  pages and partials in your tests: `from tests.web.csp import csp_problems`, then
  `assert csp_problems(response.text, rendered=True) == []`.
- Page CSS, only when a component is genuinely missing:
  `{% block head %}<link rel="stylesheet" href="/static/css/pages/<page>.css">{% endblock %}`.
- Page JS: `{% block scripts %}<script src="/static/js/<page>.js" defer></script>{% endblock %}`.
  Pass data with `data-*` attributes or a JSON island; use `window.App` (`App.toast`,
  `App.renderCharts`, `App.formatMoney`). Charts: `ui.chart(...)` and `{% block charts %}yes{% endblock %}`.
- **Plaid Link (Connection page only):** call `allow_plaid_link(request)` in the route before
  `render()`. That response then gets `script-src 'self' 'nonce-…'
  https://cdn.plaid.com/link/v2/stable/link-initialize.js`, `style-src`/`style-src-elem 'self'
  'nonce-…'`, `style-src-attr 'unsafe-inline'`, `frame-src https://cdn.plaid.com/` and
  `connect-src 'self' https://<sandbox|production>.plaid.com/` (the app's Plaid environment). In
  the template:

  ```jinja
  {% block scripts %}
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js" nonce="{{ csp_nonce }}" defer></script>
  <script src="/static/js/link.js" defer></script>
  {% endblock %}
  ```

  Only that one page's document carries the Plaid policy; HTMX partials fetched later don't need
  it (a response's CSP applies to documents, not to swapped fragments).

## 9. Session, gateway, notifier and friends

Never read `app.state` in a route; ask for a dependency (`budgetapp/web/deps.py`):

| Alias | Gives you |
| --- | --- |
| `session: SessionDep` | one `Session` per request; rolled back if the route raises, always closed; you `commit()` |
| `session_factory: SessionFactoryDep` | the `sessionmaker`, for work that needs several transactions or outlives the request (background sync, CSV import) |
| `gateway: GatewayDep` | the Plaid gateway (`plaid_gateway.PlaidGateway`), built on first use: the real one, `FakePlaidGateway` in tests, `DemoPlaidGateway` in demos (every call raises `PlaidError` "Not available with demo data") |
| `notifier: NotifierDep` | the notifier for `alerts.record_and_notify` |
| `settings: SettingsDep` | `config.Settings` (`settings.env`, `settings.plaid_env`, `settings.is_production`) |
| `store: SecretStoreDep` | the secret store (Keychain; in-memory in tests and demos) |
| `form: FormDep` | the posted form (section 5) |

"Today" and "now" come from `budgetapp.clock`, never `date.today()`.

## 10. Tests

Put web tests in `tests/web/test_<page>*.py`; the fixtures live in `tests/web/conftest.py`:

| Fixture | What it is |
| --- | --- |
| `client` | a `TestClient` at `http://127.0.0.1:8766` that sends the app's own `Origin` (POSTs pass the checks) and follows redirects. The app runs on the test database (sharing the test's connection: it sees what `db` flushed, and its commits are rolled back after the test), `fake_plaid`, the recording `notifier` and the in-memory `secrets` |
| `raw_client` | the same app without default headers (security tests) |
| `app` | that `FastAPI` app (`app.state.…`) |
| `make_app(settings=None, **overrides)` | another app: other settings (`make_app(load_settings("sandbox"))` shows the Sandbox bar, still on the test database) or other wiring (`gateway_factory=…`) |
| `make_client(app, *, origin=True, **kwargs)` | a client for it, e.g. `raise_server_exceptions=False` to see the 500 page |
| `HTMX` | `{"HX-Request": "true"}` (import from `tests.web.conftest`) |

```python
from tests.factories import make_transaction
from tests.web.conftest import HTMX


def test_filter_returns_only_the_results(client, db):
    make_transaction(db, name="Uber")
    response = client.get("/transactions?q=uber", headers=HTMX)
    assert response.status_code == 200
    assert response.template.name == "transactions/_results.html"
    assert [t.name for t in response.context["page"].rows] == ["Uber"]


def test_saving_redirects_with_a_toast(client):
    response = client.post("/rules", data={"name": "Rides"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/rules"
    assert "Rule “Rides” saved" in client.get("/rules").text
```

- `response.template.name` and `response.context[...]` work for everything `render()` returns.
- Templates render with `StrictUndefined` under pytest: a misspelt variable fails the test.
- Freeze time with `freeze_today("2026-10-15")` or `freeze_today(now=aware_datetime)`.
- `tests/web/test_shell.py::test_every_nav_page_renders_with_the_layout` requests every nav page
  on an empty database: your page must keep rendering there, with no CSP problem in its HTML.
- `tests/web/csp.py`: `csp_problems(text, rendered=False)`, the CSP scanner (template source, or
  `rendered=True` for a response body).
- Never start a server, the cluster or a browser in tests.

## 11. Running and viewing pages

- `./run` (`start`): Sandbox at http://127.0.0.1:8766: starts the cluster if needed, migrates
  the Sandbox database, warns when the Plaid keys aren't saved, opens the browser.
  `./run start --no-browser` skips the browser.
- `./run serve`: only the server (no migration). `-v` shows requests in the terminal; the log
  is `~/Library/Logs/budgetapp/web-sandbox.log`. Ctrl-C stops it.
- `./run live` (`start --env production`): production, from the installed copy only (the OPS
  package builds it). `serve --env production` refuses to run from the git checkout.
- **Realistic data:** `./run demo create <id>` then `./run serve --demo <id> --port <port>`
  (default 8800; your package's port is in PLAN.md). It serves the demo database
  `budgetapp_test_demo_<id>` with the orange "Demo data" bar, no Keychain and no network: Plaid
  calls raise "Not available with demo data". See `budgetapp/devtools/README.md`.
- Screenshots: headless Chrome won't lay out narrower than 500px with `--window-size`, so a 390
  capture from `shot.sh` is the 500px layout cropped; emulate the device viewport (DevTools
  `Emulation.setDeviceMetricsOverride`) for true phone widths.
