# Architecture

This is the technical design for the Amex Budget Tracker described in
`Amex Budget Tracker — Requirements & Workplan.md`. Requirement IDs (SYNC-1, REL-3, …)
refer to that document. Anything here that conflicts with the requirements is a bug in
this document. Facts about Plaid, Amex and macOS were verified against primary sources in
October 2026; where a behaviour is undocumented, the design says how it is checked. The
design was then reviewed adversarially for data integrity, money math and security.

## 1. Principles

1. **Your data never leaves the Mac** except to Plaid (and to Anthropic only if AI-1 is
   ever turned on). No CDNs except Plaid's own Link script, no fonts, no analytics.
2. **Exact money.** Amounts are `Decimal` in Python and `NUMERIC(14,2)` in PostgreSQL.
   Plaid JSON is parsed with `json.loads(..., parse_float=Decimal)`; plaid-python's typed
   models (which use `float`) are never used for responses.
3. **Sync is idempotent and atomic.** Re-running a sync never duplicates anything; a failed
   sync leaves the database exactly as it was (REL-1).
4. **One Plaid link, ever.** Each real link permanently uses one of the Trial plan's 10
   connections, and a second Amex link can also break the first. The app never offers a new
   production link while any trace of an existing one exists (section 7).
5. **Services are UI-agnostic.** Business logic lives in `budgetapp/services/` and takes a
   SQLAlchemy `Session`; it never imports FastAPI or Jinja. Web routes are thin. This is
   what lets a future mobile app reuse the backend through a JSON API.
6. **Sandbox by default.** Every command defaults to the Sandbox environment; real data is
   touched only with an explicit `--env production` (or `./run live`) (SEC-5).
7. **One definition of each number.** Spending for a period, a statement's totals and a
   budget's status each come from exactly one function that every page and alert calls.

## 2. Environments, paths and ports

| | sandbox (default) | production | test |
| --- | --- | --- | --- |
| Selected by | default, or `BUDGETAPP_ENV=sandbox` | `--env production`, `./run live`, launchd agents | pytest (`BUDGETAPP_ENV=test`) |
| Code that runs | the repository | the Application Support install only | the repository |
| Plaid | Sandbox | Production | `FakePlaidGateway` only |
| Database | `budgetapp_sandbox` | `budgetapp` | `budgetapp_test_<pid>` (created and dropped per pytest process) |
| DB role | `budgetapp_dev` (password in Keychain) | `budgetapp_live` (password in Keychain) | `budgetapp_test` (non-superuser, peer-mapped) |
| Web | http://127.0.0.1:8766 | http://127.0.0.1:8765 | TestClient |

Paths — never inside the git checkout, never under `~/Desktop`, `~/Documents`,
`~/Downloads`, `~/Library/Mobile Documents` or `~/Library/CloudStorage`:

- `~/Library/Application Support/budgetapp/postgres/` — the dedicated PostgreSQL 18 cluster
  (excluded from Time Machine; a copy of a live data directory is not a backup anyway)
- `~/Library/Application Support/budgetapp/run/` — the cluster's private Unix socket directory
- `~/Library/Application Support/budgetapp/venv/` — the production install of the app
- `~/Library/Application Support/budgetapp/backups/<env>/` — nightly `pg_dump` files (REL-2)
- `~/Library/Application Support/budgetapp/state/` — small marker files (alert de-duplication
  when the database or Keychain is unavailable, install stamp, recovery files)
- `~/Library/Logs/budgetapp/` — app, sync, backup and postgres logs (never contain secrets)

All are created with mode `0700`; files written there are `0600`.

### One production code copy
macOS privacy protection (TCC) blocks launchd-started processes from `~/Desktop`, where the
repository lives. So production **always** runs the Application Support install:
- `upgrade_install()` is the only way production code changes: refuse if the repository has
  uncommitted changes → stop the web, sync and backup agents → take a backup →
  `pip install -r requirements.txt` then `pip install --no-deps <repo>` (never editable) →
  run migrations from the new install while holding the sync lock → write the install stamp
  (git commit) → restart the agents.
- `./run live` calls `upgrade_install()` when the repository's commit differs from the stamp,
  then opens the web agent (or serves from the install when no web agent is installed).
- A **schema guard** in `db.py` compares the database's Alembic revision with the code's
  head at every entry point. In production nothing migrates automatically: the web app shows
  an "upgrade needed" page and sync exits quietly with one de-duplicated notice. Sandbox
  `./run` migrates the sandbox database automatically. A database that cannot be reached is
  not a schema problem: the guard raises `DatabaseUnavailable` ("try later" / an error page),
  never "upgrade needed".
- `./run setup` creates the production schema only while that database is still empty (the
  first setup); it never upgrades it afterwards, and `migrate --env production` refuses to run
  from a git checkout. Only `upgrade_install()` upgrades production.
- Once a migration has been applied to production it is never edited.

### Dedicated PostgreSQL cluster
The Mac already runs two shared development clusters (PG14 on 5432, PG18 on 5433) that
other projects use with `trust` authentication. Financial data gets its own cluster:

- PostgreSQL 18 binaries from `/opt/homebrew/opt/postgresql@18/bin` (configurable).
- `initdb -E UTF8 --locale-provider=builtin --builtin-locale=C.UTF-8 --locale=C.UTF-8
  --auth-local=peer --auth-host=scram-sha-256` (case-insensitive search works for accented
  merchant names; nothing to reindex after macOS upgrades).
- `port = 5434`, `listen_addresses = 'localhost'`, `unix_socket_directories =
  '<data root>/run'` (private, 0700), `unix_socket_permissions = 0700` (SEC-1).
- `pg_hba.conf`: `local all all peer map=localmap`; `host all all 127.0.0.1/32
  scram-sha-256`; `host all all ::1/128 scram-sha-256`. `pg_ident.conf` maps the macOS user to
  itself (the cluster superuser, used only by `setup` and `./run db` admin commands) and to
  `budgetapp_test`.
- Roles: `budgetapp_live` owns `budgetapp`; `budgetapp_dev` owns `budgetapp_sandbox`;
  `budgetapp_test` (LOGIN CREATEDB, not superuser) creates, owns and drops only
  `budgetapp_test_*` databases. `CONNECT` on `budgetapp` and `budgetapp_sandbox` is revoked
  from `PUBLIC`. Database roles are the boundary that actually separates test and sandbox code
  from real data (SEC-5).
- The app and backups connect as the environment's role over 127.0.0.1 with the password from
  Keychain and `require_auth=scram-sha-256`. Passwords are hashed client-side
  (`PGconn.encrypt_password`) before `ALTER ROLE`.
- The server needs `LC_ALL=C.UTF-8` in its environment. Under launchd it runs in the
  foreground via a `/bin/sh` wrapper that deletes a stale `postmaster.pid` only when its PID is
  not a postgres process whose `-D` argument is our data directory, then `exec postgres -D …`.
  Agents stop in the order web → sync → backup → postgres (or postgres is stopped first with
  `pg_ctl stop -m fast`).

## 3. Secrets (macOS Keychain)

All secrets are stored with `keyring` under service name **`budgetapp`**:

| Keychain account | Value |
| --- | --- |
| `plaid.client_id` | Plaid client ID (the same for every Plaid environment) |
| `plaid.secret.sandbox` / `plaid.secret.production` | Plaid secrets |
| `plaid.access_token.production` | THE production access token (v1 allows one production Item) |
| `plaid.link_in_progress.production` | marker written when a production new-link token is issued |
| `plaid.access_token.sandbox.<item_id>` + `plaid.item_ids.sandbox` | Sandbox tokens and their index |
| `db.password.budgetapp_dev` / `db.password.budgetapp_live` | Generated DB role passwords |

Rules:
- Secrets are read through `budgetapp.secrets` only; never written to the database, files,
  logs, exceptions, process arguments or templates. A logging filter redacts anything that
  looks like a Plaid token or a loaded secret. Tests use an in-memory store.
- Secrets are created only by the app's own interpreter (`./run setup`), never with the
  `security` CLI or Keychain Access, so the Keychain trusts the app's Python.
- Unattended processes (launchd agents, including the web agent) call
  `SecKeychainSetUserInteractionAllowed(false)` at start, so a Keychain that needs approval
  fails fast instead of hanging on an invisible dialog. That failure (`-25293`/`-25308`)
  raises `KeychainApprovalNeeded`: sync and backup notify once a day (de-duplicated with a
  marker file, since the database password may be the item that failed) with the exact fix —
  `./run doctor --env production` in Terminal — and exit 75; the web app stays up and shows
  a page with the same instruction. Run interactively, `doctor` reads every item of both
  environments so the user can click *Always Allow* (needed after each `brew upgrade` of
  Python).
- The production access token is written once and read back to verify; it is never
  rewritten. If the Keychain write fails after a successful exchange, the token goes to a
  0600 recovery file under `state/` and the app stops with instructions. `./run setup --env
  production --access-token` re-enters a saved token (via `getpass`), verified with
  `/item/get`. The README suggests keeping a copy in the user's password manager.

## 4. Repository layout

```
run                         one command starts everything (MNT-1)
pyproject.toml              hatchling build (installable package) + pytest/ruff config
requirements.txt            pinned runtime deps; requirements-dev.txt adds test tools
alembic.ini                 dev convenience; points at budgetapp:migrations
budgetapp/
  __main__.py, cli.py       `python -m budgetapp <command>`
  commands/                 one module per CLI command
  config.py                 Settings (env, paths, ports, DB names) — no secrets
  secrets.py                Keychain wrapper, unattended mode, markers, in-memory test store
  logsetup.py               logging setup with secret redaction
  clock.py                  today()/now() with a test override
  db.py                     engines/sessions, sync & ledger locks, schema guard, wait_for_db
  pgcluster.py              init/start/stop/status of the dedicated cluster, roles, DBs
  models.py                 all SQLAlchemy models
  migrations/               Alembic environment and versions (shipped inside the package) (MNT-3)
  money.py                  Decimal helpers and formatting
  periods.py                calendar months, statement cycles, years, days left
  plaid_gateway.py          PlaidGateway protocol, RealPlaidGateway, PlaidError
  services/
    userstate.py            the user-owned state of a transaction and how it moves
    categorize.py           categorization: payments → rules → Plaid/Amex mapping → Uncategorized
    spending.py             period_spending() and other aggregation over v_allocations
    status.py               connection status for the layout, dashboard and alerts
    events.py               SyncOutcome / ImportOutcome passed to alerts
    linking.py              Link tokens, token exchange, update mode, re-attach
    sync.py                 /transactions/sync engine, balances, liabilities, consent
    background.py           in-process job runner for the web app
    csv_import.py           Amex CSV import
    reconcile.py            CSV↔Plaid pairing, cutover, attaching a manual account
    categories.py           category CRUD, merge, hide, mapping edits
    rules.py                rule CRUD, preview, re-apply (CAT-3/4)
    transactions.py         search/filter, edit, tags, splits, CSV export
    budgets.py              budget amounts, rollover, annual budgets, overall target
    insights.py             dashboard and trends data
    recurring.py            recurring-charge detection
    statements.py           statement cycles and reconciliation
    alerts.py               alert evaluation with de-duplication
    notify.py               macOS notifications (osascript)
    backup.py               pg_dump with verification and rotation, restore
    agents.py               launchd agents, upgrade_install(), launch_production()
    doctor.py               SEC-1..6 and health checks
  web/
    app.py                  create_app(): middleware, routers, static, templates
    deps.py, security.py, templating.py, flash.py
    routes/                 one module per page
    templates/              base layout, components (macros), one folder per page
    static/                 css/app.css, js/app.js, page scripts, vendor/ (htmx, Chart.js)
tests/
  conftest.py               per-process test DB, rollback-per-test sessions, fakes
  fake_plaid.py             event-log FakePlaidGateway
  factories.py              model and Plaid-JSON builders
docs/ARCHITECTURE.md        this file
```

## 5. Data model

Conventions: `id BIGINT GENERATED ALWAYS AS IDENTITY`; `created_at`/`updated_at
TIMESTAMPTZ NOT NULL DEFAULT now()`; money `NUMERIC(14,2)`; enumerations are `TEXT` with a
`CHECK` constraint (no PostgreSQL enum types); constraint names follow a fixed SQLAlchemy
naming convention. Every foreign key states its `ON DELETE` behaviour.

**Sign convention (everywhere):** Plaid's, which Amex CSV files share. A positive amount is a
charge, fee or interest; a negative amount is a credit (refund, statement credit, payment).

### plaid_items
| column | type | notes |
| --- | --- | --- |
| plaid_item_id | text unique not null | |
| institution_id, institution_name | text | |
| status | text not null default 'healthy' | `healthy` \| `needs_reconnect` \| `error` |
| error_code, error_message | text | last error (Plaid or internal), cleared on success; `error_message` is the technical text (logs, doctor) |
| error_display_message | text | Plaid's `display_message` for the user, when sent; cleared on success |
| transactions_cursor | text | saved cursor; null = start from scratch (never store `''`) |
| transactions_update_status | text | last `transactions_update_status` |
| history_complete_at | timestamptz | first time `HISTORICAL_UPDATE_COMPLETE` was seen |
| days_requested | int not null default 730 | SYNC-3 |
| consented_products | text[] not null default '{}' | from `/item/get` |
| liabilities_status | text | `ok` \| `not_consented` \| `unsupported` \| `error` \| null (not tried) |
| linked_at, last_consented_at | timestamptz not null | `last_consented_at` also set after each update mode |
| consent_expires_at | timestamptz | `item.consent_expiration_time` |
| plaid_last_update_at, plaid_last_failed_update_at | timestamptz | from `/item/get` `status.transactions` |
| last_sync_attempt_at, last_sync_success_at | timestamptz | |
| item_snapshot | jsonb | last `/item/get` payload (no secrets) |

### accounts
| column | type | notes |
| --- | --- | --- |
| item_id | FK plaid_items ON DELETE RESTRICT, null | null = manual account (CSV-only, before linking) |
| plaid_account_id | text unique, null | updated in place when Plaid re-issues an account id (churn) |
| name, official_name, mask, type, subtype | text | |
| iso_currency_code | text default 'USD' | |
| current_balance, available_balance, credit_limit | numeric | SYNC-7 (limit is null on charge cards) |
| balances_as_of | timestamptz | |
| statement_balance, minimum_payment, last_payment_amount | numeric | Liabilities (SYNC-7), all nullable |
| statement_issue_date, payment_due_date, last_payment_date | date | |
| is_overdue | bool | |
| liabilities_as_of | timestamptz | |
| statement_closing_day | smallint | 1–31; used only to extend known closing dates (section 6) |
| plaid_history_start | date | earliest Plaid posted date, set once history is complete (CSV cutover) |
| notice | text | a message for the user (e.g. "Plaid re-issued this account; please confirm") |
| is_hidden | bool not null default false | hides the account in pickers; its money still counts |

`CHECK ((item_id IS NULL) = (plaid_account_id IS NULL))`: an account is manual or linked, never
half of each. The ORM relationship from `plaid_items` uses `passive_deletes="all"` so deleting an
Item that still has accounts is refused by the database (ON DELETE RESTRICT), not "fixed" by
nulling `item_id`.

### categories
| column | type | notes |
| --- | --- | --- |
| name | text not null | unique on `lower(name)` |
| kind | text not null default 'expense' | `expense` (spending) \| `income` (credits/rewards, not spending) \| `transfer` (card payments, not spending) |
| system_key | text unique, null | `uncategorized` (always `expense`), `card_payments` (always `transfer`) — cannot be merged away, hidden or change kind (CHECK) |
| is_hidden | bool not null default false | hidden from pickers and budget lists; transactions still count |
| color | text | palette slot `cat-1` … `cat-16` (theme-aware; the seeded categories use these), `other` (neutral grey), or a custom `#rrggbb` |
| icon | text | optional emoji |
| sort_order | int not null default 0 | |

Categories are never deleted except by merging (section 6), which re-points every reference first.

### category_mappings (CAT-2)
`(source, external_key) → category_id` (FK ON DELETE CASCADE), unique on
`(source, external_key)`. `source` is `plaid_pfc` (keys are Plaid **PFC v2** codes, detailed
or primary) or `amex_csv` (Amex CSV `Group-Subgroup` strings, or the `Group` prefix).
Lookup order: detailed/full key, then primary/group key, then Uncategorized. For a negative
amount the keys with a `|credit` suffix are tried first (unless the row reads like a fee
reversal): Amex files cash back and billing-inquiry credits under "Fees & Adjustments", so
`Fees & Adjustments|credit` and `Fees & Adjustments-Fees & Adjustments|credit` map to Credits &
Rewards while positive fees stay in Fees & Interest.

### rules (CAT-3)
| column | type | notes |
| --- | --- | --- |
| name | text | optional label |
| priority | int not null default 100 | lower runs first; ties by id |
| is_active | bool not null default true | |
| merchant_pattern | text | matched against `merchant_name` (falls back to `name` when null) |
| description_pattern | text | matched against `name` and `original_description` |
| match_type | text not null default 'contains' | `contains` \| `equals` \| `starts_with` \| `regex` (case-insensitive) |
| amount_min, amount_max | numeric | inclusive bounds on the signed amount |
| account_id | FK accounts ON DELETE CASCADE, null | optional |
| category_id | FK categories ON DELETE RESTRICT, not null | |
| is_system | bool not null default false | seeded rules |
| source_transaction_id | FK transactions ON DELETE SET NULL, null | one-click rule origin |

`CHECK` requires at least one condition. All set conditions must match (AND).

### transactions
| column | type | notes |
| --- | --- | --- |
| account_id | FK accounts ON DELETE RESTRICT, not null | |
| source | text not null | `plaid` \| `csv` |
| plaid_transaction_id | text unique, null | upsert key for Plaid rows |
| pending_transaction_id | text | Plaid id of the pending charge this posted one replaced |
| pending | bool not null default false | |
| date | date not null | Plaid `date` (posting date once posted); CSV `Date` |
| authorized_date | date | Plaid `authorized_date` |
| posted_date | date | statement membership: Plaid `date` when posted; CSV: posting date derived from the Reference when plausible, else `Date`; null while pending |
| name | text not null | Plaid `name`; CSV `Description` |
| merchant_name | text | |
| original_description | text | Plaid raw description; CSV `Appears On Your Statement As` |
| amount | numeric(14,2) not null | sign convention above |
| iso_currency_code | text | |
| pfc_primary, pfc_detailed, pfc_confidence, pfc_version | text | Plaid personal finance category |
| transaction_code | text | Plaid `transaction_code` when present (`payment`, `refund`, `interest`, `late fee`, `membership fee`, …) |
| merchant_category_code, merchant_entity_id | text | |
| external_category | text | Amex CSV `Category` |
| payment_channel, logo_url, website | text | display only |
| location | jsonb | display only |
| account_owner | text | Plaid `account_owner` or CSV `Card Member` |
| csv_reference | text | Amex `Reference` (apostrophes stripped). **Globally unique** where not null. Copied onto a Plaid row when a CSV row pairs with it |
| csv_import_id | FK csv_imports ON DELETE SET NULL, null | |
| duplicate_status | text not null default 'none' | `none` \| `suspect` (unpaired CSV row inside Plaid's history; excluded from totals) \| `kept` (user confirmed it is real) |
| category_id | FK categories ON DELETE RESTRICT, not null | effective category |
| category_source | text not null | `user` \| `system` \| `rule` \| `plaid` \| `amex` \| `default` |
| rule_id | FK rules ON DELETE SET NULL, null | rule that set the category |
| notes | text | |
| tags | text[] not null default '{}' | GIN index |
| user_edited_at | timestamptz | set when the user edits category, notes, tags or splits |
| needs_review | bool not null default false | with `review_reason`: e.g. splits rescaled after an amount change |
| review_reason | text | |

Constraints: `CHECK (source <> 'csv' OR csv_reference IS NOT NULL)`. Indexes: `(date)`,
`(posted_date)`, `(account_id, date)`, `(category_id, date)`, `(authorized_date)`,
`(merchant_name)`, GIN `(tags)`.

**Plaid-owned fields** are overwritten on every add/modify. **User-owned state** (see
`userstate.py`): the category when `category_source = 'user'`, `notes`, `tags`, splits.

### transaction_splits (TXN-4)
`transaction_id` (FK ON DELETE CASCADE), `category_id` (FK ON DELETE RESTRICT),
`amount numeric(14,2)`, `note`. A split transaction has ≥ 2 splits whose amounts sum exactly
to the transaction amount. Pending transactions cannot be split.

### v_allocations (view) — the single source of truth for money
One row per (transaction, category) allocation. Splits are used only when they sum exactly to
the transaction's amount; otherwise the whole amount goes to the transaction's category (and
the sync flags the row `needs_review`), so allocations always add up to `transactions.amount`.
Rows with `duplicate_status = 'suspect'` are excluded. The CTE names the columns it needs (not
`SELECT *`): PostgreSQL pins every column a view uses, so a migration that drops, renames or
retypes one of these columns must recreate the view; any other `transactions` column can change
freely.

```sql
WITH t AS (
    SELECT id, account_id, date, authorized_date, posted_date, pending, category_id, amount,
           pfc_primary, pfc_detailed, transaction_code, source
    FROM transactions
    WHERE duplicate_status <> 'suspect'
),
s AS (SELECT transaction_id, SUM(amount) AS total FROM transaction_splits GROUP BY 1)
SELECT t.id AS transaction_id, t.account_id, t.date, t.authorized_date, t.posted_date,
       COALESCE(t.authorized_date, t.date) AS spend_date, t.pending,
       sp.category_id, sp.amount, t.pfc_primary, t.pfc_detailed, t.transaction_code, t.source
FROM t JOIN s ON s.transaction_id = t.id AND s.total = t.amount
JOIN transaction_splits sp ON sp.transaction_id = t.id
UNION ALL
SELECT t.id, t.account_id, t.date, t.authorized_date, t.posted_date,
       COALESCE(t.authorized_date, t.date), t.pending, t.category_id, t.amount,
       t.pfc_primary, t.pfc_detailed, t.transaction_code, t.source
FROM t LEFT JOIN s ON s.transaction_id = t.id
WHERE s.transaction_id IS NULL OR s.total <> t.amount;
```

### transaction_tombstones
`plaid_transaction_id` (PK), `account_id`, `amount`, `date`, `authorized_date`, `name`,
`merchant_name`, `pending`, `user_state` jsonb (null when the row had no user edits; includes
splits), `removed_at`, `consumed_by_transaction_id` (FK transactions ON DELETE SET NULL). Written
for **every** removed Plaid transaction, so a stale update can never resurrect it and edits can
follow a charge to its replacement across syncs. Pruned after 90 days; unconsumed tombstones
with user edits are listed on the Connection page before they are pruned.

### budgets (BUD-1, BUD-3, BUD-4, BUD-5)
| column | type | notes |
| --- | --- | --- |
| scope | text not null | `category` \| `overall`; `CHECK ((scope = 'overall') = (category_id IS NULL))` |
| category_id | FK categories ON DELETE RESTRICT, null | |
| period | text not null default 'monthly' | `monthly` \| `annual` |
| amount | numeric(14,2), null | null = "no budget from this date on" |
| rollover | bool not null default false | BUD-4, monthly only |
| effective_from | date not null | |

Unique `NULLS NOT DISTINCT (category_id, period, effective_from)`. The amount for a period is
the row with the latest `effective_from <= period.end`. An edit updates the row whose
`effective_from` falls inside the current period, or inserts one at `period.start`. A category
has at most one active period type: setting a monthly budget writes an amount-null annual row
from the same date, and vice versa.

### statements (INS-4, REL-3)
| column | type | notes |
| --- | --- | --- |
| account_id | FK accounts ON DELETE RESTRICT, not null | |
| closing_date | date not null | unique per account |
| closing_date_source | text not null | `liabilities` \| `user` (a user-corrected date is never changed by sync) |
| plaid_issue_date | date | Liabilities `last_statement_issue_date`; unique per account; the snapshot key |
| period_start_override | date | only for the earliest statement (no previous closing date known) |
| reported_new_balance | numeric | |
| reported_new_balance_source | text | `liabilities` \| `user` (user values are never overwritten) |
| reported_previous_balance, reported_payments_credits, reported_new_charges, reported_fees, reported_interest | numeric | typed from the PDF statement |
| checked_at | timestamptz | |
| notes | text | |

A snapshot upserts on `(account_id, plaid_issue_date)`; if no row has that issue date, it
attaches to a row whose `closing_date` is within 7 days, else inserts a new row. A period's start
is always derived (previous `closing_date` + 1 day), never stored, except the override above.

### Other tables
- **app_settings** (single row, `id = 1`): `budget_period_mode` (`calendar` \| `statement`,
  default `calendar`), `budget_cycle_account_id` (FK accounts ON DELETE SET NULL; the card whose
  statement cycle defines budget periods), `include_pending_in_budgets` (true),
  `budget_alerts_enabled` (true), `alert_thresholds` (int[] `{80,100}`), `sync_alerts_enabled`
  (true), `large_charge_threshold` (numeric, null = off, ALR-3), `consent_reminder_days` (30),
  `statement_settle_days` (3), `ai_enabled` (false; AI-1 is not built in v1).
- **sync_runs** (SYNC-5): `item_id` (FK ON DELETE CASCADE), `trigger` (`schedule` \| `manual`
  \| `link` \| `update` \| `cli`), `status` (`running` \| `success` \| `error` \| `skipped` \|
  `interrupted`), `started_at`, `finished_at`, `added_count`, `modified_count`,
  `removed_count`, `update_status`, `error_code`, `error_message`.
- **alert_events** (ALR-1..3, SYNC-6): `kind`, `dedupe_key` (unique), `title`, `message`,
  `category_id`, `transaction_id`, `item_id` (all FK ON DELETE SET NULL), `delivered`,
  `delivered_at`, `error`.
- **csv_imports** (SYNC-8): `filename`, `file_sha256`, `account_id` (FK ON DELETE RESTRICT),
  `imported_at`, `row_count`, `inserted_count`, `duplicate_count`, `matched_plaid_count`,
  `error_count`, `date_min`, `date_max`.
- **recurring_overrides** (INS-3): `merchant_key` (unique), `status` (`ignored` \|
  `confirmed`).

## 6. Domain rules

### Spending (one function)
`spending.period_spending(session, period)` is the only definition of "spending in a budget
period". It reads `app_settings`: the date basis (posting date in statement mode,
`spend_date = COALESCE(authorized_date, date)` in calendar mode) and
`include_pending_in_budgets`, and sums `v_allocations.amount` over allocations whose category
`kind = 'expense'`, per category. Charges add; refunds (negative amounts in an expense
category) subtract from it (TXN-3). Card payments (`kind = 'transfer'`) and credits/rewards
(`kind = 'income'`) never count. The Budgets page, every "this period" widget on the dashboard,
and the budget alerts all call it. Trends always use calendar months by `spend_date`, with the
same pending setting.

### Categorization precedence (CAT-2, TXN-3)
For a new transaction:
0. **Payments (fixed, not overridable by rules).** Card Payments when `amount < 0` and any of:
   `transaction_code = 'payment'`; `pfc_detailed` is `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT` or
   `LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT`; `pfc_primary = 'TRANSFER_IN'`; the name or original
   description matches `PAYMENT.*(THANK YOU|RECEIVED)|AUTOPAY`; or a CSV row with a blank Amex
   category. A positive `PAYMENT … RETURNED/REVERSED` is also Card Payments.
   (`category_source = 'system'`.)
1. User state carried over from the transaction this one replaces (`category_source = 'user'`).
   A card payment (step 0) never takes a carried category or carried splits; the row is flagged
   `needs_review` when one was dropped.
2. The first matching active rule by `(priority, id)`.
3. Plaid PFC v2 mapping (detailed, then primary) or, for CSV rows, the Amex category mapping —
   sign-aware: negative amounts try the `|credit` mappings first (section 5), and a positive
   amount never reaches a `transfer` category through a mapping (it is a charge; real payment
   reversals were caught by step 0, and a row Plaid codes as a card payment stays one): it goes
   to Uncategorized, which counts as spending.
4. Uncategorized.

Every path that gives a row the user state of the row it replaces calls
`categorize.apply_carried_state(categorizer, row, state)`, so this order never depends on call
order. A row whose category the user already set keeps it, and keeps its own splits.

A row with `transaction_code = 'refund'` never lands in an `income` or `transfer` category:
it takes the most recent expense category used for the same merchant, else Uncategorized.
Modified transactions are re-categorized unless `category_source = 'user'`. Re-applying rules
(CAT-4) re-runs steps 0 and 2–4 on every non-user-edited transaction in scope. A user edit sets
`category_source = 'user'`; "Reset to automatic" clears it.

### User state and replacements (SYNC-4, TXN-4)
`userstate.py` defines `UserState` (category when user-set, notes, tags, splits) with
`capture`, `apply` and `merge`. Every path that moves edits — pending → posted, churn, tombstones,
CSV pairing, attaching a manual account — uses it. When state lands on a row with a different
amount, splits are rescaled proportionally (leftover cents on the largest split) and the row is
flagged `needs_review`. When an existing split row's amount is modified by Plaid, the same rule
applies.

### Sync apply algorithm (REL-1, SYNC-4)
1. **Fold** all pages of one sync, in order, into one map `transaction_id → final state`
   (a payload or REMOVED). The last occurrence wins; within one page, `removed` beats `added`
   and `modified`.
2. **Accounts first**: upsert the accounts in each page's `accounts` array. A new
   `plaid_account_id` on an Item that already has a matching account (type, subtype, mask) is
   treated as account churn: the existing account's id is updated and a `notice` is set.
3. **Upserts**: an id that a stored row names in `pending_transaction_id`, or that has a
   tombstone, is never inserted (stale update). A new id is inserted with the categorization
   precedence, inheriting user state from the row named in its `pending_transaction_id` (or from
   that row's tombstone), which is then deleted. An existing id gets only its Plaid-owned fields
   updated.
4. **Deletes**: every REMOVED id that still exists gets a tombstone (with its user state), then
   is deleted.
5. **Unlinked replacements**: unconsumed tombstones with user state from the last 30 days
   (including this batch's) are matched one-to-one to rows inserted in this batch that inherited
   nothing: same account, amount within `max($1, 20%)`, `COALESCE(authorized_date, date)` within
   7 days, similar description; best score first; posted candidates preferred for a pending
   source. The match inherits the state and the tombstone records `consumed_by_transaction_id`.
6. **Reconciliation** with CSV rows (below), then the cursor update.

### CSV ↔ Plaid reconciliation (SYNC-8)
`reconcile.reconcile_item(session, item_id)` is a full pass, run after every sync that
inserted rows, after every CSV import and after attaching a manual account:
- Candidates: unpaired CSV rows (`source = 'csv'`) and unpaired Plaid rows (no `csv_reference`)
  across all accounts of the Item, with equal amounts, the CSV date within
  `[COALESCE(authorized_date, date) − 3 days, date]` — widened to `[date − 30 days, date]` when
  `authorized_date` is null.
- One global one-to-one assignment, best first by date distance, then description similarity
  (CSV `Appears On Your Statement As`/`Description` vs Plaid `original_description`/`name`).
- A pair moves the CSV row's user state onto the Plaid row (unless the Plaid row has its own),
  copies `csv_reference` and `csv_import_id` onto it, and deletes the CSV row. The globally
  unique `csv_reference` then makes every re-import of that row a duplicate. Order matters
  because that unique key is not deferrable (CSV import uses `ON CONFLICT (csv_reference) DO
  NOTHING`): capture the CSV row's state, delete the CSV row and flush, then copy the reference
  and apply the state.
- **Cutover:** once the Item has `history_complete_at`, each account's `plaid_history_start`
  is set to its earliest Plaid posted date. Unpaired CSV rows dated on or after it become
  `duplicate_status = 'suspect'` (excluded from every total) and are listed on `/import` with
  "Keep" (→ `kept`) and "Delete".
- `reconcile.attach_manual_account(session, manual_id, plaid_account_id)` (CSV package): drops
  manual rows whose Reference already exists, moves the rest (transactions, imports, rules,
  statements — a statement that collides on `(account_id, closing_date)` or
  `(account_id, plaid_issue_date)` is merged into the Plaid account's row, user-entered dates and
  reported values winning), re-points `app_settings.budget_cycle_account_id` when it names the
  manual account (its ON DELETE SET NULL would otherwise silently turn statement-cycle budgets
  into calendar months), copies `statement_closing_day` and `notice` when the Plaid account has
  none, deletes the manual account, then runs the full pass. Linking calls it when exactly one
  manual account and one credit account exist; otherwise the Connection page asks. Once a
  production Item exists, CSV import may only target linked accounts.

### Locks (REL-1)
- **Sync lock** (per Item): `pg_try_advisory_lock` on a dedicated connection held for the whole
  of `sync_item`, released in `finally`. Busy → the run is recorded as `skipped`.
- **Ledger lock** (global): `pg_advisory_xact_lock` taken first in every transaction that
  writes transactions, splits or tombstones — sync apply, CSV import, reconciliation, attaching,
  rule re-apply, category merge, and user edits.
- **Cursor compare-and-set**: the apply transaction updates the cursor with `WHERE
  transactions_cursor IS NOT DISTINCT FROM :start`; zero rows → roll back and restart.
- **Link lock**: issuing a production new-link token checks and sets the guard under an
  advisory lock (section 7).

### Budget math (BUD-1..5)
- `available = budget amount + carry-in` (carry-in only when `rollover`: the previous period's
  `available − spent`, starting at 0 in the first rollover period; may be negative).
- `remaining = available − spent`; `percent_used = spent / available` (undefined when
  `available ≤ 0`).
- **Overall target (BUD-3):** compared with spending in categories that have no active annual
  budget. Without an overall target, "budget this period" is the sum of the monthly category
  budgets against spending in those categories, with unbudgeted spending shown separately.
- **Annual budgets (BUD-5):** calendar year; year-to-date spending uses the same basis rule as
  `period_spending`; shown with a pace line (`amount × elapsed days / days in year`) in a
  separate Annual section, never inside monthly totals.
- "Suggest from last 3 months" = mean of the last 3 complete periods, rounded up to $5.
- `days_left` counts today: `(period.end − today).days + 1`.

### Statement cycles and reconciliation (INS-4, REL-3, Gate D)
- A cycle is `(previous closing date, closing date]` by `posted_date`. Interest and fees dated
  on the closing date belong to that statement. Pending rows never count.
- Closing dates come from the `statements` table. Dates are generated only before the first or
  after the last stored date, stepping one month from the nearest stored date with the account's
  `statement_closing_day` (clamped to the month's length); a generated date within 10 days of a
  stored one is dropped, and a cycle outside 25–35 days is flagged.
- Budget periods in statement mode use the cycles of `app_settings.budget_cycle_account_id`.
- Summary identity, as printed by Amex: `New Balance = Previous Balance − Payments/Credits +
  New Charges + Fees + Interest Charged`.
- Buckets, by sign first: negative amounts → payments (Card Payments category or
  `transaction_code = 'payment'`) or credits (everything else); positive amounts → interest
  (`transaction_code = 'interest'` or PFC interest codes), fees (`late fee`, `membership fee`,
  other fee codes, PFC `BANK_FEES`, positive CSV `Fees & Adjustments`), or new charges.
- A statement is **final** only `statement_settle_days` after its closing date.
- Automatic Gate D check: when two consecutive statements both have a reported new balance,
  net activity must equal `new balance − previous new balance` to the cent; the view shows any
  difference and the transactions posted within 3 days of each boundary.

## 7. Plaid gateway and linking

`budgetapp/plaid_gateway.py` defines a `PlaidGateway` protocol with exactly these methods
(an allowlist — nothing that can move money exists, SEC-3):

```python
create_link_token(*, access_token=None, additional_consented_products=()) -> str
exchange_public_token(public_token) -> ExchangeResult(access_token, item_id)
transactions_sync(access_token, cursor) -> dict      # raw JSON with Decimal amounts
transactions_refresh(access_token) -> None
get_item(access_token) -> dict
get_accounts(access_token) -> dict
get_liabilities(access_token) -> dict
search_institutions(query, products) -> dict          # pre-link check
# sandbox only (raise in production):
sandbox_create_public_token(institution_id, *, override_username=None, override_password=None) -> str
sandbox_reset_login(access_token) -> None
sandbox_create_transactions(access_token, account_id, transactions) -> None
```

- **New-link token:** `products=['transactions']` only (Auth, Transfer or Signal would hide
  credit cards), `additional_consented_products=['liabilities']`,
  `transactions.days_requested=730` (fixed forever after linking), `country_codes=['US']`,
  `language='en'`, `client_name='Amex Budget Tracker'`, `user.client_user_id='owner-1'`. No
  `redirect_uri` (desktop pop-up OAuth), no `webhook`.
- **Update-mode token** (reconnect, consent renewal): `access_token` plus the basic fields, no
  products and no flags; `additional_consented_products=['liabilities']` when Liabilities consent
  is missing.
- **Sync request:** `count=500`, cursor omitted when none is stored,
  `include_original_description=True`, `personal_finance_category_version='v2'`.
- **Calls** use `_preload_content=False` and an explicit `_request_timeout` (sync `(10, 180)`,
  exchange `(10, 120)`, others `(10, 60)`); the body is read inside the error handling and parsed
  with `json.loads(parse_float=Decimal)`. `plaid.ApiException` → `PlaidError(status,
  error_type, error_code, error_message, display_message, request_id)`; urllib3 errors and
  non-JSON bodies → `PlaidTransportError`.
- **Classification:** `ITEM_LOGIN_REQUIRED`, `PENDING_EXPIRATION`, `PENDING_DISCONNECT`,
  `ACCESS_NOT_GRANTED`, `INVALID_CREDENTIALS`, `ITEM_LOCKED`, `USER_SETUP_REQUIRED`,
  `USER_PERMISSION_REVOKED` → `needs_reconnect`; `RATE_LIMIT_EXCEEDED`, `INSTITUTION_*`,
  `INTERNAL_SERVER_ERROR`, transport errors → `error` (retried next run);
  `TRIAL_CONNECTION_LIMIT` → shown plainly and never retried.

### Linking safety (SYNC-1)
- **The guard.** A production new-link token is issued only when there is no `plaid_items` row,
  no `plaid.access_token.production` in Keychain, no unexpired
  `plaid.link_in_progress.production` marker and no unsaved production token in a recovery file
  (`secrets.production_link_blockers`) — checked and the marker set under the link lock.
  The marker is cleared after a successful exchange (once `store_access_token` has returned
  normally) or when the user cancels Link; otherwise it expires after 4 hours (a Link token's
  lifetime). `./run setup --env production --access-token` marks a stored token's recovery
  files `*.saved`.
- **Re-attach.** If Keychain holds a production token but the database has no Item (lost or
  restored database), the Connection page offers "Re-attach existing connection" (rebuild the row
  from `/item/get` and `/accounts/get`, sync from a null cursor, reconcile) — never a new link.
- **Pre-flight.** Before the first production link the Connection page shows Plaid's checklist
  (Data Transparency use case selected, App and Company profiles complete, American Express
  enabled on the dashboard's US OAuth institutions page) and
  `search_institutions('American Express', ['liabilities'])`. A Link attempt that fails these
  checks creates no access token and uses no connection.
- **Exchange.** In production a public token whose `onSuccess` institution already has an Item
  is not exchanged. The public token stays in memory until the access token is in Keychain (read
  back) and the Item row is committed; transport failures are retried within the token's 30-minute
  life; one code path only; recovery file if the Keychain write fails (section 3).
- **After linking:** `/item/get` (consent expiry, `consented_products`), `/accounts/get`,
  attach a manual CSV account if one exists, `/liabilities/get` only if Liabilities was consented
  (otherwise the page offers an update-mode session to add it), then the first sync and backfill.
- Link `onExit`/`onEvent` treat `CLOSE_OAUTH` and `FAIL_OAUTH` as "not linked" and clear the
  marker.

## 8. Sync engine (SYNC-2, SYNC-5, SYNC-6, SYNC-7, REL-1)

```
sync_item(item_id, trigger):
  wait for the database (pg_isready, up to ~120 s) — "not ready" means try later, not an alert
  schema guard
  take the sync lock (busy → record 'skipped', return); mark older 'running' runs 'interrupted'
  record sync_run 'running' (own transaction)
  start = item.transactions_cursor            # None → omit cursor
  fetch every page until has_more is false; on TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION
     or a transport error, discard the pages and restart from `start` (bounded retries)
  NOT_READY → record success with no changes; keep the cursor null
  ONE transaction: ledger lock → apply algorithm (section 6) → reconcile_item →
     cursor compare-and-set → item healthy, error cleared, history_complete_at, cutover
  record sync_run success with counts (own transaction)
  best effort, each in its own transaction:
     /accounts/get → balances (SYNC-7)
     /item/get → consent_expires_at, consented_products, item error, Plaid freshness
     /liabilities/get when consented → liabilities columns + statements snapshot (SYNC-7)
     alerts.evaluate_after_sync
  on PlaidError: roll back, set item status/error per classification, record the run, alert
  on any other exception: roll back, record the run as error 'INTERNAL' with the exception
     class, set item status 'error', alert (ALR-2, once a day), re-raise (non-zero exit)
```

- **Schedule:** the launchd sync agent runs at 00:15, 06:15, 12:15 and 18:15
  (`StartCalendarInterval`, which macOS runs on wake if the Mac was asleep) and at login,
  skipping when the last success is less than 5 hours old. Plaid refreshes 1–4 times a day.
- **Stale data:** `status.py` reports `stale` (and alerts once a day) when the last successful
  sync is more than 36 hours old, whatever the Item status says.
- **Sync now:** `/transactions/refresh` (free on the Trial plan, 2 per minute), then syncs now
  and after about 15 s, 45 s and 2 min.
- **After linking or update mode:** the web process re-syncs with backoff until
  `HISTORICAL_UPDATE_COMPLETE` (cap about 1 hour), and once shortly after update mode.
- **Consent (SYNC-6):** read on every run; if Amex leaves it null, the due date is
  `last_consented_at + 365 days`. Renewal is update mode.
- Sandbox Items expire after 30 days (`ITEM_LOGIN_REQUIRED`); handled like any reconnect.

## 9. Web layer

FastAPI with server-rendered Jinja templates, HTMX for partial updates, Chart.js for charts.
Vendored htmx 2.0.11 and Chart.js 4.5.1 in `static/vendor/` (no CDNs). htmx's injected
indicator styles are disabled with `<meta name="htmx-config">`; those rules live in `app.css`.

Security middleware (SEC-1, SEC-4):
- Uvicorn binds to `127.0.0.1` only; the code refuses any other host.
- Host header must be `127.0.0.1` or `localhost` (blocks DNS rebinding).
- Unsafe methods require an `Origin` of this app, or no `Sec-Fetch-Site` other than
  `same-origin`/`none` (blocks cross-site form posts to localhost).
- Fetch Metadata on every method: when `Sec-Fetch-Site` is present and is neither
  `same-origin` nor `none`, only a top-level `GET` navigation (`Sec-Fetch-Mode: navigate`,
  `Sec-Fetch-Dest: document`) passes; anything else gets 403. This blocks cross-site
  subresource loads of pages such as `/transactions?q=…` (XS-Search). Requests without Fetch
  Metadata (curl, old browsers) are not checked.
- **CSP on every page:** `default-src 'self'; script-src 'self'; style-src 'self';
  style-src-attr 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none';
  form-action 'self'; frame-ancestors 'none'` (inline `style=""` attributes are allowed for
  progress widths and category colours; inline `<style>` and `<script>` elements are not). A
  per-request nonce is created in `request.state` and exposed to templates. The Connection page
  adds, for Plaid Link only: `script-src 'self' 'nonce-…'
  https://cdn.plaid.com/link/v2/stable/link-initialize.js`, `style-src`/`style-src-elem 'self'
  'nonce-…'`, `frame-src https://cdn.plaid.com/` and `connect-src 'self'
  https://<sandbox|production>.plaid.com/`.
- Other headers: `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Cache-Control: no-store` on pages. No Cross-Origin-Opener-Policy
  (Plaid's OAuth pop-up needs `window.opener`).
- No CORS and no login (single user, loopback only). `doctor` warns if other macOS user
  accounts exist on this Mac.

Pages: Dashboard `/`, Transactions `/transactions`, Budgets `/budgets`, Trends `/trends`,
Recurring `/recurring`, Statements `/statements`, Categories `/categories`, Rules `/rules`,
Import `/import`, Connection `/connection`, Settings `/settings`. A banner marks the Sandbox
environment on every page.

## 10. Commands and background jobs

| Command | Purpose |
| --- | --- |
| `./run` | Sandbox: start the cluster if needed, migrate the sandbox DB, serve from the repo, open the browser |
| `./run live` | Production: upgrade the install if the repo moved on, then open the app from the install |
| `./run setup` | create the cluster, roles and databases; store generated DB passwords and the Plaid keys you type in Keychain; `--access-token` re-enters a saved production token |
| `./run sync` | sync every Item now |
| `./run import-csv FILE` | import an Amex CSV (preview with `--dry-run`) |
| `./run backup` / `./run restore FILE` | backup now / restore one |
| `./run doctor` | check SEC-1..6, backups, agents, keys, schema; re-approve Keychain access |
| `./run install-agents` / `uninstall-agents` | `upgrade_install()` and (re)load / remove the launchd agents |
| `./run sandbox-link` / `sandbox-refresh` / `sandbox-reset-login` | Sandbox only test helpers |
| `./run test` | run the test suite |
| `./run db start\|stop\|status\|psql` | manage the dedicated cluster (`psql` connects as the env's role) |

LaunchAgents (`~/Library/LaunchAgents/local.budgetapp.*.plist`), all running the Application
Support install with `BUDGETAPP_ENV=production`, `LC_ALL=C.UTF-8` and a PATH that includes
Homebrew; every entry point waits for the database first:

| Agent | Schedule | Notes |
| --- | --- | --- |
| `postgres` | at login, kept alive | stale-lock wrapper; `Umask 077`; `ExitTimeOut 120` |
| `sync` | 00:15, 06:15, 12:15, 18:15 and at login | skips if synced in the last 5 h |
| `backup` | 02:30 and at login | skips if the newest verified dump is under 20 h old |
| `web` (optional) | at login, kept alive | serves the production app at http://127.0.0.1:8765 |

**Backups (REL-2):** as the environment's role, `pg_dump --format=custom` into a temporary file
(`umask 077`), verified with `pg_restore --list` and a full `pg_restore --file=/dev/null`, renamed
into place, newest 30 kept; a "backup failed or stale" alert fires when the newest verified dump is
over 26 hours old. **Restore** reads the dump's database name and refuses a dump from another
environment, needs an explicit `--env` and a typed confirmation for production, takes a
pre-restore dump, stops the web and sync agents and holds the sync lock, restores preserving
ownership, then runs the schema guard.

**Time Machine (SEC-6):** `setup` excludes the cluster directory. Backups stay included only if
every Time Machine destination is encrypted; `doctor` checks this and explains how to exclude
backups or encrypt the destination.

## 11. Notifications and alerts (ALR-1..3, SYNC-6)

`notify.py` calls `osascript` with `on run argv`, passing the title and message as arguments so
merchant names can never be interpreted as AppleScript. macOS attributes these notifications to
Script Editor and silently drops them until Script Editor is allowed to notify, so setup includes
a one-time step (`./run notify-test`, then allow Script Editor in System Settings →
Notifications) and `doctor` checks it.

`alerts.record_and_notify` records each alert once (its `dedupe_key`) and sends the notification
only after the caller's transaction commits, and only if the alert row is still there: a rolled
back transaction or savepoint sends nothing, and no notification runs while a lock is held.

- **Budget thresholds (ALR-1):** evaluated with `period_spending` (same basis and pending setting
  as the Budgets page) after each sync, import and edit that changes categories. A threshold `t`
  is crossed when `spent > 0` and `spent ≥ available × t / 100` (so a zero or negative available
  amount fires on the first spending). Each fires once per category, period and threshold; if
  spending falls back below a threshold (a pending hold dropped), its event is deleted so it can
  fire again.
- **Sync problems (ALR-2):** failures (Plaid or internal) once per Item, error code and day;
  reconnect needed once per occurrence; stale data after 36 h; Keychain approval needed (marker
  file).
- **Consent (SYNC-6):** renewal due within `consent_reminder_days`, once per due date.
- **Large charges (ALR-3):** only rows inserted by a sync, dated within the last 7 days, after the
  initial history load; once per charge and not again when it posts.
- **Backups:** failed or stale, once a day.

## 12. Testing (MNT-2)

- Each pytest process connects as `budgetapp_test` (non-superuser) and creates
  `budgetapp_test_<pid>`, runs the migrations, and drops it afterwards. The harness refuses to run
  unless the role is not a superuser and the database name starts with `budgetapp_test_`. Each test
  runs inside a transaction that is rolled back. Test code never gets a superuser connection and
  never starts, stops or reconfigures the real cluster; the test settings' state, logs, backups
  and install folders are a per-process scratch folder; test connections time out on locks after
  30 s instead of hanging. A test compares the catalog built by the migrations with the one
  built by the models (every column, constraint definition and index definition).
- `tests/fake_plaid.py` replays an **event log**, so one sync can carry the same id in added,
  modified and removed on different pages, exactly as Plaid may. It also serves pending → posted,
  churn, `NOT_READY`, mutation-during-pagination, transport errors and item errors.
- Optional live tests (`./run test --sandbox`) use the real Plaid Sandbox with keys from Keychain:
  a custom Sandbox user with an Amex-like card and known totals, and `user_transactions_dynamic`
  with `/transactions/refresh` for pending → posted. They produce the Gate A evidence. Tests never
  drive the Link UI.

## 13. Toward a mobile app

The mobile app will need (a) a JSON API and (b) a way to reach the Mac or a copy of the data.
The service layer already returns plain dataclasses, so (a) is a thin `/api/v1` router over the
same services. (b) is a deliberate future decision because SEC-1 currently allows only connections
from this Mac.
