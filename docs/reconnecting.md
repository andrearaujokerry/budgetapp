# Reconnecting and renewing

Each bank or card company is linked once ([Going live](going-live.md)). After that, every repair
happens **on the same connection**: Plaid reopens it in a short window and you sign in to your
bank again. A repair never uses another of your 10 Plaid connections, and your transactions,
categories, notes and budgets stay as they are. While a connection is broken nothing new arrives
from that bank, but nothing is lost either: the next successful sync catches up on everything that
happened in between. Your other connections keep syncing meanwhile.

**Never link the same bank login a second time to fix a problem.** The app refuses it anyway, before
anything is created.

## Reconnect

**When:** a bank or Plaid needs you to sign in again: you changed your password at the bank, the
bank locked the sign-in, you withdrew the app's access at your bank, or the consent ran out.

**How you notice:**

- the top bar says **Reconnect needed**, and its tooltip names the bank;
- every page shows "*Bank* needs attention. Syncing *bank* is paused until you sign in again
  through Plaid. Your existing data is safe." (with several banks: "*Bank* and *bank* need
  attention.");
- a Mac notification, if sync problem alerts are on ([Alerts and notifications](alerts.md));
- `./run doctor --env production` reports "*Bank* needs you to reconnect";
- the code under **Recent syncs** on the Connection page, and in `./run sync`'s "failed (…)"
  line, is one of these: `ITEM_LOGIN_REQUIRED` (the usual one), `PENDING_EXPIRATION`,
  `PENDING_DISCONNECT`, `INVALID_CREDENTIALS`, `ITEM_LOCKED`, `ACCESS_NOT_GRANTED`,
  `USER_PERMISSION_REVOKED` or `USER_SETUP_REQUIRED`.

**What to do:**

1. Open **Connection** (or click **Reconnect** in the banner).
2. The bank's **Reconnect needed.** box explains what the bank or Plaid said.
   Click **Reconnect**.
3. Plaid's window opens for your existing connection. Sign in to your bank (many banks open their
   own sign-in in a pop-up) and allow access to your accounts again.
4. The page says "Your *bank* connection is working again. Syncing now." The app syncs at once and
   once more shortly after.

Each connection also has a **Reconnect** button when nothing is wrong, for example right after
you changed your password at the bank. The app keeps only Plaid's access token, never your bank
password. Reconnecting doesn't change the token, so Keychain needs no update.

## Renew consent

Some banks ask you to confirm the app's access from time to time. **American Express asks once a
year.** If a consent runs out, syncing that bank stops until you renew it.

- **The due date** comes from Plaid when the bank reports one. For American Express, when Plaid
  sends none, the app estimates it as a year after you last linked or renewed, and the Connection
  page says "(estimated: a year after you last confirmed)". Another bank without a date from Plaid
  has no end date: its Connection page says "No end date" and no reminder is sent.
- **Reminders start 30 days before** (change the number under Settings → Alerts → Consent
  renewal reminder): every page shows "Renew your *bank* connection by *date* (*N* days left).",
  and a Mac notification arrives once for each due date, even with sync problem alerts off (but
  not while the connection needs reconnecting: reconnecting renews the consent too).
- **To renew:** Connection → **Renew consent** on that bank's connection (or **Renew now** in the
  banner). Plaid's window opens for the same connection; sign in and confirm. The page then says
  "Access is renewed until *date*." when Plaid reports the new date.
- **If you miss it,** the banner says "Your *bank* consent has expired." and syncing that bank
  stops. **Renew now** in the banner (or **Renew access** in the top bar) fixes it the same way,
  and the next sync fetches what you missed.

## Add accounts

To share more accounts of a login you already linked (a new card, a savings account you opened,
an account you didn't tick the first time), open **Connection** and click **Add accounts** on that
bank's connection. Plaid's window opens for the same connection and lists the login's checking,
savings, money market and credit-card accounts; pick the ones to share. No new connection is used.
The page says "Your account choice at *bank* is saved. Syncing now: accounts you added appear once
Plaid sends them." An account you untick there stops being shared with the app; what it already
synced stays.

## Statement details

Statement details (Plaid's "Liabilities": the statement balance, minimum payment and due date)
exist only for credit cards. When a card connection's **Statement details** line says **Not
shared**, the bank didn't share them. Click **Grant access to statement details**: a short Plaid
window adds them to the same connection. Transactions keep syncing either way. If the page says
"*Bank* doesn't provide them", there is nothing to do. A connection with only bank accounts has no
statement details line at all, and `./run doctor` says they don't apply.

## Sync failures that fix themselves

When a sync fails and no reconnect is needed, the Connection page shows **"The last sync
failed (CODE)."** with an explanation: Plaid's own words when Plaid sends some, otherwise the
app's. The same code appears under **Recent syncs**, in the "failed (CODE)" line of `./run sync`,
and in the **Sync failed** notification. These codes clear up on the next scheduled sync:

| Code | What it means | The app's explanation |
| --- | --- | --- |
| `TRANSPORT_ERROR` | no internet, or Plaid was down | "Plaid couldn't be reached. The next sync will try again." |
| `INSTITUTION_NOT_RESPONDING`, `INSTITUTION_DOWN` or another `INSTITUTION_…` code | an outage at the bank | "*Bank* isn't answering Plaid right now. The next sync will try again." |
| `INTERNAL_SERVER_ERROR`, `PLANNED_MAINTENANCE` | an outage or maintenance at Plaid | "Plaid had a problem on its side. The next sync will try again." |
| a code ending in `_LIMIT`, such as `TRANSACTIONS_SYNC_LIMIT` or `RATE_LIMIT` | too many requests in a short time | "Plaid asked the app to slow down. The next sync will try again." |

Other codes:

| Code | What it means | What to do |
| --- | --- | --- |
| `INTERNAL` | "Something went wrong in the app during the sync. The details are in the sync log." | The next sync tries again. If it keeps happening, read `sync-production.log` (or `web-production.log` after **Sync now**) in `~/Library/Logs/budgetapp` |
| `TRIAL_CONNECTION_LIMIT` | "Your Plaid Trial plan has used all 10 connections; Plaid refuses new ones." | Keep repairing the connections you have in place, and add accounts to them with **Add accounts**; a new link isn't possible |
| `INVALID_API_KEYS` | "Plaid didn't accept the app's Plaid keys." | Run `./run setup --keys-only` and type the client ID and the secret again, both from the same Plaid team (Developers → Keys in Plaid's Dashboard; after you rotated the secret, the new one), then click **Sync now** |
| `INVALID_ACCESS_TOKEN`, `ITEM_NOT_FOUND`, `ACCESS_TOKEN_MISSING`, `KEYCHAIN_APPROVAL_NEEDED` | a problem with the connection or its token | See [the next section](#when-the-connection-or-its-token-is-missing) |
| anything else | Plaid's explanation is shown | The next sync tries again; if the same code keeps coming back, run `./run doctor --env production` |

**"Your data may be out of date."** means no successful sync for over a day and a half. Check that
the Mac was awake and online, then click **Sync now**. If it keeps happening, run
`./run doctor --env production`: the sync agent may not be installed or allowed (see [the
launchd agents](maintenance.md#the-background-agents)).

## When the connection or its token is missing

| Situation | What you see | What to do |
| --- | --- | --- |
| The database was restored from an old backup or rebuilt, but Keychain still has a connection's token | Connection: **"A saved connection was found."** | Click **Re-attach existing connection**. The app asks Plaid which bank each saved token belongs to, rebuilds those connections and syncs their history from the beginning; no new connection is used. Linking another bank waits until this is done |
| A connection's token is missing from Keychain, but the database knows the connection | "The access token is missing from Keychain." (`ACCESS_TOKEN_MISSING`), and `./run doctor --env production` names the bank | `./run setup --env production --access-token` and paste that connection's copy from your password manager |
| The token was written to a recovery file during the link | "Linking is paused." naming the file | `./run setup --env production --access-token`, paste the token from the file, then **Re-attach existing connection** |
| macOS asks for Keychain approval (often after a `brew upgrade` of Python) | "Keychain approval needed" page, or a notification (`KEYCHAIN_APPROVAL_NEEDED`) | `./run doctor --env production` in Terminal, and click **Always Allow** for each item |
| "Plaid didn't accept the saved access token." | the last sync failed with `INVALID_ACCESS_TOKEN` | `./run doctor --env production` to check the keys; make sure the Production secret belongs to the same Plaid team |
| "Plaid no longer has this connection (it was removed)." | the last sync failed with `ITEM_NOT_FOUND` | Syncing that bank can't continue; the data already here stays. Linking it again would use another of your 10 connections, so think it through first |

`./run setup --env production --access-token` asks for the token with hidden typing, checks it
with Plaid first (Plaid also says which connection it belongs to), saves it in Keychain under that
connection's own name and never replaces a different token that is already there.

Restoring a backup brings back your edits; re-attaching alone can't, because it only rebuilds what
Plaid knows. If you have a backup, [restore it](maintenance.md#restore-a-backup) first. A backup
made after you linked already knows the connection, so no re-attach is needed.

## Practise in the Sandbox

Everything above can be tried safely with test data:

```sh
./run sandbox-reset-login    # the test connection now needs a reconnect
```

Then open http://127.0.0.1:8766/connection and click **Reconnect**. Sign in with the test
user the connection was made with (for example `user_good` / `pass_good`, or
`user_transactions_dynamic` with any password). **Add accounts** and **Renew consent** work the
same way on a test connection. Sandbox test connections also expire after 30 days and are
repaired the same way.
