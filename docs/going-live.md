# Going live

This guide links your real banks and cards and sets the app up to run by itself. Do it after the
Sandbox works ([Setting up](setup.md)): test connections are free, real ones are not.

All commands run in Terminal from the app's folder (`cd ~/Desktop/budgetapp`).

## One of your 10 Plaid connections, permanently

Plaid's free Trial plan allows **10 real connections ("Items") in total, for good**. Each bank
login you link uses one of the 10, and it stays used even if the connection is removed later:
deleting it never gives the slot back. Linking the same login a second time uses another one, and
at some banks it also breaks the first link.

So the app treats every production link as a one-time event per bank login:

- It offers **Link a bank or card** while no link is still open in another window, no unsaved
  token waits in a recovery file, no saved connection waits to be re-attached, and fewer than 10
  connections are used. Otherwise the button stays off and the page says why and what to do. The
  Connection page shows how many of the 10 the app has created so far.
- When Plaid's window closes, the app compares the bank you picked with the banks you have
  linked. The same bank again is refused before its token is exchanged, the step that uses one of
  your 10: to share more accounts of that login, use **Add accounts** on its connection; to repair
  it, use **Reconnect**. If it really is a different login at the same bank (someone else's, or a
  business login), tick **"A different login at a bank I've already linked"** in the dialog before
  you link. Even then, an account whose name and last digits match one already linked there is
  refused, because that is the same login again. The refusal can't undo what happened in Plaid's
  window: at some banks (Plaid names Chase, PNC, Navy Federal and Schwab) signing in to the same
  login a second time already breaks the first connection. If that happens, its pill says
  **Reconnect needed**: use **Reconnect** on it.
- Problems are always repaired **in place**: Reconnect, Renew consent, Add accounts and "Grant
  access to statement details" reopen the same connection in Plaid's window and never use another
  one ([Reconnecting and renewing](reconnecting.md)).
- If the database is lost or restored from an old backup, the app re-attaches the connections
  saved in Keychain instead of linking them again.
- An attempt that fails before the end (you close Plaid's window, the Dashboard checklist below
  isn't done, the bank refuses) creates no access token and uses no connection.
- Sandbox test connections never count.

The rule for you: **never link the same bank login twice, to fix something or to add an
account.** Use the Connection page's buttons, `./run doctor --env production`, or [the
troubleshooting list](maintenance.md#troubleshooting).

## Before you start

- [ ] The Sandbox app links a test bank and syncs it, and a second sync adds nothing.
- [ ] FileVault is on.
- [ ] Your Plaid team is on the Trial plan, and the Dashboard shows a Production secret.
- [ ] In Plaid's Dashboard, these two things are done. The Connection page lists them again,
      with links, right before you link:
  1. **A Data Transparency use case is selected**: Customize → Link, the Data Transparency
     Messaging section (https://dashboard.plaid.com/link/data-transparency-v5). If no use case is
     selected, pick the one closest to budgeting and save. Without one, Plaid's window fails in
     production ("something went wrong") before you pick a bank. That creates no connection, so if
     you can't find the setting, trying the link costs nothing.
  2. **Your team may use banks that sign in through their own window (OAuth)**: Institutions →
     OAuth institutions (https://dashboard.plaid.com/activity/status?tab=oauth). On the Trial plan
     it says "No action is needed to access banks through Plaid's free trial", so there is nothing
     to do. On a paid plan, your bank must show as enabled there, and Plaid also asks for the App
     and Company profiles under Settings.

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
refreshes from your banks one to four times a day, so four syncs a day are plenty.

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
bar at the top; the **Live** label under the app's name tells you it is the real one.

## 4. Link your first bank or card

Open **Connection**:

1. The **Before you link** card repeats the two Dashboard settings, each with a link to Plaid's
   Dashboard, and checks that Plaid answers with your Production keys. If that check warns, you
   can still try: a link that fails before the end uses no connection.
2. Click **Link a bank or card**. A dialog says **"This uses 1 of your 10 Plaid connections,
   permanently."** and how many the app has created so far. Tick each checklist item once it is
   really done in the Dashboard; then the dialog's **Link a bank or card** button turns on. Leave
   "A different login at a bank I've already linked" off for a bank you haven't linked yet.
3. Plaid's window opens. Pick your bank or card company, for example American Express, and the
   accounts to share. Plaid offers checking, savings and money market accounts and credit cards;
   investment and loan accounts are left out. Many banks open their own sign-in in a pop-up
   window: sign in there and approve sharing your accounts with the app. (If nothing opens, allow
   pop-ups for 127.0.0.1.) The app never sees your bank password; Plaid gives it read-only access.
4. When Plaid's window closes, the app checks that this bank isn't linked already, saves the
   access token in your Keychain under the connection's own name (and reads it back to check),
   reads the connection and your accounts, and starts syncing. The page says, for example,
   "American Express is linked. Plaid is sending your transactions; two years of history can take
   a few minutes."

Plaid sends recent transactions first and the rest of the history (up to 730 days, the most Plaid
allows, fixed at the link) soon after; sometimes that takes up to an hour. The **History** line on
the Connection page shows where it is: "Waiting for Plaid to prepare your transactions", "Recent
transactions are in; older ones follow", then "All received". The app checks again by itself for
about an hour while it is open; otherwise the next scheduled sync picks it up.

For a card, if **Statement details** says "Not shared", click **Grant access to statement
details**: a short Plaid window adds them to the same connection. Bank accounts have no statement
details; their connection shows current and available balances instead.

### If something goes wrong while linking

Every problem during a link appears in a red box titled **"Plaid Link didn't finish."**, followed
by the reason. The reason tells you whether a connection was used:

- **It begins "The connection was created"**, or a box titled **"Your access token, shown only
  this once"** appears: Plaid created the connection, so it used one of your 10. **Don't link
  that login again.** Follow the matching bullet below.
- **It says Plaid "may or may not have created the connection"**: Plaid couldn't be reached at
  the last step. Linking stays paused until the time shown; before you link again, check Plaid's
  Dashboard (dashboard.plaid.com) for a new Item at that bank, as the message says.
- **It says the bank "is already linked"**: you picked a bank you have linked before, so the app
  stopped before exchanging the token, the step that uses a connection. Use **Add accounts** or
  **Reconnect** on that bank's connection instead, or, if it really is another login, link again
  with "A different login at a bank I've already linked" ticked. If that bank's connection now
  says **Reconnect needed** (signing in again can break the first link at some banks), click
  **Reconnect** on it.
- **Any other reason** (you closed Plaid's window, a Dashboard checklist item isn't done, the bank
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
  already had a token under this connection's name; it was not replaced, and the new token went
  to a recovery file the message names. Re-attach the saved connection on the Connection page, as
  the message says.

Separately, **"Linking is paused."** above a **Link a bank or card** button that stays off means
one of these. Usually a production link was started and may still finish: a Plaid window may
still be open somewhere. Finish or close it there, or wait until the time shown (a started link
expires after 4 hours). The banner names the others when they apply: a token waiting in a
recovery file (store it with `./run setup --env production --access-token`, then re-attach), a
token in Keychain whose connection this database doesn't show (re-attach it first, so the app
knows which bank it is), or all 10 connections used.

### Keep a copy of each access token (recommended)

Each connection's access token in Keychain is that connection. If one is ever lost, the only way
back without using another connection is a copy. Once, after each link: on the Connection page,
the connection's **Keychain item** line names its item, `plaid.access_token.production.<id>`
(one per connection; `<id>` is Plaid's id for it). Open **Keychain Access**, search for
`budgetapp`, open the item with that account name, tick **Show password** and copy the value into
your password manager, labelled with the bank. Don't change the item there, and
never put the token anywhere else. With that copy, `./run setup --env production --access-token`
can restore it at any time; the app checks it with Plaid, which also says which connection it
belongs to, before saving.

## 5. Import older history

American Express keeps about two years of activity online, and Plaid's history may start later
than your oldest statement. To fill the gap, download your Amex activity as a CSV file from
americanexpress.com and import it on the **Import** page of the live app, or with
`./run import-csv FILE --env production`. Overlapping files are fine: rows Plaid already has are
merged, never added twice. [Using the app → Import](using-the-app.md#import) has the download
steps and what "held for review" means.

You can also import before linking. The rows then go to an account called "American Express
(CSV)", and when you link American Express, the app moves them onto your Amex card by itself,
pairing each one with the matching Plaid charge. Linking another bank or card leaves them alone.
If the app can't tell which Amex card they belong to, the Connection page asks (**Imported CSV
history** → **Move the history**).

## 6. Check it

- `./run doctor --env production` should show no FAIL.
- The **Connection** page shows each connection as "Connected", with its balances and the recent
  syncs.
- When your first card statement after going live closes (and settles for a few days), open
  **Statements**, type the numbers from the PDF statement and check that the app matches to the
  cent ([Statements](using-the-app.md#statements)).

## Link another bank or card

Link each bank or card company the same way as the first, from the Connection page's **Link a
bank or card** button.

- **Each bank login uses 1 of your 10 Plaid connections, for good.** The page's header says how
  many the app has created so far, and the dialog says it again before Plaid's window opens.
- **Never link the same login twice.** One login usually covers all its accounts (checking,
  savings, a card at the same bank). The app refuses a bank you have already linked before
  exchanging its token (the step that uses one of the 10), unless you tick "A different login at a
  bank I've already linked" for a genuinely different login. Don't count on the refusal, though:
  at some banks, signing in to the same login again in Plaid's window can break the first
  connection before the app sees anything (then use **Reconnect** on it).
- **New accounts at a bank you already linked** (a new card, a new savings account): click **Add
  accounts** on that bank's connection. Plaid's window opens for the same connection and lets you
  pick the accounts to share. No new connection is used, and the next sync brings the new
  accounts in.
- Each connection has its own Keychain item, its own status pill, its own Reconnect and Renew
  buttons and its own entry in `./run doctor --env production`; a problem with one never stops
  the others from syncing.

### After your first bank account syncs

Check how its money is filed. Each transaction's details say how it counts (**Counts as**).

- **Your rules apply to bank rows too** (all but a card payment Plaid marks as one), and each
  rule's preview on the Rules page lists the bank rows it matches.
- **Budgets count spending from checking.** Your overall target and every budget, annual and
  rollover ones included, count the bank's spending from its first sync on: debit purchases in
  their categories, rent and a mortgage in Home, a car loan in Transportation and a student loan
  in Education. Budgets are checked right after that first sync, so budget alerts can arrive at
  once (large-charge alerts skip the history Plaid sends when you link). If your target was sized
  for your card, raise it before you link, or move those rows with a rule and **Also apply to
  past transactions** (a Rent category of your own and a rule on your landlord's name, say, keep
  rent apart from what you buy for your home).
- **A large-charge alert amount now covers spending from checking too**, so rent above it alerts
  every month.
- **The payment to your card from checking must count as a card payment**, or that card's
  charges count twice. Open one (filter Transactions by your checking account and search for the
  card company's name): **Counts as** should say **"A card payment, not spending"**. If it says
  Spending, Plaid filed it under another category: set its category to Card Payments, then use
  **Always categorize like this** on it with **Also apply to past transactions**, so every
  payment follows (otherwise each one also sets off a large-charge alert).
- **Transfers aren't only between your own accounts.** Plaid also files payments to people (Zelle,
  Venmo, Cash App), wires, and check or cash deposits as transfers, and a transfer counts as
  neither spending nor income. Filter Transactions by the Transfers category and make a rule for
  each kind that is really spending or income (for example a description containing ZELLE PAYMENT
  TO and the person's name, with the category that payment belongs in).
- **Link every card you pay from checking.** A payment from checking to a card the app doesn't
  have is filed as a card payment too, so that card's spending counts nowhere. If you'd rather not
  link the card, set each payment's category yourself (rules don't change card payments).
- **Paychecks, interest and other money in** show as income on the Dashboard and Trends
  ([Using the app](using-the-app.md#dashboard)).

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

The upgrade to the version that links banks keeps your linked American Express as it is; nothing
needs linking again. Its access token was saved as `plaid.access_token.production`; the first sync
afterwards moves it to the connection's own Keychain item (it writes the new item, reads it back,
then deletes the old one). If macOS asks, click **Always Allow**. If the move can't happen, the old
item keeps working, linking another bank waits, and `./run doctor --env production` says what to
do. If only the old item couldn't be deleted (the background agents can't ask for your approval),
nothing waits: every later sync tries again, and `./run sync --env production` in Terminal
deletes it once you click **Always Allow**.

## Stopping the agents

```sh
./run uninstall-agents --dry-run   # show the plan
./run uninstall-agents             # stop and remove the agents
```

This stops PostgreSQL and the agents and deletes their files from `~/Library/LaunchAgents`. The
installed copy, your database and your backups stay. While the agents are off nothing syncs or
backs up, and the connection itself is untouched.
