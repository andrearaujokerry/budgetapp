# Alerts and notifications

<!-- RECHECK(ALR): this guide describes the alerts from the requirements (ALR-1..3, SYNC-6) and
     ARCHITECTURE §11. The ALR package (services/alerts.py) was being built while it was written;
     check each rule and its wording against the code after the merge (the backup alert and the
     Keychain notice already exist: services/backup.py, commands/sync.py). -->

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

| Alert | When | How often |
| --- | --- | --- |
| **Budget reached** | a category's spending reaches one of your percentages of its budget this period (80% and 100% unless you change them) | once per category, period and percentage |
| **Sync failed** | a sync of your Amex fails (Plaid, Amex or the app had a problem) | once per error and day |
| **Reconnect needed** | Amex needs you to sign in again ([Reconnecting](reconnecting.md#reconnect)) | once each time it happens |
| **Data is stale** | no successful sync for 36 hours | once a day |
| **Consent renewal due** | the yearly Amex consent runs out within your reminder days (30 unless you change it) | once per due date |
| **Large charge** | a new charge over the amount you set | once per charge |
| **Keychain approval needed** | macOS asks before the background agents may read the app's Keychain items | once a day |
| **Backup failed or stale** | the nightly backup failed, or the newest backup is over 26 hours old | once a day |

Details:

- **Budget alerts** use exactly the numbers on the Budgets page: the same period (calendar month
  or statement cycle), the same pending setting, and the budget including any rollover. A
  category whose budget is zero (or below zero after an overspent rollover) alerts on its first
  spending. They are checked after every sync, after a CSV import and after you change a
  category. If spending drops back below a percentage (a pending hold that disappeared), that
  alert can fire again later.
- **Large charges** are checked for new charges from a sync, dated within the last 7 days, and
  not while Plaid sends your history right after linking. A pending charge that posts isn't
  announced a second time, and CSV imports never raise one.
- **Keychain approval needed** asks you to run `./run doctor --env production` in Terminal and
  click **Always Allow** for each item (usually after a `brew upgrade` of Python). The sync and
  backup agents simply try again later.
- **Backup failed or stale** says to run `./run doctor --env production`;
  [Backups](maintenance.md#backups) has the details.

## Choose what you get

Settings → **Alerts**:

| Setting | Default | What it does |
| --- | --- | --- |
| Budget alerts | on | budget alerts at all |
| Alert at these percentages | 80, 100 | whole percentages from 1 to 200, up to five, separated by commas |
| Sync problem alerts | on | sync failures, stale data and reconnect alerts |
| Large-charge alert (optional) | blank (off) | the amount above which a new charge is announced |
| Consent renewal reminder | 30 days | how long before the yearly consent runs out to start reminding you (1–120) |

Turning budget alerts off keeps your percentages for when you turn them back on. The reconnect
and consent banners in the app show whether notifications are on or off; the consent banner
starts the same number of days before the due date as the reminder.
