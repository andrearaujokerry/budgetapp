# Security

Your spending history is sensitive, and the app is built so that it stays on your Mac and can't
touch your money. This page lists what protects it, and the few things that are up to you.

## Read-only: nothing can move money

- The app asks Plaid only for **Transactions** and **Liabilities** (statement balance, minimum
  payment, due date). It never asks for products that can pay or move money.
- The app's Plaid code allows a fixed list of calls: create a Plaid window, exchange its result
  for an access token, read transactions, ask Plaid to refresh them, read the connection, the
  card's balance and statement details, and look up institutions, plus three test-only calls that
  refuse to run outside the Sandbox. A test fails if any other call appears, and none of them can
  pay, transfer or change anything at American Express.
- You sign in to American Express in Plaid's window and Amex's own pop-up. The app never sees your
  Amex password; it keeps only Plaid's access token.

## Only this Mac can reach it

- The web app listens only on 127.0.0.1, and there is no option to change that. Requests must
  name 127.0.0.1 or localhost, which stops websites from reaching it through tricks with domain
  names.
- Anything that changes data must come from the app's own pages. Requests that another website
  starts are refused, except a plain link to a page, so a website can't submit forms to the app,
  embed it, or load your pages in the background to learn about them. A link from another
  website to the new-rule form opens it blank.
- Every page has a strict Content Security Policy: no scripts or styles from anywhere else (Plaid's
  window on the Connection page is the one exception), no embedding in other sites, and pages are
  never cached.
- The database is the app's own PostgreSQL 18 server, separate from any other on this Mac. It
  listens only on this Mac (127.0.0.1 and a private socket folder), and every connection needs a
  password or your macOS account.

## Secrets stay in Keychain

- Plaid keys, the access token and the database passwords live only in your login Keychain
  ([the list](setup.md#what-keychain-holds)). They are never written to the database, files, git,
  logs, error messages, web pages or command lines.
- You type keys only into `./run setup`, with hidden input.
- Logs remove anything that looks like a Plaid token or a key the app has loaded, even inside
  error reports.
- The background agents never show a Keychain dialog. When macOS wants your approval, they stop,
  tell you, and wait until you run `./run doctor --env production` in Terminal.
- In the rare case Keychain can't save the access token during the link, it goes to a recovery
  file only you can read, never to a log.

## Your data goes only to Plaid

- The only internet traffic is with Plaid (and the download of the app's Python packages: at the
  first `./run`, at the first install of the live app, and when their pinned versions change).
- There are no analytics or trackers, and no fonts or other files from other websites (the
  app's font ships with it). The one outside script is Plaid's own, on the Connection page only.
- Asking questions about your spending in plain English (AI-1) isn't part of this version, so
  nothing is ever sent to Anthropic.

## Test data and real data are separate

- Every command uses the Sandbox unless you add `--env production` or run `./run live`.
- Sandbox, production, tests and demos use different databases and different database users. The
  test and demo user can't even open the real database.
- Production runs only committed code, from an installed copy, and every upgrade backs up the
  database first.

## Your disk and backups

- FileVault encrypts the disk. `./run doctor` fails while it is off.
- Every app folder is readable only by you, and so is every file in it, including backups.
- Time Machine skips the live database folder. Backups may be copied only to encrypted disks;
  `./run doctor` checks every Time Machine destination ([Backups](maintenance.md#time-machine-and-copies-elsewhere)).

## What is up to you

- **Keep FileVault on, use a strong Mac password and lock the screen.** The app has no login of
  its own: anyone using your macOS account can open it. Remove macOS user accounts nobody uses
  (`./run doctor` lists them).
- **Type keys only into `./run setup`.** Never paste a key or token into a chat, an email, an
  issue, a note or a file, and never commit one.
- **Keep secrets and data out of git.** `.gitignore` keeps CSV downloads, spreadsheets, dumps and
  logs out of the repository, and `./run doctor` checks tracked files for Plaid tokens. Never force
  such a file into git, especially if the repository is public.
- **Delete CSV downloads** from your Downloads folder once they are imported.
- **Keep the access token's only copy outside Keychain in your password manager**, nowhere else.
- **Protect your Plaid account** with a strong password and two-step verification: it holds your
  keys.
- **After macOS or Homebrew upgrades**, run `./run doctor --env production`.

## If a secret leaks

- **A Plaid secret:** rotate it in Plaid's Dashboard (Developers → Keys), then type the new one
  into `./run setup --keys-only`.
- **The access token:** a token works only together with your client ID and secret. If you think
  both leaked, rotate the secret in Plaid's Dashboard right away: the token is then useless to
  anyone else, and the app keeps working once you have saved the new secret.
- **Your Mac or a backup disk is lost:** FileVault and an encrypted backup disk keep the data
  unreadable. Rotate your Plaid secrets as a precaution. You can also withdraw Plaid's access in
  your American Express account; the app then shows Reconnect needed and keeps the data it has.
