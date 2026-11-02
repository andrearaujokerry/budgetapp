# Going live

This guide links your real American Express card, once, and sets the app up to run by itself.
Do it after the Sandbox works ([Setting up](setup.md)): test connections are free, real ones are
not.

All commands run in Terminal from the app's folder (`cd ~/Desktop/budgetapp`).

## One of your 10 Plaid connections, permanently

Plaid's free Trial plan allows **10 real connections ("Items") in total, for good**. Every time a
real link succeeds, one of the 10 is used, and it stays used even if the connection is removed
later: deleting it never gives the slot back. Linking American Express a second time can also
break the first link.

That is why the app treats the production link as a one-time event:

- It offers **Link American Express** only while nothing is linked: no connection in the
  database, no access token in Keychain, no link still open in another window, and no unsaved
  token in a recovery file. Otherwise the button stays off and the page says why and what to do.
- Problems are always repaired **in place**: Reconnect, Renew consent and "Grant access to
  statement details" reopen the same connection in Plaid's window and never use another one
  ([Reconnecting and renewing](reconnecting.md)).
- If the database is lost or restored from an old backup, the app re-attaches the connection
  saved in Keychain instead of linking again.
- An attempt that fails before the end (you close Plaid's window, the Dashboard checklist below
  isn't done, Amex refuses) creates no access token and uses no connection.
- Sandbox test connections never count.

The rule for you: **never link your Amex a second time to fix something.** Use the Connection
page's repair buttons, `./run doctor --env production`, or [the troubleshooting
list](maintenance.md#troubleshooting).

## Before you start

- [ ] The Sandbox app links a test bank and syncs it, and a second sync adds nothing.
- [ ] FileVault is on.
- [ ] Your Plaid team is on the Trial plan, and the Dashboard shows a Production secret.
- [ ] In Plaid's Dashboard, these three things are done. The Connection page lists them again,
      with links, right before you link:
  1. **A Data Transparency use case is selected**: Dashboard → Link → Customization → Data
     Transparency Messaging. In production, Plaid's window doesn't open without one.
  2. **The App and Company profiles are complete**: Dashboard → Compliance Center, the App
     Profile and Company Profile tabs.
  3. **American Express shows as enabled for OAuth**: the Dashboard's US OAuth institutions page
     (https://dashboard.plaid.com/activity/status/oauth-institutions).

## 1. Save the Production secret

```sh
./run setup --keys-only
```

Press Enter at "Plaid client ID" and "Plaid Sandbox secret" to keep them, and type or paste your
**Production** secret at the third question, in this Terminal only. It goes into your Keychain.

## 2. Install the background agents

```sh
./run install-agents --dry-run     # show the plan; nothing changes
./run install-agents               # do it
```

This builds the production copy of the app and starts the background jobs (macOS "launchd
agents") that keep it running:

1. It makes sure PostgreSQL is running.
2. It installs the **committed** code of this folder into
   `~/Library/Application Support/budgetapp/venv`: it exports the current git commit, stops the
   agents, backs up the production database, installs the packages, upgrades the database and
   records which commit is installed. Changes that aren't committed never reach production: if a
   file git tracks has uncommitted changes, or there are new files under `budgetapp/`, the
   install stops and says so. (Files git ignores, such as CSV downloads, don't matter.)
3. It writes the agents' files to `~/Library/LaunchAgents` and loads them:

| Agent | When it runs |
| --- | --- |
| `postgres` | at login, kept running: the database server |
| `sync` | at 00:15, 06:15, 12:15 and 18:15, and at login; skipped when the last successful sync is under 5 hours old |
| `backup` | at 02:30 and at login; skipped when a verified backup was already made since the last 02:30 |
| `web` (only with `--with-web`) | at login, kept running: the production app at http://127.0.0.1:8765 |

macOS runs a scheduled job it missed while the Mac was asleep as soon as it wakes. Plaid itself
refreshes from Amex one to four times a day, so four syncs a day are plenty.

macOS then shows **"Background Items Added"**. Keep the app's background items allowed in System
Settings → General → Login Items & Extensions → Allow in the Background, or the agents won't run.
macOS doesn't know them as "Budget Tracker" (the name the app's own messages use): it lists them
under the programs they run (Python and sh).

<!-- RECHECK(GOLIVE): the names macOS shows for the agents were inferred from the plists (no app
     name or bundle identifier; ProgramArguments are the install's venv/bin/python and /bin/sh),
     not seen on a Mac: loading agents is off limits while building. After the first
     ./run install-agents, note the names under Allow in the Background and update this
     paragraph, docs/maintenance.md (The background agents, Troubleshooting) and OPS's messages
     (agents.Launchctl.bootstrap, install_agents' last line, doctor's disabled-agent fix). -->

`--with-web` keeps the production app running all the time, so http://127.0.0.1:8765 is always
there. Without it, `./run live` runs the app only while its Terminal window is open. Run
`./run install-agents` again (with or without `--with-web`) to change your mind; it is safe to
repeat.

Then check everything:

```sh
./run doctor --env production
```

macOS may ask whether the app may read each Keychain item: click **Always Allow**. Fix anything
marked FAIL before you link. "Nothing is linked in production yet" is expected at this point.

## 3. Open the live app

```sh
./run live
```

Terminal shows a **LIVE DATA** banner. `./run live` upgrades the installed copy first when the
code has changed since the last install, then opens http://127.0.0.1:8765: the web agent if you
installed it, otherwise a server that runs until you press Ctrl-C. The live app has no coloured
bar at the top; the green **Live** label tells you it is the real one.

## 4. Link your Amex

Open **Connection**:

1. The **Before you link** card repeats the three Dashboard settings, each with a link to Plaid's
   Dashboard, and asks Plaid whether it lists American Express with statement details. If that
   check warns, transactions can still be linked; you may just not get statement balances.
2. Click **Link American Express**. A dialog says **"This uses 1 of your 10 Plaid connections,
   permanently."** Tick each checklist item once it is really done in the Dashboard; then the
   dialog's **Link American Express** button turns on.
3. Plaid's window opens. Choose American Express. Amex's own sign-in opens in a pop-up window:
   sign in there and approve sharing your card with the app. (If nothing opens, allow pop-ups for
   127.0.0.1.) The app never sees your Amex password; Plaid gives it read-only access.
4. When Plaid's window closes, the app saves the access token in your Keychain (and reads it back
   to check), reads the connection and your card, and starts syncing. The page says
   "American Express is linked. Plaid is sending your transactions; two years of history can
   take a few minutes."

Plaid sends recent transactions first and the rest of the history (up to 730 days, the most Plaid
allows, fixed at the link) soon after; sometimes that takes up to an hour. The **History** line on
the Connection page shows where it is: "Waiting for Plaid to prepare your transactions", "Recent
transactions are in; older ones follow", then "All received". The app checks again by itself for
about an hour while it is open; otherwise the next scheduled sync picks it up.

If **Statement details** says "Not shared", click **Grant access to statement details**: a short
Plaid window adds them to the same connection.

### If something goes wrong while linking

Every problem during a link appears in a red box titled **"Plaid Link didn't finish."**, followed
by the reason. The reason tells you whether a connection was used:

- **It begins "The connection was created"**, or a box titled **"Your access token, shown only
  this once"** appears: Plaid created the connection, so it used one of your 10. **Don't link
  again.** Follow the matching bullet below.
- **It says Plaid "may or may not have created the connection"**: Plaid couldn't be reached at
  the last step. Linking stays paused until the time shown; before you link again, check Plaid's
  Dashboard (dashboard.plaid.com) for a new American Express Item, as the message says.
- **Any other reason** (you closed Plaid's window, a Dashboard checklist item isn't done, Amex
  refused, Keychain wanted your approval first): nothing was linked and no connection was used.
  Fix the cause and try again.

The cases where the connection exists:

- **"The connection was created, but its access token couldn't be saved to Keychain."** The app
  wrote the token to a recovery file readable only by you, under
  `~/Library/Application Support/budgetapp/state/recovery`, and the message names the file. Run
  `./run setup --env production --access-token`, paste the token from that file, then click
  **Re-attach existing connection** on the Connection page. Delete the file once the token is also
  in your password manager.
- **"Your access token, shown only this once"**: the rare case where even the recovery file
  couldn't be written. Copy all of it into your password manager right away (it isn't stored
  anywhere else), then follow the message: `./run setup --env production --access-token`, then
  re-attach.
- **"The connection was created, but Keychain already holds a different access token."** Keychain
  already had a token from an earlier link; it was not replaced, and the new token went to a
  recovery file the message names. Re-attach the saved connection on the Connection page, as the
  message says.

Separately, **"Linking is paused."** above a **Link American Express** button that stays off
means one of three things. Usually a production link was started and may still finish: a Plaid
window may still be open somewhere. Finish or close it there, or wait until the time shown (a
started link expires after 4 hours). The banner names the other two when they apply: a token
already saved in Keychain (re-attach it instead), or a token waiting in a recovery file (store it
with `./run setup --env production --access-token`, then re-attach).

### Keep a copy of the access token (recommended)

The access token in Keychain is your connection. If it is ever lost, the only way back without
using another connection is a copy. Once, after linking: open **Keychain Access**, search for
`budgetapp`, open the item whose account is `plaid.access_token.production`, tick **Show
password** and copy the value into your password manager. Don't change the item there, and
never put the token anywhere else. With that copy, `./run setup --env production --access-token`
can restore it at any time; the app checks it with Plaid before saving.

## 5. Import older history

Amex keeps about two years of activity online, and Plaid's history may start later than your
oldest statement. To fill the gap, download your activity as a CSV file from americanexpress.com
and import it on the **Import** page of the live app, or with
`./run import-csv FILE --env production`. Overlapping files are fine: rows Plaid already has are
merged, never added twice. [Using the app → Import](using-the-app.md#import) has the download
steps and what "held for review" means.

You can also import before linking. The rows then go to an account called "American Express
(CSV)", and when you link, the app moves them onto your card by itself, pairing each one with the
matching Plaid charge. If it can't tell which card they belong to, the Connection page asks
(**Imported CSV history** → **Move the history**).

## 6. Check it

- `./run doctor --env production` should show no FAIL.
- The **Connection** page shows "Connected", your balances and the recent syncs.
- When your first statement after going live closes (and settles for a few days), open
  **Statements**, type the numbers from the PDF statement and check that the app matches to the
  cent ([Statements](using-the-app.md#statements)).

## Upgrading the live app

Production never runs code straight from this folder. When the code changes (new commits), the
next `./run live` notices and upgrades the installed copy first: it backs up the production
database (a `…-pre-upgrade.dump` copy, kept apart from the nightly backups), installs the new
commit, upgrades the database while no sync can run, and restarts the agents.
`./run install-agents` does the same. Once the packages are installed an upgrade downloads
nothing, so it also works offline (only a commit that changes `requirements.txt` needs the
internet once).

If the upgrade can't start, `./run live` says why ("Not upgraded: …") and opens the installed
copy as it is: for example when the folder has changes that aren't committed (commit or stash
them, then run `./run live` again to upgrade), or when the backup before the upgrade fails.
`./run install-agents` stops instead.

Until then, the old installed copy keeps running. If the live app ever shows **"Upgrade
needed"**, run `./run live` in Terminal and reload the page.

## Stopping the agents

```sh
./run uninstall-agents --dry-run   # show the plan
./run uninstall-agents             # stop and remove the agents
```

This stops PostgreSQL and the agents and deletes their files from `~/Library/LaunchAgents`. The
installed copy, your database and your backups stay. While the agents are off nothing syncs or
backs up, and the connection itself is untouched.
