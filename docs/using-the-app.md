# Using the app

The app works the same in Sandbox, in production and with demo data; only the data differs. This
guide walks through each page in the order of the menu:

- **Money:** [Dashboard](#dashboard), [Transactions](#transactions), [Budgets](#budgets)
- **Insights:** [Trends](#trends), [Recurring](#recurring), [Statements](#statements)
- **Organize:** [Categories](#categories), [Rules](#rules)
- **Data:** [Import](#import), [Connection](#connection), [Settings](#settings)

## Everywhere

- **The status in the top bar** shows the connection: Connected, Syncing…, Data is stale (no
  successful sync for 36 hours), Sync problem, Reconnect needed, or Not linked. Click it for the
  Connection page.
- **Sync now** (next to it) asks Plaid to fetch fresh data from Amex, syncs at once and again
  after about 15 seconds, 45 seconds and 2 minutes, while Plaid catches up. You rarely need it:
  the background agent syncs four times a day.
- **Banners** at the top of every page (except Connection, which shows the same things in more
  detail) tell you when Amex needs you to reconnect or when the yearly consent renewal is due.
  See [Reconnecting and renewing](reconnecting.md).
- **`/`** jumps to the search box on pages that have one.
- Pages work in light and dark mode, follow your Mac's setting, and fit a narrow window.

## How money is counted

The same rules apply on every page and in every alert:

- **Charges count as spending, in their category.** A refund in a spending category reduces that
  category's spending. Card payments ("Card Payments") and credits such as cash back ("Credits &
  Rewards") never count as spending.
- **A budget period** is a calendar month, or your Amex statement cycle if you choose that in
  [Settings](#settings).
- **Which date counts:** in calendar-month mode, the day you made the charge; in statement-cycle
  mode, the day it posted, exactly as on your statement.
- **Pending charges** count at their pending amount until they post, unless you turn that off in
  Settings. When a pending charge posts, it keeps the category, notes, tags and split you gave it.
- **Possible duplicates** (CSV rows held for review on the [Import](#import) page) count nowhere
  until you keep them.
- A **split** charge counts each part in its own category.

## Dashboard

<!-- RECHECK(DASH): the Dashboard is being built by the DASH package at the time of writing;
     check this section against templates/dashboard/ after the merge. -->

The Dashboard (`/`) is the summary of the current budget period:

- spending against your budgets this period, with how many days are left;
- the categories and merchants where most of the money went;
- links into the other pages for the details: each figure opens the matching
  [Transactions](#transactions) list, and budgets open [Budgets](#budgets).

It also points out what needs your attention, such as rows held for review after a CSV import.
It uses exactly the same numbers as the Budgets page.

## Transactions

Every charge, refund, credit and payment, newest first.

**Finding things**

- **Search** looks in the description, the merchant, Amex's original description, your notes and
  tags, and amounts.
- **Dates:** All dates, This month (or **This statement period** in statement-cycle mode, by
  posting date, matching the Budgets page), Last month, Last 90 days, This year, or a custom range.
- **Categories:** one or several; **Status:** pending or posted; **More filters:** merchant, an
  amount range, card member, tag, and **Credits only**.
- The address bar follows your filters, so a reload or a bookmark keeps them.
- Above the list: how many transactions match, their **Charges**, **Credits & payments** and
  **Net**. These are plain sums of the listed rows (a split charge counts in full under each
  category it touches), so they can differ from budget figures.
- **Needs review** lists rows where something changed after you edited them (for example Plaid
  changed the amount of a split charge). **Possible duplicates** lists CSV rows held for review.

**Changing a transaction**

- Click the **category** in a row to change it. A category you set yourself is never changed
  again by rules or syncs (until you choose **Reset to automatic**).
- The **⋯ menu** of a row (or the transaction's own page) has **Details**, **Notes and tags**,
  **Split** and **Always categorize like this** (neither for card payments), **Reset to
  automatic** once you have set the category yourself, and **Mark as reviewed** when the row needs
  review.
- **Split** spreads one charge over several categories. The parts must add up exactly to the
  amount; a pending charge can't be split until it posts.
- **Always categorize like this** makes a rule from the transaction, with a preview of the past
  transactions it would change. For an older row imported from a CSV file, the preview warns
  when Plaid names the merchant differently, so the rule also catches new charges.
- **Details** shows everything known about the charge (Plaid's and Amex's categories, posting
  date, statement descriptor, where it came from) and **All from this merchant**.

**Export CSV** downloads the rows that match your filters. Cells that a spreadsheet would read as
a formula are made safe. Possible duplicates are never exported unless you are viewing them.

## Budgets

The budgets for one period at a time; the arrows next to the period go back and forward.

- **Monthly budgets:** one row per category with its budget, what you spent, what is left and the
  percentage used, most used first. Type a new amount and **Save**. Each spent amount opens the
  matching transactions.
- **Add budget** gives a category a monthly budget. A change applies from the period shown on;
  earlier periods keep their budgets.
- **Suggest** proposes the average of your last three complete periods, rounded up to $5.
- **Rollover** (per category) carries what is left, or what you overspent, into the next period.
- **Overall target:** one number for all your spending, compared with every category except those
  with an annual budget. Without one, the card adds up your category budgets and lists spending
  in categories without a budget separately ("Not budgeted").
- **Annual budgets** plan yearly costs such as the annual fee, travel or gifts with one amount for
  the calendar year. They are tracked year to date with a marker for an even pace, and never
  count in the monthly totals. A category has either a monthly or an annual budget, not both.
- In statement-cycle mode a budget set during a cycle starts in the middle of that cycle, so a
  statement that closes a day or two late doesn't move it into another cycle. A cycle shorter than
  25 or longer than 35 days is marked "Unusual statement cycle".

Budget alerts at 80% and 100% are described in [Alerts and notifications](alerts.md).

## Trends

<!-- RECHECK(DASH): the Trends page is being built by the DASH package at the time of writing;
     check this section against templates/trends/ after the merge. -->

Trends (`/trends`) shows your spending per category per month for the last 12 months or more,
with the change from the month before. Trends always use calendar months and the date you made
each charge, whatever budget period you chose, with the same pending setting as budgets. A month
or a category opens the matching transactions.

## Recurring

Subscriptions and other repeating charges, found automatically from the last 13 months (three
years for yearly ones).

- At the top: what they cost a month (weekly and yearly charges are spread over a month), the
  next expected charge, and any series that has lapsed.
- **Active** lists each series by its next expected charge: how often, the usual amount (with
  "was …" after a price change, or "varies" for bills that change each time), the monthly cost
  and the category. **Late** marks a charge that is overdue.
- **Ignore it** hides a series from the list and the monthly cost; **Always list it** keeps one
  the app would otherwise drop. **Your choices** lists both, with **Restore** / **Back to
  automatic**.
- A series is found once there are at least three regular charges (two a year apart for yearly
  ones), so a new subscription appears after its third charge.

## Statements

One row per statement period of your card, checked against Amex to the cent.

- A period runs from the day after one closing date to the next closing date. A transaction
  belongs to the period it **posted** in; pending charges count once they post. Interest and fees
  dated on the closing date belong to that statement.
- **Status:** "Open" for the current period; "Final" a few days after the closing date (the
  "Days a statement settles" setting, 3 by default), once late charges have posted.
- **Check:** "Matches" when the app's activity equals the change in Amex's New Balance to the
  cent; "Off by $…" otherwise. The New Balance comes from Amex through Plaid, or from you.
- Open a period for its **Summary**, laid out like the box at the top of your Amex statement
  (Previous Balance, Payments/Credits, New Charges, Fees, Interest Charged, New Balance), with the
  app's number, the statement's and the difference. Type the numbers from your PDF statement under
  **Your statement** and **Save statement**; **Mark as checked** records that you compared them.
- **Near the closing dates** lists the transactions posted within 3 days of each end of the
  period: a charge on the wrong side of a closing date is the usual cause of a difference.
- **Closing dates** come from Amex through Plaid. Before that, they are estimated from the card's
  closing day (set it in [Settings](#settings)) and marked "estimated". **Add a statement** adds
  one by the closing date printed on it; **Remove statement** removes one added by mistake; and
  moving a mistyped date onto Amex's date combines the two.
- **Print** gives a clean printout of a statement page.

## Categories

**Your categories** are the groups your spending is sorted into. Each has a kind:

| Kind | Counts as spending? | Examples |
| --- | --- | --- |
| Spending | yes; refunds in it reduce it | Groceries, Dining & Drinks, Travel |
| Credits & income | no | Credits & Rewards (cash back, statement credits) |
| Card payments & transfers | no | Card Payments |

- **Add category**, or **Edit** one to rename it, recolour it, change its kind or give it an
  emoji icon.
- **Hide** removes a category from pickers and budget lists; its transactions still count
  everywhere.
- **Merge** moves everything in one category into another (transactions, split parts, rules,
  mappings, budgets and alerts), then deletes it. The merge page shows what will move first. If
  both have budgets, the target keeps its own.
- **Uncategorized** and **Card Payments** are built in: they can be renamed, but not hidden,
  merged away or given another kind.

**Plaid & Amex categories** decides where a new charge goes when no rule matched: each Plaid
category (and each Amex CSV category) points to one of yours. Pick a category in **Goes to** and
**Save**; **Not mapped** removes a choice, so the charge falls back to its group's category, then
to Uncategorized. Some rows say "for credits": they are tried first for negative amounts, so cash
back goes to Credits & Rewards while fees stay in Fees & Interest. A change applies to new
transactions; past ones keep their category until you [re-apply rules](#rules), and the message
after saving says how many would move.

## Rules

Your rules run first, top to bottom: the first rule that matches a charge sets its category.
Charges no rule matches go by Plaid's or Amex's category (above).

- **New rule:** a merchant and/or description text (contains, is exactly, starts with, or a
  regular expression; upper or lower case and extra spaces don't matter), an optional amount range
  (charges are positive, credits negative) and an optional card. Every condition you fill in must
  match. The form previews the past transactions the rule matches; tick **Also apply to past
  transactions** to change them too.
- **Move up** and **Move down** change the order; the switch turns a rule off (it is kept but
  categorizes nothing); **Edit** and **Delete** do what they say.
- Rules never touch two things: card payments always go to Card Payments, and a category you set
  yourself on a transaction never changes.

**Re-apply rules to past transactions** runs card payments, your rules and the category mappings
again on every past transaction, all of them, since a date, or only Uncategorized ones. **Preview
changes** shows the counts and examples first; categories you set yourself never change. The same
from Terminal:

```sh
./run recategorize --dry-run                  # count what would change
./run recategorize                            # do it
./run recategorize --since 2026-01-01         # only transactions on or after a date
./run recategorize --only-uncategorized
```

## Import

Adds history from Amex CSV files: older months than Plaid has, or your whole history before you
link.

**Download the right file.** On americanexpress.com:

1. Open **Statements & Activity**.
2. Choose **Custom Date Range** (Amex keeps about two years online) and search.
3. Click **Download**, choose **CSV**, then tick **"Include all additional transaction
   details"**. The basic file (only Date, Description and Amount) is refused: without Amex's
   Reference numbers duplicates can't be detected safely.
4. Import the file as downloaded. Opening and saving it in Excel or Numbers changes the Reference
   numbers and dates.

**Import it.** Choose the file (up to 10 MB) and the account, and click **Preview import**.
Nothing is imported until you confirm. The preview groups the rows: new ones, rows held for
review, rows that match a transaction Plaid already has (merged, not added twice), rows already
imported, rows imported before with a different date or amount (skipped) and unreadable rows. The
file itself is read in memory and never saved.

- **Overlapping files are fine.** Rows already imported are recognised by their Reference number,
  and rows Plaid already has are merged into the Plaid transaction, keeping your edits.
- **Held for review:** a CSV row dated inside the period Plaid covers, with no matching Plaid
  charge, might be a duplicate, so it counts nowhere until you choose **Keep** (it is real) or
  **Delete**. Rows marked "pending in Plaid" match a charge that hasn't posted yet and merge on
  their own once it posts. A row you delete is held again if you import its file again.
- **Past imports** lists every file with what it added.
- Before you link, imported rows go to an account named "American Express (CSV)". Linking moves
  them onto your card ([Going live](going-live.md#5-import-older-history)).

From Terminal, the same preview and import:

```sh
./run import-csv ~/Downloads/activity.csv --dry-run    # preview only
./run import-csv ~/Downloads/activity.csv              # import (Sandbox)
./run import-csv ~/Downloads/activity.csv --env production
```

Without `--account ID` the file goes to your linked card, or to the "American Express (CSV)"
account when nothing is linked; when that is ambiguous, the command lists the accounts to choose
from. Delete the downloaded file when you are done with it: it holds your real transactions.

## Connection

Your Amex connection through Plaid, read-only.

- When nothing is linked: what linking does, and the **Link American Express** button (in Sandbox,
  **Link with Plaid Link** and the test tools). [Going live](going-live.md#4-link-your-amex)
  describes the production link.
- When linked: the status, **Last successful sync**, **Plaid's last update from Amex**,
  **History** (whether Plaid has sent everything), **Consent** (when the yearly renewal is due)
  and **Statement details** (whether Amex shares them).
- Your card: current balance, statement balance, minimum payment and due date, credit limit and
  last payment, as Amex reports them through Plaid. Charge cards have no preset limit.
- **Reconnect**, **Renew consent** and **Grant access to statement details** appear when needed
  ([Reconnecting and renewing](reconnecting.md)).
- **Recent syncs:** the last 20 syncs with how they started (scheduled, Sync now, …), the result
  and the counts of new, changed and removed transactions.
- **Edits we couldn't carry over:** when Plaid removes a charge you had edited and no replacement
  matches it, your edits are kept here for 90 days in case it comes back. **Dismiss** forgets them.
- **Imported CSV history:** shown when imported rows still need to be moved onto a linked card.

## Settings

- **Budget period:** calendar month or statement cycle, and the card whose cycle sets the periods;
  **Count pending charges in budgets**.
- **Statements:** each card's **statement closing day** (the day of the month printed on your
  statement; dates from Amex come first when known) and **Days a statement settles** (0–14,
  default 3).
- **Alerts:** budget alerts and their percentages (default 80, 100), sync problem alerts, an
  optional large-charge amount, and how many days before the yearly consent runs out to start
  reminding you (default 30). See [Alerts and notifications](alerts.md).
- **Mac notifications:** **Send test notification** and the one-time Script Editor permission.
- **This app:** the environment, database, data folder, logs and backups folders, and the version.

Every page uses new settings at once.
