# Alerts and notifications

The app tells you about money and connection problems with Mac notifications. Each alert is sent
**once**: the app records it, so a later sync doesn't repeat it.

## Turn notifications on (one time)

macOS shows the app's notifications under **Script Editor**'s name and icon, and silently drops
them until Script Editor is allowed to notify. Once:

```sh
./run notify-test
```

Then open System Settings → Notifications → **Script Editor**, turn on **Allow notifications** and
choose the **Alerts** style, so problems stay on screen until you dismiss them. Run
`./run notify-test` again to check. The **Send test notification** button under Settings → Mac
notifications does the same. `./run doctor` warns while this isn't set up ("Mac notifications
aren't set up yet (alerts are dropped)").

Most alerts come from the scheduled syncs and backups of the background agents
([Going live](going-live.md#2-install-the-background-agents)); the rest from the app while it is
open, after **Sync now** or a change you make. Demo data never sends any.

## What you are told

| Alert | The notification says, for example | When | How often |
| --- | --- | --- | --- |
| **Budget reached** | "Groceries: 80% of budget used", "Groceries: over budget", "Travel: 80% of the 2026 budget used", "Overall budget: 80% used" | a budget reaches one of your percentages this period, or this year for an annual budget (80% and 100% unless you change them) | once per budget, period and percentage |
| **Sync failed** | "American Express sync failed" | a sync of your Amex fails (Plaid, Amex or the app had a problem); the message names the error code | once per error code and day |
| **Reconnect needed** | "Reconnect American Express" | Amex needs you to sign in again ([Reconnecting](reconnecting.md#reconnect)) | once each time it happens |
| **Data is stale** | "American Express data is out of date" | no successful sync for 36 hours | once a day (folded into the failure or reconnect alert when one goes out in the same sync) |
| **Consent renewal due** | "Renew your American Express connection" | the yearly Amex consent runs out within your reminder days (30 unless you change it) | once per due date; not while the connection needs reconnecting (reconnecting renews the consent too) |
| **Large charge** | "$612.40 at Delta" | new spending over the amount you set, on a card or a bank account, or a pending charge that posts above it | once per charge; more than three in one sync arrive as one summary ("5 new charges over $500.00") |
| **Keychain approval needed** | "Budget Tracker needs Keychain approval" | macOS asks before the background agents may read the app's Keychain items | once a day |
| **Keychain item missing** | "Budget Tracker can't sync" | the sync agent can't find the database password or the Plaid keys in Keychain (setup isn't finished, or the login Keychain was reset); the message names the command that saves it: `./run setup` for the password, `./run setup --keys-only` for the keys | once a day |
| **Backup failed or stale** | "Budget Tracker backup problem" | the nightly backup failed, or the newest backup is over 26 hours old | once a day |
| **Upgrade needed** | "Budget Tracker needs an upgrade" | the sync agent finds the database needs an upgrade (for example after restoring an older backup); syncing waits until you run `./run live` | once a day |

Details:

- **Budget alerts** use exactly the numbers on the Budgets page: the same period (calendar month
  or statement cycle), the same pending setting, and the budget including any rollover. Each
  monthly category budget, the overall target (if you set one) and each annual budget (for the
  calendar year) is checked. A budget of zero (or below zero after an overspent rollover) alerts
  on its first spending. If one change crosses several percentages at once, you are told about
  the highest. If spending drops back below a percentage (a pending hold that disappeared), that
  alert can fire again later.
- **When budgets are checked:** after every successful sync, after a CSV import, and right after
  you change which category a charge counts in: a category click, **Reset to automatic**, a
  split, a rule saved with **Also apply to past transactions** (on the Rules page or from a
  transaction), **Re-apply rules to past transactions**, `./run recategorize`, or a merge. A new
  budget amount, new percentages or another budget period count from the next sync, within about
  six hours.
- **Large charges** are checked for new spending from a sync that is strictly over your amount
  (a charge in a spending category, on a card or from a bank account, such as rent or an ATM
  withdrawal; paying your card from checking, moving money between your own accounts and income
  never count), dated at most 7 days before the previous successful sync (so the first sync after
  a break, such as a trip with the Mac off or a reconnect fixed late, still announces the break's
  charges), and not while Plaid sends your history right after linking. Each charge is announced
  once: a pending charge that posts isn't announced a second time. A pending charge at or under
  your amount that goes above it (a tip or hotel incidentals added when it posts, or Plaid
  raising the pending amount) is announced once, when it goes above. CSV imports never raise
  one.
- **Keychain approval needed** asks you to run `./run doctor --env production` in Terminal and
  click **Always Allow** for each item (usually after a `brew upgrade` of Python). The sync and
  backup agents simply try again later.
- **Backup failed or stale** says to run `./run doctor --env production`;
  [Backups](maintenance.md#backups) has the details. The backup agent sends it when a backup
  fails; the sync agent sends it when backups have stopped because the backup agent no longer
  runs.

## Choose what you get

Settings → **Alerts**:

| Setting | Default | What it does |
| --- | --- | --- |
| Budget alerts | on | budget alerts at all |
| Alert at these percentages | 80, 100 | whole percentages from 1 to 200, up to five, separated by commas |
| Sync problem alerts | on | sync failures, stale data and reconnect alerts (the consent reminder comes either way) |
| Large-charge alert (optional) | blank (off) | the amount above which spending (a charge, rent, an ATM withdrawal) is announced, once per charge; card payments, transfers and income never are |
| Consent renewal reminder | 30 days | how long before the yearly consent runs out to start reminding you (1–120) |

Turning budget alerts off keeps your percentages for when you turn them back on. The consent
banner in the app starts the same number of days before the due date as the reminder.
