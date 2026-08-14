"""
posting_service.py -- posting_type -> Zoho Books action dispatch for the
"Post to Zoho Books" feature (2026-08-09, per Ravindra: "i am done with the
categorization now i want to post these entries to books based on the
posting type column of the statement").

Deliberately kept SEPARATE from categorize_from_module.py, same as this
project's original July 26 design -- that file has never made a live Zoho
API call of its own and still doesn't; every actual Zoho Books write (and,
new in this update, delete) lives here, using zoho_books_client.py.
main.py's /post-transactions and /delete-postings endpoints wire the two
files together:
    extract_postable_rows() (categorize_from_module.py)
      -> post_rows() (here)
      -> write_posting_results() (categorize_from_module.py)
and, for deleting:
    delete_posting() (here, per posting)
      -> clear_posting_reference() (categorize_from_module.py)

POSTING TYPE -> ZOHO ACTION MAPPING (CORRECTED 2026-08-09 -- the original
July 26 design's "transfers are just journal entries" assumption was wrong;
see create_bank_transfer()'s docstring in zoho_books_client.py for the full
story of how this was caught):
  - "Journal" -- posted as a 2-line Journal Entry (POST /journals): debit the
    row's debit_account, credit the row's credit_account, both for the same
    amount. Generic double-entry, no restriction on account type.
  - "Transfer to another account", "Transfer from another account" --
    posted as a Bank Transfer (POST /banktransactions, transaction_type=
    "transfer_fund") via zoho_books_client.create_bank_transfer(), NOT as a
    Journal Entry. This is what Zoho's own "Transfer to Another Account" UI
    screen uses under the hood, and Zoho REQUIRES both accounts to be
    Bank/Card type for it -- which is exactly why a category should only
    carry one of these two cf_type_of_transaction values when its
    cf_account_to_be_posted account really is Bank/Card type in Zoho (e.g.
    EMPLOYEE_REIMBURSEMENT_ACCOUNT, historically). from_account_id/
    to_account_id are derived from the row's own credit_account/
    debit_account -- see _post_bank_transfer() below for the direction
    reasoning. Shows up in Zoho's own Banking > Transfers view, unlike a
    journal entry.
  - "Expense" -- posted via POST /expenses (account_id = the expense GL
    account, paid_through_account_id = the bank account this statement's own
    account represents). Which of the row's debit_account/credit_account is
    which is determined by comparing each against the row's own
    account_number (this statement's own resolved account_id, from
    categorize_from_module.py's _resolve_row_accounts()) -- whichever side
    EQUALS account_number is the bank/paid-through side; the other is the
    expense account.
  - "Vendor Payment" -- posted via POST /vendorpayments, vendor looked up by
    fuzzy-matching the narration text against Zoho's own vendor contacts
    (find_contact_id() below -- same "don't require an exact match, but
    never guess an ambiguous one" substring approach used elsewhere in this
    project for OneDrive file matching and account matching). No `bills`
    array sent, since Zoho's API makes that optional -- lands as an
    on-account vendor payment.
  - "Customer payment" -- deliberately NEVER auto-posted. Real API
    limitation, not a design choice: Zoho's POST /customerpayments requires
    an `invoices` array (invoice_id + amount_applied per line) with no
    documented way to record an unapplied/on-account customer payment,
    unlike Vendor Payment where the bill reference is optional. Since this
    pipeline has no invoice-matching logic, every Customer payment row is
    left "NOT POSTED" with a clear explanation rather than guessing which
    invoice to apply it to.
  - Anything else (blank/unrecognized posting_type) -- "NOT POSTED", not an
    error -- a Categorization rule with a blank/unexpected
    cf_type_of_transaction is a data gap in the module, not a crash.

RESULT TEXT CONVENTION (used by categorize_from_module.py's
_is_success_reference(), and by delete_posting() below to figure out WHAT
kind of Zoho object a given reference points at):
  - Success:    "<Type> <zoho_record_id>", e.g. "Journal 460000000012345",
                "Expense 460000000067890", "Vendor Payment 460000000011223".
  - Not posted: "NOT POSTED: <reason>" -- eligible to retry on the next run
                once the underlying issue (missing account, unresolved
                vendor, unsupported posting type, ...) is fixed.
  - API error:  "ERROR: <exception text>" -- same retry-eligibility as
                NOT POSTED; the row simply couldn't be posted (Zoho rejected
                the call, a network error, etc).

DELETE (2026-08-09, per Ravindra's confirmed answer -- "Delete in Zoho +
clear reference (Recommended)"): delete_posting() parses the SAME "<Type>
<id>" result text back apart (this is why the exact prefix strings above
matter) to know which of delete_journal_entry()/delete_bank_transaction()/
delete_expense()/delete_vendor_payment() to call and with what ID --
main.py's /delete-postings is the only caller, one call per row being
deleted.

DUPLICATE CHECK (2026-08-09, later the same day -- real live duplicate
found and fixed): every _post_*() function below now queries Zoho LIVE for
an existing record sharing this row's reference_number (via
zoho_books_client.py's find_*_by_reference() methods) and cross-checks the
amount too, right before its create_* call -- see _check_for_duplicate()/
_find_duplicate_record() below. If a match is found, the row is left
"NOT POSTED: duplicate detected -- ..." instead of posting a second entry.
This exists because the ONLY thing that had ever stopped a re-post before
this was the local "Zoho Posting Reference" Excel cell, which was just
proven capable of being wrong/stale (the transaction_id-key bug earlier
this same day: a row's cell said "ERROR: ..." even though Zoho already had
the entry, so the next run posted it again -- a real duplicate landed in
Zoho as a direct result). This check is skipped (posting proceeds
normally) for a row with no reference_number, or if the live Zoho search
itself fails -- see _check_for_duplicate()'s docstring for the reasoning.

DESCRIPTION/NOTES FIELD (2026-08-11, per Ravindra: "For all the posting
types i want the description/notes filed to be filled with the narration
text from the statement"): every _post_*() function's create_* call now
also passes the row's own narration into whichever field Zoho calls it for
that object type -- "notes" for a Journal Entry, "description" for an
Expense/Vendor Payment/Bank Transfer (Zoho's own field-naming convention,
NOT consistent across object types -- see each create_*() method's own
docstring in zoho_books_client.py). Purely a write; doesn't change posting
behavior in any way, just makes each Zoho record self-explanatory without
needing to cross-reference back to the original statement row.

DUPLICATE CHECK BY DESCRIPTION/NOTES CONTENT (2026-08-11, per Ravindra:
"i want the content in description/notes field to be checked for duplicate
posting. while posting if it contains same desc/notes for its type of
posting it should not post"): a SECOND, independent duplicate check runs
right after the reference-based one above, in every _post_*() function --
see _check_duplicate_description() below. This exists because
reference_number (the first check's only key) is blank or non-unique for a
real fraction of rows (not every posting type/category carries a reliable
one), so a row could still double-post despite the first check passing
clean. This one instead searches Zoho for every record dated the SAME DAY
as this row (via the new find_*_by_date() methods), then compares each
candidate's own notes/description text -- normalized (whitespace-collapsed,
casefolded) via _normalize_text_for_dup_check() -- against this row's own
narration; an exact normalized match is treated as the same duplicate as
above. Runs independently (OR logic) alongside the reference-based check --
either one finding a match is enough to skip the row.

LIVE BUG FOUND AND FIXED 2026-08-11 (Ravindra, testing this feature for
real): duplicate Expenses kept posting despite genuinely identical notes/
description text, and a re-posted Journal wasn't caught as a duplicate
even though the FIRST post's entry visibly had notes text when opened
directly in Zoho's own UI. Root cause: _check_duplicate_description() was
comparing against whatever notes/description text find_*_by_date()'s LIST
response itself carried on each candidate record -- and Zoho's list/search
endpoints appear to return an abbreviated record that omits free-text
fields like notes/description entirely (this project's leading theory,
inferred from the symptom, not yet directly confirmed against a raw Zoho
response -- see zoho_books_client.py's get_journal() docstring for the full
caveat). A list response with no "notes" key compares as blank against any
real narration, so the check silently never matched anything -- every
content-based duplicate check was a no-op from the moment it was built,
letting duplicates straight through. FIX: for every same-day candidate the
list search turns up, _check_duplicate_description() now fetches that ONE
record's full detail via a dedicated get_*() method (zoho_books_client.py's
get_journal()/get_expense()/get_vendor_payment()/get_bank_transaction(),
all new this update) and compares against THAT record's real text instead
of the list entry's. One extra GET per same-day candidate -- normally a
small number -- not per row overall.

CROSS-TYPE DUPLICATE CHECK (2026-08-17, per a real live duplicate Ravindra
found: the exact same real-world transaction -- same reference_number
"25-366458167-1-151", same notes/description text, same amount -- got
posted TWICE, once as a Journal Entry (account "Net Salary Payable") and
again, on a later run, as a Bank Transfer ("Transfer Fund" to "Receivable
from Egypt"). Root cause: every duplicate check above (both the
reference-based one and the description-based one) is scoped to ONE Zoho
object type only -- find_journals_by_reference() only ever searches
Journals, find_bank_transactions_by_reference() only ever searches Bank
Transactions, etc. -- so if the SAME row gets classified with a DIFFERENT
posting_type on two different runs (e.g. because the matched
Categorization rule's cf_type_of_transaction was edited between runs, or
the row matched a different rule the second time), neither check ever
looks at the type the row was ACTUALLY posted as before -- each one only
confirms "no duplicate of THIS type", never "no duplicate at all". FIX:
_check_for_duplicate_cross_type() (below) runs the same reference_number +
amount check as _check_for_duplicate(), but against every OTHER Zoho
object type this row's own type ISN'T -- wired into every _post_*()
function as a THIRD check, after the two existing (same-type) ones, so the
extra live calls only land on rows that would otherwise actually post (not
ones already blocked more cheaply). Skipped entirely if reference_number
is blank, same as the other reference-based check.

DUPLICATE CHECK, NOTES/DESCRIPTION-ONLY (2026-08-17, later the same day --
SUPERSEDES everything in the two sections above): Ravindra, direct: "i
want the check to be done solely on the notes description field only not
even on reference no. and it can be across posting types aslo meaning if
it is a different type and it has same notes/desc matching the narration
it should show duplicate." Both reference_number-based checks above
(_check_for_duplicate() and _check_for_duplicate_cross_type()) are now
UNWIRED -- still defined (not deleted, same "unwire don't delete"
convention as /entities elsewhere in this project), but no _post_*()
function calls either one any more. reference_number is still SENT to
Zoho on every create_* call (it's still a useful field on the record
itself) -- it's just no longer used to DECIDE whether something is a
duplicate.

The single remaining duplicate check, in every _post_*() function now, is
_check_duplicate_description_cross_type() (defined right after
_check_duplicate_description() below): for a row about to post as ANY
type, it searches EVERY Zoho object type's (Journal, Expense, Vendor
Payment, Bank Transfer) same-day records -- not just the row's own type --
fetches each same-day candidate's full detail (reusing
_check_duplicate_description()'s existing, unchanged full-GET-per-
candidate logic and its exact-normalized-text comparison), and blocks the
row if ANY of them has matching notes/description text, regardless of
that candidate's own type. This is what catches the Ravindra live case
that started the cross-type conversation in the first place (same
notes/description text posted once as a Journal, again later as a Bank
Transfer) WITHOUT relying on reference_number ever being right, present,
or consistent between runs -- narration/notes-or-description text is now
the sole source of truth for "is this the same transaction". Cost:
four full by-date searches per row now (one per type), not one -- see
that function's own docstring for the tradeoff and a possible future
optimization if this proves slow at scale.

BANK CHARGES / VAT-ON-BANK-CHARGES CONSOLIDATION (2026-08-10, per Ravindra:
"i have to deal with bank charges and VAt charges seperately compared to
other categories ... posting every transaction unnecessarily adds a lot of
transactions"). categorize_from_module.py's extract_postable_rows() now
NEVER includes a Bank charges/VAT on bank charges row in its normal "rows"
list -- those two categories are never posted one-row-at-a-time, period.
Instead they come back separately as "bank_charge_rows", and main.py's NEW
/post-bank-charges route (only called when Ravindra's widget-side "Bank
Charges Included/Excluded" checkbox is checked, usually at month-end with a
complete statement) does the consolidation:
  1. categorize_from_module.match_bank_charges_to_vat() pairs each Bank
     charges row with a VAT-on-bank-charges row sharing the same trailing
     narration "code" (see that function/extract_trailing_code()), across
     every file the caller pooled together (multiple statements/date-ranges
     for one account within a month can each hold half of a pair).
  2. For the matched pairs: sum the Bank charges rows' (not the VAT rows')
     amounts, take the LATEST txn_date among them, and post ONE Expense via
     the exact same post_row()/_post_expense() path any ordinary Bank
     charges row would use -- a synthetic row dict with category="Bank
     charges", narration=reference_number="Bank Charges - VAT", amount=the
     sum, debit_account/credit_account copied from one of the real
     contributing rows (they're all the same account+category, so
     identical), tax_treatment="vat_registered" plus a tax_id resolved live
     via ZohoBooksClient.find_tax_id() for the "Standard Rate [5%]" rate
     (2026-08-11 REWORK -- Ravindra's screenshots of his org's real Expense
     form showed Tax Treatment and Tax RATE are two separate Zoho fields,
     and that tax_treatment is a short snake_case enum, not the human label
     -- the first version of this feature wrongly sent the single literal
     string "VAT Registered Standard 5%" and Zoho rejected it outright. The
     constant lives in main.py as `_TAX_TREATMENT_VAT_MATCHED`, since
     main.py's /post-bank-charges route is what builds this synthetic row,
     not this module).
  3. For the Bank charges rows with NO VAT match: same shape, but
     narration=reference_number="Bank Charges Non-VAT" and
     tax_treatment="out_of_scope", no tax_id (constant `_TAX_TREATMENT_NON_VAT`
     in main.py -- see zoho_books_client.py's create_expense() docstring for
     the full corrected-understanding writeup and the still-UNVERIFIED-LIVE
     flags on the exact enum spelling and the "no tax_id needed" assumption).
  4. Every row that fed into a posted consolidated entry -- both sides of
     each matched pair, and each unmatched Bank charges row -- gets that
     SAME success reference text written into its own "Zoho Posting
     Reference" cell (via categorize_from_module.write_posting_results(),
     one call per file touched), so none of them show up in
     bank_charge_rows again on a future run; a VAT row with no Bank charges
     match is left untouched (stays excluded from individual posting,
     unchanged from before this feature, until a future statement brings in
     its pair).
Nothing in this module changed to make this work -- post_row()'s existing
posting_type dispatch and _post_expense()'s existing tax_treatment plumbing
handle a synthetic consolidated row exactly like a real one; all of the new
matching/aggregation/write-back logic lives in categorize_from_module.py
(pure, no Zoho call) and main.py (the actual /post-bank-charges route).
"""
import json
import re
from datetime import date as _date, datetime as _datetime
from typing import List, Optional

# posting_type values (from cf_type_of_transaction) matched case-
# insensitively, stripped, so "journal", "Journal ", "JOURNAL" all work the
# same as the exact display text a Categorization record actually has.
# Split 2026-08-09 -- Journal and Transfer used to share one bucket/function;
# see the module docstring's corrected mapping above for why they're now
# separate (different Zoho endpoint, different account-type requirement).
_JOURNAL_TYPES = {"journal"}
_TRANSFER_TYPES = {"transfer to another account", "transfer from another account", "transfer"}
_EXPENSE_TYPES = {"expense"}
_VENDOR_PAYMENT_TYPES = {"vendor payment"}
_CUSTOMER_PAYMENT_TYPES = {"customer payment"}

# The exact prefixes post_row() writes on success -- delete_posting() below
# parses these back apart, so changing one here means updating the other.
_JOURNAL_PREFIX = "Journal"
_BANK_TRANSFER_PREFIX = "Bank Transfer"
_EXPENSE_PREFIX = "Expense"
_VENDOR_PAYMENT_PREFIX = "Vendor Payment"

# Vendor Payment RECONCILIATION (2026-08-12, LATER SAME DAY, per Ravindra --
# see _reconcile_vendor_payment()'s own docstring for the full feature) --
# the result-text prefix on a successful match. Deliberately does NOT start
# with "Vendor Payment " (unlike _VENDOR_PAYMENT_PREFIX above): delete_posting()
# below matches on that exact prefix+space to route a Delete click to
# delete_vendor_payment(), and reconciliation never CREATES anything in
# Zoho -- there is no record of ours to delete. Starting this prefix with
# "Vendor Payment " would make delete_posting() try (and fail, since the
# text after that prefix isn't a real record ID) to delete the vendor's
# ALREADY-EXISTING payment we never created. A distinct prefix is what lets
# delete_posting() give a clear, purpose-built "nothing to delete" message
# instead (see that function below).
_VENDOR_PAYMENT_RECONCILED_PREFIX = "Reconciled Vendor Payment"


def is_vendor_payment_reconciliation_result(result_text) -> bool:
    """True iff `result_text` is a _reconcile_vendor_payment() SUCCESSFUL
    match (starts with _VENDOR_PAYMENT_RECONCILED_PREFIX). 2026-08-12
    (later same day), per Ravindra: "when vendor payment is reconciled dont
    consider this as posted, count should go towards not posted only as we
    are not posting anything just validating." -- this pipeline never
    creates a Zoho record for a Vendor Payments-category row, it only
    checks whether a matching one already exists, so a reconciliation
    match should never be tallied as "posted" the way a genuine create is.

    Deliberately a SEPARATE notion from _is_success_reference()
    (categorize_from_module.py), which still treats this same text as a
    success -- that's what correctly keeps a reconciled row out of future
    /categorize + /post-transactions runs (extract_postable_rows()'s
    already-posted check) and lets a file finish reaching .COMPLETED once
    every row is settled, exactly per Ravindra's "if validated successfully
    document name changed to Completed else keep it as it is." Callers
    that decide "posted" vs "not posted" COUNTS/DISPLAY (post_rows_
    streaming()'s stats dict below, and main.py's per-row `posted` field
    sent to the widget) should check this FIRST and route to their
    not-posted bucket when True; callers that decide "is this row settled,
    can /categorize skip re-deriving PostingReference for it, can
    /post-transactions skip re-attempting it, is the FILE done" should keep
    using _is_success_reference() exactly as before, unchanged."""
    return str(result_text or "").startswith(_VENDOR_PAYMENT_RECONCILED_PREFIX)


# ---------------------------------------------------------------------------
# EMPLOYEE-NAME + ER-NUMBER REFERENCE EXTRACTION (2026-08-10, per Ravindra:
# "i want the reference number of the transfer fund posting to have a
# specific data from the narration of the statement which is the name of
# the employee and the ER with the number followed, it can have multiple ER
# numbers, what is the best way to pick it up"). Used ONLY by
# _post_bank_transfer() below, to build the reference_number sent to Zoho
# (and searched on for duplicate detection -- see that function's use of
# this) for "Transfer to another account" rows whose narration actually
# contains an ER-style claim number, i.e. expense-reimbursement transfers.
# A transfer row with NO ER number in its narration (Credit card payment,
# CBK to EGP, ...) is untouched -- keeps using the statement's own
# Transaction Reference exactly as every other posting type already does.
#
# Verified 2026-08-10 against Ravindra's real "Sample BS...xlsx" (all 38
# Transfer to another account / Expense reimbursement rows): every row's
# employee name extracted correctly, and the multi-ER-number rows resolved
# exactly per his confirmed rule -- e.g. "Samreen ER9002 ER9003", "Emna
# ER9016 ER9015", "MM ER9037 ER9038 ER9039".
#
# THE NARRATION SHAPE this is built against (two templates seen in real
# data):
#   "...MWP <Name> Exp/exp claim... ER<digits> ..."       (DFT-DTB rows)
#   "...TO A C <account> <Name> ER<digits> ..."            (TRANSFER-DTB rows)
# "MWP" is occasionally misspelled "MWO" in the real data (seen once, for
# an "Ashutosh" row) -- matched case-insensitively, either spelling.
#
# EMPLOYEE NAME: the first alphabetic word after "MWP"/"MWO", skipping any
# word that's actually part of an ER match (one real row -- Naoufal -- has
# "MWP ER3552 Naoufal..." where the ER number comes first). Falls back to
# the word immediately before the first ER match when there's no MWP/MWO
# marker at all (every TRANSFER-DTB row uses this path). Deliberately does
# NOT try to reconstruct a "full legal name" from the messier text earlier
# in the narration (e.g. one real row reads "Saif Nasir Ali Khan Khan Nasir
# Ali" -- a genuinely duplicated/reordered name, a data-quality issue in the
# source bank export, not a parsing bug here) -- the short name next to
# MWP/ER is the one reliably clean signal in this data.
#
# ER NUMBER(S): every explicit "ER" + 2-or-more-digits match (case-
# insensitive), PLUS bare digit-group(s) immediately trailing an ER match
# (e.g. "ER9002  9003" -> also captures "9003" as ER9003) -- stops
# consuming at the next alphabetic word or at the statement's own trailing
# bank reference code ("NN-NNNNNNNNN-N-NNN"), whichever comes first.
# TRUNCATION: per Ravindra's confirmed rule ("it could be ER1234 3456 or
# ER123 ER1234(it should pick this) ER5678"), if one captured number is a
# plain PREFIX of a longer one also found on the same line, the shorter is
# dropped and only the complete number is kept -- same "ER372"+"ER3726"
# example already in the expense-reimbursement spec PDF. Numbers are
# otherwise kept in first-seen order, deduplicated.
#
# FORMAT: "<Name> ER<num1> ER<num2> ..." (space-separated) -- matches the
# narration's own style. Easy to change to a different separator later if
# Ravindra prefers (e.g. "<Name> - ER<num1>, ER<num2>") -- purely a matter
# of editing the join() below, nothing else depends on the exact format.
#
# KNOWN LIMITATION (flagged, not solved): a few real rows have trailing
# digit fragments that don't share a clean prefix relationship with any
# other number on the line (e.g. "ER9057 15", "ER9031 9032 032") -- these
# could be further truncated ER numbers, but there's no way to recover the
# intended full digits from the narration text alone. Rather than guess,
# they're kept as their own ER<fragment> entries (e.g. "ER15", "ER032") so
# a human reviewing the posted reference in Zoho can still see something is
# there, instead of silently dropping them.
# ---------------------------------------------------------------------------
_ER_NUMBER_RE = re.compile(r"ER\s*(\d{2,})", re.IGNORECASE)
_BANK_OWN_REFERENCE_RE = re.compile(r"\d{2,3}-\d{6,}-\d-\d{2,3}")
_MWP_MARKER_RE = re.compile(r"\bMW[PO]\b", re.IGNORECASE)


def extract_employee_er_reference(narration) -> Optional[str]:
    """Parses a bank statement narration for an employee-reimbursement
    reference -- "<employee name> ER<number> [ER<number> ...]" -- or
    returns None if the narration doesn't look like one of these lines at
    all (no ER-style number found, or no usable name anchor). See the
    module-level comment block just above this function for the full
    extraction rules, real-data examples, and known limitations. Never
    raises -- a narration this can't confidently parse just returns None,
    same "don't guess" philosophy as the rest of this pipeline."""
    text = str(narration or "")
    er_matches = list(_ER_NUMBER_RE.finditer(text))
    if not er_matches:
        return None  # no ER-style number in this narration -- caller falls back

    # Explicit ER-tagged numbers, first-seen order, deduplicated.
    numbers = []
    for m in er_matches:
        n = m.group(1)
        if n not in numbers:
            numbers.append(n)

    # Bare digit-group(s) immediately trailing each ER match (the
    # "ER9002  9003" case) -- stop at the next letter token or this
    # statement's own trailing bank reference code, whichever comes first.
    for m in er_matches:
        rest = text[m.end():]
        bank_ref_m = _BANK_OWN_REFERENCE_RE.search(rest)
        window = rest[:bank_ref_m.start()] if bank_ref_m else rest
        for tok_m in re.finditer(r"(\d{2,})|([A-Za-z]+)", window):
            if tok_m.group(2):
                break  # hit a name/word -- stop scanning after this ER match
            n = tok_m.group(1)
            if n not in numbers:
                numbers.append(n)

    # Truncation handling -- drop any number that's a plain prefix of a
    # longer one also captured on this line (keep only the complete one).
    numbers = [n for n in numbers if not any(other != n and other.startswith(n) for other in numbers)]

    # Employee name anchor -- see the module-level comment block above.
    employee = None
    mwp_m = _MWP_MARKER_RE.search(text)
    if mwp_m:
        after = text[mwp_m.end():]
        er_spans_after = [(m.start(), m.end()) for m in _ER_NUMBER_RE.finditer(after)]
        for tok_m in re.finditer(r"[A-Za-z]+", after):
            if any(s <= tok_m.start() < e for s, e in er_spans_after):
                continue  # this token is part of an ER match itself -- skip it
            employee = tok_m.group(0)
            break
    if not employee:
        preceding = text[:er_matches[0].start()].strip()
        name_m = re.search(r"([A-Za-z]+)\s*$", preceding)
        employee = name_m.group(1) if name_m else None

    if not employee:
        return None  # no name anchor found either -- don't guess, caller falls back

    return employee + " " + " ".join(f"ER{n}" for n in numbers)


def _normalize_posting_type(posting_type_raw) -> str:
    return str(posting_type_raw or "").strip().lower()


def _format_date(value) -> Optional[str]:
    """Zoho Books wants dates as "YYYY-MM-DD". A statement's own date column
    is usually read back by openpyxl as a real datetime.date/datetime.datetime
    object (Excel dates are stored as numbers with a date format, and
    openpyxl converts them automatically) -- but can also come through as a
    plain string if the source cell was formatted as text. Handles both;
    returns None (not a guess) if the value can't be confidently turned into
    a date, so the caller can report a clear "NOT POSTED" instead of sending
    Zoho a malformed date."""
    if value is None:
        return None
    if isinstance(value, _datetime):
        return value.date().isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    # Already-ISO ("2026-08-09" or "2026-08-09 00:00:00") -- keep just the
    # date part, the common case for a cell that WAS read as a real date but
    # arrives here as a pre-stringified value from upstream JSON transport.
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    # dd/mm/yyyy or dd-mm-yyyy -- a common raw bank-statement date shape.
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", text)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None  # unrecognized shape -- don't guess


def format_txn_date(value) -> Optional[str]:
    """Public alias for _format_date() (2026-08-10, added for main.py's
    /post-bank-charges route, which needs to find the LATEST txn_date among
    a batch of contributing rows -- "YYYY-MM-DD" strings sort correctly as
    plain strings, so `max(format_txn_date(r["txn_date"]) for r in rows)`
    is all that's needed). Every other caller in this module keeps using
    _format_date() directly -- this just gives an outside module a
    non-underscore name to import instead of reaching into a "private"
    one."""
    return _format_date(value)


def _amount_as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def find_contact_id(narration, contacts: list) -> Optional[str]:
    """Fuzzy-matches a row's narration against a list of Zoho Contact
    records (from ZohoBooksClient.list_contacts()) -- case-insensitive
    SUBSTRING match of each contact's contact_name against the narration
    text, same "don't require an exact match, but never guess an ambiguous
    one" approach already used in this project for OneDrive file matching
    (widget.js's matchFilesToAccounts()) and account matching. Returns the
    matching contact_id, or None if zero or MORE THAN ONE contact matches --
    an ambiguous match (two vendor names both appearing as substrings of one
    narration) is treated the same as no match at all, since guessing wrong
    here would misattribute a real payment to the wrong vendor, a much worse
    outcome than just leaving the row NOT POSTED for a human to resolve."""
    text = str(narration or "").strip().lower()
    if not text:
        return None
    matches = []
    for contact in contacts:
        name = str(contact.get("contact_name") or "").strip()
        if name and name.lower() in text:
            matches.append(contact)
    if len(matches) == 1:
        return matches[0].get("contact_id")
    return None


def _find_duplicate_record(records: list, reference_number, amount: float, id_keys: tuple, tol: float = 0.01) -> Optional[dict]:
    """Cross-checks a Zoho search result set DEFENSIVELY -- confirms BOTH
    the reference_number and the amount actually match, rather than just
    trusting the API call's own query-param filtering. This matters
    because one of the four search endpoints this backs
    (find_bank_transactions_by_reference()) has an UNVERIFIED filter
    behavior -- if Zoho ever ignores that filter and returns a broader
    unrelated list, this client-side check is what stops a same-amount-
    different-transaction false positive from blocking a legitimate post.
    Returns the first matching record (as {"id": ..., "raw": ...}) or None."""
    ref_norm = str(reference_number or "").strip()
    for rec in records:
        rec_ref = str(rec.get("reference_number") or "").strip()
        if ref_norm and rec_ref != ref_norm:
            continue
        rec_amount = None
        for key in ("amount", "total", "bcy_total"):
            if rec.get(key) not in (None, ""):
                try:
                    rec_amount = float(rec[key])
                    break
                except (TypeError, ValueError):
                    continue
        if rec_amount is None or amount is None or abs(rec_amount - amount) > tol:
            continue
        rec_id = None
        for key in id_keys:
            if rec.get(key):
                rec_id = rec[key]
                break
        if rec_id is None:
            continue
        return {"id": rec_id, "raw": rec}
    return None


def _find_vendor_payment_reconciliation_match(records: list, reference_number, txn_date: str, amount: float,
                                               tol: float = 0.01) -> Optional[dict]:
    """Cross-checks a Zoho Vendor Payment search result set for the
    RECONCILIATION feature (see _reconcile_vendor_payment() below) --
    unlike _find_duplicate_record() above (reference_number + amount only),
    this ALSO requires the candidate's own `date` field to equal `txn_date`
    exactly, per Ravindra's explicit 3rd check ("date from statement
    matches date in the existing post"). Reuses the SAME amount-key-
    priority scanning (amount/total/bcy_total, tol=0.01) as
    _find_duplicate_record() for consistency with the rest of this
    module's amount-matching logic -- field names here are UNVERIFIED
    LIVE (this project has never had a real Zoho Vendor Payment API
    response to check `date`/`amount` against directly; `amount` matches
    what create_vendor_payment() itself SENDS on create, and the
    amount-key fallback order mirrors this module's own existing,
    already-relied-upon _find_duplicate_record() pattern) -- if Zoho's
    real response uses different field names, every match here would
    silently come back "not found" (a false NOT POSTED, not a false
    MATCH -- the safer failure direction) until confirmed and fixed.

    Returns the first record matching ALL THREE (reference_number, date,
    amount) as {"id": ..., "raw": ...}, or None. If more than one
    candidate shares the same reference_number, the first one that ALSO
    matches date and amount wins -- Zoho's own list order otherwise
    decides ties, same "don't overthink an edge case that hasn't been
    reported live" approach as _find_duplicate_record()."""
    ref_norm = str(reference_number or "").strip()
    if not ref_norm:
        return None
    date_norm = str(txn_date or "").strip()
    for rec in records:
        rec_ref = str(rec.get("reference_number") or "").strip()
        if rec_ref != ref_norm:
            continue
        rec_date = str(rec.get("date") or "").strip()
        if date_norm and rec_date != date_norm:
            continue
        rec_amount = None
        for key in ("amount", "total", "bcy_total"):
            if rec.get(key) not in (None, ""):
                try:
                    rec_amount = float(rec[key])
                    break
                except (TypeError, ValueError):
                    continue
        if rec_amount is None or amount is None or abs(rec_amount - amount) > tol:
            continue
        rec_id = rec.get("payment_id")
        if rec_id is None:
            continue
        return {"id": rec_id, "raw": rec}
    return None


def _reconcile_vendor_payment(client, row: dict, organization_id) -> str:
    """Vendor Payment CATEGORY reconciliation (2026-08-12, LATER SAME DAY,
    per Ravindra, verbatim: "when posting logic runs, for the category
    vendor payments, we dont want to post anything but needs to check the
    following. 1. a posting already exists for this posting type with the
    reference no =postingreference col value 2. date from statement
    matches date in the existing post 3. amount in debit column matches
    the amount in the post 4. if all match update the statement row with
    match Successful or not matched").

    CATEGORICALLY DIFFERENT from every other _post_*() function in this
    module -- this NEVER calls a create_* Zoho API method; it only
    searches for and cross-checks an ALREADY-EXISTING Vendor Payment. A
    row reaches here purely because extract_postable_rows()
    (categorize_from_module.py) flagged it row["is_vendor_payment_category"]
    -- see that key's own comment for why this is keyed on CATEGORY
    (VENDOR_PAYMENT_CATEGORY = "Vendor Payments", plural -- live-confirmed
    against Ravindra's real Categorization module), checked in post_row()
    BEFORE posting_type, unconditionally, regardless of whatever
    posting_type the matched rule happens to have.

    The three checks, in order:
      1. reference_number: row["reference_number"] (this row's already-
         resolved PostingReference column value -- see categorize_from_
         module.py's POSTING_REFERENCE_COLUMN_NAME comment; NOT recomputed
         here) is searched for via find_vendor_payments_by_reference()
         (an existing Zoho search method, originally built for the now-
         superseded reference-based duplicate check -- see this module's
         "DUPLICATE CHECK, NOTES/DESCRIPTION-ONLY" docstring section --
         still a perfectly good Zoho search, just reused here for a
         different purpose).
      2 & 3. date and amount: _find_vendor_payment_reconciliation_match()
         above cross-checks each candidate's own `date` and `amount`
         fields against this row's txn_date and row["debit_amount"] (the
         statement's raw Debit column value -- see that key's own
         comment in extract_postable_rows() for why this is deliberately
         NOT the same as row["amount"]).
      4. All three agree -> "Match Successful" (written as
         "{_VENDOR_PAYMENT_RECONCILED_PREFIX} {payment_id}"); otherwise
         -> "Not Matched" (written as "NOT POSTED: ..." with a specific
         reason -- see below).

    RESULT TEXT (extends the module docstring's RESULT TEXT CONVENTION
    with this new, deliberately distinct prefix -- see
    _VENDOR_PAYMENT_RECONCILED_PREFIX's own comment for why it must NOT
    start with "Vendor Payment "):
      - Match: "{_VENDOR_PAYMENT_RECONCILED_PREFIX} {payment_id}" -- passes
        _is_success_reference() exactly like any real posting's result
        text (that function only checks for NOT POSTED/ERROR prefixes),
        so this row is automatically treated as "already posted"/settled
        on every future extract_postable_rows() call (never re-checked
        again) and automatically counts toward a file's ".COMPLETED"
        eligibility in main.py -- zero code changes needed in either
        place, both already key off _is_success_reference()'s generic
        rule.
      - No match: "NOT POSTED: <reason>" (one of three distinct reasons:
        no PostingReference to search on; no Vendor Payment found with
        that reference_number at all; or one WAS found but its date/
        amount didn't match) -- same prefix as any other unresolved row,
        so it's correctly retried every future /post-transactions run (in
        case the real Vendor Payment shows up in Zoho later, or a data
        entry mismatch gets corrected) and correctly keeps a file out of
        .COMPLETED until resolved -- again with zero extra main.py code.

    Never raises -- a live Zoho search failure comes back as "ERROR: ...";
    post_row()'s own top-level try/except wraps this call the same as
    every other dispatch branch, so this is defense-in-depth, not the only
    thing standing between a bug here and a crashed posting run."""
    reference_number = row.get("reference_number")
    if not str(reference_number or "").strip():
        return ("NOT POSTED: no PostingReference value to reconcile against -- this row's Vendor Payments "
                "category pattern (cf_key_words) didn't match anything in the narration during categorization, "
                "so there's no reference number to look up in Zoho. Check the narration against the category's "
                "regex pattern, then re-run /categorize.")
    debit_amount = _amount_as_float(row.get("debit_amount"))
    if debit_amount is None:
        return (f"NOT POSTED: no usable amount in this statement's own Debit column for this row (raw value: "
                f"{row.get('debit_amount')!r}) -- nothing to reconcile the existing Vendor Payment's amount "
                "against.")
    txn_date = _format_date(row.get("txn_date"))
    if not txn_date:
        return (f"NOT POSTED: couldn't parse a valid date from this row's transaction date (raw value: "
                f"{row.get('txn_date')!r}).")
    try:
        candidates = client.find_vendor_payments_by_reference(reference_number, organization_id=organization_id)
    except Exception as e:
        return (f"ERROR: couldn't search Zoho for an existing Vendor Payment with reference_number="
                f"{reference_number!r} -- {e}")
    match = _find_vendor_payment_reconciliation_match(candidates, reference_number, txn_date, debit_amount)
    if match:
        return f"{_VENDOR_PAYMENT_RECONCILED_PREFIX} {match['id']}"
    ref_norm = str(reference_number).strip()
    any_ref_match = any(str(c.get("reference_number") or "").strip() == ref_norm for c in candidates)
    if any_ref_match:
        return (f"NOT POSTED: found an existing Vendor Payment in Zoho with reference_number={reference_number!r}, "
                f"but its date and/or amount didn't match this row (expected date={txn_date!r}, Debit amount="
                f"{debit_amount!r}) -- check for a data entry mismatch between the statement and Zoho.")
    return f"NOT POSTED: no existing Vendor Payment found in Zoho with reference_number={reference_number!r}."


def _check_for_duplicate(client, find_fn, id_keys: tuple, type_label: str, reference_number, amount, organization_id) -> Optional[str]:
    """SUPERSEDED 2026-08-17, NO LONGER CALLED from any _post_*() function
    -- see the module docstring's "DUPLICATE CHECK, NOTES/DESCRIPTION-ONLY"
    section. Ravindra: "i want the check to be done solely on the notes
    description field only not even on reference no." -- reference_number
    is no longer used for duplicate detection at all;
    _check_duplicate_description_cross_type() below is now the only check.
    Left in place (unwired, not deleted) in case reference-based checking
    is ever wanted again -- same "unwire, don't delete" convention as
    /entities and the other superseded pieces of this project. Original
    docstring, still accurate for what this function itself does if called
    directly:

    Shared duplicate-check used by every _post_*() function below, right
    before its create_* call. Queries Zoho LIVE for existing records
    sharing this row's reference_number (see zoho_books_client.py's
    find_*_by_reference() methods), then cross-checks the amount too
    (client-side, defensively -- see _find_duplicate_record()) before
    treating anything as a real duplicate. Returns a ready-to-return
    "NOT POSTED: duplicate detected..." string if one is found, else None
    (meaning: safe to post).

    Added 2026-08-09 -- a real live duplicate got posted to Zoho because
    this pipeline's ONLY duplicate defense until now was its own local
    "Zoho Posting Reference" Excel cell, which had just been proven capable
    of being wrong/stale (the exact transaction_id-key bug earlier the same
    day: a row's cell said "ERROR: ..." even though Zoho already genuinely
    had the entry from before that fix, so the very next run posted it a
    second time). This checks Zoho itself, fresh, on every single post
    attempt -- not a local value that can go stale between runs.

    Skips the check entirely (returns None, i.e. "proceed with posting")
    if this row has no reference_number to search on, or if the live Zoho
    search itself fails (network hiccup, Zoho outage, etc) -- same "don't
    let one soft failure block an otherwise-healthy posting run"
    philosophy as post_rows()'s vendor-contacts fetch below. A real gap
    this leaves, worth knowing: a transaction with NO reference_number can
    still be double-posted on two separate runs, since reference_number is
    the only thing this pipeline has to reliably key a duplicate check on."""
    if not reference_number:
        return None
    try:
        existing = find_fn(reference_number, organization_id=organization_id)
    except Exception as e:
        print(f"[posting_service] duplicate check against Zoho failed for reference_number={reference_number!r} "
              f"-- proceeding without it: {e}", flush=True)
        return None
    dup = _find_duplicate_record(existing, reference_number, amount, id_keys)
    if not dup:
        return None
    return (f"NOT POSTED: duplicate detected -- an existing {type_label} {dup['id']} in Zoho already has "
            f"reference_number={reference_number!r} and amount {amount} -- skipped to avoid creating a second "
            "entry for the same transaction. If this really is a different transaction that happens to share "
            "this reference number, this note will keep blocking it every run until that's resolved (e.g. by "
            "correcting the reference number so it's unique, or manually reviewing which entry is which in Zoho).")


# SUPERSEDED 2026-08-17 along with _check_for_duplicate_cross_type() below
# -- no longer called (see that function's docstring). Left in place.
#
# One (type_label, find_by_reference method name, id_keys) entry per posting
# type -- used by _check_for_duplicate_cross_type() below to search every
# OTHER Zoho object type for the same reference_number before posting.
# find_fn is looked up by NAME (getattr(client, ...)) rather than passed as
# a bound method directly, since this tuple is built once at import time,
# before any particular `client` instance exists.
_ALL_REFERENCE_SEARCHES = (
    (_JOURNAL_PREFIX, "find_journals_by_reference", ("journal_id",)),
    (_EXPENSE_PREFIX, "find_expenses_by_reference", ("expense_id",)),
    (_VENDOR_PAYMENT_PREFIX, "find_vendor_payments_by_reference", ("payment_id",)),
    (_BANK_TRANSFER_PREFIX, "find_bank_transactions_by_reference", ("transaction_id", "banktransaction_id")),
)


def _check_for_duplicate_cross_type(client, current_type_label: str, reference_number, amount,
                                     organization_id) -> Optional[str]:
    """SUPERSEDED 2026-08-17, NO LONGER CALLED -- see
    _check_duplicate_description_cross_type()'s docstring (right after
    _check_duplicate_description() below) for the current, ONLY duplicate
    check every _post_*() function runs now: notes/description content
    only, across every posting type, never reference_number. Left in place
    (unwired, not deleted), same convention as _check_for_duplicate() above.
    Original docstring, still accurate for what this function itself does
    if called directly:

    THIRD duplicate check, run last (after the same-type reference- and
    description-based checks above) in every _post_*() function -- see the
    module docstring's "CROSS-TYPE DUPLICATE CHECK" section for the real
    live bug this fixes: the exact same real-world transaction (same
    reference_number, same notes/description text, same amount) posted
    once as a Journal Entry and again, on a later run, as a Bank Transfer,
    because every _check_for_duplicate() call is scoped to ONE Zoho object
    type only -- neither type's own duplicate check ever looks at what the
    OTHER type has.

    Searches every posting type in _ALL_REFERENCE_SEARCHES EXCEPT
    `current_type_label` (that one was already checked by
    _check_for_duplicate() immediately before this is called -- searching
    it again here would be a wasted duplicate call) for a record sharing
    `reference_number`, cross-checking `amount` too via the same
    _find_duplicate_record() every other check uses. Returns a
    ready-to-return "NOT POSTED: duplicate detected -- ..." string on the
    first match found (order matches _ALL_REFERENCE_SEARCHES -- Journal,
    Expense, Vendor Payment, Bank Transfer), else None (safe to post).

    Skipped entirely (returns None) if reference_number is blank, same as
    _check_for_duplicate() -- there's nothing to search on. Deliberately
    placed LAST among the three checks in every _post_*() function: it's
    the most expensive (up to 3 extra live calls, one per other type), so
    it should only ever run for a row that's already survived every
    cheaper check and would otherwise actually post -- not one already
    blocked some other way. One type's search failing (network hiccup,
    etc) is logged and that ONE type is skipped, not the whole check --
    same "one flaky call shouldn't block the rest" philosophy as
    _check_duplicate_description()'s per-candidate handling."""
    if not reference_number:
        return None
    for type_label, find_fn_name, id_keys in _ALL_REFERENCE_SEARCHES:
        if type_label == current_type_label:
            continue  # already checked by _check_for_duplicate() right before this call
        try:
            find_fn = getattr(client, find_fn_name)
            existing = find_fn(reference_number, organization_id=organization_id)
        except Exception as e:
            print(f"[posting_service] cross-type duplicate check against {type_label} failed for "
                  f"reference_number={reference_number!r} -- skipping this one type, not the whole check: {e}",
                  flush=True)
            continue
        dup = _find_duplicate_record(existing, reference_number, amount, id_keys)
        if dup:
            return (f"NOT POSTED: duplicate detected -- an existing {type_label} {dup['id']} in Zoho already has "
                    f"reference_number={reference_number!r} and amount {amount}, even though this row would post "
                    f"as a {current_type_label} -- skipped to avoid creating a second entry for the same "
                    "transaction under a different posting type (likely because the matched Categorization "
                    "rule's posting type changed between runs, or the row matched a different rule this time). "
                    "If this really is a different transaction that happens to share this reference number, this "
                    "note will keep blocking it every run until that's resolved.")
    return None


def _normalize_text_for_dup_check(text) -> str:
    """Collapses all whitespace runs to a single space, strips the ends, and
    casefolds -- a loose-but-reliable equality comparison for narration vs.
    a Zoho record's own notes/description text (see
    _check_duplicate_description() below). Deliberately NOT a fuzzy/
    substring match -- an exact match on the normalized text only, so this
    never flags two genuinely different transactions that merely share a
    common word or two as duplicates of each other."""
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _check_duplicate_description(client, find_by_date_fn, get_full_record_fn, id_keys: tuple, field_name: str,
                                  type_label: str, description, txn_date, organization_id,
                                  cache: Optional[dict] = None) -> Optional[str]:
    """Second, independent duplicate check used by every _post_*() function
    below, right after _check_for_duplicate() -- see the module docstring's
    "DUPLICATE CHECK BY DESCRIPTION/NOTES CONTENT" section for the full
    story, including the 2026-08-11 live bug this specific implementation
    fixes. Searches Zoho for every record dated the SAME DAY as this row
    (find_by_date_fn -- zoho_books_client.py's find_*_by_date() methods),
    then for each candidate fetches that ONE record's full detail
    (get_full_record_fn -- the matching get_*() method) and compares its
    real `field_name` text against this row's own `description` (narration),
    both normalized via _normalize_text_for_dup_check(). Returns a
    ready-to-return "NOT POSTED: duplicate detected..." string on an exact
    normalized match, else None (safe to post).

    WHY a full detail GET per candidate, not just the list response's own
    text (2026-08-11 fix): Zoho's list/search endpoints were found -- live,
    via a real reported symptom, not proactively -- to likely omit free-text
    fields like notes/description from each list entry entirely (see
    zoho_books_client.py's get_journal() docstring for the full caveat on
    how confirmed this theory actually is). Comparing against the list
    response's own (missing) field would always read as blank and never
    match a real duplicate -- which is exactly the bug Ravindra hit. This
    extra GET is the fix; it only runs per SAME-DAY candidate (normally a
    handful), not per row overall.

    Skipped entirely (returns None) if `description` is blank/unusable
    after normalization, or `txn_date` is missing, or the initial by-date
    search itself fails -- same "don't let one soft failure block an
    otherwise-healthy posting run" philosophy as _check_for_duplicate().
    A single candidate's own detail-GET failing is logged and that ONE
    candidate is skipped (not the whole check) -- one flaky record shouldn't
    block comparing against every other same-day candidate.

    `cache` (2026-08-13, later still -- added per Ravindra's live report:
    "the posting logic has become very slow ... if i have a lot of
    transactions it will take for ever"): an optional dict, shared across
    every row in ONE /post-transactions run (see post_rows_streaming()'s own
    docstring), keyed by (type_label, txn_date) -> [(record_id,
    normalized_field_text), ...]. This exact cost was already flagged, not
    fixed, when the cross-type redesign shipped (see this function's own
    caller, _check_duplicate_description_cross_type()'s "Cost note" in its
    prior version) -- FOUR by-date list calls plus one detail GET per
    same-day candidate, repeated from scratch for EVERY row, even though a
    real statement's rows are typically clustered onto a handful of distinct
    dates. With a shared cache, only the FIRST row on a given date pays for
    that date's list+detail-GET cost per type; every later row on the same
    date reuses the already-fetched, already-normalized candidate list with
    zero further Zoho calls. `cache=None` (the default) preserves the exact
    old always-live-fetch behavior for any caller that doesn't pass one
    (e.g. an isolated unit test) -- nothing about the matching LOGIC changed,
    only whether repeat work is skipped. A failed by-date search is
    deliberately NEVER cached (only a successful fetch's results are stored)
    -- so a transient failure doesn't silently block every later row's own
    duplicate check for the rest of the run; the SAME transient failure
    printing once per affected row is a small, acceptable cost next to that."""
    target_norm = _normalize_text_for_dup_check(description)
    if not target_norm or not txn_date:
        return None

    cache_key = (type_label, txn_date)
    if cache is not None and cache_key in cache:
        candidates_norm = cache[cache_key]
    else:
        try:
            candidates = find_by_date_fn(txn_date, organization_id=organization_id)
        except Exception as e:
            print(f"[posting_service] description-based duplicate check (by date) failed for {type_label} dated "
                  f"{txn_date!r} -- proceeding without it: {e}", flush=True)
            return None
        candidates_norm = []
        for rec in candidates:
            rec_id = None
            for key in id_keys:
                if rec.get(key):
                    rec_id = rec[key]
                    break
            if rec_id is None:
                continue
            try:
                full = get_full_record_fn(rec_id, organization_id=organization_id)
            except Exception as e:
                print(f"[posting_service] description-based duplicate check: couldn't fetch full {type_label} "
                      f"{rec_id} to compare its {field_name!r} -- skipping this one candidate (not the whole "
                      f"check): {e}", flush=True)
                continue
            candidates_norm.append((rec_id, _normalize_text_for_dup_check(full.get(field_name))))
        if cache is not None:
            cache[cache_key] = candidates_norm

    for rec_id, norm_text in candidates_norm:
        if norm_text and norm_text == target_norm:
            return (f"NOT POSTED: duplicate detected -- an existing {type_label} {rec_id} in Zoho, dated "
                    f"{txn_date}, already has the exact same {field_name} text as this row's narration -- "
                    "skipped to avoid creating a second entry for the same transaction. If this really is a "
                    f"different transaction that happens to share this exact {field_name} text, this note will "
                    "keep blocking it every run until that's resolved (e.g. by making the narration/description "
                    "distinct, or manually reviewing which entry is which in Zoho).")
    return None


# One (type_label, find_by_date method name, get_full_record method name,
# id_keys, notes/description field name) entry per posting type -- used by
# _check_duplicate_description_cross_type() below to run
# _check_duplicate_description() against EVERY Zoho object type's same-day
# records, regardless of which type the row about to post actually is.
_ALL_DESCRIPTION_SEARCHES = (
    (_JOURNAL_PREFIX, "find_journals_by_date", "get_journal", ("journal_id",), "notes"),
    (_EXPENSE_PREFIX, "find_expenses_by_date", "get_expense", ("expense_id",), "description"),
    (_VENDOR_PAYMENT_PREFIX, "find_vendor_payments_by_date", "get_vendor_payment", ("payment_id",), "description"),
    (_BANK_TRANSFER_PREFIX, "find_bank_transactions_by_date", "get_bank_transaction",
     ("transaction_id", "banktransaction_id"), "description"),
)


def _check_duplicate_description_cross_type(client, description, txn_date, organization_id,
                                              cache: Optional[dict] = None) -> Optional[str]:
    """THE duplicate check, as of 2026-08-17 -- see the module docstring's
    "DUPLICATE CHECK, NOTES/DESCRIPTION-ONLY" section. Ravindra's own
    words: "i want the check to be done solely on the notes description
    field only not even on reference no. and it can be across posting
    types aslo meaning if it is a different type and it has same notes/desc
    matching the narration it should show duplicate." This is now the ONLY
    duplicate check every _post_*() function below runs -- reference_number
    is no longer consulted for duplicate detection at all (it's still SENT
    to Zoho on create, just never searched on).

    Thin loop wrapper around the existing, unchanged _check_duplicate_
    description() -- reuses that function's exact matching logic (full
    detail GET per same-day candidate, normalized exact-text comparison,
    per-candidate soft-fail) once per posting type in
    _ALL_DESCRIPTION_SEARCHES, INCLUDING the row's own type -- there's only
    one check now, so unlike the superseded _check_for_duplicate_cross_type()
    below, nothing needs to be excluded from the loop. Returns the first
    match found (order matches _ALL_DESCRIPTION_SEARCHES -- Journal,
    Expense, Vendor Payment, Bank Transfer), else None.

    `getattr(client, name, None)` (not a hard attribute access) so a client
    missing one of these eight methods (e.g. a narrower test fake) just
    skips that one type rather than raising -- same defensive spirit as
    _check_for_duplicate_cross_type()'s own getattr fix.

    Cost note (2026-08-13, later still -- CONFIRMED live, per Ravindra's
    report: "the posting logic has become very slow ... we had to increase
    the timeout ... if i have a lot of transactions it will take for
    ever"): this runs FOUR full by-date searches (one per type) on EVERY row
    that reaches this point, not just the row's own type -- genuinely more
    live Zoho calls per row than before this check's cross-type redesign.
    FIXED via `cache` below -- see _check_duplicate_description()'s own
    docstring for the exact caching mechanics. Without a cache passed in,
    this is still exactly as expensive as before (four list calls + one
    detail GET per same-day candidate, every single row) -- the caller MUST
    thread one shared dict through for the fix to actually apply; see
    post_rows_streaming()'s own docstring for where that dict is created."""
    for type_label, find_by_date_name, get_full_name, id_keys, field_name in _ALL_DESCRIPTION_SEARCHES:
        find_by_date_fn = getattr(client, find_by_date_name, None)
        get_full_fn = getattr(client, get_full_name, None)
        if find_by_date_fn is None or get_full_fn is None:
            continue
        dup_note = _check_duplicate_description(client, find_by_date_fn, get_full_fn, id_keys, field_name,
                                                  type_label, description, txn_date, organization_id, cache=cache)
        if dup_note:
            return dup_note
    return None


def _post_journal_like(client, row: dict, organization_id, dup_check_cache: Optional[dict] = None) -> str:
    """Handles ONLY plain "Journal" rows now -- Transfer types moved to
    _post_bank_transfer() below, 2026-08-09 (see module docstring)."""
    debit_account = row.get("debit_account")
    credit_account = row.get("credit_account")
    if not debit_account or not credit_account:
        return ("NOT POSTED: this row's Debit Account/Credit Account couldn't both be resolved "
                f"(debit_account={debit_account!r}, credit_account={credit_account!r}) -- check the matched "
                "Categorization rule's cf_account_to_be_posted and this statement's own Debit/Credit columns.")
    amount = _amount_as_float(row.get("amount"))
    if amount is None:
        return f"NOT POSTED: no usable amount on this row (raw value: {row.get('amount')!r})."
    txn_date = _format_date(row.get("txn_date"))
    if not txn_date:
        return f"NOT POSTED: couldn't parse a valid date from this row's transaction date (raw value: {row.get('txn_date')!r})."
    dup_note = _check_duplicate_description_cross_type(client, row.get("narration"), txn_date, organization_id,
                                                         cache=dup_check_cache)
    if dup_note:
        return dup_note
    line_items = [
        {"account_id": debit_account, "debit_or_credit": "debit", "amount": amount},
        {"account_id": credit_account, "debit_or_credit": "credit", "amount": amount},
    ]
    result = client.create_journal_entry(
        journal_date=txn_date, line_items=line_items,
        reference_number=row.get("reference_number"), notes=row.get("narration"), organization_id=organization_id,
    )
    journal = result.get("journal") or {}
    journal_id = journal.get("journal_id")
    if not journal_id:
        return f"ERROR: Zoho accepted the journal entry call but returned no journal_id -- raw response: {result}"
    return f"{_JOURNAL_PREFIX} {journal_id}"


def _post_bank_transfer(client, row: dict, organization_id, dup_check_cache: Optional[dict] = None) -> str:
    """"Transfer to another account" / "Transfer from another account" rows
    -- posted via POST /banktransactions (transaction_type=transfer_fund),
    NOT as a Journal Entry. Added 2026-08-09 after Ravindra pointed out this
    project's own earlier, working create_bank_transfer() usage (for Expense
    reimbursement) contradicted the "transfers = journal entries" assumption
    this pipeline had been built on -- see zoho_books_client.py's
    create_bank_transfer() docstring and the module docstring above for the
    full account-type-driven decision rule.

    Direction: categorize_from_module._resolve_row_accounts() already
    encodes which side of the statement's own Debit/Credit columns had the
    amount into (debit_account, credit_account) -- the side that GAINS money
    is debit_account, the side that LOSES money is credit_account (ordinary
    double-entry). A bank transfer's from_account_id/to_account_id use the
    same "loses/gains" sense, so from_account_id=credit_account,
    to_account_id=debit_account -- this holds for BOTH "Transfer to another
    account" and "Transfer from another account" categories unchanged, since
    the direction is already baked into which column the source statement
    had the amount in, not into the category label itself."""
    debit_account = row.get("debit_account")
    credit_account = row.get("credit_account")
    if not debit_account or not credit_account:
        return ("NOT POSTED: this row's Debit Account/Credit Account couldn't both be resolved "
                f"(debit_account={debit_account!r}, credit_account={credit_account!r}) -- check the matched "
                "Categorization rule's cf_account_to_be_posted and this statement's own Debit/Credit columns.")
    from_account_id, to_account_id = credit_account, debit_account
    amount = _amount_as_float(row.get("amount"))
    if amount is None:
        return f"NOT POSTED: no usable amount on this row (raw value: {row.get('amount')!r})."
    txn_date = _format_date(row.get("txn_date"))
    if not txn_date:
        return f"NOT POSTED: couldn't parse a valid date from this row's transaction date (raw value: {row.get('txn_date')!r})."
    # Reference number (2026-08-10, per Ravindra): if this row's narration
    # carries an employee-name + ER-number pattern (expense reimbursements),
    # use that derived reference for the actual Zoho WRITE below, instead of
    # the statement's raw Transaction Reference -- see
    # extract_employee_er_reference()'s docstring above for the extraction
    # rules. A Transfer row whose narration has no ER number (Credit card
    # payment, CBK to EGP, ...) falls straight through to
    # row["reference_number"] unchanged. NOT used for duplicate detection
    # any more (2026-08-17, see module docstring's "DUPLICATE CHECK, NOTES/
    # DESCRIPTION-ONLY" section) -- still sent to create_bank_transfer()
    # below purely as the record's own reference_number field.
    reference_number = extract_employee_er_reference(row.get("narration")) or row.get("reference_number")
    dup_note = _check_duplicate_description_cross_type(client, row.get("narration"), txn_date, organization_id,
                                                         cache=dup_check_cache)
    if dup_note:
        return dup_note
    result = client.create_bank_transfer(
        from_account_id=from_account_id, to_account_id=to_account_id, amount=amount, date=txn_date,
        reference_number=reference_number, description=row.get("narration"), organization_id=organization_id,
    )
    # CONFIRMED LIVE 2026-08-09 (Ravindra's real Cyber Knight org): Zoho wraps
    # a /banktransactions response under a "banktransaction" key, as guessed
    # -- but the ID field inside it is "transaction_id", NOT
    # "banktransaction_id" (unlike journal_id/expense_id/payment_id's own
    # singular-noun-key convention, this one doesn't repeat "banktransaction"
    # in the ID field's own name). Real response confirmed live:
    #   {"code": 0, "message": "The bank transaction has been recorded.",
    #    "banktransaction": {"transaction_id": "2122051000102657279", ...}}
    # The entry WAS posted correctly in Zoho even while this bug was live --
    # only the ID extraction (and therefore the success reference this
    # pipeline records/uses for Delete) was failing, hence the erroneous
    # "ERROR: ... returned no banktransaction_id" despite a real success.
    banktransaction = result.get("banktransaction") or {}
    banktransaction_id = banktransaction.get("transaction_id") or banktransaction.get("banktransaction_id")
    if not banktransaction_id:
        return f"ERROR: Zoho accepted the bank transfer call but returned no transaction_id -- raw response: {result}"
    return f"{_BANK_TRANSFER_PREFIX} {banktransaction_id}"


def _post_expense(client, row: dict, organization_id, dup_check_cache: Optional[dict] = None) -> str:
    debit_account = row.get("debit_account")
    credit_account = row.get("credit_account")
    account_number = row.get("account_number")  # this statement's own resolved account_id
    if not debit_account or not credit_account:
        return ("NOT POSTED: this row's Debit Account/Credit Account couldn't both be resolved "
                f"(debit_account={debit_account!r}, credit_account={credit_account!r}).")
    if not account_number:
        return "NOT POSTED: this statement's own bank account_id wasn't resolved -- can't tell which side is the paid-through account."
    # Whichever side equals this statement's own account is the bank/paid-
    # through side; the OTHER side is the expense GL account -- see the
    # module docstring's "Expense" mapping.
    if str(debit_account) == str(account_number):
        paid_through_account_id, expense_account_id = debit_account, credit_account
    elif str(credit_account) == str(account_number):
        paid_through_account_id, expense_account_id = credit_account, debit_account
    else:
        return (f"NOT POSTED: neither Debit Account ({debit_account!r}) nor Credit Account ({credit_account!r}) "
                f"matches this statement's own account_id ({account_number!r}) -- can't tell which side is the "
                "paid-through account vs the expense account.")
    amount = _amount_as_float(row.get("amount"))
    if amount is None:
        return f"NOT POSTED: no usable amount on this row (raw value: {row.get('amount')!r})."
    txn_date = _format_date(row.get("txn_date"))
    if not txn_date:
        return f"NOT POSTED: couldn't parse a valid date from this row's transaction date (raw value: {row.get('txn_date')!r})."
    dup_note = _check_duplicate_description_cross_type(client, row.get("narration"), txn_date, organization_id,
                                                         cache=dup_check_cache)
    if dup_note:
        return dup_note
    # tax_treatment / tax_id (2026-08-11 REWORK, Bank Charges/VAT
    # consolidation feature -- see module docstring): only ever set on the
    # two synthetic consolidated rows main.py's /post-bank-charges builds --
    # "vat_registered"+a resolved tax_id on "Bank Charges - VAT",
    # "out_of_scope" (no tax_id) on "Bank Charges Non-VAT" -- absent (None)
    # on every ordinary row, unchanged behavior from before this parameter
    # existed. See zoho_books_client.py's create_expense() docstring for
    # the full corrected-understanding writeup (tax_treatment and the tax
    # RATE are two separate Zoho fields) and the still-UNVERIFIED-LIVE flags.
    result = client.create_expense(
        account_id=expense_account_id, paid_through_account_id=paid_through_account_id,
        date=txn_date, amount=amount, reference_number=row.get("reference_number"),
        description=row.get("narration"), tax_treatment=row.get("tax_treatment"),
        tax_id=row.get("tax_id"),
        organization_id=organization_id,
    )
    expense = result.get("expense") or {}
    expense_id = expense.get("expense_id")
    if not expense_id:
        return f"ERROR: Zoho accepted the expense call but returned no expense_id -- raw response: {result}"
    return f"{_EXPENSE_PREFIX} {expense_id}"


def post_itemized_expense_group(client, group: dict, organization_id, dup_check_cache: Optional[dict] = None) -> str:
    """Posts ONE Egypt-entity paired salary+bank-charge group (see
    categorize_from_module.py's ITEMIZED_WITH_COLUMN_NAME and
    extract_postable_rows()'s "itemized_groups" -- Phase 2 of that feature)
    as a SINGLE itemized Zoho Expense with one line item per group member,
    instead of one separate posting per row. `group` is one entry from
    extract_postable_rows()'s "itemized_groups" list: {"group_key": int (the
    primary member's own excel_row), "members": [{excel_row, category,
    posting_type, debit_account, credit_account, account_number, amount,
    narration, reference_number, txn_date, debit_amount, zoho_reference,
    already_posted}, ...]}. Returns the same RESULT TEXT CONVENTION as
    post_row() (a single string applying to the WHOLE group -- main.py
    writes this same text/reference onto EVERY member's own row) -- never
    raises, same "one bad group can't take the run down" reasoning as
    post_row()'s own try/except.

    Design choices, all confirmed with Ravindra 2026-08-13 before writing
    this:
      - paid_through_account_id is resolved ONCE for the whole group (every
        member shares the same statement account_number by construction --
        they're rows from the same statement/run -- so there's exactly one
        paid-through account for the group, same as a normal Expense).
      - date is resolved ONCE from the group's PRIMARY member (group_key's
        own row) -- every member in a pairing group shares the same
        narration and, in every real Egypt example seen so far, the same
        transaction date too (the bank posts the transfer and its own
        charge for it on the same day); if a future statement's charge row
        genuinely has a different date, that row's own date is silently NOT
        used -- worth revisiting if that turns out to matter live.
      - the duplicate check runs exactly ONCE for the group, against the
        primary member's own narration (identical to every other member's
        narration by construction -- that's literally what made
        _detect_paired_bank_charges() group them together in the first
        place) -- NOT once per member, since that would search Zoho for the
        same shared text N times for no benefit. Reuses
        _check_duplicate_description_cross_type(), same function every
        other _post_*() here already uses.
      - reference_number sent on the whole Expense is the PRIMARY member's
        own reference_number (its Posting Reference column value) -- the
        "charge" member's own reference_number, if it has a different one,
        is not sent anywhere (Zoho's itemized Expense has exactly one
        reference_number field for the whole record, same as one date/one
        paid-through account).
      - each line item's own account_id is THAT member's own resolved
        expense-side account (debit_account or credit_account, whichever
        ISN'T this statement's own account_number -- same per-row logic
        _post_expense() uses, just applied per member instead of once) --
        this is what makes the "charge" line item post against its own
        real "Bank charges - <SUFFIX>" GL account instead of the primary's
        category's account, even though they share one Expense record.
      - each line item's own description is that member's own narration
        (identical across the group, but sent per-line since that's the
        field Zoho's line_items shape expects -- see
        zoho_books_client.py's create_itemized_expense() docstring).

    See zoho_books_client.py's create_itemized_expense() docstring for the
    still-UNVERIFIED-LIVE body-shape caveat -- this function's own logic
    (account/date/dup-check resolution) is solid; what's unverified is
    purely the exact JSON Zoho's itemized-expense endpoint expects."""
    try:
        members = group.get("members") or []
        if not members:
            return "NOT POSTED: itemized group has no members -- nothing to post."
        group_key = group.get("group_key")
        primary_member = next((m for m in members if m["excel_row"] == group_key), None)
        if primary_member is None:
            # Defensive only -- extract_postable_rows() always includes the
            # primary itself as a (self-pointing) member of its own group,
            # so this should never actually happen.
            primary_member = members[0]

        account_number = primary_member.get("account_number")
        if not account_number:
            return "NOT POSTED: this statement's own bank account_id wasn't resolved -- can't determine the paid-through account for this itemized group."

        line_items = []
        for member in members:
            debit_account = member.get("debit_account")
            credit_account = member.get("credit_account")
            if not debit_account or not credit_account:
                return (f"NOT POSTED: row {member.get('excel_row')} in this itemized group has an unresolved "
                        f"Debit Account/Credit Account (debit_account={debit_account!r}, "
                        f"credit_account={credit_account!r}).")
            if str(debit_account) == str(account_number):
                expense_account_id = credit_account
            elif str(credit_account) == str(account_number):
                expense_account_id = debit_account
            else:
                return (f"NOT POSTED: row {member.get('excel_row')}'s Debit Account ({debit_account!r}) and "
                        f"Credit Account ({credit_account!r}) neither one matches this statement's own account_id "
                        f"({account_number!r}) -- can't tell which side is the paid-through account vs the "
                        "expense account.")
            amount = _amount_as_float(member.get("amount"))
            if amount is None:
                return f"NOT POSTED: row {member.get('excel_row')} in this itemized group has no usable amount (raw value: {member.get('amount')!r})."
            line_items.append({
                "account_id": expense_account_id,
                "amount": amount,
                "description": member.get("narration"),
            })

        txn_date = _format_date(primary_member.get("txn_date"))
        if not txn_date:
            return f"NOT POSTED: couldn't parse a valid date from this itemized group's primary row (raw value: {primary_member.get('txn_date')!r})."

        dup_note = _check_duplicate_description_cross_type(client, primary_member.get("narration"), txn_date, organization_id,
                                                             cache=dup_check_cache)
        if dup_note:
            return dup_note

        result = client.create_itemized_expense(
            paid_through_account_id=account_number, date=txn_date, line_items=line_items,
            reference_number=primary_member.get("reference_number"),
            organization_id=organization_id,
        )
        expense = result.get("expense") or {}
        expense_id = expense.get("expense_id")
        if not expense_id:
            return f"ERROR: Zoho accepted the itemized expense call but returned no expense_id -- raw response: {result}"
        return f"{_EXPENSE_PREFIX} {expense_id}"
    except Exception as e:
        return f"ERROR: {e}"


def _post_vendor_payment(client, row: dict, organization_id, vendor_contacts: list,
                          dup_check_cache: Optional[dict] = None) -> str:
    account_number = row.get("account_number")
    if not account_number:
        return "NOT POSTED: this statement's own bank account_id wasn't resolved -- can't determine the paid-through account."
    vendor_id = find_contact_id(row.get("narration"), vendor_contacts)
    if not vendor_id:
        return (f"NOT POSTED: no single matching vendor contact found for narration {row.get('narration')!r} -- "
                "either no vendor name appears in it, or more than one vendor name matched ambiguously.")
    amount = _amount_as_float(row.get("amount"))
    if amount is None:
        return f"NOT POSTED: no usable amount on this row (raw value: {row.get('amount')!r})."
    txn_date = _format_date(row.get("txn_date"))
    dup_note = _check_duplicate_description_cross_type(client, row.get("narration"), txn_date, organization_id,
                                                         cache=dup_check_cache)
    if dup_note:
        return dup_note
    result = client.create_vendor_payment(
        vendor_id=vendor_id, amount=amount, paid_through_account_id=account_number,
        date=txn_date, reference_number=row.get("reference_number"), description=row.get("narration"),
        organization_id=organization_id,
    )
    payment = result.get("payment") or result.get("vendorpayment") or {}
    payment_id = payment.get("payment_id")
    if not payment_id:
        return f"ERROR: Zoho accepted the vendor payment call but returned no payment_id -- raw response: {result}"
    return f"{_VENDOR_PAYMENT_PREFIX} {payment_id}"


# ---------------------------------------------------------------------------
# AI REFERENCE -- MOVED TO CATEGORIZATION TIME (2026-08-12, LATER SAME DAY,
# per Ravindra: "I want the creating of the reference to happen while doing
# the catergorization not while posting"). generate_ai_reference_preview()
# below is STILL the function that actually calls Gemini and STILL lives
# here -- but it's no longer called from anywhere in THIS module. It's now
# called from categorize_from_module.py's categorize_workbook(), as tier 2
# of a 3-tier lookup (rule's own cf_reference -> Gemini -> the statement's
# raw Reference column) that resolves each row's reference ONCE, up front,
# writing the result into a new "PostingReference" Excel column -- see that
# function's own module-level comment (POSTING_REFERENCE_COLUMN_NAME) for
# the complete story. By the time a row reaches post_row()/
# post_rows_streaming() below, extract_postable_rows() has already read
# that column straight into row["reference_number"] -- this module has NO
# Gemini/AI awareness at all any more, same as before EITHER AI-reference
# version of this feature (this one, or the even-shorter-lived "live while
# posting" one just before it) ever existed.
#
# HISTORY, for anyone reading this later and wondering why the same feature
# moved twice in one day:
#   1. DRY RUN ONLY (2026-08-2x, per Ravindra: "I am planning to fill the
#      reference number for an entry(All types) while posting by running
#      its narration thru a gemini AI prompt ... As of now please give me
#      the prompt response ... which i will inspect") -- purely
#      informational, logged/shown in the widget, zero effect on what was
#      actually sent to Zoho.
#   2. LIVE WHILE POSTING (2026-08-12, per Ravindra: "i want to now post
#      these reference no to the respective entries not just preview") --
#      post_rows_streaming() below called generate_ai_reference_preview()
#      itself, per row, immediately before post_row() dispatched it, and
#      overwrote row["reference_number"] on a clean success.
#   3. LIVE AT CATEGORIZATION TIME (2026-08-12, LATER THE SAME DAY, this
#      version) -- Ravindra decided the reference should be settled once,
#      up front, not re-decided (with a fresh Gemini call) on every posting
#      run -- see categorize_from_module.py for the current implementation.
# Steps 1 and 2's own reasoning-leak defenses (gemini_client.py's
# stop_sequences, this module's _clean_suggested_narration()/
# _looks_like_reasoning_leak() below) carried forward unchanged into step 3
# -- generate_ai_reference_preview() itself never needed to change across
# any of these three moves, only WHO calls it and WHEN.
#
# STILL TRUE, unchanged by any of these moves: an AI hiccup (Gemini error,
# blocked/empty response, reasoning-leak flag) never blocks or fails a real
# posting -- categorize_workbook()'s tier-3 fallback (the statement's own
# raw Reference column) plays the same role step 2's row["reference_number"]
# fallback used to.
#
# EXCEPTION worth knowing, also unchanged by any of these moves --
# _post_bank_transfer() above still checks its own
# extract_employee_er_reference(narration) FIRST and only falls through to
# row["reference_number"] if that regex finds nothing. So for a Transfer
# row whose narration matches the employee-expense-reimbursement ER-number
# pattern, that deterministic, zero-hallucination-risk regex extraction
# still wins over whatever categorize_workbook() wrote into
# PostingReference for that row -- worth knowing if a Transfer row's actual
# posted reference doesn't match what the categorized file's
# PostingReference column showed.
#
# ORIGINAL REQUEST (2026-08-2x, per Ravindra: "I am
# planning to fill the reference number for an entry(All types) while
# posting by running its narration thru a gemini AI prompt ... As of now
# please give me the prompt response ... which i will inspect").
#
# SHAPE REWRITTEN 2026-08-12, per Ravindra: "Can you modify the prompt in
# such a way that the input is just a simple narration line from the
# statement and output is just a suggested narration and nothing else.
# currently it is taking a lot of input and giving a lot of output." The
# ORIGINAL structured-JSON-in/structured-JSON-out design (narration/amount/
# currency/direction/transaction_reference/own_accounts/related_entities/
# loan_providers in, a 10-key JSON object out) is SUPERSEDED -- this is now
# plain-text-in/plain-text-out: just the row's narration, and just a
# suggested narration string back. Per Ravindra's explicit follow-up choice,
# the three classification steps that depended on external context
# (own-account transfer / intercompany transfer / loan-provider matching --
# which needed own_accounts/related_entities/loan_providers) are DROPPED
# entirely, not baked in as fixed lists -- `ai_prompt_template` (still
# fetched LIVE from the cm_referenceaiprompt module's cf_ai_prompt field, per
# Ravindra's choice to keep that architecture rather than hardcode the
# prompt in code) is now expected to hold the simplified 5-step prompt
# (Salary / Employee Expense Reimbursement / Bank Charge-VAT / Counterparty
# fallback / Other) verified in test_gemini_narration_sdk.py, NOT the old
# structured spec text -- Ravindra needs to paste the new prompt text into
# that Zoho record himself; this code has no way to change that field.
#
# The three functions/constants this replaces -- AI_CLASSIFICATION_RESPONSE_
# SCHEMA, _build_ai_classification_input(), _parse_ai_classification_
# response() -- are left in place below, UNWIRED (not deleted, same
# convention as /entities and this module's other superseded pieces), in
# case structured classification is ever wanted again. AI_CLASSIFICATION_
# OUTPUT_KEYS is the one exception -- still ACTIVELY used, since
# generate_ai_reference_preview() below still returns a dict shaped with
# every one of those keys (only "suggested_narration" is ever populated
# now) so main.py's row_detail-building code (which reads
# ai_classification.get("suggested_narration")/.get("flag_for_review")/
# .get("confidence")/.get("error")) keeps working completely UNCHANGED --
# flag_for_review/confidence just come back None now, which main.py's
# existing display logic already handles as "don't show a flag/confidence
# suffix".
#
# 2026-08-12, LATER SAME DAY (x2) -- see the "AI REFERENCE -- MOVED TO
# CATEGORIZATION TIME" header at the very top of this section for where
# this function is called from NOW (categorize_from_module.py's
# categorize_workbook(), not anywhere in this module). generate_ai_
# reference_preview() ITSELF is unchanged across every one of these moves
# -- still just narration-in/suggestion-out, still never raises -- only
# WHO calls it and WHEN changed, each time.
# ---------------------------------------------------------------------------

# Every key the OLD structured-output spec's JSON schema defined -- STILL
# ACTIVELY USED (see note above) purely so generate_ai_reference_preview()
# below can keep returning a dict shaped exactly like before (every one of
# these keys always present), even though only "suggested_narration" is
# ever populated now -- this is what lets categorize_from_module.py's
# categorize_workbook() (its current caller) read a consistent shape
# without needing to know this history.
AI_CLASSIFICATION_OUTPUT_KEYS = (
    "transaction_purpose", "counterparty_name", "matched_entity", "employee_name",
    "invoice_or_ref_number", "period_reference", "suggested_narration",
    "flag_for_review", "confidence", "notes",
)

# SUPERSEDED 2026-08-12 -- no longer passed to gemini_client.generate_text()
# (that method no longer even accepts a response_schema param at all, since
# gemini_client.py was rewritten the same day to use the google-genai SDK
# for plain-text-only output -- see that file's module docstring for why
# structured JSON mode was dropped along with the REST rewrite). Left in
# place, unwired, same "unwire don't delete" convention as elsewhere in this
# module, in case structured classification is ever wanted again -- would
# need its own fresh verification against whatever Gemini API shape is
# current at that point, not just re-wired as-is.
AI_CLASSIFICATION_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "transaction_purpose": {
            "type": "STRING",
            "enum": [
                "inter_account_transfer", "intercompany_transfer", "loan_drawdown",
                "loan_repayment", "interest_income_from_provider", "salary",
                "employee_reimbursement", "bank_charge", "vat_tax", "vendor_payment",
                "customer_receipt", "other",
            ],
        },
        "counterparty_name": {"type": "STRING"},
        "matched_entity": {"type": "STRING"},
        "employee_name": {"type": "STRING"},
        "invoice_or_ref_number": {"type": "STRING"},
        "period_reference": {"type": "STRING"},
        "suggested_narration": {"type": "STRING"},
        "flag_for_review": {"type": "BOOLEAN"},
        "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "notes": {"type": "STRING"},
    },
    "required": list(AI_CLASSIFICATION_OUTPUT_KEYS),
}


def _build_ai_classification_input(row: dict, context: Optional[dict]) -> dict:
    """SUPERSEDED 2026-08-12, NO LONGER CALLED -- see the module-level "AI
    REFERENCE PREVIEW" section header above. generate_ai_reference_preview()
    now sends just the row's own narration as plain text, not this
    structured JSON block. Left in place, unwired, same convention as
    AI_CLASSIFICATION_RESPONSE_SCHEMA above. Original docstring, still
    accurate for what this function itself does if called directly:

    Builds the JSON object the "Bank Narration Extraction Prompt" spec's
    "Inputs to pass with each call" section describes -- narration/amount/
    direction/transaction_reference come straight off `row` (see
    categorize_from_module.extract_postable_rows()'s row shape; `direction`
    is "debit"/"credit"/None, added specifically for this feature).
    `currency` isn't tracked anywhere in this pipeline yet (a statement's
    currency isn't currently carried per-row) -- sent as None/null rather
    than guessed; see main.py's own TODO on this.
    `context` is {"own_accounts": [...], "related_entities": [...],
    "loan_providers": [...]} -- built ONCE per run by the caller (see
    main.py's _build_ai_classification_context()) and passed through
    unchanged for every row; empty lists if the caller has none available
    yet (steps 1-3 of the spec's CLASSIFICATION ORDER just won't match
    anything in that case -- not a crash, just a weaker classification)."""
    context = context or {}
    return {
        "narration": row.get("narration"),
        "amount": row.get("amount"),
        "currency": row.get("currency"),  # not yet populated anywhere -- see docstring above
        "direction": row.get("direction"),
        "transaction_reference": row.get("reference_number"),
        "own_accounts": context.get("own_accounts") or [],
        "related_entities": context.get("related_entities") or [],
        "loan_providers": context.get("loan_providers") or [],
    }


def _parse_ai_classification_response(raw_text: str) -> dict:
    """SUPERSEDED 2026-08-12, NO LONGER CALLED -- see the module-level "AI
    REFERENCE PREVIEW" section header above. generate_ai_reference_preview()
    now takes Gemini's plain text response as-is (it's already just the
    suggested narration, nothing to parse) instead of JSON-decoding it. Left
    in place, unwired, same convention as the two above. Original docstring,
    still accurate for what this function itself does if called directly:

    Parses Gemini's response text as the spec's OUTPUT JSON schema.
    Strips a leading/trailing markdown code fence (```json ... ``` or
    plain ``` ... ```) first -- Gemini frequently wraps JSON output in one
    even when explicitly asked for "ONLY this JSON", so stripping it here
    beats asking Ravindra to fix the prompt wording for something this code
    can just handle. On any parse failure, returns
    {"error": "...", "raw_response": raw_text} with every one of
    AI_CLASSIFICATION_OUTPUT_KEYS also present (blank/None) so a caller can
    treat a parse failure and a successful-but-sparse response identically
    (always index the same keys) rather than needing a separate
    success/failure branch everywhere it reads this result."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    blank_result = {k: None for k in AI_CLASSIFICATION_OUTPUT_KEYS}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        return {**blank_result, "error": f"Gemini's response wasn't valid JSON ({e})", "raw_response": raw_text}
    if not isinstance(parsed, dict):
        return {**blank_result, "error": f"Gemini's response was valid JSON but not an object -- got {type(parsed).__name__}",
                "raw_response": raw_text}
    result = {**blank_result, **parsed, "error": None, "raw_response": raw_text}
    return result


# REASONING-LEAK GUARD (2026-08-12, added after a real live case: Ravindra's
# widget showed a row's AI Reference Preview as visible reasoning/commentary
# text instead of a clean answer -- "Let's check length: `Cyber Knights
# Technology Free Zone` = 34 characters (well under 80 chars). 3. **Refine
# Output:** * `Cyber" -- cut off mid-sentence. Root cause: this project's SDK
# (google.generativeai) has no way to disable Gemini's internal "thinking"
# (see gemini_client.py's THINKING CONTROL note), and this model apparently
# sometimes narrates that reasoning as ordinary visible text rather than a
# separate hidden channel, despite the prompt's own "no explanation" rule.
# THREE layers of defense, in order:
#   1. The prompt itself (cf_ai_prompt in Zoho) was strengthened with more
#      explicit anti-reasoning wording -- see NEW_cf_ai_prompt_text.txt.
#   2. gemini_client.generate_text() is called with stop_sequences=["\n"]
#      below -- Gemini stops generating at the first newline, so a multi-
#      line reasoning dump can't fully reach this function even if it
#      starts one.
#   3. _clean_suggested_narration() + _looks_like_reasoning_leak() below --
#      strips common markdown/reasoning artifacts from whatever's left, and
#      if it STILL doesn't look like a clean short reference line, this
#      function returns an "error" (with the raw text preserved) instead of
#      silently showing garbled text as if it were a real suggestion --
#      same "don't guess, surface it for a human" philosophy as this
#      module's ambiguous-vendor-match and duplicate-detection handling.
# None of these three is airtight alone (a stop sequence doesn't stop a
# reasoning-flavored FIRST line; a prompt instruction can be ignored) --
# together they should catch the vast majority of cases, and the third
# layer's job is specifically to catch whatever the first two miss without
# ever passing obviously-broken text through as if it were trustworthy.
# ---------------------------------------------------------------------------
_MARKDOWN_LEADING_RE = re.compile(r"^[\s\*\-•]+|^\d+[\.\)]\s*")
_MARKDOWN_TRAILING_RE = re.compile(r"[\s\*]+$")
# Phrases/patterns that showed up in the one real reasoning-leak case seen
# live, plus other obvious tells of a model narrating its own process
# instead of just answering -- deliberately loose (any ONE hit is enough to
# flag), since the cost of a false positive (a real answer gets flagged for
# Ravindra to glance at) is much lower than the cost of a false negative
# (a reasoning dump gets treated as a real suggested narration).
_REASONING_LEAK_RE = re.compile(
    r"let'?s check|let'?s (verify|see|think)|refine output|character[s]?\s*\(|"
    r"^\d+\.\s*\*\*|well under \d+ chars|step \d+[:.]", re.IGNORECASE,
)
# 2026-08-12: tightened from 160 (2x the prompt's own 80-char cap, "generous
# headroom" when this was purely a display preview) down to 100, now that a
# passing result gets WRITTEN into Zoho's own reference_number field on a
# real posted record -- Zoho's actual max length for that field isn't
# confirmed (not worth guessing/hardcoding a number that might itself be
# wrong), so staying much closer to the prompt's own 80-char instruction
# reduces the chance of ever sending something long enough to risk a real
# 400 from Zoho on an otherwise-postable row. 100 still leaves comfortable
# room over real examples seen so far (e.g. "Employee Reimbursement -
# YASEEN PASHA ELIYAS - ER3617 ER3742" is 63 chars).
_MAX_PLAUSIBLE_NARRATION_LEN = 100


def _clean_suggested_narration(raw_text: str) -> str:
    """Best-effort cleanup of Gemini's raw reply into a plain reference
    line -- takes only the first line (defense-in-depth alongside
    stop_sequences=["\\n"] in generate_ai_reference_preview() below, in case
    that stop sequence didn't fire for some reason), then strips leading
    markdown bullets/numbering ("* ", "- ", "1. ") BEFORE stripping wrapping
    backticks/whitespace -- order matters here: a reply like "* `Cyber...`"
    has its backtick as the SECOND character, not the first, so stripping
    backticks first (the original order this function shipped with) would
    miss it and leave a stray leading backtick in the output; caught in
    testing and fixed same-day. Trailing asterisks are stripped last. Does
    NOT attempt to fix a genuinely broken/reasoning-flavored response -- see
    _looks_like_reasoning_leak() for that check, which runs on this
    function's OUTPUT."""
    text = (raw_text or "").strip()
    text = text.split("\n", 1)[0].strip()
    text = _MARKDOWN_LEADING_RE.sub("", text)
    text = text.strip("`").strip()
    text = _MARKDOWN_TRAILING_RE.sub("", text)
    return text.strip()


def _looks_like_reasoning_leak(cleaned_text: str) -> bool:
    """True if `cleaned_text` (already run through _clean_suggested_
    narration()) still doesn't look like a plausible short reference line --
    either implausibly long for what the prompt asks for (<=80 chars), or
    matches one of the reasoning-narration phrases/patterns seen in the one
    real live case this guard was built from. See the module-level
    "REASONING-LEAK GUARD" comment above for the full three-layer defense
    this is the last layer of."""
    if not cleaned_text:
        return False  # blank is its own (different) problem -- not this check's job
    if len(cleaned_text) > _MAX_PLAUSIBLE_NARRATION_LEN:
        return True
    if _REASONING_LEAK_RE.search(cleaned_text):
        return True
    return False


def generate_ai_reference_preview(gemini_client, ai_prompt_template: str, row: dict, context: Optional[dict] = None) -> dict:
    """THE current AI Reference Preview logic, as of 2026-08-12 -- see the
    module-level "AI REFERENCE PREVIEW" section header above for the full
    story of what changed and why. Sends just `ai_prompt_template` (the
    cm_referenceaiprompt module's cf_ai_prompt field -- now expected to hold
    the SIMPLIFIED narration-only prompt, not the old structured spec text)
    plus this row's own narration, as plain text, to Gemini -- and returns
    Gemini's plain text reply wrapped into the SAME dict shape this function
    has always returned (every key in AI_CLASSIFICATION_OUTPUT_KEYS present,
    plus "error"/"raw_response") so main.py's existing row_detail-building
    code needs zero changes: "suggested_narration" holds Gemini's answer,
    every other classification key (transaction_purpose/counterparty_name/
    etc.) is always None now since nothing populates them any more.

    `context` is accepted but ignored/unused -- kept in the signature only
    so main.py's existing call site (which still passes
    ai_classification_context, built once per run from Zoho's own_accounts/
    related_entities/loan_providers) doesn't need to change either. Those
    three classification steps are dropped from the prompt itself now (per
    Ravindra's explicit choice), so that context has nothing left to feed.

    Prompt shape sent to Gemini: `ai_prompt_template`, a blank line, then
    "NARRATION:", a blank line, then the row's own narration text --
    mirrors test_gemini_narration_sdk.py/test_gemini_prompt.py's own prompt
    shape exactly (both standalone scripts Ravindra used to validate this
    simplified prompt before asking for it to be wired in here).

    2026-08-12 (later, same day): now also passes stop_sequences=["\\n"] to
    gemini_client.generate_text(), and runs the result through
    _clean_suggested_narration()/_looks_like_reasoning_leak() before
    returning -- see the "REASONING-LEAK GUARD" comment block above this
    function for the real live bug this fixes (Gemini's internal
    "thinking" narrated as visible text instead of a clean one-line answer).
    A response that still looks like a reasoning leak after cleanup comes
    back as an "error" (with the raw text preserved in "raw_response" so
    Ravindra can still see exactly what Gemini said) rather than being
    shown as if it were a trustworthy suggested narration.

    Never raises -- on any failure (blank narration, missing/invalid
    GEMINI_API_KEY, a Gemini API error, a blocked/empty response, a
    reasoning-leak-flagged response, ...) returns the same blank-keys-plus-
    "error" shape as before, so every caller only ever needs to check the
    one "error" key.

    2026-08-12 (this function's callER has moved TWICE the same day -- see
    the module-level "AI REFERENCE -- MOVED TO CATEGORIZATION TIME" header
    above for the full history): this function ITSELF has stayed
    unchanged, narration-in/suggestion-out, across every move -- it's
    always been the caller's job to decide what to do with the result.
    CURRENT caller: categorize_from_module.py's categorize_workbook(),
    which calls this as tier 2 of its 3-tier PostingReference lookup and,
    on a clean, non-error result, uses "suggested_narration" as that row's
    PostingReference cell value; on any error, falls through to its own
    tier 3 (the statement's own raw Reference column) instead. This
    function's own "never raises, always returns the same error-or-success
    shape" contract is exactly what makes that safe regardless of which
    caller/tier structure sits on top of it: a caller only ever applies the
    result when "error" is None, so this function's soft-fail behavior is
    the thing standing between a Gemini hiccup and a bad reference reaching
    a real Zoho posting -- same "never let an optional step take down the
    real work" spirit as this module's other soft-fail helpers (e.g.
    _check_duplicate_description()'s per-candidate try/except)."""
    blank_result = {k: None for k in AI_CLASSIFICATION_OUTPUT_KEYS}
    narration = row.get("narration")
    if not str(narration or "").strip():
        return {**blank_result, "error": "no narration on this row -- nothing to send to Gemini", "raw_response": None}
    prompt = f"{ai_prompt_template}\n\nNARRATION:\n{narration}"
    try:
        raw_text = gemini_client.generate_text(prompt, max_output_tokens=1024, stop_sequences=["\n"])
    except Exception as e:
        return {**blank_result, "error": f"AI reference preview unavailable -- {e}", "raw_response": None}
    cleaned = _clean_suggested_narration(raw_text)
    if _looks_like_reasoning_leak(cleaned):
        return {**blank_result,
                "error": "Gemini's response looked like reasoning/commentary rather than a clean reference line "
                         "-- see raw_response for exactly what it said.",
                "raw_response": raw_text}
    return {**blank_result, "suggested_narration": cleaned, "error": None, "raw_response": raw_text}


def post_row(client, row: dict, organization_id=None, vendor_contacts: Optional[list] = None,
             dup_check_cache: Optional[dict] = None) -> str:
    """Posts ONE row (a dict from categorize_from_module.extract_postable_
    rows()'s "rows" list) to Zoho Books, dispatching on row["posting_type"]
    per the module docstring's mapping. Returns the result text (see the
    RESULT TEXT CONVENTION section above) -- never raises; any exception
    from the Zoho API call is caught and turned into an "ERROR: ..." result
    so one bad row can't take the whole posting run down.

    vendor_contacts is only needed for Vendor Payment rows -- pass the
    already-fetched list (see post_rows() below, which fetches it once per
    run, not once per row) or None/omit if this row definitely isn't a
    Vendor Payment (post_rows() always passes it; a direct caller posting a
    single non-vendor-payment row can leave it out).

    dup_check_cache (2026-08-13, later still, per Ravindra's "posting logic
    has become very slow" report): an optional dict, shared across every row
    in ONE run, threaded straight through to whichever _post_*() function
    this row dispatches to -- see _check_duplicate_description()'s own
    docstring for the caching mechanics this enables. None/omit (the
    default) preserves the old always-live-fetch behavior -- post_rows_
    streaming() below is what actually creates and shares one across a
    whole file's rows; a direct caller posting a single row in isolation has
    no reason to pass one.

    2026-08-12 (later same day), per Ravindra: for the Vendor Payments
    CATEGORY (row["is_vendor_payment_category"], set by
    categorize_from_module.extract_postable_rows() -- keyed on the
    Categorization module's Category field, NOT row["posting_type"]/cf_
    type_of_transaction), this pipeline never creates a new Zoho Vendor
    Payment. Instead it RECONCILES against one that should already exist in
    Zoho: "we dont want to post anything but needs to check ... 1. a
    posting already exists for this posting type with the reference no
    =postingreference col value 2. date from statement matches date in the
    existing post 3. amount in debit column matches the amount in the post
    4. if all match update the statement row with match Successful or not
    matched." See _reconcile_vendor_payment() for the full match logic --
    checked FIRST, before the posting_type dispatch below, since a Vendor
    Payments-category row should never fall through to _post_vendor_payment
    (which creates a new record) even if its posting_type also happens to
    be a vendor-payment type."""
    try:
        if row.get("is_vendor_payment_category"):
            return _reconcile_vendor_payment(client, row, organization_id)
        posting_type = _normalize_posting_type(row.get("posting_type"))
        if posting_type in _JOURNAL_TYPES:
            return _post_journal_like(client, row, organization_id, dup_check_cache=dup_check_cache)
        elif posting_type in _TRANSFER_TYPES:
            return _post_bank_transfer(client, row, organization_id, dup_check_cache=dup_check_cache)
        elif posting_type in _EXPENSE_TYPES:
            return _post_expense(client, row, organization_id, dup_check_cache=dup_check_cache)
        elif posting_type in _VENDOR_PAYMENT_TYPES:
            return _post_vendor_payment(client, row, organization_id, vendor_contacts or [],
                                         dup_check_cache=dup_check_cache)
        elif posting_type in _CUSTOMER_PAYMENT_TYPES:
            return ("NOT POSTED: Customer payments must be applied to a specific invoice manually in Zoho "
                    "Books -- this pipeline has no invoice-matching logic (Zoho's POST /customerpayments "
                    "requires an invoices array with no documented way to record an unapplied payment).")
        else:
            return (f"NOT POSTED: unrecognized or blank posting type {row.get('posting_type')!r} -- expected one "
                    "of Journal / Transfer to another account / Transfer from another account / Expense / "
                    "Vendor Payment / Customer payment (set on the matched Categorization rule's "
                    "cf_type_of_transaction).")
    except Exception as e:
        return f"ERROR: {e}"


def post_rows_streaming(client, rows: List[dict], organization_id=None, dup_check_cache: Optional[dict] = None):
    """Generator variant of post_rows() -- posts each row ONE AT A TIME,
    yielding (excel_row, result_text, stats_so_far) immediately after each
    row's own Zoho call returns, instead of posting everything and handing
    back one final result. Added 2026-08-09 (later still), per Ravindra:
    "i want to see live posting of each post as it updates and count also
    updates live ... not at the end" -- main.py's /post-transactions now
    streams these events straight to the widget as they happen (an NDJSON
    response) instead of the widget only finding out once the whole file
    is done.

    2026-08-12, UNWIRED AGAIN (SAME DAY): this briefly called
    generate_ai_reference_preview() itself, per row, BEFORE post_row()
    dispatched it, to compute/apply an AI-suggested reference_number live
    WHILE posting -- see the module-level "AI REFERENCE PREVIEW" section
    header above for that version's own history. Ravindra then asked for a
    further pivot the SAME day: "I want the creating of the reference to
    happen while doing the catergorization not while posting." So that
    logic (and the gemini_client/ai_prompt_template params, and the
    ai_classification/ai_reference_applied values this used to also yield)
    moved to categorize_from_module.py's categorize_workbook() instead --
    see POSTING_REFERENCE_COLUMN_NAME's own comment there for the current
    3-tier lookup (rule cf_reference -> Gemini -> the statement's own raw
    Reference column), now resolved ONCE at categorization time and simply
    READ from the categorized file's PostingReference column by
    categorize_from_module.extract_postable_rows() -- by the time a row
    reaches THIS function, row["reference_number"] already holds its final
    value; this function (and post_row() below) have no AI/Gemini
    awareness at all any more, same as before either AI-reference version
    of this feature ever existed. generate_ai_reference_preview()/
    _clean_suggested_narration()/_looks_like_reasoning_leak() themselves
    are UNCHANGED and still fully wired -- just called from
    categorize_workbook() now instead of from here.

    post_rows() below is a thin wrapper around this generator -- exact same
    behavior/return shape, zero change for any existing caller that doesn't
    need live per-row progress.

    Fetches vendor contacts ONCE up front, only if at least one row needs
    it (Vendor Payment) -- same "list once, match client-side" shape as
    the original post_rows(). Posts strictly one row at a time (not
    parallel) -- avoids hammering Zoho's API rate limit and keeps each
    row's own log line (and each yielded event) in clean sequential order.

    stats_so_far is yielded as a FRESH, independent snapshot (a real copy,
    including its nested "by_type" dict, not a shared mutable reference)
    -- safe for a caller to serialize directly into a JSON line without it
    silently changing underneath them as the loop continues to the next
    row.

    dup_check_cache (2026-08-13, later still, per Ravindra's "posting logic
    has become very slow ... if i have a lot of transactions it will take
    for ever" report): THIS is where the fix actually lives. Creates one
    fresh dict here if the caller doesn't pass one, and threads the SAME
    dict into every row's post_row() call for this whole run -- so
    _check_duplicate_description()'s (type, date) cache (see its own
    docstring) is shared across every row in the file, not just within one
    row. A caller that also posts Egypt itemized groups in the SAME run
    (main.py's /post-transactions, right after this generator finishes) can
    pass its OWN dict in and reuse it for post_itemized_expense_group() too
    -- both phases then share one cache for the whole file, not two
    separate ones."""
    if dup_check_cache is None:
        dup_check_cache = {}
    needs_vendor_contacts = any(
        _normalize_posting_type(r.get("posting_type")) in _VENDOR_PAYMENT_TYPES for r in rows
    )
    vendor_contacts = []
    if needs_vendor_contacts:
        try:
            vendor_contacts = client.list_contacts(contact_type="vendor", organization_id=organization_id)
        except Exception as e:
            print(f"[posting_service] post_rows_streaming: couldn't fetch vendor contacts -- every Vendor Payment "
                  f"row this run will fail to match a vendor -> {e}", flush=True)
            vendor_contacts = []

    stats = {"total_attempted": 0, "posted": 0, "not_posted": 0, "errors": 0, "by_type": {}}
    for row in rows:
        excel_row = row["excel_row"]
        result_text = post_row(client, row, organization_id, vendor_contacts, dup_check_cache=dup_check_cache)
        stats["total_attempted"] += 1
        posting_type_label = str(row.get("posting_type") or "(blank)").strip() or "(blank)"
        stats["by_type"][posting_type_label] = stats["by_type"].get(posting_type_label, 0) + 1
        upper = result_text.upper()
        # 2026-08-12 (later same day), per Ravindra: a Vendor Payment
        # reconciliation MATCH ("Reconciled Vendor Payment <id>") is
        # checked before the generic NOT POSTED/ERROR/else split below --
        # it doesn't start with either of those prefixes so it would
        # otherwise fall into the `else` (posted) bucket, but nothing was
        # actually posted to Zoho for this row, only validated against an
        # existing record -- see is_vendor_payment_reconciliation_result()'s
        # own docstring for why _is_success_reference() (which still treats
        # this same text as a success, for .COMPLETED/already-posted
        # purposes) is deliberately NOT used here.
        if is_vendor_payment_reconciliation_result(result_text):
            stats["not_posted"] += 1
        elif upper.startswith("NOT POSTED"):
            stats["not_posted"] += 1
        elif upper.startswith("ERROR"):
            stats["errors"] += 1
        else:
            stats["posted"] += 1
        print(f"[posting_service] post_rows_streaming: row {excel_row} (category={row.get('category')!r}, "
              f"posting_type={row.get('posting_type')!r}) -> {result_text}", flush=True)
        # Real copy, not dict(stats) (which would share the nested by_type
        # dict by reference across every yielded snapshot) -- see docstring.
        stats_snapshot = {**stats, "by_type": dict(stats["by_type"])}
        yield excel_row, result_text, stats_snapshot


def post_rows(client, rows: List[dict], organization_id=None) -> tuple:
    """Posts every row in `rows` (extract_postable_rows()'s "rows" list) to
    Zoho Books and returns only once everything is done -- a thin wrapper
    around post_rows_streaming() (2026-08-09, later still) for any caller
    that just wants the final result and doesn't need live per-row
    progress; main.py's /post-transactions uses post_rows_streaming()
    directly instead, to stream progress to the widget as it happens.

    Returns (results, stats):
      results -- {excel_row: result_text, ...}, ready to pass straight into
                 categorize_from_module.write_posting_results().
      stats   -- {"total_attempted": int, "posted": int, "not_posted": int,
                  "errors": int, "by_type": {posting_type: count, ...}}
                 (by_type counts every attempted row, including failures --
                 "how many of each kind did we even try" not just successes)."""
    results = {}
    stats = {"total_attempted": 0, "posted": 0, "not_posted": 0, "errors": 0, "by_type": {}}
    for excel_row, result_text, stats_so_far in post_rows_streaming(client, rows, organization_id):
        results[excel_row] = result_text
        stats = stats_so_far
    return results, stats


def parse_zoho_reference(zoho_reference) -> tuple:
    """Parses the "<Type> <id>" convention post_row() writes into a "Zoho
    Posting Reference" cell on a successful posting (e.g. "Expense 12345")
    into (type_label, record_id). Added 2026-08-13 for main.py's Supporting
    Document Attachment feature: per Ravindra ("why is it even using date
    it has to just check the notes.desc field right" -> confirmed:
    attach directly using the row's own already-known posting reference,
    not a fresh notes/description search) -- the row a supporting document
    gets attached to is ALREADY known (it's exactly what post_row() posted
    it as), so there's no need to search Zoho for it by narration/date at
    all; this just needs the (type, id) pulled back out of that same text.

    Same prefix-matching logic as delete_posting()'s own inline parsing
    below -- kept as an intentional DUPLICATE here (not refactored into a
    shared call site delete_posting() also uses) specifically so this new
    caller can't change delete_posting()'s already-live, already-tested
    behavior by accident.

    Returns (None, None) if `zoho_reference` is blank, is a
    "<Reconciled Vendor Payment> ..." note (a reconciliation MATCH, not a
    record this pipeline created -- see _VENDOR_PAYMENT_RECONCILED_PREFIX's
    own comment), or doesn't start with any of the four recognized "<Type> "
    prefixes (e.g. a "NOT POSTED: ..."/"ERROR: ..." note -- not a real
    posting reference to begin with)."""
    text = str(zoho_reference or "").strip()
    if not text:
        return (None, None)
    if text.startswith(_VENDOR_PAYMENT_RECONCILED_PREFIX + " "):
        return (None, None)
    if text.startswith(_VENDOR_PAYMENT_PREFIX + " "):
        return (_VENDOR_PAYMENT_PREFIX, text[len(_VENDOR_PAYMENT_PREFIX) + 1:].strip() or None)
    if text.startswith(_BANK_TRANSFER_PREFIX + " "):
        return (_BANK_TRANSFER_PREFIX, text[len(_BANK_TRANSFER_PREFIX) + 1:].strip() or None)
    if text.startswith(_JOURNAL_PREFIX + " "):
        return (_JOURNAL_PREFIX, text[len(_JOURNAL_PREFIX) + 1:].strip() or None)
    if text.startswith(_EXPENSE_PREFIX + " "):
        return (_EXPENSE_PREFIX, text[len(_EXPENSE_PREFIX) + 1:].strip() or None)
    return (None, None)


def delete_posting(client, zoho_reference, organization_id=None) -> dict:
    """Deletes the Zoho Books object a "Zoho Posting Reference" cell's text
    refers to -- parses the SAME "<Type> <id>" convention post_row() writes
    on success (see the module docstring's RESULT TEXT CONVENTION) to know
    which of delete_journal_entry()/delete_expense()/delete_vendor_payment()
    to call. 2026-08-09, per Ravindra's confirmed Delete-button behavior
    ("Delete in Zoho + clear reference (Recommended)") -- main.py's
    /delete-postings calls this once per row being deleted, then (only on
    success) calls categorize_from_module.clear_posting_reference() to blank
    that row's cell so it's eligible to post again.

    Returns {"success": bool, "message": str}. Never raises -- any Zoho API
    error (e.g. the record was already deleted by hand in Zoho, or the
    reference text doesn't parse) comes back as success=False with the
    reason in "message", so main.py can report it per-row without one bad
    delete aborting a "Delete All" batch."""
    text = str(zoho_reference or "").strip()
    if not text:
        return {"success": False, "message": "No Zoho Posting Reference to delete -- this row was never posted."}

    # Longest/most-specific prefix first -- "Vendor Payment " and "Bank
    # Transfer " both contain a space themselves, so a naive split(" ", 1)
    # on any prefix would mis-cut it. "Bank Transfer " doesn't collide with
    # any other prefix here, so its position among these elif branches
    # doesn't matter beyond being checked before the fallback else.
    if text.startswith(_VENDOR_PAYMENT_RECONCILED_PREFIX + " "):
        # 2026-08-12 (later same day): a Vendor Payments-category row's
        # result never CREATED anything in Zoho -- _reconcile_vendor_
        # payment() only matched against a Vendor Payment that already
        # existed there. Deliberately checked before the plain
        # _VENDOR_PAYMENT_PREFIX branch below (this prefix doesn't start
        # with "Vendor Payment " -- see _VENDOR_PAYMENT_RECONCILED_PREFIX's
        # own comment -- so order wouldn't actually matter, but keeping the
        # more-specific check first reads clearly regardless).
        return {"success": False,
                "message": f"{text!r} is a reconciliation match, not something this pipeline posted -- there's "
                            "nothing here to delete. If the underlying Vendor Payment in Zoho needs to change, "
                            "edit or delete it directly in Zoho Books; you can still clear this cell's reference "
                            "so the row is eligible to be re-checked on the next posting run."}
    elif text.startswith(_VENDOR_PAYMENT_PREFIX + " "):
        record_id = text[len(_VENDOR_PAYMENT_PREFIX) + 1:].strip()
        delete_fn = client.delete_vendor_payment
        type_label = _VENDOR_PAYMENT_PREFIX
    elif text.startswith(_BANK_TRANSFER_PREFIX + " "):
        record_id = text[len(_BANK_TRANSFER_PREFIX) + 1:].strip()
        delete_fn = client.delete_bank_transaction
        type_label = _BANK_TRANSFER_PREFIX
    elif text.startswith(_JOURNAL_PREFIX + " "):
        record_id = text[len(_JOURNAL_PREFIX) + 1:].strip()
        delete_fn = client.delete_journal_entry
        type_label = _JOURNAL_PREFIX
    elif text.startswith(_EXPENSE_PREFIX + " "):
        record_id = text[len(_EXPENSE_PREFIX) + 1:].strip()
        delete_fn = client.delete_expense
        type_label = _EXPENSE_PREFIX
    else:
        return {"success": False,
                "message": f"Couldn't tell what kind of Zoho record {text!r} refers to -- expected it to start "
                            f"with {_JOURNAL_PREFIX!r}, {_BANK_TRANSFER_PREFIX!r}, {_EXPENSE_PREFIX!r}, or "
                            f"{_VENDOR_PAYMENT_PREFIX!r} (this isn't a real posting reference, e.g. a "
                            "'NOT POSTED: ...'/'ERROR: ...' note has nothing in Zoho to delete)."}
    if not record_id:
        return {"success": False, "message": f"Couldn't parse a record ID out of {text!r}."}

    try:
        delete_fn(record_id, organization_id=organization_id)
        return {"success": True, "message": f"Deleted {type_label} {record_id} from Zoho Books."}
    except Exception as e:
        return {"success": False, "message": f"Zoho rejected deleting {type_label} {record_id}: {e}"}