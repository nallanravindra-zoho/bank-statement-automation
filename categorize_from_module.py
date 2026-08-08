"""
categorize_from_module.py

Categorization core used by the GCP service the Zoho Books widget calls.
Unlike categorize_statement.py (which reads a Categories.xlsx workbook off
OneDrive), this reads category/account/posting rules from the Zoho Books
"Categorization" custom module -- the widget fetches those records itself
(via the GCP /categorization-rules endpoint) and sends them straight through
in the request payload, so everything here works on plain Python dicts/lists,
with no Zoho connection of its own.

Expected raw record shape (one dict per Categorization module record, exact
field API names from that module):
    cf_category            -- str, the category label, e.g. "Bank Charges"
    cf_account_to_be_posted -- str, "<account_id> - <account name>" (a
                               hyphen-formatted dropdown, same display
                               convention as main.py's cf_entity -- see
                               _parse_account_to_be_posted()) -- the SOLE
                               source for the account to use for whichever
                               side of the entry ISN'T this statement's own
                               bank account. (The old cf_from_account/
                               cf_to_account two-field lookup is no longer
                               read at all -- removed 2026-08-07 per
                               Ravindra: "i dont need the fall back just use
                               the new field.")
    cf_key_words            -- str, comma-separated keywords -- see the
                               PATTERN KEYWORDS section below for entries
                               like "an ER number" that aren't a fixed string.
    cf_type_of_transaction  -- str, posting type, e.g. "Journal Entry"
    cf_priority             -- OPTIONAL str/number, e.g. "1" -- see the
                               PRIORITY section below. Fine to leave every
                               record's cf_priority blank; nothing changes
                               until at least one record sets it.

PATTERN KEYWORDS (2026-07-24 update): most keywords are plain substrings --
"bank charge" matches any narration containing "bank charge" (case-
insensitive). But some categories key off a PATTERN rather than fixed text --
e.g. Expense reimbursement narrations contain "ER" followed by AT LEAST 2
digits (ER42, ER2201, ER9001, ...), where the literal text isn't fixed but the
SHAPE is. Plain substring matching can't express "2 or more digits" -- there's
no literal string to search for. For these, wrap a regular expression in
slashes in cf_key_words: /ER\d{2,}/ matches "ER" followed by 2-or-more
digits, anywhere in the narration, case-insensitively. Mix freely with plain
keywords in the same field: "salary, /ER\d{2,}/, bank charge" -- the comma
splitter (_split_keywords_respecting_regex()) is regex-aware, so a comma
INSIDE a /.../ pattern (e.g. \d{2,4}) doesn't get mis-split. An invalid
regex is skipped with a printed warning rather than crashing the run.

PRECEDENCE between literal keywords and patterns (2026-07-24, later update):
a single narration can contain BOTH a specific literal keyword and something
that matches a broad pattern -- e.g. a VAT line or a Bank Charges line can
still mention an ER-style claim/reference number in its free text ("...VAT
Claire ER3700...", "...BANK CHARGE S Claire ER3700..."). If patterns were
checked in the same pass as keywords, whichever rule happened to come first
in the module's record order would win by accident, which could easily
misfile a VAT/Bank Charges row as Expense reimbursement. To make this
deterministic instead of order-dependent, classify_rule() checks every
rule's LITERAL keywords first, across the whole rule list, and only
falls back to checking patterns (across the whole list again) if nothing
matched literally anywhere. In practice: give VAT, Bank Charges, and any
other specific category a literal keyword ("vat", "bank charge", ...) --
that literal match will always be found and win before the broad
/ER\d{2,}/ pattern is ever consulted, regardless of which order the
categories happen to sit in inside the Categorization module. No per-rule
"exclude" list or module-level ordering is needed for this.

PRIORITY (2026-07-26 update, per Ravindra): the literal-vs-pattern precedence
above only solves ONE specific ordering problem (a specific keyword beating a
broad regex). It does NOT help when two categories both match by LITERAL
keyword on the same narration -- e.g. a WeFi loan-repayment line, its own
bank-charges line, and its own VAT-on-charges line all mention "WeFi" (the
counterparty), so a "Loan taken" rule keyed on "wefi" will literal-match all
three unless "Bank Charges"/"VAT" are guaranteed to be checked FIRST. Before
this update, that first-checked-wins order was just whatever order the
Categorization module happens to return records in -- not something Ravindra
could reliably control from the Zoho UI. New OPTIONAL field `cf_priority`
(any integer, e.g. 1, 2, 10 -- lower runs first) fixes this: build_rules()
sorts the whole rule list by cf_priority (ascending) before classify_rule()
ever sees it, so a rule with cf_priority=1 is always checked before one with
cf_priority=10, in BOTH the literal pass and the pattern pass, regardless of
which record was created first in Zoho. Leave cf_priority blank on a record
and it sorts after every prioritized one, keeping its place relative to other
un-prioritized records unchanged (a stable sort, so this is 100% backward
compatible -- if no record sets cf_priority, behavior is identical to before
this update). Recommended use: give "Bank Charges" and "VAT on Bank Charges"
a low cf_priority (1, 2) so they're always checked before a broader/looser
rule like "Loan taken" that might share a keyword with them.

FIELD NAME, live-confirmed (2026-08-02): Ravindra's real Categorization
module names this field `cf_rule` (the "RULE" column he actually built in
Zoho), not `cf_priority` -- a real /categorization-rules dump confirmed
every record's priority value lives under `cf_rule` ("1".."27"), with no
`cf_priority` key present anywhere in the response. build_rules() reads
`cf_priority` first (for a future customer whose module genuinely uses
that name) and falls back to `cf_rule` if that's blank/missing -- either
name works, whichever the module actually has wins.

Adds FOUR columns to the output workbook (categorize_statement.py's CLI path
only adds one -- Category):
    Category        -- dropdown of all categories + "Others", matched
                       category pre-selected (same data-validation mechanism
                       as categorize_statement.py's add_category_dropdown()).
    Debit Account    -- see below.
    Credit Account   -- see below.
    Posting Type     -- dropdown of every distinct posting type seen across
                        the rules, matched rule's type pre-selected.

Debit Account / Credit Account logic (2026-07-24 update, MODULE-VALUE SOURCE
CHANGED 2026-08-06): the OTHER side of every entry (not this statement's own
bank account) is always "the account this category should post against",
which comes from the matched Categorization rule, not the statement. So for
each transaction row:
  1. Look at the STATEMENT's own Debit/Credit columns for that row (not the
     module) to see which side of the transaction it is.
  2. Whichever output column corresponds to that side gets account_number,
     passed in by the caller (main.py's /categorize).
  3. The OTHER output column gets the matched rule's "module_account" value
     (see build_rules()) -- per Ravindra ("the other account debit or
     credit but be taken from the categorization module field
     cf_account_to_be_posted ... previously we were looking up this from
     the debit/credit account no fields"), this is the account_id parsed
     out of cf_account_to_be_posted (a single hyphen-formatted "<account_id>
     - <account name>" dropdown field -- see _parse_account_to_be_posted()).
     This is the SOLE source (no fallback field -- removed 2026-08-07 per
     Ravindra: "i dont need the fall back just use the new field") -- a
     record with cf_account_to_be_posted blank simply has no module_account,
     same as before: that side of the entry is left blank.
Example: row has an amount in the statement's Debit column -> Credit Account
output = account_number (this account is the credit side), Debit Account
output = the matched rule's module_account value.
If a row has neither/both a Debit and Credit amount, or account_number
wasn't provided, whichever side can't be determined is left blank rather
than guessed.

IMPORTANT (2026-07-25 update, per Ravindra): despite its name, account_number
must be the current statement's own bank account's real Zoho Books
chart-of-account unique ID (account_id) -- the SAME kind of value
cf_account_to_be_posted already stores -- NOT the bank account's own
number/IBAN. The bank account number (e.g. "012977101447") is only valid for
finding the right OneDrive file to begin with; it is not a valid value to
land in a Debit Account/Credit Account column, since whatever consumes this
workbook downstream to actually post entries (e.g. the module's "Post Bank
Transactions" button) needs a real account_id there, exactly as the
Categorization module's own cf_account_to_be_posted field already does. The
caller (widget.js) is responsible for resolving the bank account number to
its real account_id before calling /categorize -- this module has no Zoho
connection of its own and just writes back whatever string it's given, so
it cannot do that resolution or validate it itself. Pass "" if the caller
couldn't resolve an account_id -- that side is then left blank instead of
writing a wrong-shaped value.

Re-running on an already-categorized file (2026-07-24 update): the FOUR
generated columns (under their current OR any previous name this code has
used) are stripped from the header/data before recomputing, so running this
twice on the same file replaces the columns in place instead of piling up a
second set each time -- see _strip_previously_generated_columns().

Rows matching no rule's keywords get Category="Others"; Debit/Credit Account
for an Others row still gets whichever side account_number can determine,
but the "module value" side stays blank since there's no matched rule to
pull it from.

LIVE-RECALCULATING Debit Account/Credit Account/Posting Type (2026-07-25
update, per Ravindra): these three columns are now Excel FORMULAS, not
static values -- if someone changes a row's Category dropdown by hand (e.g.
overriding a mismatch), Debit Account/Credit Account/Posting Type
automatically update to match the newly picked category, live in Excel, no
re-run of this service needed. This works by writing a hidden "_Rules"
sheet (see _write_rules_lookup_sheet()) -- one row per category with its
module-side account (the account_id parsed from cf_account_to_be_posted)
and posting type, plus this statement's own resolved account_id
in a fixed cell (_Rules!$G$1) -- and pointing each row's three output cells
at VLOOKUP()/IF() formulas against that sheet and the row's OWN Category/
Debit/Credit cells, instead of baking in today's computed value. The
same "whichever side has an amount gets account_number, the other gets the
category's module value, both blank if ambiguous" logic from the section
above is reproduced exactly in the formula (see _account_formula()) -- this
is a presentation change (Python still classifies every row and computes
`stats` exactly as before, for the widget's summary numbers), not a change
to the underlying rule. A formula cell can still be overwritten by typing a
literal value directly into it, same as any Excel formula -- that's the
manual-override path for a one-off exception.
"""
import re
import sys
from copy import copy as _copy_style
from typing import List, Optional, Tuple

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from categorize_statement import (
    NARRATION_HEADER_ALIASES,
    FALLBACK_CATEGORY,
    HEADER_SCAN_LIMIT,
    find_header_row,
    find_column,
)

# Statement's OWN Debit/Credit columns (distinct from the Categorization
# module's cf_account_to_be_posted) -- these say which side of the
# transaction a row is on, so we know which output column the current
# account number belongs in. Same find_column() exact-then-substring
# matching as NARRATION_HEADER_ALIASES.
DEBIT_HEADER_ALIASES = {"debit", "debit amount", "debit(dr)", "debit (dr)", "dr", "withdrawal", "withdrawal amount"}
CREDIT_HEADER_ALIASES = {"credit", "credit amount", "credit(cr)", "credit (cr)", "cr", "deposit", "deposit amount"}

# Needed only for POSTING (extract_postable_rows() below), not categorization
# itself -- Zoho Books requires a date on every posting, and the reference
# number is worth carrying through for audit trail if the statement has one.
DATE_HEADER_ALIASES = {"transaction date", "date", "txn date", "value date"}
REFERENCE_HEADER_ALIASES = {"transaction reference", "reference", "reference number", "cheque number"}

# New column posting adds (2026-07-26, per Ravindra) -- deliberately NOT
# added to GENERATED_COLUMN_NAMES below: that set is stripped and rebuilt
# fresh every time categorize_workbook() re-runs, which is correct for
# Category/Debit Account/Credit Account/Posting Type (those should always
# reflect the CURRENT rules) but would be actively dangerous here -- stripping
# this column on a re-categorize would erase the record of which rows were
# already posted to Zoho Books, and a subsequent posting run would have no
# way to tell and could double-post. Because it's excluded from
# GENERATED_COLUMN_NAMES, categorize_workbook() passes it through completely
# untouched, same as any other column that isn't one of the four it manages.
POSTED_COLUMN_NAME = "Zoho Posting Reference"

# Every header name this code has ever generated -- stripped from the
# header/data before recomputing on a re-run, so re-running never adds a
# second set of columns. Includes the OLD "From Account"/"To Account" names
# too, in case a file was already run once under the previous version of
# this code before the Debit/Credit Account rename.
GENERATED_COLUMN_NAMES = {
    "category", "posting type",
    "debit account", "credit account",
    "from account", "to account",  # pre-2026-07-24 names
}

OTHERS_RULE = {
    "category": FALLBACK_CATEGORY,
    "keywords": [],
    "patterns": [],
    "module_account": None,
    "type_of_posting": None,
}

# Priority any rule gets if cf_priority is blank/missing (see module
# docstring's PRIORITY section) -- deliberately far larger than any priority
# a person would realistically type, so an un-prioritized rule always sorts
# after every prioritized one. Rules that both fall back to this same
# default keep their original module-record relative order, since the sort
# in build_rules() is stable -- this is what makes the feature 100%
# backward-compatible when no cf_priority is set anywhere.
DEFAULT_PRIORITY = 1_000_000


def _parse_priority(priority_raw, category: str) -> int:
    """Parses cf_priority into an int, defaulting to DEFAULT_PRIORITY if
    blank/missing. A non-numeric value is treated the same as blank, with a
    warning printed to stderr, rather than failing the whole rules build over
    one typo.

    2026-08-02 fix: tries int() first, then falls back to float() before
    giving up. This matters because if cf_priority is set up in Zoho as a
    "Decimal" custom field (rather than "Number"/"Auto Number"), the API
    returns its value decimal-formatted -- "1.00", not "1" -- and plain
    int("1.00") raises ValueError. Before this fix, THAT would silently push
    every single prioritized rule back to DEFAULT_PRIORITY (falls back to
    plain module-record order), which looks exactly like "the priority
    feature stopped doing anything" even though every record still has its
    priority value set correctly in Zoho -- only a warning printed to the
    Cloud Run logs (not visible in the widget) would have hinted at it."""
    if priority_raw is None or str(priority_raw).strip() == "":
        return DEFAULT_PRIORITY
    raw_str = str(priority_raw).strip()
    try:
        return int(raw_str)
    except ValueError:
        pass
    try:
        return int(round(float(raw_str)))
    except ValueError:
        print(f"Warning: Categorization rule {category!r} has a non-numeric cf_priority "
              f"{priority_raw!r} -- ignoring it (this rule will be checked in its default/"
              f"last position instead).", file=sys.stderr)
        return DEFAULT_PRIORITY


def _split_keywords_respecting_regex(raw: str) -> List[str]:
    """Splits cf_key_words on commas, EXCEPT commas that fall inside a
    /regex/ segment -- a naive .split(",") would wrongly cut a regex
    quantifier like \\d{2,4} in half. Toggles an "inside a /.../ pattern"
    flag on every "/" seen; commas are only split-points while that flag is
    off."""
    parts = []
    current = []
    in_regex = False
    for ch in raw:
        if ch == "/":
            in_regex = not in_regex
            current.append(ch)
        elif ch == "," and not in_regex:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _parse_keywords(keywords_raw, category: str):
    """Splits a raw cf_key_words value into (literal_keywords, compiled_patterns).
    A piece wrapped in slashes (e.g. "/ER\\d{4}/") is compiled as a
    case-insensitive regex; anything else is a plain lowercased literal
    keyword, matched as a substring same as before. An invalid regex is
    skipped with a warning printed to stderr rather than raising -- one bad
    rule shouldn't fail the whole categorization run."""
    keywords = []
    patterns = []
    for piece in _split_keywords_respecting_regex(str(keywords_raw)):
        if len(piece) >= 2 and piece.startswith("/") and piece.endswith("/"):
            pattern_text = piece[1:-1]
            try:
                patterns.append(re.compile(pattern_text, re.IGNORECASE))
            except re.error as e:
                print(f"Warning: Categorization rule {category!r} has an invalid regex keyword "
                      f"{piece!r} -- skipping it: {e}", file=sys.stderr)
        else:
            keywords.append(piece.lower())
    return keywords, patterns


def _parse_account_to_be_posted(raw_value) -> Optional[str]:
    """Parses cf_account_to_be_posted -- a Categorization module field
    Ravindra confirmed (2026-08-06) is a hyphen-formatted "<account_id> -
    <account name>" dropdown value, e.g. "2122051000000069040 - Bank
    Charges Expense" (same display convention as main.py's cf_entity field,
    "<organization_id> - <organization name>" -- see that file's
    _extract_org_id_from_cf_entity()). Returns just the account_id (the
    text before the FIRST hyphen, stripped), or None if raw_value is
    blank/missing -- that side of the entry is then left blank rather than
    guessed (no fallback field, per Ravindra: "i dont need the fall back
    just use the new field").

    Uses split("-", 1) rather than a digit-extraction regex (unlike
    cf_entity's org_id, which is always purely numeric) because a Zoho
    account_id is also always purely numeric in practice, but splitting on
    the first hyphen is more direct here and doesn't assume the ID never
    contains anything but digits."""
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    first_part = text.split("-", 1)[0].strip()
    return first_part or None


def _has_amount(value) -> bool:
    """True if a statement cell looks like it holds a real (non-zero)
    amount -- None/blank/"0"/"0.00" all count as "no amount" so a
    Debit/Credit column with an empty cell for this row doesn't get
    mistaken for the transaction's side. NOT called by categorize_workbook()
    itself since the 2026-07-25 formula update -- that same check is now
    reproduced inside the Debit Account/Credit Account Excel formulas (see
    _account_formula()), so it lives client-side in the spreadsheet and
    recomputes if a Debit/Credit amount is edited. Left here as a documented,
    still-correct utility rather than deleted, in case a future server-side
    check needs it again."""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    try:
        return float(text.replace(",", "")) != 0
    except ValueError:
        return True  # non-numeric but present -- treat as "has a value"


def _strip_previously_generated_columns(header: list, data_rows: list) -> Tuple[list, list, list]:
    """If header already contains any column this code generates (from an
    earlier run on the same file), drops those columns from both the header
    and every data row before the caller appends fresh ones -- otherwise
    re-running on an already-categorized file would pile up a second set of
    Category/Debit Account/Credit Account/Posting Type columns instead of
    replacing them. Also returns keep_indices -- the ORIGINAL (0-based)
    column index each surviving output column came from, in order -- so the
    caller can still copy each surviving column's original cell styling
    (see _copy_cell_style()) even after this stripping (2026-08-03, per
    Ravindra's "significantly different from original" report)."""
    header_lower = [str(h).strip().lower() if h is not None else "" for h in header]
    keep_indices = [i for i, h in enumerate(header_lower) if h not in GENERATED_COLUMN_NAMES]
    if len(keep_indices) == len(header):
        return header, data_rows, keep_indices  # nothing generated here before -- unchanged
    new_header = [header[i] for i in keep_indices]
    new_rows = []
    for row in data_rows:
        if row is None:
            new_rows.append(row)
        else:
            new_rows.append(tuple(row[i] if i < len(row) else None for i in keep_indices))
    return new_header, new_rows, keep_indices


def build_rules(records: List[dict]) -> List[dict]:
    """Normalizes raw Zoho "Categorization" module records into the plain
    rule shape this module works with, and returns them SORTED by cf_priority
    (ascending -- ties keep original module-record order, see PRIORITY in the
    module docstring). This is the order classify_rule() checks rules in, for
    BOTH the literal pass and the pattern pass -- if two categories could both
    match the same narration by literal keyword (or both by pattern), the one
    with the lower cf_priority wins; among equal (or all-default) priorities,
    the one earlier in the module's record order wins, same as before this
    feature existed. Order/priority does NOT matter for keyword-vs-pattern
    conflicts (e.g. a VAT line that also happens to contain an ER-style
    reference number) -- see classify_rule()'s docstring: literal keywords
    are always checked, across every rule, before any pattern is consulted,
    regardless of priority or record order. A rule with no keywords yet is
    still kept -- it just can't match anything until keywords are filled in,
    same tolerant behavior as categorize_statement.py's load_categories()."""
    rules = []
    for rec in records:
        category = str(rec.get("cf_category") or "").strip()
        if not category:
            continue  # malformed/blank row -- skip rather than fail the whole batch
        keywords_raw = rec.get("cf_key_words") or ""
        keywords, patterns = _parse_keywords(keywords_raw, category)

        # cf_account_to_be_posted (2026-08-06, per Ravindra; fallback removed
        # 2026-08-07 per Ravindra: "i dont need the fall back just use the
        # new field") -- the SOLE source for "the other side"'s account
        # (whichever of Debit Account/Credit Account isn't this statement's
        # own bank account). See _parse_account_to_be_posted()'s docstring
        # for the exact "<id> - <name>" format this parses. The old
        # cf_from_account/cf_to_account two-field lookup is no longer read
        # or used at all -- a record with cf_account_to_be_posted blank
        # simply has no module_account (same as before: that side of the
        # entry is left blank rather than guessed).
        _raw_account_to_be_posted = rec.get("cf_account_to_be_posted")
        module_account = _parse_account_to_be_posted(_raw_account_to_be_posted)
        print(f"[categorize_from_module] build_rules: category={category!r} -- RAW cf_account_to_be_posted="
              f"{_raw_account_to_be_posted!r} -> module_account={module_account!r} (this is the exact value "
              f"that will be written into this rule's row on the hidden _Rules sheet, and land in Debit "
              f"Account/Credit Account for any row on 'the other side' from this account -- see "
              f"_write_rules_lookup_sheet()/_account_formula())", flush=True)
        type_of_posting = str(rec.get("cf_type_of_transaction") or "").strip() or None
        # 2026-08-02 fix: Ravindra's real Categorization module names this
        # field `cf_rule` (matching the "RULE" column he actually built in
        # Zoho), not `cf_priority` -- reading only `cf_priority` meant this
        # lookup silently returned None for every record, every rule fell
        # back to DEFAULT_PRIORITY, and the whole feature quietly stopped
        # doing anything (confirmed live: a real /categorization-rules dump
        # showed every record's actual priority sitting under `cf_rule`,
        # values "1".."27", with no `cf_priority` key anywhere in the
        # response). Checks `cf_priority` first (in case a future customer's
        # module genuinely uses that name) and falls back to `cf_rule` --
        # whichever is present and non-blank wins.
        priority_raw = rec.get("cf_priority")
        if priority_raw is None or str(priority_raw).strip() == "":
            priority_raw = rec.get("cf_rule")
        priority = _parse_priority(priority_raw, category)
        rules.append({
            "category": category,
            "keywords": keywords,
            "patterns": patterns,
            "module_account": module_account,
            "type_of_posting": type_of_posting,
            "priority": priority,
        })
    # Stable sort -- rules with equal (including default) priority keep their
    # original module-record relative order, so this is a no-op on behavior
    # when no record sets cf_priority anywhere.
    rules.sort(key=lambda r: r["priority"])
    return rules


def classify_rule(narration, rules: List[dict]) -> dict:
    """First-match-wins, but in TWO passes rather than one (2026-07-24 update):
    every rule's LITERAL keywords are checked first, across the whole rule
    list, before any rule's PATTERN is consulted at all. This is deliberate --
    a literal keyword like "VAT" or "bank charge" is a much more specific
    signal than a broad shape-based pattern like /ER\\d{2,}/, and a single
    narration can legitimately contain both (a VAT or Bank Charges line can
    still reference an ER claim number in its free text). Without this
    two-pass split, whichever category happened to be checked first in the
    module's record order would win by accident; with it, a specific literal
    match always beats a generic pattern match regardless of record order, so
    a "VAT ... ER3700 ..." narration lands on VAT/Bank Charges rather than
    Expense reimbursement even though ER3700 also matches the ER pattern.

    WITHIN each pass, `rules` is expected already sorted by cf_priority
    (build_rules() does this -- see PRIORITY in the module docstring), so
    "first match wins" here really means "lowest-priority match wins" -- e.g.
    a narration that literal-matches both "Bank Charges" (cf_priority=1) and
    "Loan taken" (cf_priority=10, or unset) resolves to Bank Charges, because
    it's checked first in this loop, regardless of which record Zoho happened
    to create first."""
    text = str(narration) if narration is not None else ""
    text_lower = text.lower()
    for rule in rules:
        if any(kw in text_lower for kw in rule["keywords"]):
            return rule
    for rule in rules:
        if any(p.search(text) for p in rule["patterns"]):
            return rule
    return OTHERS_RULE


def _add_list_dropdown(out_wb, out_ws, sheet_name, values, col_number,
                        num_metadata_rows, num_data_rows, error_title):
    """Shared helper: writes `values` to a hidden sheet and attaches an Excel
    data-validation dropdown over the given column's data-row range. A
    cell-range reference (not an inline comma-separated formula) is used
    deliberately -- Excel's inline-list data validation is capped at 255
    characters, which a few dozen names can exceed."""
    if num_data_rows == 0 or not values:
        return
    list_ws = out_wb.create_sheet(sheet_name)
    list_ws.sheet_state = "hidden"
    for i, name in enumerate(values, start=1):
        list_ws.cell(row=i, column=1, value=name)

    dv = DataValidation(
        type="list",
        formula1=f"{sheet_name}!$A$1:$A${len(values)}",
        allow_blank=True,
    )
    dv.error = "Choose a value from the dropdown list."
    dv.errorTitle = error_title
    out_ws.add_data_validation(dv)

    col_letter = get_column_letter(col_number)
    header_row_excel = num_metadata_rows + 1
    first_data_row = header_row_excel + 1
    last_data_row = header_row_excel + num_data_rows
    dv.add(f"{col_letter}{first_data_row}:{col_letter}{last_data_row}")


# Fixed cell on the hidden "_Rules" sheet holding this statement's own
# resolved account_id -- referenced by every row's Debit Account/Credit
# Account formula (see _account_formula()) as the "not this rule's side"
# constant, instead of baking that value into every single formula string.
RULES_SHEET_NAME = "_Rules"
RULES_SHEET_ACCOUNT_NUMBER_CELL = "G1"


def _write_rules_lookup_sheet(out_wb, rules: List[dict], account_number: str):
    """Writes a hidden lookup sheet Excel formulas reference so Debit
    Account/Credit Account/Posting Type recompute live when a row's Category
    is changed by hand -- see the module docstring's "LIVE-RECALCULATING"
    section. Columns: Category, PostingType, ModuleValue. ModuleValue is
    r["module_account"] -- the account_id parsed out of that rule's
    cf_account_to_be_posted (see build_rules()/_parse_account_to_be_posted())
    -- precomputed there so this function (and _resolve_row_accounts(), the
    posting-side equivalent) don't need to duplicate that parsing. Also
    includes an explicit FALLBACK_CATEGORY ("Others") row with everything
    blank, so re-categorizing a row TO Others resolves cleanly via VLOOKUP
    instead of relying on IFERROR to catch a #N/A -- keeps this sheet a
    complete, self-documenting picture of every value the Category dropdown
    can hold. account_number (this statement's own resolved account_id) is
    written to a single fixed cell (RULES_SHEET_ACCOUNT_NUMBER_CELL) that
    every row's formula points at, rather than repeating the literal string
    in every formula."""
    rules_ws = out_wb.create_sheet(RULES_SHEET_NAME)
    rules_ws.sheet_state = "hidden"
    rules_ws.append(["Category", "PostingType", "ModuleValue"])
    for r in rules:
        module_value = r["module_account"] or ""
        rules_ws.append([r["category"], r["type_of_posting"] or "", module_value])
    rules_ws.append([FALLBACK_CATEGORY, "", ""])
    rules_ws["F1"] = "AccountNumber(ThisStatement'sOwnAccount)"
    rules_ws[RULES_SHEET_ACCOUNT_NUMBER_CELL] = account_number
    # DEBUG (2026-08-02, per Ravindra; updated 2026-08-07 for the
    # cf_account_to_be_posted-only source, fallback removed): list any rule
    # whose category could match a row here but has no module_account at all
    # (cf_account_to_be_posted blank/not set) -- for a row on "the other
    # side" from this account (the ModuleValue lookup branch), that means
    # Debit/Credit Account legitimately has nothing to show for that
    # specific side, independent of account_number entirely (a data gap on
    # that Categorization record, same class of gap already found live for
    # "Loan taken" on 2026-07-26 -- not necessarily this account's issue at
    # all).
    blank_account_categories = [r["category"] for r in rules if not r["module_account"]]
    if blank_account_categories:
        print(f"[categorize_from_module] _write_rules_lookup_sheet: category rule(s) with no module_account "
              f"(cf_account_to_be_posted blank/not set) -- their ModuleValue side will show blank no matter "
              f"what: {blank_account_categories}", flush=True)
    return rules_ws


def _account_formula(category_cell: str, this_side_amount_cell: str, other_side_amount_cell: str) -> str:
    """Builds the Excel formula for ONE of Debit Account/Credit Account,
    reproducing the exact "whichever side has an amount gets account_number,
    the other gets the category's module value, both blank if the row has
    both/neither" logic from categorize_workbook()'s original Python (see
    the module docstring) -- just recomputed live from the row's OWN
    Category/Debit/Credit cells instead of baked in once at generation time.
    this_side_amount_cell is the statement's own amount column matching THIS
    output column (e.g. the statement's Debit column when building the
    Debit Account formula); other_side_amount_cell is the other one.
    "Has an amount" is approximated as non-blank and non-zero -- a close
    match to _has_amount() but not 100% identical (a non-numeric-but-present
    statement cell is treated by _has_amount() as "has a value" too; that
    edge case isn't reproduced in-formula since Excel's <>0 comparison on a
    text value is already true, so it doesn't actually diverge in practice)."""
    # Mirrors the original Python exactly: THIS side having the amount (and
    # the other not) means THIS output column gets the module value (e.g. for
    # Debit Account: statement's Debit has an amount, Credit doesn't -> Debit
    # Account = module_value); the OTHER side having the amount means THIS
    # output column gets account_number instead (this account is the OTHER
    # side of the entry). Both-or-neither -> blank, same as before.
    this_only = f'AND({this_side_amount_cell}<>"",{this_side_amount_cell}<>0,OR({other_side_amount_cell}="",{other_side_amount_cell}=0))'
    other_only = f'AND({other_side_amount_cell}<>"",{other_side_amount_cell}<>0,OR({this_side_amount_cell}="",{this_side_amount_cell}=0))'
    # NOTE (2026-08-02, found while testing the recalculation fix below): a
    # plain IFERROR(VLOOKUP(...),"") is NOT enough to get a blank result when
    # the looked-up ModuleValue cell is itself blank (e.g. the "Others" row,
    # or any real category whose rule has cf_account_to_be_posted empty -- a
    # real gap Ravindra already hit live, e.g. "Loan taken" had neither
    # from/to account set as of the July 26 update) -- VLOOKUP succeeds (no
    # error, so IFERROR doesn't intercept it) but returns the numeric
    # coercion of a blank cell, which both Excel and LibreOffice show as 0,
    # NOT "". Wrapping in an extra IF(...=0,"",...) turns that literal "0"
    # into a proper blank -- confirmed against a real LibreOffice
    # recalculation, not just reasoned about (see the two-cell test this fix
    # was verified with). Safe to compare against 0 here since a real Zoho
    # account_id is never the literal string/number 0.
    #
    # _Rules sheet columns (2026-08-07, simplified to 3 -- Category,
    # PostingType, ModuleValue -- since FromAccount/ToAccount are no longer
    # read at all, see build_rules()): ModuleValue is column 3 ($A:$C).
    module_value_lookup = (
        f'IFERROR(IF(VLOOKUP({category_cell},{RULES_SHEET_NAME}!$A:$C,3,FALSE)=0,"",'
        f'VLOOKUP({category_cell},{RULES_SHEET_NAME}!$A:$C,3,FALSE)),"")'
    )
    account_number_ref = f"{RULES_SHEET_NAME}!${RULES_SHEET_ACCOUNT_NUMBER_CELL[0]}${RULES_SHEET_ACCOUNT_NUMBER_CELL[1:]}"
    return f'=IF({this_only},{module_value_lookup},IF({other_only},{account_number_ref},""))'


def _posting_type_formula(category_cell: str) -> str:
    """Posting Type is simpler than Debit/Credit Account -- it's always just
    the matched category's own posting type, regardless of which side of the
    transaction the row is on. Same blank-vs-0 guard as _account_formula()'s
    module_value_lookup, for a category whose cf_type_of_transaction is blank
    (2026-08-02). PostingType is column 2 of the (2026-08-07) 3-column
    _Rules sheet ($A:$B)."""
    return (
        f'=IFERROR(IF(VLOOKUP({category_cell},{RULES_SHEET_NAME}!$A:$B,2,FALSE)=0,"",'
        f'VLOOKUP({category_cell},{RULES_SHEET_NAME}!$A:$B,2,FALSE)),"")'
    )


def _copy_cell_style(src_cell, dst_cell):
    """Copies one source cell's full visual style (font, fill, border,
    alignment -- including wrap_text/vertical alignment -- and number_format)
    onto a freshly-written destination cell. openpyxl's cell.font/fill/
    border/alignment/protection objects are immutable and already shared
    across every cell using that same style, so a plain attribute copy here
    is exactly what openpyxl itself does internally -- no deep copy needed.

    2026-08-03 (per Ravindra, on his real CBQ QAR 3001 statement): "the new
    file created is significantly different from original as far as the
    header is concerned, not acceptable when recreating." categorize_
    workbook() rebuilds its output as a brand-new workbook, and before this
    fix copied every metadata/header/data row via out_ws.append(list(row))
    -- VALUES ONLY. His statement's multi-line "Label : Value" metadata rows
    (IBAN/Account Number, Account Type, Name, ...) rely on wrap_text plus a
    row height sized for several wrapped lines to display in full; once that
    formatting was dropped, the SAME text landed in a default-height,
    non-wrapping cell, so only whatever fragment fit the default single-line
    row height stayed visible (matches his screenshot exactly: every long
    metadata line showed only its tail, while the one genuinely short line,
    "To Date : 30/06/2026", displayed in full because it never needed
    wrapping to begin with). This function, plus the row-height/column-width/
    merged-cell/image copying alongside it, is the fix -- see
    categorize_workbook()'s use of it below."""
    if src_cell.has_style:
        # openpyxl hands back font/fill/border/alignment/protection as a
        # StyleProxy (an immutable read-only wrapper), not the real style
        # object -- assigning the proxy straight across raises
        # "unhashable type: 'StyleProxy'" (confirmed while testing this fix
        # against a synthetic file). copy() unwraps it into a real, mutable
        # style object first, same as openpyxl's own docs recommend for
        # "copy a style from one cell to another".
        dst_cell.font = _copy_style(src_cell.font)
        dst_cell.fill = _copy_style(src_cell.fill)
        dst_cell.border = _copy_style(src_cell.border)
        dst_cell.alignment = _copy_style(src_cell.alignment)
        dst_cell.number_format = src_cell.number_format
        dst_cell.protection = _copy_style(src_cell.protection)


def _copy_metadata_merges_and_images(ws, out_ws, keep_indices, num_metadata_rows):
    """Best-effort copy of merged-cell ranges and embedded images (e.g. a
    bank's letterhead logo) that live in the METADATA block (the rows above
    the real transaction header) from the original sheet onto the freshly-
    built output sheet.

    Deliberately scoped to ONLY the metadata block, not the transaction
    table: metadata rows are always copied 1:1 (never dropped), so their row
    numbers are identical between `ws` and `out_ws` and a merge/image
    position there maps straight across. DATA rows, by contrast, can be
    individually skipped if blank (see categorize_workbook()'s main loop),
    which would silently misplace anything anchored there -- so this
    intentionally does not touch that region at all.

    Every item is wrapped in its own try/except: losing one cosmetic merge or
    image is far better than failing the whole categorization run over a
    display-only detail. 2026-08-03, per Ravindra -- see _copy_cell_style()'s
    docstring for the full report this is fixing."""
    col_map = {orig: new for new, orig in enumerate(keep_indices, start=1)}

    for merge_range in list(ws.merged_cells.ranges):
        try:
            if merge_range.max_row > num_metadata_rows:
                continue  # not in the metadata block -- see docstring
            if merge_range.min_col - 1 not in col_map or merge_range.max_col - 1 not in col_map:
                continue  # one side of this merge was stripped -- skip rather than guess
            out_ws.merge_cells(start_row=merge_range.min_row, start_column=col_map[merge_range.min_col - 1],
                                end_row=merge_range.max_row, end_column=col_map[merge_range.max_col - 1])
        except Exception as e:
            print(f"[categorize_from_module] _copy_metadata_merges_and_images: couldn't copy merge "
                  f"range {merge_range!r} -- skipping it (cosmetic only): {e}", flush=True)

    for img in list(getattr(ws, "_images", [])):
        try:
            anchor_row = img.anchor._from.row if hasattr(img.anchor, "_from") else None
            if anchor_row is None or anchor_row >= num_metadata_rows:
                continue  # not in the metadata block (or anchor shape unrecognized) -- see docstring
            out_ws.add_image(img, img.anchor)
        except Exception as e:
            print(f"[categorize_from_module] _copy_metadata_merges_and_images: couldn't copy an "
                  f"embedded image -- skipping it (cosmetic only): {e}", flush=True)


def categorize_workbook(wb, rules: List[dict], account_number: str = "") -> Tuple["openpyxl.Workbook", dict]:
    """Runs the categorization pass over the first sheet of `wb` (an already-
    opened openpyxl Workbook), using `rules` (from build_rules()).
    account_number is the current statement's own bank account's real Zoho
    account_id (NOT its bank account number -- see the IMPORTANT note in the
    module docstring) -- used to fill in whichever of Debit Account/Credit
    Account corresponds to "this account" for each row (see module
    docstring). Pass "" if unknown; that side is then left blank instead of
    guessed. Returns (new_workbook, stats) where
    stats = {"total": int, "others": int, "by_category": {category: count, ...}}."""
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Sheet is empty.")

    header_row_idx = find_header_row(rows)
    if header_row_idx is None:
        # 2026-08-03 (per Ravindra -- CBQ QAR 3001 statement, a converted
        # .xls, hit a version of this error): print AND include the actual
        # first few rows this code saw (repr'd, so stray whitespace/hidden
        # characters are visible) and every sheet name in this workbook --
        # ws = wb[wb.sheetnames[0]] only ever reads the FIRST sheet, so if a
        # bank's export puts the real transaction table on a later sheet
        # (e.g. a "Summary" sheet first), this is exactly what would surface
        # that. Folded into the raised message itself, not just printed, so
        # it's visible directly in the widget's error banner (now shows full,
        # untruncated text -- 2026-08-03) without a separate log lookup.
        preview = [tuple(r) for r in rows[:HEADER_SCAN_LIMIT]]
        print(f"[categorize_from_module] categorize_workbook: no header row found. Sheets in this workbook: "
              f"{wb.sheetnames!r}. First {len(preview)} row(s) read from {wb.sheetnames[0]!r}: {preview!r}",
              flush=True)
        raise ValueError(
            f"Couldn't find a header row (one containing a Narration-like column) in the first "
            f"{HEADER_SCAN_LIMIT} rows of sheet {wb.sheetnames[0]!r} (this workbook's sheets: "
            f"{wb.sheetnames!r}). Expected a column named one of {sorted(NARRATION_HEADER_ALIASES)}. "
            f"Rows actually read: {preview!r}"
        )
    metadata_rows = rows[:header_row_idx]
    header = list(rows[header_row_idx])
    data_rows = rows[header_row_idx + 1:]

    # Strip any Category/Debit Account/Credit Account/Posting Type columns a
    # PREVIOUS run already added -- otherwise re-running on the same file
    # piles up a second set instead of replacing them.
    header, data_rows, keep_indices = _strip_previously_generated_columns(header, data_rows)

    header_lower = [str(h).strip().lower() if h is not None else "" for h in header]
    nar_idx = find_column(header_lower, NARRATION_HEADER_ALIASES)
    debit_idx = find_column(header_lower, DEBIT_HEADER_ALIASES)
    credit_idx = find_column(header_lower, CREDIT_HEADER_ALIASES)
    if debit_idx is None or credit_idx is None:
        # 2026-08-03 (per Ravindra -- see the header-row-not-found branch
        # above for the same reasoning): fold the ACTUAL header row this
        # code read (repr'd) into both the log and the raised message
        # itself. This matters here specifically because Ravindra's own
        # screenshot of this exact file showed headers literally named
        # "Debit Amount"/"Credit Amount" -- which SHOULD match -- so seeing
        # exactly what this code parsed (not what Excel displays) is the
        # one thing that can explain the mismatch (e.g. a hidden/non-
        # breaking character, a merged cell leaving neighboring cells
        # blank, or the header row landing on the wrong sheet/row).
        print(f"[categorize_from_module] categorize_workbook: Debit/Credit column not found. Sheets in this "
              f"workbook: {wb.sheetnames!r}. Header row used (row index {header_row_idx} of sheet "
              f"{wb.sheetnames[0]!r}): {header!r}", flush=True)
        raise ValueError(
            "Couldn't find this statement's own Debit/Credit columns -- needed to know which side "
            "of each transaction this account is on. Expected a column named one of "
            f"{sorted(DEBIT_HEADER_ALIASES)} and one of {sorted(CREDIT_HEADER_ALIASES)}. "
            f"Header row actually read (row {header_row_idx + 1} of sheet {wb.sheetnames[0]!r}): {header!r}"
        )
    # DEBUG (2026-08-02, per Ravindra -- debit/credit account not populating
    # for one specific account/file while others are fine): print exactly
    # which column this file's Debit/Credit detection actually landed on.
    # Debit Account/Credit Account can ONLY come out non-blank for a row if
    # (a) this statement's own Debit/Credit columns were found AND the row
    # has a real amount in one of them, AND/OR (b) account_number (below) is
    # non-blank -- so seeing the wrong header/column here (e.g. this bank's
    # real debit/credit columns are spelled differently than every other
    # bank's and DEBIT_HEADER_ALIASES/CREDIT_HEADER_ALIASES picked an
    # unrelated always-blank column instead) is one of the two things that
    # would explain this exact symptom, independent of account_number.
    print(f"[categorize_from_module] categorize_workbook: account_number(resolved account_id)={account_number!r}; "
          f"Debit column detected: {header[debit_idx]!r} (col {get_column_letter(debit_idx + 1)}); "
          f"Credit column detected: {header[credit_idx]!r} (col {get_column_letter(credit_idx + 1)})",
          flush=True)

    out_wb = openpyxl.Workbook()
    out_ws = out_wb.active
    out_ws.title = ws.title

    # Column widths for every ORIGINAL column that survives stripping
    # (2026-08-03, per Ravindra -- see _copy_cell_style()'s docstring).
    for new_col_idx, orig_col_idx in enumerate(keep_indices, start=1):
        orig_letter = get_column_letter(orig_col_idx + 1)
        if orig_letter in ws.column_dimensions and ws.column_dimensions[orig_letter].width:
            out_ws.column_dimensions[get_column_letter(new_col_idx)].width = \
                ws.column_dimensions[orig_letter].width

    def _copy_row(src_row_num, dst_row_num, row_values):
        """Writes one row's plain values into out_ws AND, for every column
        that survived _strip_previously_generated_columns, copies that
        column's ORIGINAL cell style across too (see _copy_cell_style()).
        Extra columns this code appends itself (Category, Debit Account,
        ...) have no `orig_col_idx` to copy from and are left with
        openpyxl's default style, same as before this fix. Also copies the
        source row's own height, if one was explicitly set."""
        for new_col_idx, value in enumerate(row_values, start=1):
            cell = out_ws.cell(row=dst_row_num, column=new_col_idx, value=value)
            orig_col_idx = keep_indices[new_col_idx - 1] if new_col_idx - 1 < len(keep_indices) else None
            if orig_col_idx is not None:
                _copy_cell_style(ws.cell(row=src_row_num, column=orig_col_idx + 1), cell)
        src_height = ws.row_dimensions[src_row_num].height if src_row_num in ws.row_dimensions else None
        if src_height:
            out_ws.row_dimensions[dst_row_num].height = src_height

    for i, row in enumerate(metadata_rows):
        _copy_row(i + 1, i + 1, list(row))

    new_header = list(header) + ["Category", "Debit Account", "Credit Account", "Posting Type"]
    _copy_row(header_row_idx + 1, len(metadata_rows) + 1, new_header)

    # Merged cells + any embedded image (e.g. a bank's letterhead logo) living
    # in the metadata block -- see _copy_metadata_merges_and_images()'s
    # docstring. 2026-08-03, per Ravindra's "significantly different from
    # original" report.
    _copy_metadata_merges_and_images(ws, out_ws, keep_indices, len(metadata_rows))

    # Column numbers (1-based) of the 4 appended columns, and the statement's
    # OWN Debit/Credit columns -- needed as Excel column letters so each
    # row's Debit Account/Credit Account/Posting Type formula can reference
    # its own row's Category/Debit/Credit cells (see _account_formula()/
    # _posting_type_formula()).
    category_col = len(header) + 1
    posting_col = len(header) + 4
    category_col_letter = get_column_letter(category_col)
    stmt_debit_col_letter = get_column_letter(debit_idx + 1)
    stmt_credit_col_letter = get_column_letter(credit_idx + 1)

    # The hidden lookup sheet every row's formula below points at -- written
    # BEFORE the data rows since the formulas reference it by name, though
    # sheet creation order doesn't actually matter to Excel; done here just
    # to keep the sheet's own reasoning (account_number, rules) close to
    # where it's first used.
    _write_rules_lookup_sheet(out_wb, rules, account_number)

    stats = {"total": 0, "others": 0, "by_category": {}}
    total_data_rows = 0
    current_row_num = len(metadata_rows) + 1  # header row; incremented before first data row below
    # DEBUG (2026-08-02, per Ravindra): tally, across every row, which of the
    # 4 "has an amount" combinations the detected Debit/Credit columns
    # actually produced -- printed once as a summary after the loop below.
    # This is the other half of the diagnosis alongside the account_number
    # print above: if debit_only + credit_only is 0 (or near-0) for a file
    # where other files show healthy counts, the Debit/Credit column
    # detection above landed on the wrong columns for THIS file's layout --
    # every row would fall to "neither/both" and both output columns go
    # blank regardless of account_number or the matched rule's own accounts.
    _debug_amount_tally = {"debit_only": 0, "credit_only": 0, "both": 0, "neither": 0}
    for src_offset, row in enumerate(data_rows):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        # Row number in the ORIGINAL sheet this data row came from -- distinct
        # from current_row_num (the DESTINATION row number) as soon as any
        # earlier blank row gets skipped/compacted out above. Needed so
        # _copy_row() below can still find the right original cell to copy
        # style from for a row that's landed at a different row number in
        # out_ws than it started at (2026-08-03, per Ravindra).
        src_row_num = header_row_idx + 2 + src_offset
        total_data_rows += 1
        current_row_num += 1
        matched = classify_rule(row[nar_idx], rules)

        _d_has = _has_amount(row[debit_idx] if debit_idx < len(row) else None)
        _c_has = _has_amount(row[credit_idx] if credit_idx < len(row) else None)
        if _d_has and not _c_has:
            _debug_amount_tally["debit_only"] += 1
        elif _c_has and not _d_has:
            _debug_amount_tally["credit_only"] += 1
        elif _d_has and _c_has:
            _debug_amount_tally["both"] += 1
        else:
            _debug_amount_tally["neither"] += 1

        # Debit Account/Credit Account/Posting Type are written as FORMULAS
        # (see module docstring's "LIVE-RECALCULATING" section), not the
        # values matched["..."] would otherwise give -- those values are
        # still used below for `stats`, which reflects this run's initial
        # classification for the widget's summary, independent of whatever
        # a human edits in Excel afterward.
        category_cell = f"{category_col_letter}{current_row_num}"
        stmt_debit_cell = f"{stmt_debit_col_letter}{current_row_num}"
        stmt_credit_cell = f"{stmt_credit_col_letter}{current_row_num}"

        # DEBUG (2026-08-03, per Ravindra -- debugging Debit/Credit Account
        # not populating for certain statements again): right before this
        # row's Debit Account/Credit Account cells get written, log exactly
        # what SHOULD end up in them. _resolve_row_accounts() (below,
        # originally written for the POSTING feature) reproduces
        # _account_formula()'s Excel-formula logic in plain Python -- calling
        # it here purely to log an expected value doesn't change what's
        # actually written (still the live formulas via _account_formula()/
        # _posting_type_formula() just below); it just gives a concrete
        # "this row should show X" line to compare against what the
        # delivered file actually displays after recalculation.
        _expected_debit_acct, _expected_credit_acct = _resolve_row_accounts(
            matched,
            row[debit_idx] if debit_idx < len(row) else None,
            row[credit_idx] if credit_idx < len(row) else None,
            account_number,
        )
        print(f"[categorize_from_module] categorize_workbook: row {current_row_num} "
              f"(narration={row[nar_idx] if nar_idx < len(row) else None!r}) -- matched category="
              f"{matched['category']!r}, rule module_account={matched.get('module_account')!r} (from "
              f"cf_account_to_be_posted), account_number={account_number!r}, debit_has={_d_has}, "
              f"credit_has={_c_has} -- about to write this row's Debit Account/Credit Account FORMULAS; they "
              f"should resolve (once recalculated) to Debit Account={_expected_debit_acct!r}, "
              f"Credit Account={_expected_credit_acct!r}", flush=True)

        row_out = list(row) + [
            matched["category"],
            _account_formula(category_cell, stmt_debit_cell, stmt_credit_cell),
            _account_formula(category_cell, stmt_credit_cell, stmt_debit_cell),
            _posting_type_formula(category_cell),
        ]
        _copy_row(src_row_num, current_row_num, row_out)

        stats["total"] += 1
        stats["by_category"][matched["category"]] = stats["by_category"].get(matched["category"], 0) + 1
        if matched["category"] == FALLBACK_CATEGORY:
            stats["others"] += 1

    print(f"[categorize_from_module] categorize_workbook: {total_data_rows} data row(s) -- amount-side tally: "
          f"{_debug_amount_tally}. (debit_only rows get Debit Account=rule's account, Credit "
          f"Account=account_number; credit_only rows get the reverse; 'both'/'neither' rows get BOTH columns "
          f"blank regardless of account_number or the rule's own accounts -- a high 'both'/'neither' count "
          f"here on a file where other files show mostly debit_only/credit_only means the Debit/Credit column "
          f"detection above picked the wrong column(s) for this specific file's layout.)", flush=True)

    category_values = [r["category"] for r in rules] + [FALLBACK_CATEGORY]
    _add_list_dropdown(out_wb, out_ws, "_CategoryList", category_values, category_col,
                        len(metadata_rows), total_data_rows, "Not a recognized category")

    # Distinct posting types, in first-seen order, blanks skipped.
    seen = set()
    posting_values = []
    for r in rules:
        pt = r["type_of_posting"]
        if pt and pt not in seen:
            seen.add(pt)
            posting_values.append(pt)
    _add_list_dropdown(out_wb, out_ws, "_PostingTypeList", posting_values, posting_col,
                        len(metadata_rows), total_data_rows, "Not a recognized posting type")

    return out_wb, stats


"""
POSTING (2026-07-26 update, per Ravindra): everything below reads an
ALREADY-CATEGORIZED workbook and figures out exactly what should be posted to
Zoho Books for each eligible row -- WITHOUT making any Zoho API call itself.
This module stays Zoho-connection-free by design (see the top-of-file
docstring); the actual posting calls live in posting_service.py, using
zoho_books_client.py. main.py's /post-transactions endpoint wires the two
together: extract_postable_rows() here -> posting_service.post_rows() ->
write_posting_results() here.

Why Debit Account/Credit Account/Posting Type are RECOMPUTED here rather than
read from their cells: those three columns are live Excel FORMULAS (see the
LIVE-RECALCULATING section above). A formula cell's value is only as fresh as
the last time a real spreadsheet engine (Excel, Excel Online, LibreOffice,
Google Sheets) recalculated and saved the file -- openpyxl itself never
evaluates a formula it reads, so a freshly-generated file (or one read back
without ever passing through one of those engines) has a BLANK cached value,
confirmed 2026-07-26 against a real delivered file. Recomputing from the
CURRENT Category cell (always a plain value, whether auto-selected or
manually overridden) plus the same `rules` list used to build the _Rules
sheet sidesteps that entirely, and always agrees with what the formula WOULD
show once recalculated -- see _resolve_row_accounts()'s docstring for the
exact logic, which deliberately mirrors _account_formula() line for line.
"""


def _resolve_row_accounts(rule: Optional[dict], stmt_debit_val, stmt_credit_val,
                           account_number: str) -> Tuple[Optional[str], Optional[str]]:
    """Computes (debit_account, credit_account) for ONE row, in Python,
    reproducing _account_formula()'s Excel-formula logic exactly (see that
    function and the POSTING section's docstring above for why this is
    computed here rather than read from the sheet). Mirrors the two
    _account_formula() calls categorize_workbook() makes per row: whichever
    statement side (debit/credit) has a non-zero amount on THIS row gets the
    matched rule's module_account value (the account_id parsed from
    cf_account_to_be_posted -- see build_rules()); the OTHER side gets
    account_number (this statement's own resolved account_id). Both are
    None if the row has both or neither amount, or account_number wasn't
    resolved -- never guessed."""
    module_value = rule.get("module_account") if rule else None
    debit_has = _has_amount(stmt_debit_val)
    credit_has = _has_amount(stmt_credit_val)
    account_number = account_number or None
    if debit_has and not credit_has:
        return (module_value, account_number)
    elif credit_has and not debit_has:
        return (account_number, module_value)
    else:
        return (None, None)


def _is_success_reference(value) -> bool:
    """A row is treated as already-posted (skipped on re-run) ONLY if its
    Zoho Posting Reference cell holds a genuine success reference (e.g.
    "Journal 460000000012345", "Expense 460000000067890") -- NOT a
    "NOT POSTED: ..." or "ERROR: ..." note, both of which posting_service.py
    always writes with that exact prefix (see its post_row()/post_rows()).
    This is deliberate: a row that failed to post (a missing account, an
    unresolved vendor, a real Zoho API error) must stay eligible to retry on
    the NEXT posting run, once whatever was wrong gets fixed -- treating any
    non-blank cell as "done" would permanently skip it instead."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return False
    upper = text.upper()
    return not upper.startswith("NOT POSTED") and not upper.startswith("ERROR")


def extract_postable_rows(wb, rules: List[dict], account_number: str = "") -> dict:
    """Reads an already-categorized workbook and returns every row that's
    ready to post, plus enough bookkeeping for write_posting_results() to
    write the outcome back afterward.

    IMPORTANT -- `wb` must be loaded WITHOUT data_only=True (openpyxl's
    default). This is the OPPOSITE of how main.py loads it for /categorize.
    Loading with data_only=True would read the Debit Account/Credit
    Account/Posting Type cells' CACHED value (often blank -- see this
    section's docstring) instead of their formula, and if that same workbook
    object is later saved again (which write_posting_results() does, to add
    the Zoho Posting Reference column), the live formulas would be
    permanently replaced by whatever blank/stale value was cached --
    silently breaking the auto-recalculate-on-Category-change feature for
    this file. Loading without data_only leaves every formula cell's formula
    string completely intact; we never need to read those cells' computed
    value here anyway; Category/Debit/Credit/Narration/Date/Reference are
    all plain (non-formula) cells regardless of how the workbook is loaded.

    A row is POSTABLE only if: its Category is set and isn't "Others", AND
    (if a Zoho Posting Reference column already exists) that column is still
    blank for this row -- an already-posted row is silently excluded here
    entirely (counted in "already_posted" instead), so posting_service.py
    never even sees it and there's no way for it to accidentally re-post.

    Returns {"rows": [...], "header_row": int (0-based), "posted_col":
    int|None (0-based, None if the column doesn't exist yet), "header": list,
    "already_posted": int, "no_category": int}. Each entry in "rows" is a
    dict: excel_row (1-based sheet row, for writing the result back),
    category, posting_type (from the matched RULE's cf_type_of_transaction,
    NOT the sheet's Posting Type formula cell), debit_account, credit_account
    (from _resolve_row_accounts()), account_number (passed through, for
    telling paid-through apart from expense account in posting_service.py),
    amount (whichever of the statement's own Debit/Credit is non-zero),
    narration, reference_number, txn_date."""
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Sheet is empty.")

    header_row_idx = find_header_row(rows)
    if header_row_idx is None:
        raise ValueError(
            f"Couldn't find a header row (one containing a Narration-like column) in the first "
            f"{HEADER_SCAN_LIMIT} rows."
        )
    header = list(rows[header_row_idx])
    header_lower = [str(h).strip().lower() if h is not None else "" for h in header]

    nar_idx = find_column(header_lower, NARRATION_HEADER_ALIASES)
    debit_idx = find_column(header_lower, DEBIT_HEADER_ALIASES)
    credit_idx = find_column(header_lower, CREDIT_HEADER_ALIASES)
    category_idx = find_column(header_lower, {"category"})
    date_idx = find_column(header_lower, DATE_HEADER_ALIASES)
    reference_idx = find_column(header_lower, REFERENCE_HEADER_ALIASES)

    if category_idx is None:
        raise ValueError(
            "No 'Category' column found -- this file hasn't been through /categorize yet. "
            "Run categorization before posting."
        )
    if debit_idx is None or credit_idx is None:
        raise ValueError(
            "Couldn't find this statement's own Debit/Credit columns -- needed to determine each "
            "row's amount and which side of the entry it's on."
        )
    if date_idx is None:
        raise ValueError(
            f"Couldn't find a transaction date column -- Zoho Books requires one to post a Journal/"
            f"Expense/Vendor Payment. Expected a column named one of {sorted(DATE_HEADER_ALIASES)}."
        )

    posted_col_idx = header_lower.index(POSTED_COLUMN_NAME.lower()) if POSTED_COLUMN_NAME.lower() in header_lower else None

    rules_by_category = {r["category"]: r for r in rules}

    postable = []
    already_posted = 0
    no_category = 0
    header_row_excel = header_row_idx + 1  # 1-based Excel row number of the header itself
    for offset, row in enumerate(rows[header_row_idx + 1:]):
        excel_row = header_row_excel + 1 + offset
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue

        category = str(row[category_idx]).strip() if category_idx < len(row) and row[category_idx] is not None else ""
        if not category or category.lower() == FALLBACK_CATEGORY.lower():
            no_category += 1
            continue
        if posted_col_idx is not None and posted_col_idx < len(row) and _is_success_reference(row[posted_col_idx]):
            already_posted += 1
            continue

        rule = rules_by_category.get(category)
        stmt_debit_val = row[debit_idx] if debit_idx < len(row) else None
        stmt_credit_val = row[credit_idx] if credit_idx < len(row) else None
        debit_account, credit_account = _resolve_row_accounts(rule, stmt_debit_val, stmt_credit_val, account_number)
        amount = stmt_debit_val if _has_amount(stmt_debit_val) else stmt_credit_val

        postable.append({
            "excel_row": excel_row,
            "category": category,
            "posting_type": (rule.get("type_of_posting") if rule else None) or "",
            "debit_account": debit_account,
            "credit_account": credit_account,
            "account_number": account_number or None,
            "amount": amount,
            "narration": row[nar_idx] if nar_idx is not None and nar_idx < len(row) else None,
            "reference_number": (str(row[reference_idx]).strip()
                                  if reference_idx is not None and reference_idx < len(row) and row[reference_idx] is not None
                                  else None),
            "txn_date": row[date_idx] if date_idx < len(row) else None,
        })

    return {
        "rows": postable,
        "header_row": header_row_idx,
        "posted_col": posted_col_idx,
        "header": header,
        "already_posted": already_posted,
        "no_category": no_category,
    }


def write_posting_results(wb, extraction: dict, results: dict) -> int:
    """Writes each row's posting OUTCOME back into the SAME workbook object
    `extraction` was built from (extract_postable_rows()) -- adds the
    'Zoho Posting Reference' column if it doesn't already exist, then sets
    each posted row's cell to either the Zoho record reference (e.g.
    "Journal 460000000012345") or a "NOT POSTED: ..."/"ERROR: ..." note.
    Writes DIRECTLY into `wb`'s existing cells (ws.cell(...).value = ...)
    instead of rebuilding the workbook -- this is what leaves every other
    cell, including the live Debit Account/Credit Account/Posting Type
    FORMULAS, completely untouched. `results` is {excel_row: result_text,
    ...}, keyed by the same excel_row values extract_postable_rows() put on
    each row dict. Returns the 1-based Excel column number the Posted
    Reference column ended up in."""
    ws = wb[wb.sheetnames[0]]
    header_row_excel = extraction["header_row"] + 1
    posted_col = extraction["posted_col"]
    if posted_col is None:
        posted_col = len(extraction["header"])  # 0-based -- next free column
        ws.cell(row=header_row_excel, column=posted_col + 1, value=POSTED_COLUMN_NAME)
    posted_col_excel = posted_col + 1  # 1-based

    for excel_row, text in results.items():
        ws.cell(row=excel_row, column=posted_col_excel, value=text)

    return posted_col_excel