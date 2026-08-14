"""
reconciliation_service.py -- Month-end bank statement reconciliation.

New feature, 2026-08-13, per Ravindra: "i want to write a reconciliation
system, when i click a button which will allow me to select month-year
combination like Sep-2026 and it should select a predefined month end
statement and does various checks. 1. The total month end balance from
excel should match the total amount in the books. 2. The total credits and
debits in the books must match the credits and debits of the month, the
aggregate values. 3. whether every entry in the statement has successfully
been posted? 4. Results needs to be posted in a nice and understandable and
intuitive way."

Confirmed design decisions (2026-08-13):
  - The month-end statement file lives in the SAME OneDrive folder as every
    other statement, found by filename match (account number digit-run +
    a month/year token) -- see find_month_end_statement_file() below --
    same "never guess, surface ambiguity" philosophy as every other
    OneDrive file-matching in this codebase (widget.js's
    matchFilesToAccounts(), onedrive_client.find_files_containing()).
  - Check 1 (balance) compares against Zoho's LIVE bank account balance
    (ZohoBooksClient.get_bank_account_balance()) -- Ravindra confirmed this
    choice knowing it's a real-time value, not historical: only meaningful
    for the most recently completed month with nothing posted to this
    account in Zoho after the statement's own end date. That caveat is
    carried into the result dict (see run_reconciliation()'s "caveats" key)
    so it's visible in the widget every time, not just in this docstring.
  - Check 3 (every entry posted) searches Zoho directly by narration+date
    (see match_statement_rows_to_zoho()) -- not this account's own feed at
    all, see that function's own docstring for the 2026-08-13 redesign.
  - Check 2 (aggregate credits/debits) originally compared against Zoho's
    bank-account transaction feed (ZohoBooksClient.find_bank_account_
    transactions(), one ambiguous "amount" per high-level object type --
    Expense, Vendor Payment, Bank Transfer, etc. -- requiring this module to
    guess each one's debit/credit direction). SUPERSEDED 2026-08-14 (later
    still), per Ravindra: "can you actually pull the transactions for the
    specified time period using Journal records in Zoho where you get all
    the debit and credit entries for each of the transactions for the
    selected account" -- now sourced from ZohoBooksClient.list_account_
    transactions() (GET /chartofaccounts/{account_id}/transactions), Zoho's
    own General Ledger view of the account, where every entry is already
    resolved to a specific debit or credit against THIS account -- no
    per-object-type direction guessing needed. See classify_account_
    transaction_amount()'s own docstring for the full reasoning; find_bank_
    account_transactions()/classify_bank_transaction_amount() are kept as a
    fallback path, not deleted.

UNVERIFIED LIVE, same discipline as every other new Zoho integration in
this codebase: the exact response shape of get_bank_account_balance() and
list_account_transactions() (see their own docstrings in zoho_books_
client.py) is a best-guess built from this environment's inability to fetch
Zoho's real (JS-rendered) API docs in full. This module is written
defensively around that uncertainty -- classify_account_transaction_
amount() below never silently miscounts an unrecognized record shape; it's
collected into an "unclassified" bucket that's surfaced in the result
rather than dropped, and run_reconciliation() always includes an explicit
"data source unverified" caveat until a real live run confirms the shape.

The month-end statement itself is treated as a RAW, unprocessed bank export
(exactly like the sample file this was built against -- "Account
Statement"/"Balance Information" header banner, then Transaction Date/Value
Date/Narration/Transaction Reference/Debit/Credit/Running Balance columns),
NOT a file that has already been through categorize_workbook()/posting --
it has no "Zoho Posting Reference" column to read. Because of that, "was
this row posted" (check 3) can't be answered by reading a column -- it's
answered by trying to MATCH each statement row to a Zoho transaction by
amount+date (see match_statement_rows_to_zoho()), the same fundamental
technique real bank reconciliation always uses when there's no direct link
between the two sides.
"""
import calendar
import re
from typing import List, Optional, Tuple

from categorize_from_module import (
    NARRATION_HEADER_ALIASES,
    DEBIT_HEADER_ALIASES,
    CREDIT_HEADER_ALIASES,
    DATE_HEADER_ALIASES,
    REFERENCE_HEADER_ALIASES,
    HEADER_SCAN_LIMIT,
    find_header_row,
    find_column,
)
from posting_service import (
    format_txn_date,
    _amount_as_float,
    _normalize_text_for_dup_check,
    _ALL_DESCRIPTION_SEARCHES,
)

# Not in categorize_from_module.py -- only this feature needs a Running
# Balance column, so it's kept local rather than added to that module's
# already-long list of shared alias sets.
RUNNING_BALANCE_HEADER_ALIASES = {"running balance", "balance", "closing balance", "available balance"}

# Amount-match tolerance for every numeric comparison in this module (a
# statement's own comma-formatted text and Zoho's own float can legitimately
# differ by a sub-cent rounding amount without that being a real mismatch).
_AMOUNT_TOLERANCE = 0.01


def _digits_only(text) -> str:
    return re.sub(r"[^0-9]", "", str(text or ""))


def find_month_end_statement_file(file_names: List[str], bank_account_number: str,
                                   month: int, year: int) -> dict:
    """Matches a month-end statement file out of a folder listing, the same
    loose-but-never-guessing style as widget.js's matchFilesToAccounts()
    (account number as a digit-run substring, case-insensitive) -- extended
    here with a required month/year token, since a regular OneDrive folder
    also holds every OTHER statement for the same account.

    Month token accepts either the 3-letter abbreviation ("sep"), the full
    name ("september"), or the zero-padded 2-digit number ("09") appearing
    anywhere in the filename -- deliberately NOT a bare single-digit "9"
    (too likely to false-positive against an unrelated digit elsewhere in
    the name, e.g. part of the account number itself). Year token requires
    the full 4-digit year ("2026") -- a 2-digit year ("26") is similarly too
    likely to collide with other digits in a real filename.

    Returns {"matched": [file_name, ...], "digits_used": str,
    "month_tokens_tried": [...], "year_token": str} -- ALWAYS returns every
    match found, never picks a "best" one; the caller decides what to do
    with 0 (not found), 1 (use it), or 2+ (ambiguous -- ask, don't guess)
    matches, same convention as onedrive_client.find_file_by_name()'s own
    FileNotFoundError-with-a-list-of-what-was-found approach."""
    digits = _digits_only(bank_account_number)
    month_tokens = [
        calendar.month_abbr[month].lower(),
        calendar.month_name[month].lower(),
        f"{month:02d}",
    ]
    year_token = str(int(year))
    matched = []
    for name in file_names:
        lower = (name or "").lower()
        if digits and digits not in _digits_only(name):
            continue
        if year_token not in lower:
            continue
        if not any(tok in lower for tok in month_tokens):
            continue
        matched.append(name)
    return {"matched": matched, "digits_used": digits, "month_tokens_tried": month_tokens, "year_token": year_token}


def _find_header_balance_text(rows, header_row_idx: int, label_pattern: str) -> Optional[float]:
    """Scans every metadata row ABOVE the real transaction header (the
    "Account Statement"/"Balance Information" banner rows -- see this
    module's own docstring) for a cell matching `label_pattern` followed by
    a number, e.g. "Current Balance: 224603.92" -- Zoho-agnostic, bank-
    export-agnostic free text, so this is a best-effort CROSS-CHECK only,
    never the primary source of truth (see parse_month_end_statement()'s
    "closing_balance_computed", which is derived from the transaction
    table itself, not free text). Returns None if no matching cell is
    found in the scanned rows -- not every bank's export banner uses this
    exact wording, and that's fine, this is a bonus check not a
    requirement."""
    pattern = re.compile(label_pattern, re.IGNORECASE)
    for row in rows[:header_row_idx]:
        for cell in (row or []):
            if cell is None:
                continue
            text = str(cell)
            m = pattern.search(text)
            if m:
                return _amount_as_float(m.group(1))
    return None


def _find_statement_period_header(rows, header_row_idx: int) -> Tuple[Optional[str], Optional[str]]:
    """Scans the metadata banner rows (same region _find_header_balance_text()
    scans) for a "From: <date> to <date>" -style declared statement period
    -- e.g. this bank's own "From: 01-07-2026 to 05-08-2026" banner line --
    and returns (period_start, period_end) as "YYYY-MM-DD" strings (via
    format_txn_date(), same dd/mm/yyyy-aware parser every other date in this
    module uses), or (None, None) if no such line is found/parseable. Not
    every bank's export includes this -- best-effort only, same "bonus
    check" status as _find_header_balance_text()'s own "Current Balance"
    line.

    Added 2026-08-13 (later still), per a real live bug Ravindra caught: a
    statement whose raw export included ONE extra day's worth of
    transactions dated AFTER its own declared period end (this bank's
    export apparently includes whatever had posted by download time,
    regardless of the declared "to" date) was producing a closing balance
    that didn't match either the statement's own "Current Balance:" header
    text OR the declared period -- because parse_month_end_statement() used
    to always take the single LATEST date found anywhere in the row data,
    with no awareness that a statement can declare its own period boundary
    explicitly. Confirmed against the real sample: the row dated exactly at
    the declared period end (05-08-2026) has a running balance that matches
    "Current Balance: 224603.92" exactly, while the extra 06-08-2026 row(s)
    -- which sort first in this bank's newest-first export order, so they
    LOOK like "the start" of the statement -- push the naive max-date
    calculation to a different, wrong number (229603.92)."""
    pattern = re.compile(r"from\s*:?\s*([\d/-]{6,10})\s*to\s*([\d/-]{6,10})", re.IGNORECASE)
    for row in rows[:header_row_idx]:
        for cell in (row or []):
            if cell is None:
                continue
            m = pattern.search(str(cell))
            if m:
                start = format_txn_date(m.group(1))
                end = format_txn_date(m.group(2))
                if start or end:
                    return start, end
    return None, None


def parse_month_end_statement(wb) -> dict:
    """Parses a RAW (unprocessed) month-end bank statement export -- see
    this module's own docstring for why this is treated as raw rather than
    already-categorized/posted. Returns:
      {"rows": [{"excel_row", "txn_date" ("YYYY-MM-DD" or None),
                 "txn_date_raw", "narration", "reference_number",
                 "debit", "credit", "running_balance"}, ...],
       "total_debit", "total_credit" (sums across every parsed row),
       "closing_balance_computed" (running_balance of the FIRST eligible
         row IN FILE ORDER -- 2026-08-14, per Ravindra: this bank's export
         lists transactions newest-first, so the top row's own Running
         Balance IS the statement's closing balance directly. The primary,
         transaction-table-derived source of truth for the statement's own
         closing balance -- see the "Closing balance" comment at this
         function's own computation below for the full reasoning and the
         defensive layout check that goes with it),
       "closing_balance_computed_date", "closing_balance_tied_rows" (count
         of OTHER rows that share that same date as the picked row --
         informational), "closing_balance_layout_mismatch" (True if the
         picked FIRST row's date ISN'T actually the latest date among
         eligible rows -- means this file isn't in the assumed newest-first
         order, surfaced as a caveat rather than silently trusting a wrong
         assumption), "closing_balance_row_backfilled" (True if the row
         actually used for the closing balance didn't have a Running
         Balance value in the file itself and had to be computed here --
         see "running_balance_backfilled_count" and the backfill comment at
         this function's own computation below for why that happens and how
         it's done), "running_balance_backfilled_count" (total count of
         rows across the WHOLE file whose Running Balance was backfilled,
         not just the one used for the closing balance),
       "closing_balance_header_text" (best-effort cross-check, parsed from
         a "Current Balance: ..." -style free-text cell above the header
         row -- None if not found),
       "account_number_header_text" (best-effort, parsed from an "Account
         Number : ..." -style cell -- None if not found, used only for a
         bonus sanity check, never for the 3 real checks),
       "period_start", "period_end" (best-effort, parsed from a "From: ...
         to ..." -style declared-period cell -- None/None if not found),
       "rows_after_period_end" (count of rows dated AFTER "period_end", when
         known -- see the closing-balance/total_debit/total_credit note
         below; 0 if period_end wasn't found, since nothing can be judged
         "after" an unknown boundary),
       "unparseable_date_rows" (count of rows whose date column couldn't be
         parsed at all -- excluded from closing-balance-by-date logic, but
         STILL included in total_debit/total_credit, since those sums don't
         depend on the date parsing correctly),
       "header_row", "header"}.

    "period_end" AWARENESS (2026-08-13, later still -- see
    _find_statement_period_header()'s own docstring for the real live bug
    this fixes): when the statement declares its own period end, both
    "closing_balance_computed" and "total_debit"/"total_credit" are
    computed ONLY from rows dated ON OR BEFORE that declared end -- a row
    dated after it (this bank's export apparently includes whatever had
    posted by download time, not strictly the declared period) is excluded
    from those three figures specifically, so check 1 (balance) and check 2
    (aggregate) stay consistent with each other and with what the statement
    itself claims to cover. Those excluded rows are NOT dropped from the
    "rows" list itself -- check 3 (every entry posted) still tries to match
    them against Zoho, since "was this real transaction actually posted" is
    a meaningful question regardless of which side of the declared period
    boundary it happens to fall on. When no declared period is found at
    all, behavior is UNCHANGED from before this fix (max date found in the
    data, no rows excluded from anything).

    Raises ValueError (same "clear message, no guessing" convention as
    extract_postable_rows()) if no header row / Narration / Debit / Credit
    / Date column can be found at all -- a genuinely malformed or
    unrecognizable file should fail loudly here, not produce a
    reconciliation result built on missing data."""
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Sheet is empty.")

    header_row_idx = find_header_row(rows)
    if header_row_idx is None:
        raise ValueError(
            f"Couldn't find a header row (one containing a Narration-like column) in the first "
            f"{HEADER_SCAN_LIMIT} rows -- this doesn't look like a recognizable bank statement export."
        )
    header = list(rows[header_row_idx])
    header_lower = [str(h).strip().lower() if h is not None else "" for h in header]

    nar_idx = find_column(header_lower, NARRATION_HEADER_ALIASES)
    debit_idx = find_column(header_lower, DEBIT_HEADER_ALIASES)
    credit_idx = find_column(header_lower, CREDIT_HEADER_ALIASES)
    date_idx = find_column(header_lower, DATE_HEADER_ALIASES)
    reference_idx = find_column(header_lower, REFERENCE_HEADER_ALIASES)
    running_balance_idx = find_column(header_lower, RUNNING_BALANCE_HEADER_ALIASES)

    if debit_idx is None or credit_idx is None:
        raise ValueError(
            f"Couldn't find this statement's own Debit/Credit columns -- expected one of "
            f"{sorted(DEBIT_HEADER_ALIASES)} and one of {sorted(CREDIT_HEADER_ALIASES)}."
        )
    if date_idx is None:
        raise ValueError(
            f"Couldn't find a transaction date column -- expected one of {sorted(DATE_HEADER_ALIASES)}."
        )
    if running_balance_idx is None:
        raise ValueError(
            f"Couldn't find a Running Balance column -- expected one of {sorted(RUNNING_BALANCE_HEADER_ALIASES)}. "
            f"This is needed to determine the statement's own closing balance."
        )

    closing_balance_header_text = _find_header_balance_text(rows, header_row_idx, r"current\s*balance\s*:?\s*([\d,]+\.?\d*)")
    period_start, period_end = _find_statement_period_header(rows, header_row_idx)
    account_number_header_text = None
    m_acct = re.search(r"account\s*number\s*:?\s*(\d+)", "\n".join(
        str(c) for row in rows[:header_row_idx] for c in (row or []) if c is not None
    ), re.IGNORECASE)
    if m_acct:
        account_number_header_text = m_acct.group(1)

    parsed_rows = []
    total_debit = 0.0
    total_credit = 0.0
    unparseable_date_rows = 0
    rows_after_period_end = 0
    header_row_excel = header_row_idx + 1
    for offset, row in enumerate(rows[header_row_idx + 1:]):
        excel_row = header_row_excel + 1 + offset
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        narration = row[nar_idx] if nar_idx is not None and nar_idx < len(row) else None
        # A pure metadata/blank-narration row (nothing to reconcile) is
        # skipped -- same "Narration is what makes this a real transaction
        # row" assumption find_header_row()/find_column() already lean on
        # elsewhere in this codebase.
        if narration is None or not str(narration).strip():
            continue
        raw_date = row[date_idx] if date_idx < len(row) else None
        txn_date = format_txn_date(raw_date)
        if txn_date is None:
            unparseable_date_rows += 1
        debit = _amount_as_float(row[debit_idx]) if debit_idx < len(row) else None
        credit = _amount_as_float(row[credit_idx]) if credit_idx < len(row) else None
        running_balance = (_amount_as_float(row[running_balance_idx])
                            if running_balance_idx < len(row) else None)
        reference_number = (str(row[reference_idx]).strip()
                             if reference_idx is not None and reference_idx < len(row)
                             and row[reference_idx] is not None else None)
        # Excluded from the aggregate totals (not from parsed_rows itself --
        # see this function's own "period_end AWARENESS" docstring section)
        # when this row's date is confirmed AFTER the statement's own
        # declared period end. A row with an unparseable date is NEVER
        # excluded here -- unchanged "can't judge it either way, so include
        # it" behavior from before this fix.
        after_period_end = period_end is not None and txn_date is not None and txn_date > period_end
        if after_period_end:
            rows_after_period_end += 1
        else:
            total_debit += debit or 0.0
            total_credit += credit or 0.0
        parsed_rows.append({
            "excel_row": excel_row,
            "txn_date": txn_date,
            "txn_date_raw": raw_date,
            "narration": narration,
            "reference_number": reference_number,
            "debit": debit,
            "credit": credit,
            "running_balance": running_balance,
            "running_balance_backfilled": False,
        })

    # Backfill missing Running Balance cells via the SAME chain formula the
    # source file itself uses -- added 2026-08-14 (later still), per a real
    # file Ravindra shared where the closing balance still looked wrong
    # after the "first row" fix above. Root cause, confirmed by re-reading
    # that file with data_only=False: its Running Balance column is a
    # FORMULA chain (e.g. "=G20-E19+F19", i.e. balance[row] = balance[row
    # below] - debit[row] + credit[row] -- consistent with a newest-first
    # file, where "the row below" is the chronologically OLDER transaction).
    # The file's most recent several rows (the ones actually needed for the
    # "first entry" fix above to work) had NEVER been recalculated -- their
    # cached formula result was blank/None, while older rows further down
    # (which happened to have been calculated at some earlier point) were
    # fine. Reading with data_only=True (required, since this codebase never
    # evaluates formulas itself elsewhere either) silently returns None for
    # an uncalculated formula cell, which made those newest rows look like
    # "no running balance available" and pushed the "first eligible row"
    # selection down to an much older, wrong date -- reproducing the exact
    # "balance from the wrong part of the statement" symptom this whole
    # fix chain has been chasing, just from a different root cause (a stale
    # spreadsheet cache, not date-boundary logic).
    #
    # Fix: walk parsed_rows from the BOTTOM up (oldest to newest, since this
    # file is newest-first) and fill any blank running_balance using the
    # row directly below it (already known, either originally present or
    # itself just backfilled) via balance[i] = balance[i+1] - debit[i] +
    # credit[i] -- the exact same arithmetic the file's own formulas encode,
    # so this reproduces what Excel would have shown if it had actually
    # recalculated. Rows filled this way are marked "running_balance_
    # backfilled": True so it's visible (not hidden) that the value was
    # computed here rather than read directly from the file -- surfaced as
    # a caveat by run_reconciliation() when it affects the row actually
    # used for the closing balance. If NO row in the whole file has a real
    # running_balance to seed off of, nothing can be backfilled and this is
    # a no-op (same as before this fix).
    for i in range(len(parsed_rows) - 2, -1, -1):
        row = parsed_rows[i]
        if row["running_balance"] is not None:
            continue
        below = parsed_rows[i + 1]
        if below["running_balance"] is None:
            continue  # nothing known yet to chain from -- leave blank
        row["running_balance"] = below["running_balance"] - (row["debit"] or 0.0) + (row["credit"] or 0.0)
        row["running_balance_backfilled"] = True
    running_balance_backfilled_count = sum(1 for r in parsed_rows if r["running_balance_backfilled"])

    # Closing balance: the running_balance of the FIRST eligible row IN
    # FILE ORDER -- 2026-08-14, per Ravindra: "for the statement total
    # amount consider the final balance from the first entry of the
    # statement not the last one as the statement entries are in
    # descending order of date. typically the running balance of the
    # statement." This bank's export lists transactions newest-first, so
    # the very top row's own Running Balance IS the account's current/
    # closing balance directly -- no date comparison needed at all.
    #
    # SUPERSEDES the previous "whichever row has the latest parsed
    # txn_date, tie-broken by the last such row in file order" logic --
    # that approach was deliberately date-driven (not position-driven)
    # because bank exports can differ on newest-first vs oldest-first
    # ordering (see raw-bank-statement-rules-and-accuracy.md's "layout
    # diversity" findings), but Ravindra has now confirmed THIS bank's
    # export order directly, so position is both simpler and matches his
    # explicit instruction. Still restricted to on-or-before the declared
    # period end (when known) -- see this function's own "period_end
    # AWARENESS" docstring section -- since that's a separate, still-valid
    # concern (a real extra day's worth of transactions past the statement's
    # own declared end, regardless of which row position ends up "first").
    #
    # Defensive check kept from the old logic, not dropped: if the first
    # eligible row's date ISN'T actually the latest date among eligible
    # rows, the descending-order assumption above doesn't hold for this
    # particular file -- "layout_mismatch" surfaces that explicitly (see
    # run_reconciliation()'s caveat for it) rather than silently trusting
    # position on a file that isn't actually ordered the way assumed.
    dated_rows = [r for r in parsed_rows if r["txn_date"] is not None and r["running_balance"] is not None]
    balance_eligible_rows = (
        [r for r in dated_rows if r["txn_date"] <= period_end] if period_end is not None else dated_rows
    )
    if period_end is not None and not balance_eligible_rows:
        # Declared period end is known, but literally nothing in the file is
        # dated on or before it (e.g. a badly mismatched/misparsed period
        # line) -- fall back to every dated row rather than returning no
        # closing balance at all; rows_after_period_end below still makes
        # this visible as a caveat.
        balance_eligible_rows = dated_rows
    closing_balance_computed = None
    closing_balance_computed_date = None
    closing_balance_tied_rows = 0
    closing_balance_layout_mismatch = False
    closing_balance_row_backfilled = False
    if balance_eligible_rows:
        first_row = balance_eligible_rows[0]
        closing_balance_computed = first_row["running_balance"]
        closing_balance_computed_date = first_row["txn_date"]
        closing_balance_row_backfilled = first_row["running_balance_backfilled"]
        tied = [r for r in balance_eligible_rows if r["txn_date"] == closing_balance_computed_date]
        closing_balance_tied_rows = len(tied) - 1
        max_date = max(r["txn_date"] for r in balance_eligible_rows)
        closing_balance_layout_mismatch = closing_balance_computed_date != max_date

    return {
        "rows": parsed_rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "closing_balance_computed": closing_balance_computed,
        "closing_balance_computed_date": closing_balance_computed_date,
        "closing_balance_tied_rows": closing_balance_tied_rows,
        "closing_balance_layout_mismatch": closing_balance_layout_mismatch,
        "closing_balance_row_backfilled": closing_balance_row_backfilled,
        "running_balance_backfilled_count": running_balance_backfilled_count,
        "closing_balance_header_text": closing_balance_header_text,
        "account_number_header_text": account_number_header_text,
        "period_start": period_start,
        "period_end": period_end,
        "rows_after_period_end": rows_after_period_end,
        "unparseable_date_rows": unparseable_date_rows,
        "header_row": header_row_idx,
        "header": header,
    }


# Zoho transaction_type values this module knows how to classify as a
# ledger DEBIT (increases the bank account, an ASSET -- money IN) vs a
# ledger CREDIT (decreases it -- money OUT), covering every posting type
# this codebase itself creates (see posting_service.py's own
# _JOURNAL_TYPES/_EXPENSE_TYPES/etc CONSTANTS and _post_expense()/
# _post_vendor_payment()/_post_bank_transfer()) plus the generic deposit/
# withdrawal wording Zoho's own Banking module UI uses for bank-feed lines.
#
# CORRECTED 2026-08-14, per Ravindra: "The statement's total (sum of) on
# the credit side should match with the debit side total sum of the ledger
# in zoho... Sum of all the credit entries in the statement should equal to
# sum of all postings in the ledger on debit side." A bank STATEMENT's
# Debit/Credit is from the BANK's own point of view (debit = money the bank
# paid out of your account); Zoho's ledger records the SAME bank account as
# an ASSET from the BUSINESS's point of view, where an asset increases
# (money deposited IN) on the DEBIT side and decreases (money withdrawn
# OUT) on the CREDIT side -- the exact opposite of the statement's own
# columns. This set previously had the money-out types labeled "debit" and
# the money-in types labeled "credit", which was backwards for what a Zoho
# ledger actually records; swapped below so aggregate_bank_transactions()'s
# totals mean "money IN per Zoho" (debit) and "money OUT per Zoho" (credit),
# which run_reconciliation() then deliberately cross-compares against the
# statement's OPPOSITE column (see that function's own aggregate_match
# comment). UNVERIFIED LIVE -- see classify_bank_transaction_amount()'s own
# docstring for the field-name/shape guessing this doesn't change.
_DEBIT_TRANSACTION_TYPES = {
    "invoice", "customer_payment", "customerpayment", "deposit", "retainer_invoice", "credit",
}
_CREDIT_TRANSACTION_TYPES = {
    "expense", "vendor_payment", "withdrawal", "bill_payment", "billpayment", "debit",
}


def classify_bank_transaction_amount(txn: dict, account_id: str = None) -> Tuple[Optional[float], Optional[float]]:
    """SUPERSEDED as run_reconciliation()'s primary check-2 data source as
    of 2026-08-14 (later still) by classify_account_transaction_amount()
    below, which reads Zoho's own General Ledger view of the account (GET
    /chartofaccounts/{account_id}/transactions) instead of the ambiguous
    per-object-type /banktransactions feed this function was built for --
    see that function's own docstring for why. Left defined (not deleted):
    classify_account_transaction_amount() falls back to calling this
    function when a record doesn't look like a GL entry, so this is still
    live code, just no longer the first thing tried.

    Returns (debit_amount, credit_amount) for ONE Zoho bank-transaction
    feed record, in Zoho's OWN ledger sense (debit = money IN/asset
    increase, credit = money OUT/asset decrease -- see the CORRECTED note
    above _DEBIT_TRANSACTION_TYPES/_CREDIT_TRANSACTION_TYPES for why this is
    the opposite convention from a bank statement's own Debit/Credit
    columns). Exactly one of the two is a float and the other is None for a
    normal record; BOTH None means this record's shape wasn't recognized at
    all (goes to aggregate_bank_transactions()'s "unclassified" bucket,
    never silently dropped or silently miscounted -- see this module's own
    docstring on why that matters for a financial reconciliation tool built
    against an unconfirmed API shape).

    Tries, in order:
      1. Explicit debit_amount/credit_amount fields, if Zoho's feed splits
         them that way -- left as-is (not swapped) since if Zoho literally
         sends fields already named "debit"/"credit", they're presumably
         Zoho's own authoritative ledger values already in ledger sense,
         not this module's guess to correct.
      2. deposit_amount/withdrawal_amount (and deposits/withdrawals) --
         added 2026-08-14, matching the literal "DEPOSITS"/"WITHDRAWALS"
         column headers Ravindra confirmed live in Zoho's own Banking
         module register UI for a real bank account. A deposit is a ledger
         debit (money in), a withdrawal is a ledger credit (money out).
      3. A single "amount" field alongside a "transaction_type" whose value
         is in _DEBIT_TRANSACTION_TYPES/_CREDIT_TRANSACTION_TYPES above.
      4. A single "amount" field with a SIGN (negative = credit/money out,
         positive = debit/money in) and no usable transaction_type -- last
         resort, since sign convention itself isn't confirmed either.

    UNVERIFIED LIVE -- none of these shapes has been confirmed against a
    real Zoho response; the exact field names (including the new deposit/
    withdrawal guesses above) still need confirming on a live run. Correct
    this function first (it's the one place this guessing logic lives, kept
    deliberately out of zoho_books_client.py -- see that file's
    find_bank_account_transactions() docstring) once a real `txn` dict is
    available to inspect -- if a live run's aggregate check keeps failing
    and aggregate_match's "zoho_unclassified_count" is nonzero, that's the
    first thing to check (see run_reconciliation()'s caveat for that).

    `account_id` (added 2026-08-14, per a real live reconciliation run
    Ravindra reported): a Bank Transfer record (transaction_type=
    "transfer_fund" -- see zoho_books_client.py's create_bank_transfer())
    is STRUCTURALLY DIFFERENT from every other type above -- it has a fixed,
    UNSIGNED "amount" plus explicit from_account_id/to_account_id (money
    moves FROM from_account_id TO to_account_id), and whether that's a
    deposit or a withdrawal depends entirely on which side of the transfer
    THIS account is on -- there is no fixed "transfer_fund = debit" or
    "transfer_fund = credit" mapping the way there is for expense/invoice/
    etc. Confirmed live: a real reconciliation run showed every single
    "Transfer Fund" row in Zoho's own Banking-module register for one
    account under the WITHDRAWALS column only (this account was
    consistently the FROM side, paying out to an Employee Reimbursements
    clearing account) -- but "transfer_fund" was never in either type set
    above, so every one of those records fell through to the sign-based
    fallback and got misclassified as money IN (debit), since their
    unsigned positive "amount" looked identical to a real deposit. Passing
    `account_id` (the SAME account_id this reconciliation run's
    find_bank_account_transactions() call was scoped to) lets this function
    compare it against the record's own from_account_id/to_account_id and
    resolve the real direction instead of guessing from the sign. Without
    `account_id` (None, the default), behavior for transfer_fund records is
    unchanged from before this fix -- falls through to the type-set/sign
    guess, which is known wrong for this record type."""
    for debit_key, credit_key in (
        ("debit_amount", "credit_amount"),
        ("deposit_amount", "withdrawal_amount"),
        ("deposits", "withdrawals"),
    ):
        d = _amount_as_float(txn.get(debit_key))
        c = _amount_as_float(txn.get(credit_key))
        if d or c:
            return (d or None), (c or None)

    amount = _amount_as_float(txn.get("amount"))
    txn_type = str(txn.get("transaction_type") or "").strip().lower()

    if txn_type in ("transfer_fund", "banktransfer", "bank_transfer") and amount is not None and account_id:
        from_id = str(txn.get("from_account_id") or "").strip()
        to_id = str(txn.get("to_account_id") or "").strip()
        target = str(account_id).strip()
        if from_id and from_id == target:
            # This account is the SOURCE of the transfer -> money OUT -> credit.
            return None, abs(amount)
        if to_id and to_id == target:
            # This account is the DESTINATION of the transfer -> money IN -> debit.
            return abs(amount), None
        # Neither id matched this account (or matched both, or was blank) --
        # can't resolve direction from this record; falls through to the
        # generic type-set/sign logic below, same as when account_id isn't
        # passed at all.

    if amount is not None and txn_type:
        if txn_type in _DEBIT_TRANSACTION_TYPES:
            return abs(amount), None
        if txn_type in _CREDIT_TRANSACTION_TYPES:
            return None, abs(amount)

    if amount is not None and amount != 0:
        return (None, abs(amount)) if amount < 0 else (abs(amount), None)

    return None, None


def _summarize_zoho_transaction_for_debug(txn: dict, debit: Optional[float], credit: Optional[float]) -> dict:
    """Builds a small, human-readable summary of ONE raw Zoho bank-
    transaction record plus how classify_bank_transaction_amount() read it
    -- added 2026-08-14, per Ravindra: "can you show me either on the
    screen or the log the transaction amounts we posted whether credit or
    debit on the screen so that i can confirm whether ur reading the
    values. can you list down all the entries for the time period from
    zoho and print them." This is a DIAGNOSTIC view, not a new source of
    truth -- it pulls a handful of plausible display fields (date,
    reference, description/party) via best-guess keys the same way the
    rest of this module does, purely so a human can eyeball "does this row
    look right" without needing direct access to the raw API response or
    the Cloud Run logs. Never raises -- a field that isn't found is just
    left None."""
    def _first(*keys):
        for k in keys:
            v = txn.get(k)
            if v not in (None, ""):
                return v
        return None

    return {
        "transaction_type": txn.get("transaction_type"),
        "date": _first("date", "transaction_date", "txn_date"),
        "reference_number": _first("reference_number", "reference"),
        "description": _first("description", "notes", "reason", "party_name", "vendor_name", "customer_name"),
        "raw_amount": _first("amount", "debit_amount", "credit_amount", "deposit_amount", "withdrawal_amount",
                              "deposits", "withdrawals"),
        "from_account_id": txn.get("from_account_id"),
        "to_account_id": txn.get("to_account_id"),
        "classified_debit": debit,
        "classified_credit": credit,
        "classification": "debit" if debit is not None else ("credit" if credit is not None else "unclassified"),
    }


def aggregate_bank_transactions(transactions: List[dict], account_id: str = None) -> dict:
    """Sums debit/credit totals across every Zoho bank-transaction feed
    record, via classify_bank_transaction_amount() per record. `account_id`
    (added 2026-08-14) is threaded straight through to that function --
    needed to correctly resolve transfer_fund records' direction, see its
    own docstring. Returns {"total_debit", "total_credit",
    "unclassified_count", "unclassified_sample" (up to 10 raw records that
    couldn't be classified, for display/diagnosis -- never silently
    swallowed), "transactions_detail" (EVERY record -- see
    _summarize_zoho_transaction_for_debug() -- for the "show me every
    entry and how it was classified" diagnostic view Ravindra asked for)}."""
    total_debit = 0.0
    total_credit = 0.0
    unclassified = []
    detail = []
    for txn in transactions:
        debit, credit = classify_bank_transaction_amount(txn, account_id=account_id)
        detail.append(_summarize_zoho_transaction_for_debug(txn, debit, credit))
        if debit is None and credit is None:
            unclassified.append(txn)
            continue
        total_debit += debit or 0.0
        total_credit += credit or 0.0
    return {
        "total_debit": total_debit,
        "total_credit": total_credit,
        "unclassified_count": len(unclassified),
        "unclassified_sample": unclassified[:10],
        "transactions_detail": detail,
    }


def classify_account_transaction_amount(txn: dict, account_id: str = None) -> Tuple[Optional[float], Optional[float]]:
    """Returns (debit_amount, credit_amount) for ONE record from Zoho's own
    GENERAL LEDGER view of an account (zoho_books_client.py's
    list_account_transactions(), GET /chartofaccounts/{account_id}/
    transactions) -- added 2026-08-14 (later still), per Ravindra: "can you
    actually pull the transactions for the specified time period using
    Journal records in Zoho where you get all the debit and credit entries
    for each of the transactions for the selected account."

    This is a MUCH more reliable classification than classify_bank_
    transaction_amount() above: that function has to guess a direction for
    a single ambiguous "amount" field across 8+ different Zoho object types
    (Expense, Vendor Payment, Bank Transfer, ...), including the transfer_
    fund special-case where direction depends on comparing from_account_id/
    to_account_id against the account being reconciled. A General Ledger
    entry, by definition, is ALREADY resolved to a specific side (debit or
    credit) against THIS SPECIFIC account -- there's no "amount + type ->
    guess direction" step needed at all, IF Zoho's response actually tags
    each entry that way (which is the standard shape for any ledger/GL
    report, but still UNVERIFIED LIVE against a real response from this
    environment -- see list_account_transactions()'s own docstring).

    Tries, in order:
      1. debit_amount/credit_amount -- the standard field-pair name for a
         ledger entry (and also what Zoho's OWN Journal line_items already
         use elsewhere in this codebase's create_journal_entry()/
         _extract_zoho_record_amount(), so it's a reasonable first guess
         here too).
      2. debit/credit (shorter alternate spelling some ledger/report APIs
         use instead of the "_amount" suffix).
      3. Falls back to classify_bank_transaction_amount(txn, account_id) --
         covers the case where this endpoint actually returns records
         shaped like the /banktransactions feed (transaction_type + a
         single "amount", possibly a transfer_fund needing from/to-id
         resolution) rather than a true tagged ledger entry -- so a live
         run still gets a best-effort classification either way instead of
         going straight to "unclassified" if the GL-style guess doesn't
         match.

    UNVERIFIED LIVE -- correct this function first (following the same
    "paste the raw record back" discipline as every other guess in this
    module) once a real record from list_account_transactions() is
    available to inspect."""
    for debit_key, credit_key in (("debit_amount", "credit_amount"), ("debit", "credit")):
        d = _amount_as_float(txn.get(debit_key))
        c = _amount_as_float(txn.get(credit_key))
        if d or c:
            return (d or None), (c or None)

    return classify_bank_transaction_amount(txn, account_id=account_id)


def aggregate_account_transactions(transactions: List[dict], account_id: str = None) -> dict:
    """Sums debit/credit totals across every record from Zoho's General
    Ledger view of an account (list_account_transactions()), via
    classify_account_transaction_amount() per record -- the PRIMARY data
    source run_reconciliation() now uses for check 2 (aggregate credits/
    debits), see that function's own docstring for why this replaced
    aggregate_bank_transactions() above. Same return shape as that
    function: {"total_debit", "total_credit", "unclassified_count",
    "unclassified_sample", "transactions_detail"} -- see
    aggregate_bank_transactions()'s own docstring for what each key means,
    unchanged here."""
    total_debit = 0.0
    total_credit = 0.0
    unclassified = []
    detail = []
    for txn in transactions:
        debit, credit = classify_account_transaction_amount(txn, account_id=account_id)
        detail.append(_summarize_zoho_transaction_for_debug(txn, debit, credit))
        if debit is None and credit is None:
            unclassified.append(txn)
            continue
        total_debit += debit or 0.0
        total_credit += credit or 0.0
    return {
        "total_debit": total_debit,
        "total_credit": total_credit,
        "unclassified_count": len(unclassified),
        "unclassified_sample": unclassified[:10],
        "transactions_detail": detail,
    }


def _extract_zoho_record_amount(full_record: dict) -> Optional[float]:
    """Best-effort amount extraction from a Zoho record's own full detail
    (a GET response, same records match_statement_rows_to_zoho() below
    already fetched for their narration text) -- used to confirm the
    "check the credit/debit amount" half of that function's match. Tries
    the flat field names this codebase itself SENDS when creating each
    record type ("amount" -- see zoho_books_client.py's create_expense()/
    create_vendor_payment()/create_bank_transfer(), all of which take a
    flat `amount` param), then "total" (a Journal Entry has no flat
    `amount` at all -- it's a double-entry line_items array, so its detail
    response is expected to expose a computed running total there
    instead), then as a last resort sums a Journal's own line_items --
    debit-side amounts if "debit_or_credit" tagging is present, else half
    the sum of every line item (a balanced journal's total debit always
    equals its total credit, which equals half the sum of both sides).
    Returns None if nothing usable is found at all.

    UNVERIFIED LIVE, same discipline as classify_bank_transaction_amount()
    -- none of these field names has been confirmed against a real Zoho
    response for this specific purpose; correct this first if a real
    narration match is still failing the amount check."""
    for key in ("amount", "total", "bcy_total"):
        val = _amount_as_float(full_record.get(key))
        if val:
            return val
    line_items = full_record.get("line_items")
    if isinstance(line_items, list) and line_items:
        debit_total = sum(
            (_amount_as_float(li.get("amount")) or 0.0)
            for li in line_items
            if str(li.get("debit_or_credit") or "").strip().lower() == "debit"
        )
        if debit_total:
            return round(debit_total, 2)
        all_total = sum((_amount_as_float(li.get("amount")) or 0.0) for li in line_items)
        if all_total:
            return round(all_total / 2, 2)
    return None


def match_statement_rows_to_zoho(statement_rows: List[dict], client, organization_id) -> dict:
    """Answers "was this statement row actually posted" (reconciliation
    check 3). REDESIGNED 2026-08-13 (later same day), per Ravindra: "the
    row check is a simple check, find the posting in zoho with the
    narration==description/notes and if found check the credit/debit
    amount, if match then its a match for the post" -- replacing the
    original amount+direction match against the guessed bank-transaction
    feed (find_bank_account_transactions() -- as of 2026-08-14 no longer
    used for check 2's aggregate totals either, see this module's own
    docstring) with a narration-first search.

    Deliberately reuses posting_service.py's EXISTING, already-live
    duplicate-detection infrastructure instead of inventing a new search:
    _ALL_DESCRIPTION_SEARCHES (one (type_label, find_by_date method name,
    get_full_record method name, id_keys, notes/description field name)
    entry per Journal/Expense/Vendor Payment/Bank Transfer) and
    _normalize_text_for_dup_check() (whitespace-collapse + casefold, exact
    match on the normalized text -- not fuzzy, same reasoning as that
    function's own docstring: a loose substring match could flag two
    genuinely different transactions that merely share a word or two).
    This is the same mechanism that already runs on every single post this
    pipeline makes (_check_duplicate_description_cross_type()), so unlike
    the balance/aggregate checks' own Zoho endpoints, this search path is
    NOT a fresh guess -- only _extract_zoho_record_amount()'s per-type
    amount field above is new/unverified.

    For each statement row (in file order): normalize its narration, then
    search EVERY Zoho object type dated the exact same day as the row (one
    find_*_by_date() list call per type, cached per date across rows so a
    statement with several rows on the same day doesn't repeat the same
    Zoho calls) for a record whose own notes/description text -- fetched
    via a full detail GET, same as the duplicate check -- normalizes to an
    EXACT match. Among any such narration matches not already claimed by
    an earlier statement row, the first whose extracted amount is within
    _AMOUNT_TOLERANCE of this row's own debit/credit amount is the match,
    and is marked consumed so two statement rows can never claim the same
    Zoho record.

    Returns {"matched_rows": [{...statement row fields..., "zoho_
    transaction_id", "zoho_record_type"}], "unmatched_rows": [...statement
    row fields, plus "unmatched_reason" explaining WHY -- no narration
    match at all, vs. a narration match with no matching amount (with the
    closest amount found, for a quick manual cross-check) -- never just a
    bare "not found"], "matched_count", "unmatched_count"}.

    NOTE: this searches across the WHOLE organization for the day, not
    restricted to this specific bank account (none of the find_*_by_date()
    methods support an account filter) -- see run_reconciliation()'s own
    caveats list for why that's an acceptable, deliberate trade-off given
    how specific a narration+amount match already is."""
    consumed = set()  # (type_label, record_id)
    candidates_by_date = {}  # txn_date ("YYYY-MM-DD") -> [candidate dict, ...]

    def _candidates_for_date(txn_date):
        if txn_date in candidates_by_date:
            return candidates_by_date[txn_date]
        found = []
        for type_label, find_fn_name, get_fn_name, id_keys, field_name in _ALL_DESCRIPTION_SEARCHES:
            try:
                listed = getattr(client, find_fn_name)(txn_date, organization_id=organization_id)
            except Exception as e:
                print(f"[reconciliation_service] {type_label} search failed for {txn_date}: {e}", flush=True)
                continue
            for rec in listed:
                rec_id = next((rec[key] for key in id_keys if rec.get(key)), None)
                if rec_id is None:
                    continue
                try:
                    full = getattr(client, get_fn_name)(rec_id, organization_id=organization_id)
                except Exception as e:
                    print(f"[reconciliation_service] couldn't fetch {type_label} {rec_id} dated {txn_date}: "
                          f"{e}", flush=True)
                    continue
                found.append({
                    "type_label": type_label,
                    "id": rec_id,
                    "narration_norm": _normalize_text_for_dup_check(full.get(field_name)),
                    "amount": _extract_zoho_record_amount(full),
                })
        candidates_by_date[txn_date] = found
        return found

    matched_rows = []
    unmatched_rows = []
    for row in statement_rows:
        row_amount = (row.get("debit") or 0.0) or (row.get("credit") or 0.0)
        narration_norm = _normalize_text_for_dup_check(row.get("narration"))
        txn_date = row.get("txn_date")
        if not row_amount or not narration_norm or not txn_date:
            unmatched_rows.append({
                **row,
                "unmatched_reason": "This row is missing a narration, a parseable date, or a debit/credit "
                                     "amount -- can't search Zoho for it at all.",
            })
            continue

        narration_matches = [
            c for c in _candidates_for_date(txn_date)
            if c["narration_norm"] and c["narration_norm"] == narration_norm
            and (c["type_label"], c["id"]) not in consumed
        ]
        if not narration_matches:
            unmatched_rows.append({
                **row,
                "unmatched_reason": f"No Journal/Expense/Vendor Payment/Bank Transfer dated {txn_date} in Zoho "
                                     f"has this exact narration in its own notes/description field.",
            })
            continue

        amount_match = next(
            (c for c in narration_matches
             if c["amount"] is not None and abs(c["amount"] - row_amount) <= _AMOUNT_TOLERANCE),
            None,
        )
        if amount_match is None:
            with_amount = [c for c in narration_matches if c["amount"] is not None]
            closest = min(with_amount, key=lambda c: abs(c["amount"] - row_amount)) if with_amount else None
            reason = (f"{len(narration_matches)} Zoho record(s) dated {txn_date} have this exact narration, but "
                      f"none has a matching amount (statement row: {row_amount}"
                      + (f", closest Zoho match: {closest['type_label']} {closest['id']} = {closest['amount']})"
                         if closest else ", none of the matches had a readable amount at all)."))
            unmatched_rows.append({**row, "unmatched_reason": reason})
            continue

        consumed.add((amount_match["type_label"], amount_match["id"]))
        matched_rows.append({
            **row,
            "zoho_transaction_id": amount_match["id"],
            "zoho_record_type": amount_match["type_label"],
        })

    return {
        "matched_rows": matched_rows,
        "unmatched_rows": unmatched_rows,
        "matched_count": len(matched_rows),
        "unmatched_count": len(unmatched_rows),
    }


def run_reconciliation(statement_wb, zoho_balance_info: dict, zoho_transactions: List[dict],
                        client, organization_id, zoho_account_id, expected_bank_account_number: str,
                        month: int, year: int) -> dict:
    """Orchestrates the 3 confirmed checks against ONE already-downloaded,
    already-parsed month-end statement workbook. Returns a single
    structured result:
      {"month", "year",
       "statement_summary": {closing_balance_computed, closing_balance_
         header_text, total_debit, total_credit, row_count,
         unparseable_date_rows, closing_balance_tied_rows},
       "account_number_sanity": {"statement_account_number",
         "expected_bank_account_number", "matches": bool|None (None if the
         statement's own header didn't have a parseable account number at
         all -- not every bank export includes one)},
       "checks": {
         "balance_match": {"pass": bool, "statement_balance",
           "zoho_balance", "difference"},
         "aggregate_match": {"pass": bool, "statement_total_debit",
           "statement_total_credit", "zoho_total_debit", "zoho_total_credit"
           (all 4 raw totals, each side in ITS OWN native convention -- see
           the cross-mapping note below), "statement_debit_vs_zoho_credit_
           difference", "statement_credit_vs_zoho_debit_difference"
           (the two DIFFERENCES that actually decide "pass", deliberately
           CROSS-mapped -- see comment at this check's computation below for
           why), "zoho_unclassified_count", "zoho_transactions_count" (raw
           record count returned by list_account_transactions() for this
           account/period, BEFORE any classification -- 0 here with both
           Zoho totals also 0.00 means Zoho returned nothing at all, not
           that everything failed to classify), "zoho_transactions_
           debug" (EVERY raw record fetched, each with its type/date/
           reference/description/raw amount/from-to account ids and how it
           was classified -- added 2026-08-14 so the actual Zoho-side data
           can be eyeballed directly instead of trusting the totals alone)},
         "all_posted": {"pass": bool, "matched_count", "unmatched_count",
           "unmatched_rows": [...]},
       },
       "overall_pass": bool (True only if all 3 checks pass),
       "caveats": [str, ...] (ALWAYS includes the real-time-balance caveat
         and the unverified-Zoho-API-shape caveat -- see this module's own
         docstring -- plus a note if any Zoho transaction couldn't be
         classified at all)}.

    `client`/`organization_id` (2026-08-13, later same day, added alongside
    match_statement_rows_to_zoho()'s narration-based redesign): the live
    ZohoBooksClient and org override check 3 now needs to run its own
    by-date searches directly -- `zoho_transactions` (still required) is
    ONLY used for check 2's aggregate totals now, not check 3 at all.

    `zoho_account_id` (added 2026-08-14): this account's real Zoho
    account_id -- passed straight through to aggregate_account_transactions()/
    classify_account_transaction_amount() (and, via that function's
    fallback, to classify_bank_transaction_amount() too) for correctly
    resolving any record whose direction still depends on comparing
    from_account_id/to_account_id against this specific account.

    `zoho_transactions` (2026-08-14, later still, per Ravindra: "pull the
    transactions for the specified time period using Journal records in
    Zoho where you get all the debit and credit entries for each of the
    transactions for the selected account"): now expected to be the output
    of zoho_books_client.py's list_account_transactions() -- Zoho's own
    General Ledger view of this account -- NOT find_bank_account_
    transactions()'s per-object-type feed as before. See aggregate_account_
    transactions()'s own docstring for why this is a more reliable source.
    The parameter name is kept as-is (not renamed) purely to minimize
    signature churn for this same-day change; what it should now contain
    has changed."""
    parsed = parse_month_end_statement(statement_wb)
    aggregate = aggregate_account_transactions(zoho_transactions, account_id=zoho_account_id)
    match_result = match_statement_rows_to_zoho(parsed["rows"], client, organization_id)

    zoho_balance = _amount_as_float(zoho_balance_info.get("balance"))
    statement_balance = parsed["closing_balance_computed"]
    balance_diff = (None if statement_balance is None or zoho_balance is None
                     else round(statement_balance - zoho_balance, 2))
    balance_pass = balance_diff is not None and abs(balance_diff) <= _AMOUNT_TOLERANCE

    # CROSS-MAPPED ON PURPOSE (2026-08-14, per Ravindra): "The statement's
    # total on the credit side should match with the debit side total sum
    # of the ledger in zoho, same thing with the credit side also." A raw
    # bank STATEMENT's Debit/Credit columns are from the BANK's point of
    # view (parse_month_end_statement() reads them as-is, unrelabeled --
    # statement Debit = money OUT of the account, Credit = money IN).
    # classify_bank_transaction_amount() above was just corrected so
    # aggregate["total_debit"]/["total_credit"] are in Zoho's own LEDGER
    # sense for this bank account as an ASSET (debit = money IN, credit =
    # money OUT) -- the OPPOSITE convention from the statement's columns for
    # the exact same money movement. So the two sides that describe the
    # SAME real-world direction are: statement Debit (money out) <-> Zoho
    # Credit (money out), and statement Credit (money in) <-> Zoho Debit
    # (money in) -- deliberately cross-mapped below. Do NOT "fix this back"
    # to same-side (statement_debit vs zoho_debit) -- that reintroduces the
    # exact bug being fixed here, just moved up one level.
    debit_diff = round(parsed["total_debit"] - aggregate["total_credit"], 2)
    credit_diff = round(parsed["total_credit"] - aggregate["total_debit"], 2)
    aggregate_pass = abs(debit_diff) <= _AMOUNT_TOLERANCE and abs(credit_diff) <= _AMOUNT_TOLERANCE

    all_posted_pass = match_result["unmatched_count"] == 0

    statement_account_digits = _digits_only(parsed.get("account_number_header_text"))
    expected_digits = _digits_only(expected_bank_account_number)
    account_number_matches = (None if not statement_account_digits
                               else statement_account_digits == expected_digits)

    caveats = [
        "Check 1 (month-end balance) compares against Zoho's CURRENT bank account balance, not a "
        "point-in-time historical one -- Zoho Books' API has no confirmed way to fetch a balance as of a "
        "past date. This comparison is only meaningful if nothing has posted to this account in Zoho "
        "after the statement's own end date.",
        "The 'books' side of checks 1 and 2 (balance, aggregate credits/debits) comes from Zoho's own "
        "General Ledger view of this account (list_account_transactions()) -- the exact response field "
        "names for that endpoint could not be confirmed against Zoho's own live documentation from this "
        "environment (only the endpoint's existence, from its public API reference page) -- flagged "
        "UNVERIFIED LIVE in the code. Confirm these two numbers look right on the first real run, and see "
        "'zoho_transactions_debug' below to check exactly what was read from each record.",
        "Check 3 (every entry posted) matches each statement row to a Zoho record by an EXACT normalized "
        "match on its narration against that record's own notes/description field, then confirms the "
        "debit/credit amount -- it only searches Zoho records dated the exact same day as the statement "
        "row, across the whole organization (not just this specific bank account), so a real posting dated "
        "a day off (e.g. value date vs. transaction date) or whose narration text was altered on save will "
        "show as unmatched even though it's actually posted. See each unmatched row's own reason for which "
        "of these applies.",
    ]
    if aggregate["unclassified_count"] and aggregate["unclassified_count"] < len(zoho_transactions):
        caveats.append(
            f"{aggregate['unclassified_count']} of {len(zoho_transactions)} Zoho transaction(s) for this "
            f"account/period had a shape this tool didn't recognize as either a debit or a credit -- "
            f"excluded from the aggregate totals above (see 'zoho_unclassified_count' and "
            f"'zoho_transactions_debug'). This likely means the guessed field names in classify_account_"
            f"transaction_amount() (or its classify_bank_transaction_amount() fallback) need correcting "
            f"against a real response."
        )
    if parsed["unparseable_date_rows"]:
        caveats.append(
            f"{parsed['unparseable_date_rows']} statement row(s) had a transaction date this tool "
            f"couldn't parse -- still included in the debit/credit totals, but excluded from the "
            f"closing-balance-by-date calculation."
        )
    if parsed["closing_balance_tied_rows"]:
        caveats.append(
            f"{parsed['closing_balance_tied_rows']} other row(s) shared the same date as the row used for "
            f"the closing balance -- the closing balance used is the FIRST such row in file order (this "
            f"bank's export lists transactions newest-first); check the statement directly if that's not "
            f"the right one."
        )
    if parsed["closing_balance_layout_mismatch"]:
        caveats.append(
            f"The row used for the closing balance (dated {parsed['closing_balance_computed_date']!r}, the "
            f"FIRST row in the file among eligible rows) is NOT actually the latest date found among "
            f"eligible rows -- this statement doesn't appear to be in the assumed newest-first order. The "
            f"closing balance above may not be correct; check the statement directly."
        )
    if parsed["running_balance_backfilled_count"]:
        note = (
            f"{parsed['running_balance_backfilled_count']} row(s) in this statement had a blank Running "
            f"Balance cell (a formula that was never recalculated before the file was saved/exported) -- "
            f"computed here instead using each row's own debit/credit against the nearest row below it "
            f"with a known balance, the same arithmetic the file's own formulas use."
        )
        if parsed["closing_balance_row_backfilled"]:
            note += (
                f" This INCLUDES the row actually used for the closing balance above (dated "
                f"{parsed['closing_balance_computed_date']!r}) -- worth double-checking that figure "
                f"against the statement directly, since it was computed here rather than read from the "
                f"file."
            )
        caveats.append(note)
    if (parsed["closing_balance_header_text"] is not None and parsed["closing_balance_computed"] is not None
            and abs(parsed["closing_balance_header_text"] - parsed["closing_balance_computed"]) > _AMOUNT_TOLERANCE):
        caveats.append(
            f"The transaction-table-derived closing balance above ({parsed['closing_balance_computed']}) "
            f"does NOT match this statement's own 'Current Balance:' header text "
            f"({parsed['closing_balance_header_text']}) -- a difference of "
            f"{round(parsed['closing_balance_computed'] - parsed['closing_balance_header_text'], 2)}. The "
            f"transaction-table figure is what's used for the check above (it reflects every row actually "
            f"in the file, including any backfilled above), but this mismatch usually means the header "
            f"banner text is stale relative to the transaction rows (e.g. rows were added/edited after the "
            f"header was last generated) -- worth confirming which one is actually current."
        )
    if parsed["rows_after_period_end"]:
        caveats.append(
            f"This statement declares its own period as {parsed['period_start']!r} to "
            f"{parsed['period_end']!r}, but {parsed['rows_after_period_end']} row(s) are dated AFTER that "
            f"declared end -- excluded from the closing balance and aggregate debit/credit totals above "
            f"(so those stay consistent with what the statement itself claims to cover), but still "
            f"included in the 'every entry posted' check below, since they're real transactions either "
            f"way. This usually means the export included a bit more than its own declared period covers "
            f"-- worth confirming {parsed['closing_balance_computed_date']!r} (not a later date in the "
            f"file) really is the balance you want checked."
        )
    if account_number_matches is False:
        caveats.append(
            f"The statement's own header account number ({parsed['account_number_header_text']!r}) does "
            f"not match the account this reconciliation was run for ({expected_bank_account_number!r}) -- "
            f"double-check the right file was matched before trusting any of the checks below."
        )
    if not zoho_transactions:
        caveats.append(
            "Zoho returned ZERO general-ledger records for this account/period (list_account_"
            "transactions() came back empty) -- both Zoho aggregate totals below are 0.00 because there "
            "was nothing to sum, not because everything failed to classify. This usually means the "
            "account_id, date_start/date_end, or organization_id sent to that call don't actually match "
            "what Zoho has, or that endpoint isn't returning what this tool assumes it does -- see "
            "'zoho_transactions_debug' (empty here) and the Cloud Run logs around this request for the "
            "exact account_id/date range that was queried."
        )
    elif aggregate["unclassified_count"] == len(zoho_transactions):
        caveats.append(
            f"Zoho returned {len(zoho_transactions)} record(s) for this account/period, but NONE of them "
            f"could be classified as a debit or credit at all (see 'zoho_transactions_debug' below for the "
            f"raw values read from each one) -- both Zoho aggregate totals are 0.00 as a result. This "
            f"points at classify_bank_transaction_amount()'s guessed field names being wrong for this "
            f"organization's actual response shape, not at the records themselves being empty."
        )

    return {
        "month": month,
        "year": year,
        "statement_summary": {
            "closing_balance_computed": statement_balance,
            "closing_balance_computed_date": parsed["closing_balance_computed_date"],
            "closing_balance_header_text": parsed["closing_balance_header_text"],
            "total_debit": parsed["total_debit"],
            "total_credit": parsed["total_credit"],
            "row_count": len(parsed["rows"]),
            "unparseable_date_rows": parsed["unparseable_date_rows"],
            "closing_balance_tied_rows": parsed["closing_balance_tied_rows"],
            "closing_balance_layout_mismatch": parsed["closing_balance_layout_mismatch"],
            "closing_balance_row_backfilled": parsed["closing_balance_row_backfilled"],
            "running_balance_backfilled_count": parsed["running_balance_backfilled_count"],
            "period_start": parsed["period_start"],
            "period_end": parsed["period_end"],
            "rows_after_period_end": parsed["rows_after_period_end"],
        },
        "account_number_sanity": {
            "statement_account_number": parsed["account_number_header_text"],
            "expected_bank_account_number": expected_bank_account_number,
            "matches": account_number_matches,
        },
        "checks": {
            "balance_match": {
                "pass": balance_pass,
                "statement_balance": statement_balance,
                "zoho_balance": zoho_balance,
                "difference": balance_diff,
            },
            "aggregate_match": {
                "pass": aggregate_pass,
                # Raw totals, each in its OWN native convention (statement =
                # bank's point of view, zoho = ledger/asset point of view --
                # see the cross-mapping comment above where debit_diff/
                # credit_diff are computed). NOT meant to be read same-side
                # against each other -- use the *_difference keys below,
                # which are already correctly cross-mapped.
                "statement_total_debit": parsed["total_debit"],
                "statement_total_credit": parsed["total_credit"],
                "zoho_total_debit": aggregate["total_debit"],
                "zoho_total_credit": aggregate["total_credit"],
                # Cross-mapped: statement Debit (money out) vs Zoho Credit
                # (money out), and statement Credit (money in) vs Zoho Debit
                # (money in) -- these two differences are what actually
                # decide "pass" above.
                "statement_debit_vs_zoho_credit_difference": debit_diff,
                "statement_credit_vs_zoho_debit_difference": credit_diff,
                "zoho_unclassified_count": aggregate["unclassified_count"],
                # Diagnostic view (2026-08-14, per Ravindra: "list down all
                # the entries for the time period from zoho and print
                # them") -- EVERY raw Zoho record fetched for this account/
                # period, plus how classify_bank_transaction_amount() read
                # it, so it's possible to eyeball on screen whether the
                # right records were even found at all, not just whether
                # the final totals match. See _summarize_zoho_transaction_
                # for_debug()'s own docstring.
                "zoho_transactions_count": len(zoho_transactions),
                "zoho_transactions_debug": aggregate["transactions_detail"],
            },
            "all_posted": {
                "pass": all_posted_pass,
                "matched_count": match_result["matched_count"],
                "unmatched_count": match_result["unmatched_count"],
                "unmatched_rows": match_result["unmatched_rows"],
            },
        },
        "overall_pass": balance_pass and aggregate_pass and all_posted_pass,
        "caveats": caveats,
    }