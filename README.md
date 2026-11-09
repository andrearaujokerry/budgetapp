# Budget Tracker

A personal budget app for your credit cards and bank accounts. It runs only on your Mac and opens
in your browser. It pulls your transactions from your banks and card companies through Plaid,
sorts them into categories, and shows budgets, income and spending trends, recurring charges and
a statement check that matches each card statement to the cent.

- **Read-only.** The app can read your accounts' transactions and balances, and your cards'
  statement details, and nothing else. No feature can pay, move money or change anything at your
  bank. It asks Plaid only for read products (Transactions, and Liabilities for statement
  details). Its Plaid code allows only a fixed list of calls, none of which can move money, and a
  test fails if anything else appears.
- **Stays on this Mac.** The web app and its database accept connections only from this Mac.
  Your data goes only between this Mac and Plaid. There are no analytics or trackers, and no
  fonts or scripts from the internet (the app's font ships with it); Plaid's own sign-in
  window on the Connection page is the one exception.
- **Secrets in Keychain.** Plaid keys, the access tokens (one per bank login you link) and the
  database passwords live in macOS Keychain, never in files, logs or git.
- **Free to run.** Plaid's free Trial plan supplies the data.
- **Exact.** Money is stored as exact decimals, and refunds count against their category. Card
  payments never count as spending, and neither does paying a card from a linked checking account
  when Plaid files that payment as a card payment (no rule changes it) or as a transfer (unless a
  rule of yours moves it), so the card's charges count once. Each transaction's details say how
  it counts.

## Quick start

```sh
cd ~/Desktop/budgetapp
./run setup          # once: database, folders and your Plaid keys (typed here, hidden)
./run notify-test    # once: allow Mac notifications
./run                # the app with Plaid's Sandbox test data, at http://127.0.0.1:8766
```

To look around before you have any keys, run `./run setup --skip-plaid` and then use made-up
demo data: `./run demo create try`, then `./run serve --demo try` and open
http://127.0.0.1:8800.

Your real banks and cards come last, once the Sandbox works: see [Going live](docs/going-live.md).

## Guides

| Guide | What's in it |
| --- | --- |
| [Setting up](docs/setup.md) | What you need, `./run setup`, your Plaid keys, notifications, starting the app, Sandbox and production, demo data |
| [Going live](docs/going-live.md) | The production key, the background agents, linking your banks and cards, importing Amex CSV history, upgrades |
| [Using the app](docs/using-the-app.md) | Every page, from the Dashboard to Settings, and how money is counted |
| [Reconnecting and renewing](docs/reconnecting.md) | Reconnect, consent renewals, statement details, adding accounts, re-attaching a saved connection |
| [Alerts and notifications](docs/alerts.md) | Budget, sync, consent, large-charge and backup alerts |
| [Maintenance and troubleshooting](docs/maintenance.md) | Backups and restore, the launchd agents, `./run doctor`, logs, problems and fixes, tests |
| [Security](docs/security.md) | What protects your data, and what is up to you |
| [Architecture](docs/ARCHITECTURE.md) | The technical design, for developers |

## Setup and keys

`./run setup` does the one-time work and is safe to run again. It creates private folders, starts
the app's own PostgreSQL 18 server on port 5434 (separate from any other PostgreSQL on this Mac),
creates the databases with generated passwords saved in Keychain, and then asks for your Plaid
keys. [Setting up](docs/setup.md) has the details.

Your Plaid keys come from Plaid's Dashboard (Developers → Keys). **Type them only into
`./run setup` in your own Terminal.** Typing is hidden, nothing is echoed or logged, and the keys
go straight into your login Keychain. Never paste them into a chat, an email, a file, a command
line or a web page; the app never asks for them anywhere else. To add or change a key later, run
`./run setup --keys-only` (press Enter to keep a saved value).

## Starting the app

`./run` is the one command that starts everything for Sandbox: the database, any database
upgrade, the web server, and your browser at http://127.0.0.1:8766. Press Ctrl-C in Terminal to
stop it.

`./run live` opens the real app with your accounts at http://127.0.0.1:8765. It runs a separate
installed copy of the app and upgrades that copy first when the code has changed (after a
backup). Every other command also uses Sandbox unless you add `--env production`.
[Setting up](docs/setup.md#sandbox-and-production) explains the difference.

## Reconnecting

A bank sometimes needs you to sign in again (after a password change, for example), and some
banks, American Express among them, ask you to renew the app's access once a year. The app tells
you with a banner on every page that names the bank, a red status in the top bar and a Mac
notification. Open **Connection** and click **Reconnect** or **Renew consent** on that bank's
connection: Plaid opens a short window where you sign in to your bank. That repairs the same
connection. It never uses another of your 10 Plaid connections, and your data and edits stay.
Never link the same bank login a second time to fix a problem.
[Reconnecting and renewing](docs/reconnecting.md) covers every case.

## Commands

Run them from this folder. `./run <command> --help` explains each one.

| Command | What it does |
| --- | --- |
| `./run` | Start the Sandbox app and open the browser (`./run start --no-browser` skips the browser) |
| `./run live` | Open the production app with your real accounts (upgrades the installed copy first when needed) |
| `./run setup` | One-time setup: folders, database, Keychain passwords, Plaid keys (`--keys-only`, `--skip-plaid`) |
| `./run setup --env production --access-token` | Re-enter a saved production access token of any connection (for example from your password manager) |
| `./run serve` | Only the Sandbox web server, no migration and no browser (`--demo NAME [--port N]` for demo data) |
| `./run demo create NAME` | Make a demo database with realistic made-up data (`demo list`, `demo drop NAME`) |
| `./run sync` | Sync every connection now (what the agent does; the Sync now button also asks Plaid to refresh first) |
| `./run import-csv FILE` | Import an Amex CSV download (`--dry-run` previews it, `--account ID` picks the account) |
| `./run recategorize` | Re-apply rules and category mappings to past transactions (`--since`, `--only-uncategorized`, `--dry-run`) |
| `./run backup` | Back up the database now (add `--env production` for your real data) |
| `./run restore FILE` | Restore a backup; asks you to confirm by typing the environment's name |
| `./run doctor` | Check security, backups, agents, Keychain and the database (`--env production` for the real app) |
| `./run install-agents` | Install or upgrade the production copy and the background agents (`--with-web`, `--dry-run`) |
| `./run uninstall-agents` | Remove the background agents (`--dry-run` shows the plan) |
| `./run notify-test` | Send a test notification (the one-time Script Editor permission) |
| `./run db status` | Show whether the database server runs (`db start`, `db stop`, `db psql`, `db url`) |
| `./run migrate` | Upgrade the Sandbox database to the code's schema (`./run` does it for you) |
| `./run sandbox-link` | Sandbox only: create a test connection without Plaid's window |
| `./run sandbox-refresh` | Sandbox only: ask Plaid for new test transactions, then sync |
| `./run sandbox-reset-login` | Sandbox only: break the test sign-in so you can try Reconnect |
| `./run test` | Run the test suite (`--sandbox` runs only the live Plaid Sandbox tests) |

## Where things live

Nothing is stored in this folder except the code and its Python environment (`.venv`).

| What | Where |
| --- | --- |
| The code | this folder (`~/Desktop/budgetapp`) |
| The database (PostgreSQL 18 on 127.0.0.1:5434) | `~/Library/Application Support/budgetapp/postgres` |
| Backups | `~/Library/Application Support/budgetapp/backups/sandbox` and `…/backups/production` |
| The installed production copy | `~/Library/Application Support/budgetapp/venv` |
| Logs | `~/Library/Logs/budgetapp` |
| Keys, access tokens and passwords | your login Keychain, under the name `budgetapp` |
| Background agents | `~/Library/LaunchAgents/local.budgetapp.*.plist` |

[Maintenance and troubleshooting](docs/maintenance.md#where-everything-lives) lists every file.

## When something is wrong

Run `./run doctor` (or `./run doctor --env production` for the real app). It checks FileVault,
the database, Keychain, the background agents, backups and the Plaid connection, and prints a fix
for each problem. It only reads; it never changes anything.
[Maintenance and troubleshooting](docs/maintenance.md#troubleshooting) lists common problems.
