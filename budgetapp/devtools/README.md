# Demo data (`budgetapp/devtools`)

Realistic, made-up data for building and looking at pages before a real card is linked. A demo is
its own PostgreSQL database, `budgetapp_test_demo_<NAME>`, in the app's cluster (port 5434). It
is created and read as the non-superuser `budgetapp_test` over the private socket, with
test-environment settings. It never touches the Sandbox or production databases, Keychain or Plaid.

```sh
./run demo create budgets              # (re)create demo "budgets" (seed 1, today)
./run demo create budgets --seed 4     # other data, same shape
./run demo create shots --today 2026-10-15   # pretend today is that date
./run serve --demo budgets --port 8805 # serve it (default port 8800); Ctrl-C to stop
./run demo list
./run demo drop budgets
```

* Names: 1–20 characters `a-z 0-9 _`, starting with a letter or digit.
* `create` always drops and rebuilds; it takes about a second. The same `--seed` and `--today`
  always give the same data. The one exception: when "today" is the real date, the data stops at
  the current time. Sync runs after now don't exist, and neither do charges that would reach
  Plaid after the last sync: made at 10 am, a demo has none of today's coffee or lunch yet (they
  arrive with the 12:15 sync). With `--today` the day is complete up to its 18:15 sync.
* The summary `create` prints shows this month's spending per category against the money
  available, rollover included ("of $307.53 (incl. $187.53 rolled over)").
* `--env production` is refused. Feature agents: use your package's port (PLAN.md §0) and drop
  your demo when you are done.
* `serve --demo` uses `DemoPlaidGateway`. Every Plaid call raises `PlaidError` with the message
  "Not available with demo data" (`INVALID_REQUEST`/`DEMO_MODE`: not a reconnect, not retried).
  Linking, "Sync now" and refresh therefore show a friendly error. The layout shows the orange
  "Demo data" bar.
* If the code's schema moves on (a new migration), `serve --demo` says to recreate the demo.

## Python API (the contract `commands/serve.py` uses)

| Name | What it does |
| --- | --- |
| `demo.DEMO_DB_PREFIX` | `"budgetapp_test_demo_"` |
| `demo.demo_db_name(name)` | the database name; `DemoError` for a bad name |
| `demo.demo_settings(name, *, port=8800)` | `load_settings("test", db_name=…, web_port=port)`. `DemoError` ("run ./run demo create NAME first") when the database is missing, or recreate when the schema is outdated |
| `demo.create_demo(name, *, seed=1, today=None)` | drop, create, migrate and fill. Returns a `DemoSummary` with `.lines()` |
| `demo.drop_demo(name) -> bool` / `demo.list_demos() -> list[str]` | |
| `demo_gateway.DemoPlaidGateway()` | the no-network `PlaidGateway` |

`demo_plan.build_plan(seed, today, now)` builds everything in memory, with no database. It is
deterministic. `demo_writer.write_plan(session, plan)` stores a plan through the models and the
real `Categorizer`. `demo_catalog` holds the persona's merchants, budgets and rules.

## What is in a demo

Dates below are relative to "today" (T).

**The card.** One linked **Amex Blue Cash Preferred®** card ··3005 (a credit card, not a charge
card).
- Account fields: credit limit $18,000; current and available balances; Liabilities statement
  balance, minimum payment, due date and last payment; `statement_closing_day` 27.
- `plaid_history_start` is set.
- The Plaid Item:
  - Sandbox-style ids, `ins_10` "American Express", status `healthy`.
  - Linked at T−340 days, with `consented_products` `transactions` + `liabilities`.
  - `consent_expires_at` is 25 days out, inside the 30-day reminder, so the consent banner shows.
  - History is complete; there is a saved cursor and an `item_snapshot`.

**About 2,500 transactions over 24 months.**
- Spending spreads across every default category plus one custom category, **Pets** (`#9f1239`).
- Daily habits: coffee, lunch, dinners, delivery, groceries, rides, bike share, parking and
  Amazon.
- Monthly bills, charged overnight: gym on the 1st, Spotify, Comcast, Chewy, ChatGPT, KQED,
  Netflix, PG&E, Prime, Verizon, iCloud and NYT. Some of them change price once; PG&E follows the
  season (about a fifth higher in January, a fifth lower in July).
- Seasons: more evenings out in summer; holiday shopping from Thanksgiving week to Dec 20 (a
  Black Friday Best Buy run, more Amazon, Target and Uniqlo), and December gifts from Etsy,
  UncommonGoods and Paper Source.
- Periodic charges: a haircut about every 5 weeks, Costco, Clipper, gas and FasTrak.
- Yearly charges: the $95 annual fee, renters insurance, car registration, One Medical, the
  dentist and the vet.
- Trips. While the persona is away, nothing is bought in person at home: a yearly visit (dentist,
  vet, IKEA, Apple Store) or an in-store return that falls on a trip day moves to the day after
  the trip; bills still charge. Each trip eats around: every meal slot picks from 4–9 local
  places, at most twice each.
  - Seattle (CSV era) and Los Angeles (Los Angeles stores and restaurants).
  - Lake Tahoe (tag `Tahoe`).
  - New York (tag `NYC conference`).
  - Lisbon (tag `Lisbon <year>`). Abroad, every charge is in USD with a separate 2.7%
    `FOREIGN TRANSACTION FEE` row.
  - A flight booked at T−4.
- Credits and refunds: Amex Offer credits, Reward Dollars statement credits, and 7–8 refunds.

**How the rows follow Plaid.**
- Sign: positive is a charge, negative is a credit.
- Dates: `authorized_date` is 0–3 days before `date`. Friday charges post on Monday.
  - Posted rows have `posted_date = date`.
  - A handful of the latest charges are **pending**: `posted_date` NULL and
    `date = authorized_date`. There are more on weekends.
  - Posted rows synced after linking usually carry a `pending_transaction_id`.
- Payload fields:
  - PFC v2 primary/detailed codes, with confidence and version `v2`.
  - `merchant_name`, `merchant_entity_id` (stable per merchant), MCC, channel and website.
  - `location` exactly as Plaid sends it: all-null for online merchants, `Decimal` lat/lon for
    stores.
- `transaction_code` is on every Plaid row: `purchase`, `refund`, `payment`, `adjustment`
  (statement credits), `interest`, `membership fee` and `bank charge` (foreign fee). Amex may
  not send codes in reality.
- Payments, interest, fees and credits have no merchant.

**How every row is categorized.**
- Each row is categorized by the real `services.categorize.Categorizer`, never guessed:
  - payments are Card Payments (`system`);
  - rule matches set `rule_id`;
  - other rows follow the seeded PFC or Amex mapping, else Uncategorized (`default`).
- Uncategorized rows include a few recent pop-up stalls that have no PFC.
- Refunds are categorized last, so the refund rule sees their merchant's purchases. One REI
  return that Plaid labels `TRANSFER_IN` lands in Shopping as `default`.
- Rules (7):
  - small Apple charges and streaming/news → Subscriptions;
  - Costco → Groceries (a one-click rule with `source_transaction_id`);
  - pet stores and the vet → Pets;
  - gym → Health & Fitness;
  - PG&E → Utilities;
  - one inactive rule.

**User edits.**
- About six rows have a category set by the user (`category_source = 'user'`).
- Notes and tags appear on many rows: whole trips, `reimbursable`, `gift`, `family`, and so on.
- Edited rows have `user_edited_at` set.
- Three split rows (every split sums exactly to its transaction):
  - a Costco run (Groceries + Home);
  - a Target run (Groceries + Home + Personal Care);
  - the New York hotel. Its splits were made for $612.40; Plaid then changed the amount to
    $628.15, so `userstate.fit_splits_to_amount` rescaled them. It is the only `needs_review`
    row, with the rescale `review_reason`.

**The CSV era.**
- Rows posted before the Plaid history start (about T−18 months) came from one extended Amex CSV
  download (`csv_imports`: `activity.csv`, imported two days after linking). They are
  `source = 'csv'`.
- Each CSV row has:
  - a `csv_reference`: 18 digits, `3` + YYYYDDD of the posting date + 10 digits, globally unique;
  - `posted_date` from that Reference;
  - the Amex "Group-Subgroup" category in `external_category` (blank for payments);
  - a Card Member in `account_owner` (`ALEX RIVERA`, sometimes `SAM RIVERA`);
  - no `authorized_date`, `merchant_name` or PFC.
- Amex categories are coarser, so some CSV-era spending lands in Services, Other or Shopping
  where Plaid-era spending lands in Personal Care, Home, Gifts & Donations or Education, and
  Trends shows that step at the CSV-to-Plaid cutover. That is what a real import looks like:
  only the subgroups seen in a real Amex export are seeded (RESEARCH T4.2), the others fall back
  to their group (`Other-Charities` → Other). The CAT package's "Plaid & Amex categories" editor
  is where the user remaps them (then re-applies categories); a demo made after a mapping change
  picks it up.
- The file ran three weeks into the Plaid history:
  - Those rows paired with their Plaid rows. The Plaid rows carry the `csv_reference` and
    `csv_import_id`; `matched_plaid_count` counts them.
  - Exceptions: the Los Angeles hotel and rental car. The CSV dates them at check-in/pick-up,
    5 days before Plaid's authorized date and outside the pairing window. They stayed as the two
    `duplicate_status = 'suspect'` rows; `v_allocations` and every total exclude them.

**Statements.**
- The last 12 closed cycles, closing near the 27th and drifting by a day or two.
- Each statement has `closing_date_source` and `reported_new_balance_source` `liabilities`, with
  `plaid_issue_date` = closing date.
- The earliest statement has `period_start_override`, because the closing before it is not
  stored.
- Every reported field is filled. `reported_payments_credits` is a **positive** amount, so:
  `new = previous − payments_credits + new_charges + fees + interest`.
- Membership is by `posted_date` in (previous closing, closing].
- Every statement adds up, and each `reported_previous_balance` is the previous statement's new
  balance.
- AUTOPAY pays each statement balance on its due date (closing + 25). There are two exceptions:
  - one month after New York was paid only in part, so interest was charged on the next closing
    date (that statement has a note);
  - one month had an extra MOBILE PAYMENT.
- The $95 annual fee and the Lisbon foreign fees show up as Fees.
- **Exactly one cycle is off by $0.01** (the Gate D failure). On the Amex statement, one Lisbon
  charge is a cent higher than in Plaid's data, so that cycle's new charges and new balance
  differ from the app's numbers by $0.01. Every later reported balance carries the cent, and so
  does the card's current balance. Only that cycle's Gate D check fails.
- `checked_at` is set on final statements except the mismatched one and the latest, which is
  still settling.

**Budgets** (calendar mode; `budget_cycle_account_id` is the card, so statement mode works too).
- Monthly budgets for 11 categories and an overall target of $3,800. Rows are versioned from
  the link month, for example Groceries $650 → $700 and Dining & Drinks $700 → $650.
- Personal Care has rollover from 5 months ago.
- Travel ($6,000) and Gifts & Donations ($800) are annual budgets from Jan 1. Each has an
  amount-NULL monthly row from the same date, as ARCHITECTURE §5 requires; Travel had a monthly
  budget before that.
- On any day of the month:
  - **Health & Fitness is at 85%** (the $255 gym bill on the 1st against $300);
  - **Entertainment is over budget** (concert tickets);
  - every other budgeted category is at or under 75%.

**Sync runs, alerts and other rows.**
- `sync_runs`: every 6 hours at 00:15/06:15/12:15/18:15 for the last 4 days, plus one manual run.
  All succeeded except one transient `INSTITUTION_NOT_RESPONDING` failure 2 days ago. The counts
  are computed from when rows arrived.
- `alert_events` holds:
  - the consent reminder;
  - that sync failure;
  - this period's budget threshold alerts, computed with `services.spending` the way
    `alerts.evaluate_budgets` would. Older periods' alerts are not generated.
- `large_charge_threshold` stays off (NULL).
- `recurring_overrides`: one `ignored` series (the Clipper top-ups), keyed by its
  `merchant_entity_id`.
- No tombstones and no manual accounts.

## Tests

`tests/test_demo.py` builds a scratch demo database (`budgetapp_test_demo_t<pid>`) for a fixed
seed and date and drops it afterwards. Its checks:
- determinism and recreation;
- realism: no in-person charge at home on a trip day, varied trip meals, holiday and seasonal
  patterns, nothing stored before the sync that brought it, the rollover in the summary;
- the Plaid conventions;
- every non-user row is exactly what the Categorizer decides;
- payments are in Card Payments;
- the CSV references and the import row;
- the suspects are excluded from `period_spending`;
- splits and `needs_review`;
- the statement identity, with the one-cent cycle;
- autopay;
- the budget shape and alerts;
- consent and the sync history;
- the gateway and the command.
