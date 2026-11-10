# Maintenance and troubleshooting

Commands run in Terminal from the app's folder (`cd ~/Desktop/budgetapp`). Each one uses the
Sandbox unless you add `--env production`.

## Backups

The backup agent backs up your real data every night at 02:30 and at each login, and skips a
run when a backup was already made since the last 02:30 (a Mac asleep at 02:30 catches up when it
wakes). A backup made during the day, by hand or at login, never makes the next night's run
skip, so backups stay about a day apart. To back up by hand:

```sh
./run backup --env production   # your real data
./run backup                    # the Sandbox database
```

- Each backup is a complete PostgreSQL dump, checked by reading it back in full before it gets its
  final name, so a file in the backup folder is always a usable backup.
- Backups go to `~/Library/Application Support/budgetapp/backups/production` (or `…/sandbox`),
  named after the database and the time in UTC, for example
  `budgetapp-20261101T023012Z.dump`. Only you can read them.
- The newest 30 are kept (`--keep N` changes that for a manual run).
- Each upgrade of the live app (`./run live` or `./run install-agents` after new commits) first
  saves a copy named `…-pre-upgrade.dump`. These copies are kept apart from the 30 backups (the
  newest 5 stay), so upgrading, or retrying an upgrade, never pushes a nightly backup out.
- If a backup fails, or the newest one is more than 26 hours old, you get a notification (once a
  day) and `./run doctor --env production` reports it. The sync agent checks too, so you also hear
  about it when the backup agent itself stops running.
- Backups hold the database only. Your keys and each connection's access token stay in Keychain;
  keep a copy of each token in your password manager ([Going live](going-live.md#keep-a-copy-of-each-access-token-recommended)).

### Time Machine and copies elsewhere

`./run setup` tells Time Machine to skip the live database folder: a copy of a running database
isn't a usable backup. The dumps in the backups folder are real backups, and Time Machine copies
them like any other file. Because they hold your full spending history, they may leave the Mac
only on an **encrypted** disk. `./run doctor` checks every Time Machine destination; if one isn't
encrypted, either turn on encryption for it (Time Machine settings → choose the disk → Encrypt
backups), or keep the dumps on this Mac:

```sh
tmutil addexclusion "$HOME/Library/Application Support/budgetapp/backups"
```

Copy dumps by hand only to an encrypted disk, never to cloud storage.

## Restore a backup

```sh
ls "$HOME/Library/Application Support/budgetapp/backups/production"
./run restore "$HOME/Library/Application Support/budgetapp/backups/production/budgetapp-20261101T023012Z.dump" --env production
```

(For the Sandbox, leave out `--env production`. Restoring production needs it typed on the
command line.)

1. The app reads the whole backup first and shows what it will do: the file, when it was made,
   and the database layout it holds.
2. It asks you to type the environment's name (`production` or `sandbox`); anything else
   cancels and changes nothing.
3. It refuses a backup of the other environment, and one made by a newer version of the app.
4. It never interrupts a running sync: if one is running, it changes nothing and asks you to try
   again in a few minutes. A sync that starts during the restore is skipped until the next run.
   In production it also stops the web, sync and backup agents for the duration and starts them
   again afterwards.
5. It makes a **safety copy** of the data you are about to replace, named
   `…-pre-restore.dump` in the same folder. The newest 5 safety copies are kept, separately from
   the 30 backups.
6. It restores everything in one transaction: the restore either completes or changes nothing.
7. It prints what the database now holds and the exact command that undoes the restore
   ("To undo, restore the copy taken first: …").

If the backup comes from an older version of the app, the message says how to upgrade it:
`./run live` for production, `./run migrate` for the Sandbox.

- **"other connection(s) … are open"**: close the app (and any `./run db psql`), or add
  `--force-disconnect` to end those connections.
- **The safety copy failed** (for example a damaged table): the app restores nothing and prints
  the same command with `--without-safety-copy`. Use it only if you are sure: the current data is
  then lost unless an older backup has it.

## The background agents

Installed by `./run install-agents` ([Going live](going-live.md#2-install-the-background-agents)),
they run the installed production copy with your real data, never the Sandbox:

| Agent | What it runs | When | Its log |
| --- | --- | --- | --- |
| `local.budgetapp.postgres` | the database server | at login, kept running | `postgres.log` |
| `local.budgetapp.sync` | `sync --env production --if-due --unattended` | 00:15, 06:15, 12:15, 18:15 and at login; skipped when the last successful sync is under 5 hours old | `sync.log` |
| `local.budgetapp.backup` | `backup --env production --if-due --unattended` | 02:30 and at login; skipped when a backup was already made since the last 02:30 | `backup.log` |
| `local.budgetapp.web` (with `--with-web`) | the production app at http://127.0.0.1:8765 | at login, kept running | `web.log` |

- Their files are `~/Library/LaunchAgents/local.budgetapp.*.plist`; logs are in
  `~/Library/Logs/budgetapp`.
- They never show a Keychain dialog. When macOS wants your approval, they notify you once a day
  and try again later: run `./run doctor --env production` in Terminal and click **Always Allow**.
- Keep them allowed in System Settings → General → Login Items & Extensions → Allow in the
  Background. macOS lists them under the programs they run (Python and sh), not as "Budget
  Tracker" (the name the app's messages use).
- `./run install-agents` again re-installs them (and upgrades the installed copy if the code
  changed); `--dry-run` shows the plan first. `./run uninstall-agents` removes them and keeps your
  data, backups and the installed copy.
- `./run doctor --env production` checks that each one is loaded and allowed.

## ./run doctor

```sh
./run doctor --env production   # the real app
./run doctor                    # the Sandbox
```

Doctor checks everything that keeps your data safe and the app running, and prints PASS, WARN,
FAIL or INFO for each check, with a one-line fix under each problem. It only reads: it never
changes a file, the database, the agents or Keychain. It exits with status 1 when anything
FAILs.

| Section | What it checks |
| --- | --- |
| This Mac | FileVault is on; no other macOS user accounts (the app has no login); app folders are private and outside Desktop, Documents, Downloads and iCloud; no log has grown huge |
| Database | the server runs and accepts this environment's user; it listens only on this Mac; its socket and settings are private; the client and server versions match; no stale lock file; the database layout matches the code |
| Web app | whether the app answers on its port, and only on this Mac |
| Keychain | every key and password this environment needs is saved and readable |
| Background agents | each agent is loaded and allowed; the installed copy matches the code; Mac notifications are set up |
| Backups | a recent verified backup exists; the folder and files are private; Time Machine skips the live database and copies backups only to encrypted disks |
| Code | no Plaid tokens in files git tracks; CSV downloads are kept out of git |
| Plaid connection | for each connection, by name: it works; its consent isn't about to run out; a card's statement details are shared; its last sync isn't too old |

Run in Terminal, doctor also reads every Keychain item of both environments (the marker an
unfinished Plaid link can leave behind too), so after a `brew upgrade` of Python macOS asks about
each one; click **Always Allow**. Values are never shown. A report looks like this (shortened):

```text
Budget Tracker doctor · production · database budgetapp

This Mac
  PASS  FileVault is on (SEC-6)
  PASS  No other macOS user accounts on this Mac
  …
Background agents
  WARN  Mac notifications aren't set up yet (alerts are dropped)
        Fix: ./run notify-test, then allow Script Editor in System Settings → Notifications
  …
Plaid connection
  WARN  Renew American Express consent by Nov 26, 2026 (25 days left)
        Fix: Connection page → Renew consent on American Express

All good: 24 passed, 2 warning(s), 0 failed, 1 info
```

## Logs

Logs never contain your keys, tokens or passwords: the app removes anything that looks like one
before writing.

| File in `~/Library/Logs/budgetapp` | Written by |
| --- | --- |
| `postgres.log` | the database server |
| `web-sandbox.log`, `web-production.log` | the web app: requests, warnings and errors (production logs no requests) |
| `sync-sandbox.log`, `sync-production.log` | `./run sync`, the sync agent and the Sandbox helpers |
| `sync.log`, `backup.log`, `web.log` | the background agents' own output |

Logs can't fill the disk. The app's own logs (`web-…` and `sync-…`) start a new file at 5 MB and
keep the 3 before it (`web-production.log.1` and so on). Each night the backup agent cuts any
other log over 10 MB (an agent that keeps failing and restarting writes one quickly) down to its
newest 1 MB, and `./run doctor` warns about a log over 50 MB with the command that shows its last
lines.

## Where everything lives

| Path | What it is |
| --- | --- |
| `~/Desktop/budgetapp` | the code (git), with its Python environment in `.venv` |
| `~/Library/Application Support/budgetapp/postgres` | the PostgreSQL 18 data (skipped by Time Machine) |
| `~/Library/Application Support/budgetapp/run` | the database server's private socket folder |
| `~/Library/Application Support/budgetapp/backups/production`, `…/sandbox` | verified backups, pre-restore safety copies and pre-upgrade copies |
| `~/Library/Application Support/budgetapp/venv` | the installed production copy that `./run live` and the agents run |
| `~/Library/Application Support/budgetapp/bin` | the small script the postgres agent starts |
| `~/Library/Application Support/budgetapp/state` | small marker files (once-a-day notices, the install stamp) and, only if Keychain failed during a link, `recovery/` |
| `~/Library/Logs/budgetapp` | logs |
| `~/Library/LaunchAgents/local.budgetapp.*.plist` | the background agents |
| login Keychain, name `budgetapp` | Plaid keys, each connection's access token and database passwords ([the list](setup.md#what-keychain-holds)) |

Every folder is readable only by you (mode 700) and every file the app writes there only by you
(600). The databases: `budgetapp` (production) and `budgetapp_sandbox`, plus `budgetapp_test_…`
databases that tests and demos create, all in the app's own PostgreSQL server on 127.0.0.1:5434.

Advanced: if Homebrew lives somewhere else, `BUDGETAPP_PG_BIN` names the PostgreSQL 18 `bin`
folder and `BUDGETAPP_PYTHON` the Python 3.14 program.

## Troubleshooting

Start with `./run doctor` (or `./run doctor --env production`): most problems show up there with
their fix.

| You see | What to do |
| --- | --- |
| A page says **"The database isn't responding"**, or a command says the database isn't running | `./run db start`, then reload. `./run db status` shows the server's state; `postgres.log` says why it stopped. With the agents installed, `./run doctor --env production` checks the postgres agent |
| **"Upgrade needed"** | Sandbox: `./run migrate` (or restart `./run`). Production: `./run live`, which backs up first. Demo: `./run demo create NAME` |
| **"The Sandbox database was upgraded by a newer version of the app"**, or "which this code doesn't know: newer code upgraded it" from a command | Another branch or checkout with newer code migrated it, so `./run migrate` can't help: check out that code again, or restore an older Sandbox backup with `./run restore FILE` ([Restore a backup](#restore-a-backup)) |
| **"Setup isn't finished"** | `./run setup --keys-only` for the Plaid keys, or `./run setup` for the database passwords |
| **"Keychain approval needed"**, or **"Linking is paused."** saying macOS Keychain needs your approval | `./run doctor --env production` in Terminal, then **Always Allow** for each item |
| **"127.0.0.1:8766 is already in use"** | The app already runs in another Terminal window: open the address, or stop that one with Ctrl-C |
| **"PostgreSQL 18 not found"** | `brew install postgresql@18` |
| **"Python 3.14 is required"** | `brew install python@3.14` |
| **"The repository has uncommitted changes"** from `./run live` or `./run install-agents` | Production only runs committed code: commit or stash the changes, then try again. Until then `./run live` opens the installed copy as it is ("Not upgraded: …") |
| **"Refusing to serve the PRODUCTION app from a git checkout"** or to migrate it | Use `./run live`, which runs and upgrades the installed copy |
| `launchctl bootstrap … failed`, or the agents don't run | Allow the app's background items (listed as Python and sh, not "Budget Tracker") in System Settings → General → Login Items & Extensions → Allow in the Background, then `./run install-agents` again |
| No notifications | `./run notify-test`, then allow Script Editor in System Settings → Notifications ([Alerts](alerts.md#turn-notifications-on-one-time)) |
| **Reconnect needed**, consent banners, **"Linking is paused"** | [Reconnecting and renewing](reconnecting.md) |
| **"Your data may be out of date."** | The Mac was asleep or offline, or the sync agent isn't running: **Sync now**, then `./run doctor --env production` |
| **"The last sync failed (…)."**, **Failed** with a code under Recent syncs, or "failed (…)" from `./run sync` | Look the code up in [Reconnecting → Sync failures that fix themselves](reconnecting.md#sync-failures-that-fix-themselves): most clear up by themselves, the codes under [Reconnect](reconnecting.md#reconnect) need you |
| **"Plaid Link didn't load."** | Check the internet connection and any content blocker for 127.0.0.1, then reload |
| A statement is **"Off by $…"** | Open it: check the transactions near the closing dates and the numbers you typed ([Statements](using-the-app.md#statements)) |
| Wrong categories | Fix one with a click, make a rule, then re-apply rules ([Rules](using-the-app.md#rules)) |
| A CSV file is refused as "Amex's basic download" | Download it again with "Include all additional transaction details" ticked ([Import](using-the-app.md#import)) |

## Running the tests

```sh
./run test                         # the whole suite
./run test -k budgets -x           # anything after "test" goes to pytest
./run test --sandbox               # only the live tests against Plaid's Sandbox
```

- `./run test` runs everything except the live Plaid tests, in a throwaway database
  (`budgetapp_test_<number>`) that it creates and deletes. It never touches your Sandbox or real
  data, Keychain, Plaid or the agents, and sends no notifications. It needs the database server
  running (`./run db start`).
- `./run test --sandbox` calls Plaid's Sandbox over the internet with the Sandbox keys in your
  Keychain. It is free and uses only test data.
- `BUDGETAPP_PFC_CSV=/path/to/plaid-pfc-taxonomy.csv ./run test -k seed_matches` also checks the
  app's built-in Plaid category list against Plaid's published taxonomy file (skipped without
  it).
