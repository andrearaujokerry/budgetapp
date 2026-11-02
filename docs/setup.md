# Setting up

This guide takes you from a fresh checkout to the app running on Plaid's Sandbox test data. Your
real card comes later, once, in [Going live](going-live.md).

Every command here runs in Terminal from the app's folder:

```sh
cd ~/Desktop/budgetapp
```

## What you need

- **A Mac with Apple silicon**, with Homebrew installed in `/opt/homebrew`.
- **FileVault turned on** (System Settings → Privacy & Security → FileVault). Your spending
  history is on this disk; `./run doctor` reports a failure while FileVault is off.
- **Python 3.14 and PostgreSQL 18 from Homebrew:**

  ```sh
  brew install python@3.14 postgresql@18
  ```

  You don't need to start PostgreSQL yourself (with `brew services`, for example): the app runs
  its own private PostgreSQL server on port 5434, with its data under
  `~/Library/Application Support/budgetapp`. It never uses or changes any other PostgreSQL on
  this Mac.
- **git**, which comes with Apple's Command Line Tools (`xcode-select --install` if `git` is
  missing). The production copy of the app is built from git.
- **A Plaid account on the free Trial plan.** Sign up as a new team at
  https://dashboard.plaid.com/trial-plan. The Trial plan includes Transactions and Liabilities
  and allows 10 real connections ([what that means](going-live.md#one-of-your-10-plaid-connections-permanently)).
- **An internet connection** for the first run (it downloads the app's Python packages) and for
  Plaid. Nothing else is downloaded or uploaded.

The first time you run any `./run` command, it creates a Python environment in `.venv` inside the
folder and installs the exact package versions the app was tested with. It does that again by
itself whenever those versions change.

## First-time setup

```sh
./run setup
```

Setup prints a ✓ line for each step and is safe to run again; a second run changes only what is
missing. It:

1. creates private folders (only you can open them) under
   `~/Library/Application Support/budgetapp` and `~/Library/Logs/budgetapp`;
2. creates the app's PostgreSQL 18 server, if it doesn't exist yet, and starts it on
   127.0.0.1:5434;
3. creates the database users and the two databases, `budgetapp_sandbox` (test data) and
   `budgetapp` (your real data), with generated passwords saved in your Keychain;
4. tells Time Machine to skip the live database folder (a copy of a running database is not a
   usable backup; the app makes real backups instead);
5. brings the Sandbox database up to date, and creates the production database's tables the
   first time only (after that, only the guarded upgrade in `./run live` and
   `./run install-agents` changes them);
6. asks for your Plaid keys (next section). Add `--skip-plaid` to do that later.

It ends with your next steps.

## Your Plaid keys

You need three values from Plaid's Dashboard, under **Developers → Keys**
(https://dashboard.plaid.com/developers/keys):

| Key | Used for |
| --- | --- |
| Client ID | every environment |
| Sandbox secret | the Sandbox app (test data) |
| Production secret | your real Amex; optional now, needed when you [go live](going-live.md) |

Setup asks for them one at a time:

```text
Plaid client ID:
Plaid Sandbox secret:
Plaid Production secret (optional, Enter to skip):
```

- **Type or paste each key only here, in your own Terminal.** Typing is hidden, nothing is shown
  or logged, and each key goes straight into your login Keychain. Never put a key into a chat,
  an email, a file, a command line, a note or a web page. The app never asks for keys anywhere
  else: no page in the app has a key field.
- Press Enter to keep a value that is already saved, or to skip the optional Production secret.
- The app checks the shape of what you type. A Plaid client ID is usually 24 hexadecimal
  characters and a secret 30; anything unusual asks "Save it anyway? [y/N]". Values with spaces,
  values that are too short, and Plaid tokens (`access-…`, `public-…`, `link-…`) are refused.
- To add or change a key later, run `./run setup --keys-only`. It asks the same three questions;
  press Enter for the ones you keep.
- If you ever think a key leaked, rotate it in Plaid's Dashboard, then type the new one into
  `./run setup --keys-only`.

### What Keychain holds

Everything is in your login Keychain under the name `budgetapp`. Values are never written to the
database, files, logs or git.

| Keychain item | What it is |
| --- | --- |
| `plaid.client_id` | your Plaid client ID |
| `plaid.secret.sandbox`, `plaid.secret.production` | your Plaid secrets |
| `plaid.access_token.production` | the token for your linked Amex (written once, at the link) |
| `plaid.link_in_progress.production` | a short-lived marker while a production link is open |
| `plaid.access_token.sandbox.<id>`, `plaid.item_ids.sandbox` | Sandbox test connections |
| `db.password.budgetapp_dev`, `db.password.budgetapp_live` | the generated database passwords |

Let the app create and change these items itself, through `./run setup`. Don't create or edit
them with Keychain Access or the `security` command: macOS trusts the program that created an
item, and the background agents rely on that.

After a `brew upgrade` that updates Python, macOS may ask once per item whether the app may read
it. Run `./run doctor --env production` in Terminal and click **Always Allow** for each item, so
the background agents can read them again without asking.

## Allow notifications (one time)

```sh
./run notify-test
```

This sends a test notification. macOS shows the app's notifications under **Script Editor**'s
name and icon, and silently drops them until Script Editor is allowed to notify. So, once:

1. Open System Settings → Notifications → Script Editor (it appears after the first try, which
   just happened).
2. Turn on **Allow notifications** and choose the **Alerts** style, so problems stay on screen.
3. Run `./run notify-test` again to check.

The Settings page has the same button (**Send test notification**). [Alerts and
notifications](alerts.md) explains what you will be told.

## Start the app

```sh
./run
```

`./run` (the same as `./run start`) starts the database server if it isn't running, waits for it,
upgrades the Sandbox database if the code needs it, warns you if the Plaid keys aren't saved yet,
starts the web app and opens your browser at **http://127.0.0.1:8766**. Terminal shows
`Budget Tracker is running at http://127.0.0.1:8766 (press Ctrl-C to stop)`.

- Press **Ctrl-C** in that Terminal window to stop it ("Budget Tracker stopped.").
- `./run start --no-browser` starts it without opening the browser.
- Without Plaid keys, everything works except linking and syncing.
- If you see "127.0.0.1:8766 is already in use", the app is probably already running in another
  Terminal window: open the address, or stop the other one first.

The app has no login. It listens only on 127.0.0.1, so only someone using this Mac can open it.

## Sandbox and production

The app has two separate environments, plus demo data. They never share a database.

| | Sandbox (the default) | Production | Demo data |
| --- | --- | --- | --- |
| Start it with | `./run` | `./run live` | `./run serve --demo NAME` |
| Address | http://127.0.0.1:8766 | http://127.0.0.1:8765 | http://127.0.0.1:8800 (or `--port N`) |
| Data | Plaid's Sandbox: fake banks and test users | your real American Express | made-up data, no Plaid at all |
| Database | `budgetapp_sandbox` | `budgetapp` | `budgetapp_test_demo_NAME` |
| Code that runs | this folder | the installed copy in `~/Library/Application Support/budgetapp/venv` | this folder |
| In the browser | a purple "Sandbox — test data" bar | no bar; a green "Live" label | an orange "Demo data" bar |
| Plaid connections | unlimited and free | 1 of your 10, once | none |

- **Every command uses Sandbox unless you say otherwise.** Real data is touched only by
  `./run live` and by commands you give `--env production` (for example
  `./run backup --env production`). The background agents always run production.
- **Production runs from an installed copy**, not from this folder. macOS doesn't let background
  jobs read your Desktop, so `./run live` and `./run install-agents` install the committed code
  into `~/Library/Application Support/budgetapp/venv` (after a backup) and run it from there.
  [Going live](going-live.md) walks through it.
- **Sandbox test connections expire after 30 days** at Plaid; the app then asks you to
  reconnect, like a real one.

## Try the Sandbox

Open **Connection** in the Sandbox app (http://127.0.0.1:8766/connection). With your Sandbox
keys saved you can:

- click **Link with Plaid Link**, pick **First Platypus Bank** and sign in with `user_good` and
  `pass_good`: the same Plaid window your real link will use;
- or choose test data under **Sandbox** and click **Create a test Item without Link**. "Amex-like
  card, known totals" gives a credit card with a known set of purchases, a refund and payments.

The same things from Terminal:

```sh
./run sandbox-link                  # a test connection without Plaid's window
./run sandbox-link --user custom    # the Amex-like test card
./run sandbox-refresh               # ask Plaid for new test transactions, then sync
./run sandbox-reset-login           # break the test sign-in, to try Reconnect
./run sync                          # sync every connection now
```

`./run sandbox-link` uses the test user `user_transactions_dynamic` unless you pass `--user`
(`custom`, `user_transactions_dynamic` or `user_good`); that user's pending charges post after a
`./run sandbox-refresh`. Sandbox connections are free and never count toward your 10 real ones.
These commands refuse to run in production.

## Demo data

Demo data lets you explore every page with two years of realistic, made-up card activity, without
Plaid keys or a link. It needs only the app's database server: run `./run setup` first
(`--skip-plaid` if you have no keys yet), and `./run db start` if the server isn't running.

```sh
./run demo create try          # make (or remake) demo "try"; takes about a second
./run serve --demo try         # serve it at http://127.0.0.1:8800; Ctrl-C to stop
./run demo list                # list your demos
./run demo drop try            # delete it
```

- A demo is its own database (`budgetapp_test_demo_try`) in the app's PostgreSQL server. It never
  touches your Sandbox or real data, Keychain or Plaid, and sends no notifications.
- It holds one linked Amex card with about 2,500 transactions over two years, budgets (with
  rollover, annual budgets and an overall target), statements (one deliberately off by $0.01),
  recurring charges, rules, CSV rows held for review and a consent renewal due soon.
- Linking, syncing and renewing are turned off; the Connection page says so.
- Names use `a-z`, `0-9` and `_`, up to 20 characters. `--seed N` gives different data of the
  same shape, and `--today YYYY-MM-DD` pretends today is another date. The same seed and date
  always give the same data.
- `--port N` serves it on another port (not 8765 or 8766, which belong to the real apps, and not
  below 1024).
- If the app's database layout changes after an update, the demo page tells you to run
  `./run demo create NAME` again.

## Next

- Learn the pages: [Using the app](using-the-app.md).
- Link your real card: [Going live](going-live.md).
- Check everything: `./run doctor` ([Maintenance and troubleshooting](maintenance.md)).
