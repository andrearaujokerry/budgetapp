# Amex CSV fixtures (synthetic)

Every value here is made up: card members, account numbers, References, merchants and amounts.
No row comes from a real statement. The layouts follow the real Amex downloads documented in
RESEARCH T4.1–T4.3b (header names and order, quoting, encodings, Reference format).

A Reference is `3` + YYYYDDD (the posting date: year and day of the year) + 10 digits, so
`reconcile.reference_posting_date` reads a plausible posting date from each one.

| File | What it exercises |
| --- | --- |
| `activity_2025-08.csv` | The 14-column extended header (2021 order, with `Receipt`), UTF-8 with a BOM, CRLF records with LF inside quoted multi-line cells (Extended Details, Address, City/State), apostrophe-wrapped References, padded descriptions, a payment (blank Category), a refund, cash back and an annual fee (`Fees & Adjustments`), five identical same-day hotel charges with distinct References, 0- and 1-decimal amounts (`42`, `6.5`), `é` in UTF-8, two card members |
| `activity_2025-09.csv` | The 13-column header (2022 order, no `Receipt`, no BOM); overlaps the August file by two rows (same References) |
| `activity_cp1252.csv` | Windows-1252 bytes (`é` as `0xE9`), LF line ends, columns in another order, `MM/DD/YY` dates, References without apostrophes, a 1-decimal amount |
| `activity_basic.csv` | Amex's basic download (`Date,Description,Amount`): refused, it has no Reference |
| `activity_malformed.csv` | Row errors with their row numbers: an impossible date, a non-number amount, a missing Reference, three decimals, a Reference mangled by a spreadsheet (`3.20250000000001E+17`), a Reference repeated in the file, a missing date, a missing description, a row with more values than the header |
| `activity_unclosed_quote.csv` | A quote that is never closed and swallows the rows after it: the whole file is refused |
