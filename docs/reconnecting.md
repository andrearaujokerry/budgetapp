# Reconnecting and renewing

Your Amex is linked once ([Going live](going-live.md)). After that, every repair happens **on the
same connection**: Plaid reopens it in a short window and you sign in to Amex again. A repair
never uses another of your 10 Plaid connections, and your transactions, categories, notes and
budgets stay as they are. While a connection is broken nothing new arrives, but nothing is lost
either: the next successful sync catches up on everything that happened in between.

**Never link your Amex a second time to fix a problem.** The app won't offer it anyway while a
connection exists.

## Reconnect

**When:** Amex or Plaid needs you to sign in again: you changed your Amex password, Amex locked
the sign-in, you withdrew the app's access in your Amex account, or the yearly consent ran out.

**How you notice:**

- the top bar says **Reconnect needed** (red);
- every page shows "Your Amex connection needs attention. Syncing is paused until you sign in to
  Amex again through Plaid. Your existing data is safe.";
- a Mac notification, if sync problem alerts are on ([Alerts and notifications](alerts.md));
- `./run doctor --env production` reports "American Express needs you to reconnect";
- the code under **Recent syncs** on the Connection page, and in `./run sync`'s "failed (…)"
  line, is one of these: `ITEM_LOGIN_REQUIRED` (the usual one), `PENDING_EXPIRATION`,
  `PENDING_DISCONNECT`, `INVALID_CREDENTIALS`, `ITEM_LOCKED`, `ACCESS_NOT_GRANTED`,
  `USER_PERMISSION_REVOKED` or `USER_SETUP_REQUIRED`.

**What to do:**

1. Open **Connection** (or click **Reconnect** in the banner).
2. The **Reconnect needed.** box explains what Amex or Plaid said. Click **Reconnect**.
3. Plaid's window opens for your existing connection. Sign in to American Express in the pop-up
   and allow access to your card again.
4. The page says "Your Amex connection is working again. Syncing now." The app syncs at once and
   once more shortly after.

The app keeps only Plaid's access token, never your Amex password. Reconnecting doesn't change the
token, so Keychain needs no update.

## Renew the yearly consent

American Express asks you to confirm the app's access **once a year**. If the consent runs out,
syncing stops until you renew it.

- **The due date** comes from Plaid when Amex reports it; otherwise the app estimates it as a year
  after you last linked or renewed, and the Connection page says "(estimated: a year after you
  last confirmed)".
- **Reminders start 30 days before** (change the number under Settings → Alerts → Consent
  renewal reminder): every page shows "Renew your Amex connection by *date* (*N* days left).",
  and a Mac notification arrives once for each due date, even with sync problem alerts off (but
  not while the connection needs reconnecting: reconnecting renews the consent too).
- **To renew:** Connection → **Renew consent** (or **Renew now** in the banner). Plaid's window
  opens for the same connection; sign in to Amex and confirm. The page then says "Access is
  renewed until *date*."
- **If you miss it,** the banner says "Your Amex consent has expired." and syncing stops.
  **Renew now** in the banner (or **Renew access** in the top bar) fixes it the same way, and the
  next sync fetches what you missed.

## Statement details

When the Connection page's **Statement details** line says **Not shared**, Amex didn't share the
statement balance, minimum payment and due date (Plaid's "Liabilities"). Click **Grant access to
statement details**: a short Plaid window adds them to the same connection. Transactions keep
syncing either way. If the page says "American Express doesn't provide them for this card", there
is nothing to do.

## Sync failures that fix themselves

When a sync fails and no reconnect is needed, the Connection page shows **"The last sync
failed (CODE)."** with an explanation: Plaid's own words when Plaid sends some, otherwise the
app's. The same code appears under **Recent syncs**, in the "failed (CODE)" line of `./run sync`,
and in the **Sync failed** notification. These codes clear up on the next scheduled sync:

| Code | What it means | The app's explanation |
| --- | --- | --- |
| `TRANSPORT_ERROR` | no internet, or Plaid was down | "Plaid couldn't be reached. The next sync will try again." |
| `INSTITUTION_NOT_RESPONDING`, `INSTITUTION_DOWN` or another `INSTITUTION_…` code | an outage at Amex | "Amex isn't answering Plaid right now. The next sync will try again." |
| `INTERNAL_SERVER_ERROR`, `PLANNED_MAINTENANCE` | an outage or maintenance at Plaid | "Plaid had a problem on its side. The next sync will try again." |
| a code ending in `_LIMIT`, such as `TRANSACTIONS_SYNC_LIMIT` or `RATE_LIMIT` | too many requests in a short time | "Plaid asked the app to slow down. The next sync will try again." |

Other codes:

| Code | What it means | What to do |
| --- | --- | --- |
| `INTERNAL` | "Something went wrong in the app during the sync. The details are in the sync log." | The next sync tries again. If it keeps happening, read `sync-production.log` (or `web-production.log` after **Sync now**) in `~/Library/Logs/budgetapp` |
| `TRIAL_CONNECTION_LIMIT` | "Your Plaid Trial plan has used all 10 connections; Plaid refuses new ones." | Keep repairing the connection you have in place; a new link isn't possible |
| `INVALID_ACCESS_TOKEN`, `ITEM_NOT_FOUND`, `ACCESS_TOKEN_MISSING`, `KEYCHAIN_APPROVAL_NEEDED` | a problem with the connection or its token | See [the next section](#when-the-connection-or-its-token-is-missing) |
| anything else | Plaid's explanation is shown | The next sync tries again; if the same code keeps coming back, run `./run doctor --env production` |

**"Your data may be out of date."** means no successful sync for over a day and a half. Check that
the Mac was awake and online, then click **Sync now**. If it keeps happening, run
`./run doctor --env production`: the sync agent may not be installed or allowed (see [the
launchd agents](maintenance.md#the-background-agents)).

## When the connection or its token is missing

| Situation | What you see | What to do |
| --- | --- | --- |
| The database was restored from an old backup or rebuilt, but Keychain still has the token | Connection: **"A saved connection was found."** | Click **Re-attach existing connection**. The app rebuilds the connection from Plaid and syncs your history from the beginning; no new connection is used |
| The token is missing from Keychain, but the database knows the connection | "The access token is missing from Keychain." (`ACCESS_TOKEN_MISSING`) | `./run setup --env production --access-token` and paste the copy from your password manager |
| The token was written to a recovery file during the link | "Linking is paused." naming the file | `./run setup --env production --access-token`, paste the token from the file, then **Re-attach existing connection** |
| macOS asks for Keychain approval (often after a `brew upgrade` of Python) | "Keychain approval needed" page, or a notification (`KEYCHAIN_APPROVAL_NEEDED`) | `./run doctor --env production` in Terminal, and click **Always Allow** for each item |
| "Plaid didn't accept the saved access token." | the last sync failed with `INVALID_ACCESS_TOKEN` | `./run doctor --env production` to check the keys; make sure the Production secret belongs to the same Plaid team |
| "Plaid no longer has this connection (it was removed)." | the last sync failed with `ITEM_NOT_FOUND` | Syncing can't continue; the data already here stays. Linking again would use another of your 10 connections, so think it through first |

`./run setup --env production --access-token` asks for the token with hidden typing, checks it
with Plaid first, saves it in Keychain and never replaces a different token that is already
there.

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
`user_transactions_dynamic` with any password). Sandbox test connections also expire after 30
days and are repaired the same way.
