# Using the app

The app works the same in Sandbox, in production and with demo data; only the data differs. This
guide walks through each page in the order of the menu:

- **Money:** [Dashboard](#dashboard), [Transactions](#transactions), [Budgets](#budgets)
- **Insights:** [Trends](#trends), [Recurring](#recurring), [Statements](#statements)
- **Organize:** [Categories](#categories), [Rules](#rules)
- **Data:** [Import](#import), [Connection](#connection), [Settings](#settings)

## Everywhere

- **The status in the top bar** shows your connections: Connected, Syncing…, Data is stale (no
  successful sync for 36 hours), Sync problem, Reconnect needed, or Not linked. With several
  connections it shows the one that needs you most, and its tooltip lists each bank with its
  state. Click it for the Connection page.
- **Sync now** (next to it) asks Plaid to fetch fresh data from your banks and cards, syncs at
  once and again after about 15 seconds, 45 seconds and 2 minutes, while Plaid catches up. You
  rarely need it: the background agent syncs four times a day. A connection that needs
  reconnecting waits for you while the others sync. When no connection can sync, the button
  becomes **Reconnect** or **Renew access** and opens the Connection page.
- **Banners** at the top of every page (except Connection, which shows the same things in more
  detail) name each bank that needs you to reconnect and each one whose consent renewal is due.
  See [Reconnecting and renewing](reconnecting.md).
- **`/`** jumps to the search box on pages that have one.
- Pages work in light and dark mode, follow your Mac's setting, and fit a narrow window.

## How money is counted

The same rules apply on every page and in every alert:

- **Charges count as spending, in their category.** A refund in a spending category reduces that
  category's spending. Card payments ("Card Payments") and credits such as cash back ("Credits &
  Rewards") never count as spending.
- **Bank accounts count the same way.** Rent, debit-card purchases, loan payments (a car,
  mortgage or student loan) and ATM withdrawals ("Cash & ATM") from checking are spending. Paying
  your card from checking is a card payment when Plaid marks it as one, so the money counts once,
  as the card's charges, when that card is linked too: a payment to a card the app doesn't have
  is still a card payment, so that card's spending counts nowhere. Plaid's category for that
  payment isn't guaranteed: if Plaid files one as spending, the card's charges count twice until
  you set it to Card Payments, so check its **Counts as** once after linking. What
  Plaid files as a transfer ("Transfers") is neither spending nor income: moving money between
  your own accounts and money a lender puts in your account, but also payments to and from other
  people (Zelle, Venmo, wires, check deposits) until a rule of yours moves them. Paychecks,
  interest and other money coming in go to "Paycheck", "Interest" and "Other Income", and so does
  money coming in that Plaid doesn't classify, marked **Needs review** (change its category if it
  is a refund or a transfer). A transaction's details say how it counts (**Counts as**). Once a
  bank is linked, check what [Going live](going-live.md#after-your-first-bank-account-syncs)
  lists.
- **A budget period** is a calendar month, or a card's statement cycle if you choose that in
  [Settings](#settings).
- **Which date counts:** in calendar-month mode, the day you made the charge; in statement-cycle
  mode, the day it posted, exactly as on your statement.
- **Pending charges** count at their pending amount until they post, unless you turn that off in
  Settings. When a pending charge posts, it keeps the category, notes, tags and split you gave it.
- **Possible duplicates** (CSV rows held for review on the [Import](#import) page) count nowhere
  until you keep them.
- A **split** charge counts each part in its own category.

## Dashboard

The Dashboard (`/`) is the summary of the current budget period, with exactly the numbers of the
Budgets page. The heading names the period and the days left; **Budgets** and **Trends** next to
it open those pages.

- **The tiles at the top:** **Spent** against your overall target (or your budgets added up, or
  "in all categories" before you set any), **Remaining** (with about how much a day that leaves),
  **Days left**, and **Compared with** the previous period: all your spending so far (categories
  with an annual budget included, like the pace chart, so it can be more than **Spent**) against
  what you had spent by the same day then.
- **Show income** (next to **Budgets** and **Trends**, once any money has come in to an income
  category) adds two tiles after **Spent**: **Income** this period, with its largest source, and
  **Net**: income minus all your spending so far (above zero is money you kept). Income is money
  in to your income categories from any account: paychecks, interest, other income, and Amex
  credits (Credits & Rewards, its own line wherever income is broken down). It uses the same dates
  and pending setting as your budgets. Card payments and transfers are neither income nor
  spending. The switch is on to begin with, and the app remembers your choice
  for the Dashboard and Trends. The **Income** tile opens the period's income in Transactions,
  where money in shows as negative amounts (as Plaid sends it), so the list's **Net** is the
  figure with a minus sign. Until you link a bank account, the only income is your card credits,
  so **Net** is close to minus your spending; the tile says so, with a link to Connection.
- **Accounts:** every account you haven't hidden, with the balance Plaid last sent: bank
  accounts with their balance (and, under it, the available balance when the bank sends one),
  their balances added up as **Cash**, and cards with what you owe, added up as **Owed on
  cards** (a negative balance is a credit in your favour; an overdrawn bank account shows in
  red). An account imported from a CSV file has no balance, nor has one whose balance Plaid
  hasn't sent yet. A total adds up only the balances it has: under it, a note says how many
  accounts without a balance it leaves out, and while none of its accounts has a balance it
  shows "—" rather than $0.00. An account Plaid no longer shares (see [Connection](#connection))
  keeps the last balance Plaid sent, marked "no longer shared", and is left out of **Cash** and
  **Owed on cards**. Each name opens that account's transactions.
- **Budgets:** your six most-used monthly budgets with what is left; **All budgets** opens the
  Budgets page.
- **Top categories:** where the money went this period, with each category's share (hidden
  categories included and marked); the rest are added up in one line so the list adds up to all
  your spending.
- **Spending pace:** a chart of your spending added up day by day, against the previous period.
- **Top merchants:** the places you spent most, with how many charges.
- **Coming up:** recurring charges expected before the period ends, with their usual amount.
- **Annual budgets:** each annual budget's spending this year. In statement-cycle mode, when the
  current cycle runs across December 31, the card shows the year you are in, and a note points to
  the other year on the Budgets page (with the year that ended, its final tally).

Each category, budget and merchant name opens the matching [Transactions](#transactions) list,
with the same dates (by posting date in statement-cycle mode) and pending setting, so the list's
**Net** is the amount shown, split charges included. Banners point out rows held for review after
a CSV import (**Review** opens the Import page), an unusual statement cycle, and statement cycles
chosen in Settings that can't be used yet. Before anything is linked or imported, the page offers
**Link a bank or card** and **Import a CSV file**; once an account is linked, it says "Your
transactions are on their way" until the first ones arrive, with the **Accounts** card below.

## Transactions

Every charge, refund, credit, payment and transfer on your cards and bank accounts, newest first.

**Finding things**

- **Search** looks in the description, the merchant, the original description (as the bank or an
  Amex CSV file gave it), your notes and tags, and amounts.
- **Dates:** All dates, This month (or **This statement period** in statement-cycle mode, by
  posting date, matching the Budgets page), Last month, Last 90 days, This year, or a custom range.
- **Categories:** one or several; **Status:** pending or posted; **More filters:** merchant, an
  amount range, **Account** (once you have more than one: each card and bank account, under its
  bank), card member, tag, and **Credits only**.
- The address bar follows your filters, so a reload or a bookmark keeps them.
- Above the list: how many transactions match, their **Charges**, **Credits & payments** and
  **Net**, over every page of the list. Filtered by category, a split charge counts only its parts
  in the chosen categories (its row still shows the whole charge and its split, and the summary
  says how many split charges count only in part), so a list opened from a spent amount on the
  Dashboard, Budgets or Trends adds up to that amount. Without a category filter they add up whole
  amounts, card payments, transfers, paychecks and credits included, so they differ from spending
  figures.
- Rows that are never spending are greyed out: **Payment** marks a card payment (on the card, and
  its other side on checking), **Transfer** what Plaid files as a transfer: money moved between
  your own accounts, and payments to or from other people until a rule moves them.
- **Needs review** lists rows where something changed after you edited them (for example Plaid
  changed the amount of a split charge), and money in to a bank account that Plaid didn't
  classify. **Possible duplicates** lists CSV rows held for review.

**Changing a transaction**

- Click the **category** in a row to change it (in a narrow window, the row's ⋯ menu has
  **Change category**). A category you set yourself is never changed again by rules or syncs
  (until you choose **Reset to automatic**).
- The **⋯ menu** of a row (or the transaction's own page) has **Details**, **Notes and tags**,
  **Split** (not for card payments and transfers), **Always categorize like this** (not for a
  payment on the card itself), **Reset to automatic** once you have set the category yourself, and
  **Mark as reviewed** when the row needs review.
- **Split** spreads one charge over several categories. The parts must add up exactly to the
  amount; a pending charge can't be split until it posts.
- **Always categorize like this** makes a rule from the transaction, with a preview of the past
  transactions it would change. For an older row imported from a CSV file, the preview warns
  when Plaid names the merchant differently, so the rule also catches new charges.
- **Details** shows everything known about the charge (Plaid's and Amex's categories, posting
  date, statement descriptor, the account and bank it is on, where it came from) and **All from
  this merchant**.

**Export CSV** downloads the rows that match your filters. Cells that a spreadsheet would read as
a formula are made safe. Possible duplicates are never exported unless you are viewing them.

## Budgets

The budgets for one period at a time; the arrows next to the period go back and forward.

- **Monthly budgets:** one row per category with its budget, what you spent, what is left and the
  percentage used, most used first. Type a new amount; it saves when you press Enter or leave the
  field. Each spent amount opens the matching transactions.
- **Add budget** gives a category a monthly budget. A change applies from the period shown on;
  earlier periods keep their budgets.
- **Suggest** fills in the average of your last three complete periods, rounded up to $5. In a
  budget row it saves like a typed amount, when you leave the field or press Enter; Esc puts
  the old amount back.
- **Rollover** (per category) carries what is left, or what you overspent, into the next period.
- **Overall target:** one number for all your spending, from every card and bank account,
  compared with every category except those with an annual budget. Without one, the card adds up
  your category budgets and lists spending in categories without a budget separately
  ("Not budgeted").
- **Annual budgets** plan yearly costs such as the annual fee, travel or gifts with one amount for
  the calendar year. They are tracked year to date with a marker for an even pace, and never
  count in the monthly totals. A category has either a monthly or an annual budget, not both.
- In statement-cycle mode a budget set during a cycle starts in the middle of that cycle, so a
  statement that closes a day or two late doesn't move it into another cycle. A cycle shorter than
  25 or longer than 35 days is marked "Unusual statement cycle".
- A statement cycle that runs across December 31 counts each year's days by the budget the
  category had at that year's end. If an annual budget starts in January, the category's
  late-December spending shows under **Spending without a budget** (marked "Annual budget for"
  the new year); if a monthly budget replaces an annual one in January, its row counts only the
  January days, because the old year's annual budget already counts December.

Budget alerts at 80% and 100% are described in [Alerts and notifications](alerts.md).

## Trends

Trends (`/trends`) shows your spending per category per month. Trends always use calendar months
and the date you made each charge, whatever budget period you chose (in statement-cycle mode a
note says so), with the same pending setting as budgets.

- **Show** 12, 24 or 36 months, **Through** a month you choose (this month by default, back to the
  first month of your history and at least a year), for all categories or one **Category**.
  Months before your first transaction are left out.
- **The change from last month** compares the last month that has ended with the one before it.
  The first month of your history has no month before it, so there the page says "No month to
  compare yet" rather than comparing with $0. The month in progress is shown "so far" and
  compared with the same days of the month before (only days your history covers), so a few days
  are never set against a whole month. The **Monthly spending average** (a category's **Monthly
  average** in its own view) counts complete months only. The tiles that hold spending say so
  ("Spent in …"), so they are never read as income or net when income is shown.
- **Change from last month** lists each category with both months, the change and the average;
  **Every month** shows every category for every month, in whole dollars ("—" before your
  history starts).
- A category's name opens its own month-by-month view; there, each month's amount opens the
  matching [Transactions](#transactions), whose **Net** is that amount. In the change table, the
  amount of the last complete month does the same.
- **Show income** (as on the [Dashboard](#dashboard), the same choice) turns the page into a
  monthly income statement for all categories: tiles with the last complete month's income (and
  how it changed) and **Net** (with its monthly average); an **Income and spending** chart with
  income bars next to spending bars and the net as a dashed line; and, in **Every month**, after
  **All spending**, one row per income category (Credits & Rewards on its own line), **All
  income** and **Net** (income minus spending). Each income amount opens that month's income in
  Transactions (money in shows as negative amounts there). A single category's view stays
  spending only; with the switch on, it says that income and net show with **All categories**.
  Once a bank account is linked, income counts from the day its history starts, since that is
  where your paychecks arrive. If that is later than your card's history (a bank that sends
  less history, or older Amex statements you imported), the months before it show "—" for
  income and net rather than a loss, its first month is partial, the averages count only the
  months it covers, and a note above **Income and spending** says when it starts.

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

One row per statement period of your card, checked against the printed statement to the cent.
With more than one card, each has its own tab. Checking and savings accounts have no statements here, and the
**Card whose statement cycle sets the periods** in [Settings](#settings) is always a card.

- A period runs from the day after one closing date to the next closing date. A transaction
  belongs to the period it **posted** in; pending charges count once they post. Interest and fees
  dated on the closing date belong to that statement.
- **Status:** "Open" for the current period; "Final" a few days after the closing date (the
  "Days a statement settles" setting, 3 by default), once late charges have posted.
- **Check:** "Matches" when the app's activity equals the change in the statement's New Balance
  to the cent; "Off by $…" otherwise. The New Balance comes from the card company through Plaid,
  or from you.
- Open a period for its **Summary**, laid out like the summary box at the top of your statement
  (Previous Balance, Payments/Credits, New Charges, Fees, Interest Charged, New Balance), with the
  app's number, the statement's and the difference. Type the numbers from your PDF statement under
  **Your statement** and **Save statement**; **Mark as checked** records that you compared them.
- **Near the closing dates** lists the transactions posted within 3 days of each end of the
  period: a charge on the wrong side of a closing date is the usual cause of a difference.
- **Closing dates** come from the card company through Plaid. Before that, they are estimated
  from the card's closing day (set it in [Settings](#settings)) and marked "estimated". **Add a
  statement** adds one by the closing date printed on it; **Remove statement** removes one added by
  mistake; and moving a mistyped date onto the card company's date combines the two.
- **Print** gives a clean printout of a statement page.

## Categories

**Your categories** are the groups your spending is sorted into. Each has a kind:

| Kind | Counts as spending? | Examples |
| --- | --- | --- |
| Spending | yes; refunds in it reduce it | Groceries, Dining & Drinks, Travel, Cash & ATM |
| Credits & income | no | Paycheck, Interest, Other Income, Credits & Rewards (cash back, statement credits) |
| Card payments & transfers | no | Card Payments, Transfers (moves between your accounts, and anything else Plaid files as a transfer) |

Paycheck, Interest, Other Income, Transfers and Cash & ATM are for money in and out of your bank
accounts; they were added with bank accounts and work like any other category. A category you
already had with one of those names was kept as it was.

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
category (and each Amex CSV category) points to one of yours. Pick a category in **Goes to**; it
saves at once. **Not mapped** removes a choice, so the charge falls back to its group's category,
then to Uncategorized. Some rows say "for credits": they are tried first for negative amounts, so cash
back goes to Credits & Rewards while fees stay in Fees & Interest. Rows marked **Bank accounts**
are tried first for checking and savings accounts, so a paycheck goes to Paycheck there while the
same Plaid category on a card stays in Credits & Rewards; the row without the mark is for cards
(and for bank accounts when there is no bank row). Money in to a bank account that Plaid didn't
classify (no category, or Other) goes where Income → Other's **Bank accounts** row says, and
counts there. A change applies to new
transactions; past ones keep their category until you [re-apply rules](#rules), and the message
after saving says how many would move.

## Rules

Your rules run first, top to bottom: the first rule that matches a charge sets its category.
Charges no rule matches go by Plaid's or Amex's category (above).

- **New rule:** a merchant and/or description text (contains, is exactly, starts with, or a
  regular expression; upper or lower case and extra spaces don't matter), an optional amount range
  (charges are positive, credits negative) and an optional account (a card or a bank account).
  Every condition you fill in must match. The form previews the past transactions the rule
  matches; tick **Also apply to past transactions** to change them too. A regular expression that
  takes too long to check (a repeat inside a repeat, like `(a|aa)+`) is refused: simplify it.
- **Move up** and **Move down** change the order; the switch turns a rule off (it is kept but
  categorizes nothing); **Edit** and **Delete** do what they say.
- Rules never touch two things: card payments, and a category you set yourself on a transaction.
  A payment on your card always goes to Card Payments, and a payment from checking that Plaid
  marks as a card payment goes where its **Bank accounts** row on the Plaid & Amex categories tab
  says (Card Payments), so no rule can count it as spending. Otherwise your rules come first on a
  bank account too, so a rule can, for example, turn a regular transfer into income, or a payment
  Plaid files as a transfer (a Zelle or Venmo payment to someone) into spending.

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

**Import it.** Choose the file (up to 10 MB) and the account, and click **Preview import**. The
file goes to your American Express card, or to "American Express (CSV)" before you link it; bank
accounts and other banks' cards are never offered.
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
- Before you link, imported rows go to an account named "American Express (CSV)". Linking
  American Express moves them onto your Amex card
  ([Going live](going-live.md#5-import-older-history)). Linking a bank account first changes
  nothing here: only a linked Amex card takes over.

From Terminal, the same preview and import:

```sh
./run import-csv ~/Downloads/activity.csv --dry-run    # preview only
./run import-csv ~/Downloads/activity.csv              # import (Sandbox)
./run import-csv ~/Downloads/activity.csv --env production
```

Without `--account ID` the file goes to your linked Amex card, or to the "American Express (CSV)"
account when nothing is linked; when that is ambiguous, the command lists the accounts to choose
from. Delete the downloaded file when you are done with it: it holds your real transactions.

## Connection

Your banks and cards through Plaid, read-only, one section per connection (each bank login you
linked).

- **Link a bank or card** opens a dialog first. In production it says the link uses 1 of your 10
  Plaid connections, how many the app has created so far, and lists the Dashboard checklist. It
  also has "A different login at a bank I've already linked", for a second login at a bank you
  linked before; without it, that bank is refused. In the Sandbox, test connections can also be
  made without Plaid's window. [Going live](going-live.md#4-link-your-first-bank-or-card)
  describes the production link.
- Each connection: the bank's name and status, **Last successful sync**, **Plaid's last update
  from** the bank, **History** (whether Plaid has sent everything), **Consent** (when a renewal is
  due, or "No end date") and, for cards, **Statement details** (whether the bank shares them).
- Its accounts: a card shows the current balance, statement balance, minimum payment and due date,
  credit limit and last payment; a checking or savings account shows its current and available
  balance. Charge cards have no preset limit.
- Each connection's **Add accounts** and **Reconnect**, **Renew consent** when a renewal date is
  known, and **Grant access to statement details** for a card connection that doesn't share them
  ([Reconnecting and renewing](reconnecting.md)).
- **Recent syncs:** the last 20 syncs with how they started (scheduled, Sync now, …), the result
  and the counts of new, changed and removed transactions.
- **Edits we couldn't carry over:** when Plaid removes a charge you had edited and no replacement
  matches it, your edits are kept here for 90 days in case it comes back. **Dismiss** forgets them.
- **Imported CSV history:** shown when rows imported from Amex CSV files still need to be moved
  onto a linked American Express card.

## Settings

- **Budget period:** calendar month or statement cycle, and the card whose cycle sets the periods;
  **Count pending charges in budgets**.
- **Statements:** each card's **statement closing day** (the day of the month printed on your
  statement; dates the card company sends through Plaid come first when known) and **Days a
  statement settles** (0–14, default 3).
- **Alerts:** budget alerts and their percentages (default 80, 100), sync problem alerts, an
  optional large-charge amount, and how many days before a connection's consent runs out to start
  reminding you (default 30; American Express asks once a year). See
  [Alerts and notifications](alerts.md).
- **Mac notifications:** **Send test notification** and the one-time Script Editor permission.
- **This app:** the environment, database, data folder, logs and backups folders, and the version.

Every page uses new settings at once.
