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
    cf_categorisation      -- str, the category label, e.g. "Bank Charges".
                               2026-08-11, per Ravindra: switched from the
                               old cf_category field to this one (now made
                               MANDATORY on the module) -- see build_rules()
                               below.
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
    cf_reference            -- OPTIONAL str, e.g. "Bank Charges" -- 2026-08-12
                               addition, for the PostingReference column (see
                               POSTING_REFERENCE_COLUMN_NAME's own comment
                               below for the full 3-tier lookup this feeds
                               into). Fine to leave blank -- a row whose
                               matched category has no cf_reference just
                               falls through to Gemini, then the statement's
                               own raw Reference column, same as if this
                               field didn't exist.

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

Adds FIVE columns to the output workbook (categorize_statement.py's CLI path
only adds one -- Category; this was FOUR before 2026-08-12's PostingReference
addition -- see POSTING_REFERENCE_COLUMN_NAME's own comment below):
    Category        -- dropdown of all categories + "Others", matched
                       category pre-selected (same data-validation mechanism
                       as categorize_statement.py's add_category_dropdown()).
    Debit Account    -- see below.
    Credit Account   -- see below.
    Posting Type     -- dropdown of every distinct posting type seen across
                        the rules, matched rule's type pre-selected.
    PostingReference -- a plain (non-formula) value, unlike the four above --
                        for a Vendor Payment row classified via a REGEX
                        pattern, the exact text that pattern matched in the
                        narration (supersedes everything else -- see
                        POSTING_REFERENCE_COLUMN_NAME's comment, tier 0);
                        otherwise the matched rule's cf_reference, or a
                        Gemini suggestion, or the statement's own raw
                        Reference column value, whichever is found first.
                        STICKY once set (see POSTING_REFERENCE_COLUMN_NAME's
                        comment) --
                        the one exception to this section's "LIVE-
                        RECALCULATING" behavior below, which only applies to
                        the first four.

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
# 2026-08-12, added for the PostingReference feature (see
# POSTING_REFERENCE_COLUMN_NAME's own comment below) -- categorize_workbook()
# now calls Gemini itself, at categorization time, reusing the EXACT SAME
# function (and its reasoning-leak defenses) posting_service.py's own AI
# Reference feature used earlier the same day. One-directional import only
# (posting_service.py imports nothing from this module) -- no circular
# import risk; main.py already imports both as siblings.
from posting_service import generate_ai_reference_preview

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

# PostingReference (2026-08-12, per Ravindra: "I want the creating of the
# reference to happen while doing the catergorization not while posting").
# This SUPERSEDES the same-day-earlier "AI Reference" feature that computed
# a Gemini suggestion live, per row, WHILE posting (see posting_service.py's
# "AI REFERENCE PREVIEW" module-level section header for that history) --
# Ravindra decided the reference should instead be settled once, up front,
# during categorize_workbook() below, and simply READ (not recomputed) at
# posting time. Same "config lives in a Zoho custom module, not hardcoded
# here" pattern as everywhere else in this file: for each row, the value
# comes from (checked in order, first non-blank wins):
#   VENDOR PAYMENT ROWS ARE A COMPLETE EXCEPTION TO ALL OF THE BELOW -- see
#   their own paragraph right after this list. Everything from #1 onward
#   only ever applies to a NON-Vendor-Payment row.
#   1. The matched Categorization rule's own cf_reference field (see
#      build_rules() below) -- a fixed reference text for that whole
#      category, e.g. every "Bank Charges" row always gets "Bank Charges".
#   2. Gemini, via generate_ai_reference_preview() (imported from
#      posting_service.py -- same function, same reasoning-leak defenses,
#      just called from HERE now instead of from posting_service.py's own
#      posting loop) -- only reached if #1 was blank/not set for this row's
#      category.
#   3. The bank statement's OWN raw Reference-style column (same
#      REFERENCE_HEADER_ALIASES detection extract_postable_rows() below has
#      always used for this) -- only reached if #1 and #2 both came up
#      empty (no gemini_client/ai_prompt_template available this run, or
#      Gemini itself failed/errored/was flagged as a reasoning leak).
#
# VENDOR PAYMENT (2026-08-12, LATER SAME DAY, first cut per Ravindra: "just
# for Vendor Payment category, supersedes everything" -- REVISED again the
# SAME DAY after Ravindra reported these rows weren't refreshing on
# re-categorize and traced it himself: "is it becoz postingreference column
# is untouched since it is already populated?" -- correct diagnosis; the
# first cut placed this INSIDE the sticky branch, so any row that already
# had SOME PostingReference value from before this tier existed just kept
# it forever, tier 0 never even ran. Ravindra's fix instruction: "for
# vendor payment category i dont want a reference col or gemini or any
# other check it should take just from the patteren match only"):
# a row whose Category base name (entity suffix stripped -- see
# _category_base()) matches VENDOR_PAYMENT_CATEGORY ("Vendor Payments" --
# PLURAL, live-confirmed against Ravindra's real Categorization module; see
# that constant's own comment for the singular/plural mismatch this fixed)
# is now handled COMPLETELY SEPARATELY from every tier above and from
# STICKY itself -- see the
# `_is_vendor_payment_category` branch in categorize_workbook() below,
# which runs BEFORE the sticky check even happens for these rows, not
# after it. For a Vendor Payment row: whatever text its category's own
# REGEX pattern (in cf_key_words -- not a literal keyword; see
# classify_rule()'s pattern_match_text return value) matches in the
# narration THIS RUN is written verbatim, EVERY single /categorize run,
# unconditionally overwriting whatever was there before (including a value
# a human typed in by hand). If no pattern matches this run (e.g. the row
# matched via a literal keyword instead, or the narration simply has
# nothing the pattern recognizes), PostingReference is left BLANK for that
# row -- no cf_reference, no Gemini, no raw statement column, and no
# STICKY reuse of an old value either; Ravindra was explicit that Vendor
# Payment gets none of those. Vendor Payment narrations are expected to
# carry only ONE such match (confirmed by Ravindra) -- the first match
# classify_rule() finds is used as-is, no multi-match joining.
# Placed right after Posting Type (the 5th generated column, per Ravindra's
# explicit confirmation) -- NOT forced to the very end like
# POSTED_COLUMN_NAME above, since PostingReference must exist BEFORE a row
# is ever posted, while POSTED_COLUMN_NAME is only ever written AFTER a real
# post.
#
# STICKY, per Ravindra's explicit choice ("compute once, then leave it
# alone" over "recompute every categorize run") -- unlike Category/Debit
# Account/Credit Account/Posting Type (which always reflect the CURRENT
# rules on every re-run), a NON-Vendor-Payment row that already has a
# non-blank PostingReference value keeps it untouched on every subsequent
# categorize_workbook() call, even if that row's Category is later changed
# by hand or the matched rule's cf_reference/the AI prompt changes. This
# trades a real correctness gap (a stale value after a hand-edit) for
# avoiding a repeated Gemini call -- and cost -- on every re-run of a file
# that's already been categorized. Deliberately NOT added to
# GENERATED_COLUMN_NAMES below, same reasoning/mechanism as
# POSTED_COLUMN_NAME's own "passes through untouched" comment just above --
# a blank cell (new row, or a row whose lookup above found nothing at all)
# is still (re-)computed every run; only a row that ALREADY has a value
# skips recomputation. VENDOR PAYMENT ROWS NEVER GO THROUGH STICKY AT ALL
# (see the VENDOR PAYMENT paragraph above) -- they're always recomputed,
# every run, regardless of what's already in the cell.
POSTING_REFERENCE_COLUMN_NAME = "PostingReference"

# Every header name this code has ever generated -- stripped from the
# header/data before recomputing on a re-run, so re-running never adds a
# second set of columns. Includes the OLD "From Account"/"To Account" names
# too, in case a file was already run once under the previous version of
# this code before the Debit/Credit Account rename.
#
# "supporting required" (2026-08-12, still later -- per Ravindra: "please
# add code in categorize path to add a new column Supporting doc required
# from customization module field(api name:cf_supporting_required) before
# the category column if not already added") -- MUST stay in sync with
# SUPPORTING_REQUIRED_COLUMN_NAME's own literal value further below (kept
# as a plain literal here, not a reference to that constant, matching every
# other entry in this set -- SUPPORTING_REQUIRED_COLUMN_NAME is defined
# later in this file, after this set, so it can't be referenced here
# directly). Added to this set (unlike PostingReference/Zoho Posting
# Reference, which are deliberately EXCLUDED so their values survive a
# re-categorize untouched) because this column's value should always
# reflect the CURRENT cf_supporting_required flag for a row's category,
# same "always recomputed, never stale" behavior as Category itself --
# adding it here means _strip_previously_generated_columns() removes any
# existing instance (wherever it happens to sit -- before Category from a
# previous categorize_workbook() run, or appended at the end from an
# earlier /attach-supporting-docs run's add_supporting_required_column())
# before categorize_workbook() rebuilds exactly one fresh instance,
# positioned before Category -- see categorize_workbook()'s own comment
# where new_header is built. This IS what satisfies Ravindra's "if not
# already added" requirement: a strip-then-rebuild-fresh-every-time
# approach can never end up with two of this column, by construction.
GENERATED_COLUMN_NAMES = {
    "category", "posting type",
    "debit account", "credit account",
    "from account", "to account",  # pre-2026-07-24 names
    "supporting required",
    "itemized with",  # 2026-08-13 -- see ITEMIZED_WITH_COLUMN_NAME's own comment below
}

# ---------------------------------------------------------------------------
# BANK CHARGES / VAT-ON-BANK-CHARGES CONSOLIDATION (2026-08-10, per Ravindra:
# "i have to deal with bank charges and VAt charges seperately compared to
# other categories ... posting every transaction unnecessarily adds a lot of
# transactions"). These two categories are NEVER posted as individual rows,
# unconditionally -- extract_postable_rows() below excludes them from its
# normal "rows" list regardless of the caller/widget's "Bank Charges
# Included/Excluded" checkbox, and instead surfaces them separately as
# "bank_charge_rows" (see that function). What the checkbox actually
# controls (entirely in main.py's /post-bank-charges route, not here) is
# whether a SEPARATE consolidation pass runs at all this month: matching
# each Bank charges row to its corresponding VAT-on-bank-charges row (see
# match_bank_charges_to_vat()/extract_trailing_code() below) and posting ONE
# summed entry for the matched ones and ONE summed entry for the leftover
# (no-VAT-match) ones, instead of one Zoho entry per individual transaction.
#
# Matched EXACTLY as named in the widget's Category dropdown (case-
# insensitive, whitespace-stripped) -- same comparison style as
# FALLBACK_CATEGORY ("Others") elsewhere in this module. If Ravindra's
# Categorization module ever spells these differently, update the two
# constants below; nothing else needs to change.
BANK_CHARGES_CATEGORY = "Bank charges"
VAT_ON_BANK_CHARGES_CATEGORY = "VAT on bank charges"
_BANK_VAT_CHARGE_CATEGORIES_LOWER = {BANK_CHARGES_CATEGORY.lower(), VAT_ON_BANK_CHARGES_CATEGORY.lower()}

# ---------------------------------------------------------------------------
# EGYPT ENTITY: PAIRED SALARY + BANK-CHARGE ITEMIZED POSTING (2026-08-13,
# per Ravindra -- see the project's own
# "egypt-paired-bank-charge-itemized-posting-spec.md" for the full,
# confirmed design this implements). PHASE 1 ONLY (detection + labeling at
# categorize time) -- Phase 2 (combined itemized posting) lives in
# posting_service.py/main.py, not yet built as of this comment.
#
# THE PATTERN: on the Egypt entity's statements specifically, one real
# transfer (e.g. a 30,000 salary payment) produces TWO rows sharing the
# EXACT SAME narration text (which embeds a unique transfer reference, e.g.
# "Outward Swift Payment FT262049TDRP\DXB") -- the transfer itself, and the
# bank's own charge for executing it (a much smaller amount). The smaller
# row has no keyword/pattern of its own that could ever categorize it as
# Bank charges -- it only makes sense paired with its larger sibling.
#
# OPT-IN ONLY: gated behind categorize_workbook()'s new
# `detect_paired_bank_charges` parameter (default False) -- a manual
# checkbox in the widget for this run, NOT automatic by organization_id
# (confirmed with Ravindra: safer than hardcoding an org ID, zero risk to
# any other entity's processing when left off).
#
# CONFIRMED PAIRING RULES (2026-08-13):
#   1. Two or more rows qualify as a group if their narration text is
#      EXACTLY identical (same whitespace-collapsed, casefolded comparison
#      as posting_service._normalize_text_for_dup_check() -- duplicated
#      here as _normalize_narration_for_pairing() rather than importing
#      posting_service, to keep this module's existing zero-dependency-on-
#      posting_service.py shape) AND their amounts aren't all identical --
#      no date-match requirement, no amount cap; the narration's own
#      embedded unique transfer reference is judged sufficient protection
#      against an accidental false match.
#   2. Within a qualifying group, the SINGLE LARGEST amount is "primary"
#      (its normal rule-matched category is left completely untouched);
#      EVERY OTHER row in the group has its Category force-overridden to
#      BANK_CHARGES_CATEGORY, regardless of what its own narration would
#      otherwise match (this is the whole point -- these rows have nothing
#      distinctive on their own to match against).
#   3. SAFETY GUARD (not explicitly in the confirmed rules, added here to
#      avoid a wrong guess): if MORE THAN ONE row in a group ties for the
#      largest amount, this module refuses to guess which one is the real
#      "primary" -- the whole group is left alone (normal per-row
#      categorization, no override, no "Itemized With" value) and a warning
#      is printed. A tie among the smaller ("charge") amounts is fine and
#      expected (each still becomes its own separate charge line item).
#
# "Itemized With" (ITEMIZED_WITH_COLUMN_NAME below): a new column holding
# the DESTINATION Excel row number of the group's primary row -- written on
# EVERY row in a detected group, including the primary itself (pointing to
# its own row), so a reader can resolve the whole group from any one row's
# cell alone. In GENERATED_COLUMN_NAMES (recomputed fresh every
# /categorize run, same as Category itself, NOT sticky) -- deterministic
# from narration+amount, which don't change between re-categorize runs on
# the same statement, so there's no "flip-flopping" risk; if the checkbox
# is later turned OFF for a re-categorize, this column simply isn't
# rebuilt and any previously-overridden Bank charges rows revert to
# whatever normal rule-matching finds for them, same as disabling any other
# opt-in behavior in this codebase.
#
# IMPORTANT INTERACTION FOR PHASE 2 (not yet fixed -- flagging so it isn't
# missed): extract_postable_rows()'s existing _BANK_VAT_CHARGE_CATEGORIES_
# LOWER exclusion (just above) currently routes EVERY "Bank charges"-
# categorized row into the SEPARATE month-end consolidation bucket
# (main.py's /post-bank-charges), regardless of source. A row this feature
# force-categorizes as Bank charges must NOT go through that unrelated
# path -- Phase 2 needs extract_postable_rows() to check
# ITEMIZED_WITH_COLUMN_NAME FIRST and route any row that has it set to the
# new itemized-pair posting path instead, before the existing bank-charges-
# category exclusion ever gets a chance to claim it.
# ---------------------------------------------------------------------------
ITEMIZED_WITH_COLUMN_NAME = "Itemized With"

# Vendor Payment PostingReference tier (2026-08-12, LATER SAME DAY) -- see
# POSTING_REFERENCE_COLUMN_NAME's own comment above for the full feature.
# LIVE-CONFIRMED 2026-08-12, per Ravindra: his real Categorization module
# spells this category PLURAL -- "Vendor Payments" -- not singular "Vendor
# Payment" (which is what the FIRST version of this constant guessed, since
# that's Zoho's own posting-type label spelling used throughout
# posting_service.py's _VENDOR_PAYMENT_TYPES). That mismatch is exactly why
# the Vendor Payment tier never fired for Ravindra's real data: the
# equality check compared _category_base(matched["category"]).lower()
# against the wrong literal string, so it silently never matched -- NOT a
# suffix-stripping bug in _category_base() itself (that part was already
# tested and confirmed working correctly against a "Vendor Payment - CBK"-
# shaped value; the constant it was being compared to was just spelled
# wrong). Same "matched EXACTLY as named in the widget's Category dropdown"
# convention as BANK_CHARGES_CATEGORY/VAT_ON_BANK_CHARGES_CATEGORY above --
# if Ravindra's Categorization module ever spells this differently again,
# update ONLY this constant; nothing else needs to change.
VENDOR_PAYMENT_CATEGORY = "Vendor Payments"


# ---------------------------------------------------------------------------
# SUPPORTING DOCUMENT ATTACHMENT (2026-08-12, later same day -- SCOPE CUT
# 2026-08-12, still later, per Ravindra: "i want to park the document
# extraction and validation aside and want only to attach the required
# document to the expense entry ... Please discard all the previous changes
# made for validation and gemini pdf doc reading. Make only the changes i
# mentioned in this."). This SUPERSEDES the earlier, much larger "Supporting
# Document VERIFICATION" design (Gemini PDF extraction, invoice-to-name/
# amount/VAT checks, a per-entity invoice-folder Entities field, an Expense
# amount/tax correction) -- ALL of that was built, then explicitly discarded
# the same day, before ever running live. What's left, and all this feature
# does now: for every already-posted row whose category requires a
# supporting document (cf_supporting_required on the Categorization module),
# find that row's invoice PDF in OneDrive (under the selected org's own
# mapped folder, in a "docs/<bank account number>/" subfolder -- see
# main.py's /attach-supporting-docs route for exactly how that path is
# built), find the matching Zoho record by comparing its own notes/
# description text against this row's narration (same field/matching
# mechanism posting_service.py's duplicate-detection already uses), and
# attach the PDF to it. No Gemini call, no amount/tax correction, no
# per-entity invoice-folder Zoho field -- see main.py's /attach-supporting-
# docs docstring for the full pipeline; the column-name constants and the
# functions further below are just this module's (categorize_from_module.py's)
# side of it -- reading/writing the statement itself, same division of
# responsibility as every other feature in this file (categorize_from_
# module.py never talks to Zoho/OneDrive directly).
SUPPORTING_REQUIRED_COLUMN_NAME = "Supporting Required"
ATTACHMENT_STATUS_COLUMN_NAME = "Attachment Status"


def _category_base(category_text) -> str:
    """Strips a trailing ' - <ENTITY>' suffix off a category label, e.g.
    "Bank charges - CBK" -> "Bank charges", "VAT on bank charges - NFT" ->
    "VAT on bank charges". 2026-08-1x, per Ravindra: cf_categorisation
    values now carry a per-entity suffix on EVERY category (not just Bank
    charges/VAT -- "Insurance - CBK", "Rent - CBK", etc. all follow the same
    pattern), so the category label alone tells you which entity/org it
    belongs to -- "Bank charges - CBK" for Cyber Knight, "Bank charges -
    NFT" for another entity. A bare exact-string comparison against
    BANK_CHARGES_CATEGORY/VAT_ON_BANK_CHARGES_CATEGORY (or any other fixed
    category constant) no longer matches anything once a suffix is present.

    Deliberately NOT a substring/"contains" check ("bank charges" in
    text.lower()) -- Ravindra's own suggested fix, but that would wrongly
    match "VAT on bank charges - CBK" against BANK_CHARGES_CATEGORY too,
    since the literal text "bank charges" is itself a substring of "vat on
    bank charges". Splitting off only the LAST " - "-delimited segment
    (rpartition, not split -- so a category whose own name legitimately
    contains " - " isn't mis-split, only the trailing entity tag is
    removed) and comparing the remainder EXACTLY avoids that collision
    entirely, while still matching across every entity uniformly. Mirrors
    the same "<value> - <suffix>" parsing convention already used elsewhere
    in this codebase (see _parse_account_to_be_posted() below and main.py's
    _extract_org_id_from_cf_entity()). A category with no suffix at all (no
    " - ") is returned unchanged -- fully backward compatible with any
    category that was never given an entity tag."""
    text = str(category_text or "").strip()
    base, sep, _suffix = text.rpartition(" - ")
    return base if sep else text


def _category_suffix(category_text) -> str:
    """Companion to _category_base() just above -- returns the trailing
    ' - <ENTITY>' suffix ITSELF (e.g. "Bank charges - CBK" -> "CBK"), or ""
    if the category has no suffix at all. Added 2026-08-13 for the Egypt
    entity paired bank-charge feature (see _resolve_group_bank_charges_
    rules() below): the correct Bank charges rule to use for a "charge" row
    isn't always literally named "Bank charges" (Ravindra's own module has
    it as "Bank charges - EGP", following this exact per-entity suffix
    convention) -- matching the group's PRIMARY row's own suffix (e.g. a
    primary categorized "Salary - EGP" pairs with "Bank charges - EGP", not
    some other entity's "Bank charges - CBK") is what actually finds the
    real, live category from the org's own Categorization module instead of
    a hardcoded guess."""
    text = str(category_text or "").strip()
    base, sep, suffix = text.rpartition(" - ")
    return suffix.strip() if sep else ""

# The "code" a Bank charges row and its corresponding VAT-on-bank-charges row
# share -- per Ravindra: "usually the last part of the narration is the code
# which is common for both bank charges and vat on bank charges" (his real
# example: both "Outward Swift Charges FT26183ZFN48\\DXB AC-012001940536
# |RedSeal Inc|SW-PNCCUS33 FT26183ZFN48" and "Tax Invoice Debit
# FT26183ZFN48\\DXB AC-012001940536 |RedSeal Inc|SW-PNCCUS33 FT26183ZFN48"
# end in "FT26183ZFN48"). Grabs the LAST run of letters/digits in the
# narration, allowing (and discarding) any trailing punctuation after it --
# e.g. a narration ending "...FT26183ZFN48." or "...FT26183ZFN48 " still
# yields "FT26183ZFN48". Deliberately simple/general rather than hard-coded
# to this one example's shape, since "usually" (Ravindra's own word) implies
# this won't be the exact format every time -- a narration whose last token
# doesn't look like a real code just won't match anything, which is the
# correct fallback (see match_bank_charges_to_vat()'s "unmatched" bucket)
# rather than a crash or a wrong guess.
_TRAILING_CODE_RE = re.compile(r"([A-Za-z0-9]+)[^A-Za-z0-9]*$")
# A bare 1-3 character trailing token (e.g. narration ends in a stray "Dr"/
# "AE") is too short to trust as a real shared code -- would risk matching
# together two genuinely unrelated Bank charges/VAT rows that just happen to
# both end the same short way. Real codes seen so far (e.g. "FT26183ZFN48")
# are much longer than this floor.
_MIN_TRAILING_CODE_LEN = 4


def extract_trailing_code(narration) -> Optional[str]:
    """Returns the last alphanumeric token of `narration`, uppercased, or
    None if the narration is blank or its last token is too short to trust
    as a real shared code (see _MIN_TRAILING_CODE_LEN). Never raises. See
    the module-level comment block above for the reasoning and real-data
    example this is built against."""
    text = str(narration or "").strip()
    if not text:
        return None
    m = _TRAILING_CODE_RE.search(text)
    if not m:
        return None
    code = m.group(1)
    if len(code) < _MIN_TRAILING_CODE_LEN:
        return None
    return code.upper()


def match_bank_charges_to_vat(bank_charge_rows: List[dict]) -> dict:
    """Splits `bank_charge_rows` (the list extract_postable_rows() returns
    under that key -- every not-yet-posted row categorized Bank charges OR
    VAT on bank charges, across however many files/accounts the caller has
    pooled together) into Bank charges rows matched to a same-code VAT row,
    and Bank charges rows with no VAT match at all. Called ONLY from
    main.py's /post-bank-charges route (this module has no Zoho connection
    of its own -- see the top-of-file docstring), which is itself only
    invoked when Ravindra's "Bank Charges Included/Excluded" checkbox is
    checked; extract_postable_rows() always excludes both categories from
    individual posting regardless of this function ever being called.

    Matching is by extract_trailing_code() equality only -- NOT by amount,
    date, or file -- since the whole point (per Ravindra) is that a Bank
    charges row and its VAT row can land in different files (multiple
    statements/date-ranges for one account within a month) and even be
    listed in any order within a file, with only the shared trailing code to
    tie them together. Each VAT row is consumed by at most one Bank charges
    row (first-seen order) -- a VAT row with no code, or whose code doesn't
    match any Bank charges row's code, is simply left unused; per Ravindra's
    spec only the BANK CHARGE side needs its own "Non-VAT" bucket when
    unmatched, so an orphan VAT-only row isn't specially reported here (it
    just stays excluded from individual posting forever, same as it already
    was before this feature, until a future statement happens to bring in
    its matching Bank charges row).

    Returns {"matched_pairs": [(bank_row, vat_row), ...], "unmatched_bank_rows":
    [bank_row, ...]} -- each row dict is the SAME dict extract_postable_rows()
    built (excel_row, category, posting_type, debit_account, credit_account,
    account_number, amount, narration, reference_number, txn_date), so the
    caller can go straight from a matched/unmatched row back to exactly which
    sheet row (and, via the caller's own bookkeeping, which file) to write
    the consolidated result into."""
    bank_rows = [r for r in bank_charge_rows if _category_base(r.get("category")).lower() == BANK_CHARGES_CATEGORY.lower()]
    vat_rows = [r for r in bank_charge_rows if _category_base(r.get("category")).lower() == VAT_ON_BANK_CHARGES_CATEGORY.lower()]

    vat_by_code = {}
    for r in vat_rows:
        code = extract_trailing_code(r.get("narration"))
        if code:
            vat_by_code.setdefault(code, []).append(r)

    matched_pairs = []
    unmatched_bank_rows = []
    for br in bank_rows:
        code = extract_trailing_code(br.get("narration"))
        candidates = vat_by_code.get(code) if code else None
        if candidates:
            matched_pairs.append((br, candidates.pop(0)))
        else:
            unmatched_bank_rows.append(br)

    return {"matched_pairs": matched_pairs, "unmatched_bank_rows": unmatched_bank_rows}


OTHERS_RULE = {
    "category": FALLBACK_CATEGORY,
    "keywords": [],
    "patterns": [],
    "module_account": None,
    "type_of_posting": None,
    "reference": None,  # 2026-08-12 -- see POSTING_REFERENCE_COLUMN_NAME's comment; "Others" has no matched rule to pull cf_reference from
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
        # cf_categorisation (2026-08-11, per Ravindra: switched from
        # cf_category to this field, now made MANDATORY on the module) --
        # clean swap, no fallback to the old field name, same convention as
        # cf_account_to_be_posted's own fallback removal on 2026-08-07 ("i
        # dont need the fall back just use the new field"). Since the field
        # is now mandatory in Zoho, a blank value here means either an old
        # record that predates the mandatory rule (hasn't been backfilled
        # yet) or some other malformed row -- still skipped rather than
        # failing the whole batch, same tolerant behavior as before.
        category = str(rec.get("cf_categorisation") or "").strip()
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
        # cf_reference (2026-08-12, added for the PostingReference feature --
        # see POSTING_REFERENCE_COLUMN_NAME's own comment above). OPTIONAL,
        # same tolerant "blank means not set" handling as every other cf_*
        # field here -- a category with no cf_reference simply falls through
        # to the next tier (Gemini, then the statement's own raw Reference
        # column) for every row matched to it, exactly as if this field
        # didn't exist on the module at all.
        reference = str(rec.get("cf_reference") or "").strip() or None
        # cf_supporting_required (2026-08-12, later same day, added for the
        # Supporting Document Verification feature -- see
        # SUPPORTING_REQUIRED_COLUMN_NAME's own comment above). OPTIONAL
        # Yes/No field on the Categorization module -- a category with this
        # set to "Yes" means every row posted under it needs a real
        # invoice/receipt checked (and, if it shows VAT, the posting
        # corrected) before it's considered fully done. Same tolerant
        # "blank/anything-not-yes means No" handling as every other cf_*
        # field here -- a module that's never had this field added at all
        # just means every row's Supporting Required comes out "No", not an
        # error. Accepts "yes"/"true"/"1" case-insensitively in case the
        # real module ends up a checkbox/boolean field rather than a
        # Yes/No dropdown -- UNVERIFIED which shape Ravindra's module
        # actually uses, since this field doesn't exist there yet.
        supporting_required = str(rec.get("cf_supporting_required") or "").strip().lower() in ("yes", "true", "1")
        rules.append({
            "category": category,
            "keywords": keywords,
            "patterns": patterns,
            "module_account": module_account,
            "type_of_posting": type_of_posting,
            "priority": priority,
            "reference": reference,
            "supporting_required": supporting_required,
        })
    # Stable sort -- rules with equal (including default) priority keep their
    # original module-record relative order, so this is a no-op on behavior
    # when no record sets cf_priority anywhere.
    rules.sort(key=lambda r: r["priority"])
    return rules


def classify_rule(narration, rules: List[dict]) -> Tuple[dict, Optional[str]]:
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
    to create first.

    Returns (rule, pattern_match_text) -- 2026-08-12, added for the
    PostingReference feature's new Vendor Payment tier (see
    POSTING_REFERENCE_COLUMN_NAME's own comment below): pattern_match_text is
    the exact substring whichever REGEX pattern matched (re.Match.group(0)),
    if this row's category was decided in the PATTERN pass below -- None if
    it was decided by a literal keyword instead (the two passes are mutually
    exclusive per call: literal keywords are always checked, across every
    rule, before any pattern is ever consulted -- see above), or if nothing
    matched at all (OTHERS_RULE). A rule with more than one pattern in its
    cf_key_words only ever contributes the ONE pattern that actually matched
    first -- same "first match wins" order as the surrounding loops. This
    function has exactly one caller (categorize_workbook() below) -- changing
    its return shape here doesn't affect anything else in this codebase."""
    text = str(narration) if narration is not None else ""
    text_lower = text.lower()
    for rule in rules:
        if any(kw in text_lower for kw in rule["keywords"]):
            return rule, None
    for rule in rules:
        for p in rule["patterns"]:
            m = p.search(text)
            if m:
                return rule, m.group(0)
    return OTHERS_RULE, None


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


def categorize_workbook(wb, rules: List[dict], account_number: str = "",
                         gemini_client=None, ai_prompt_template: Optional[str] = None,
                         detect_paired_bank_charges: bool = False) -> Tuple["openpyxl.Workbook", dict]:
    """Runs the categorization pass over the first sheet of `wb` (an already-
    opened openpyxl Workbook), using `rules` (from build_rules()).
    account_number is the current statement's own bank account's real Zoho
    account_id (NOT its bank account number -- see the IMPORTANT note in the
    module docstring) -- used to fill in whichever of Debit Account/Credit
    Account corresponds to "this account" for each row (see module
    docstring). Pass "" if unknown; that side is then left blank instead of
    guessed. Returns (new_workbook, stats) where
    stats = {"total": int, "others": int, "by_category": {category: count, ...},
    "itemized_pairs_detected": int}.

    detect_paired_bank_charges (2026-08-13, per Ravindra's Egypt entity
    special case -- see this module's "EGYPT ENTITY: PAIRED SALARY +
    BANK-CHARGE ITEMIZED POSTING" comment block, right after
    _BANK_VAT_CHARGE_CATEGORIES_LOWER above, for the full confirmed design):
    default False, OPT-IN ONLY (a checkbox in the widget for this run) --
    when True, runs _detect_paired_bank_charges() before the main per-row
    loop below and, for every detected group, force-overrides every
    non-primary row's Category to BANK_CHARGES_CATEGORY and writes every
    row in the group's ITEMIZED_WITH_COLUMN_NAME cell with the group's
    primary row's own destination Excel row number. Zero effect on the
    output when left False (default) -- no new column, no category
    overrides, byte-for-byte the same behavior as before this parameter
    existed.

    gemini_client / ai_prompt_template (2026-08-12, added for the
    PostingReference feature -- see POSTING_REFERENCE_COLUMN_NAME's own
    comment above for the full 3-tier lookup this powers): both optional,
    both default None. If either is missing, tier 2 (Gemini) of that lookup
    is simply skipped for every row this run -- same graceful "AI hiccup
    never blocks the real work" degradation as everywhere else this project
    calls Gemini, just applied here to "the AI tier gets skipped" rather
    than "the whole request fails."

    PERFORMANCE NOTE, worth knowing: unlike /post-transactions (which
    streams results back row-by-row as they happen), main.py's /categorize
    route is a single synchronous request/response -- so on a FIRST-time
    categorization of a large statement, every row whose category has no
    cf_reference makes a real sequential Gemini call before this function
    returns, with no live progress shown meanwhile (same request just takes
    longer). PostingReference is STICKY (see POSTING_REFERENCE_COLUMN_NAME's
    comment) specifically to keep this a one-time cost per file, not a
    recurring one on every re-categorize -- but the very first run on a
    large, previously-uncategorized file could still take noticeably longer
    than before this feature existed. Not addressed here (would mean making
    /categorize streaming too, a much bigger change) -- flagged as a known,
    accepted tradeoff, not an oversight."""
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

    # Always keep "Zoho Posting Reference" as the LAST column, regardless of
    # where it sat before -- 2026-08-17, per Ravindra ("add that column
    # always in the last"). POSTED_COLUMN_NAME is deliberately excluded
    # from GENERATED_COLUMN_NAMES (see that constant's own docstring --
    # stripping it on every re-categorize would erase which rows were
    # already posted), so it survives _strip_previously_generated_columns()
    # completely untouched, at whatever position it started at. Without
    # this extra step, a file that's already been posted once (so this
    # column exists) and then gets RE-categorized would end up with this
    # column sitting BEFORE the freshly-rebuilt Category/Debit Account/
    # Credit Account/Posting Type columns (which always get appended at
    # the true end, below) instead of after them -- pulled out here and
    # re-appended, unchanged in content, once those 4 are also in place.
    # A file that's never been posted yet (no such column present) is
    # completely unaffected -- _posted_col_values stays None and nothing
    # about the rest of this function changes.
    #
    # TRADEOFF: the relocated column loses its original cell styling (falls
    # back to openpyxl's default, like every OTHER column this function
    # appends itself) -- keep_indices no longer has an entry for it once
    # removed below, so _copy_row()'s per-column style copy correctly (not
    # accidentally) skips it, same as Category/Debit Account/Credit
    # Account/Posting Type already do. Only the cell's CONTENT (each row's
    # actual posting result text) is preserved -- that's the part that
    # actually matters for correctness.
    _posted_col_values = None
    _header_lower_for_posted = [str(h).strip().lower() if h is not None else "" for h in header]
    if POSTED_COLUMN_NAME.lower() in _header_lower_for_posted:
        _posted_idx = _header_lower_for_posted.index(POSTED_COLUMN_NAME.lower())
        header = header[:_posted_idx] + header[_posted_idx + 1:]
        keep_indices = keep_indices[:_posted_idx] + keep_indices[_posted_idx + 1:]
        _posted_col_values = []
        _new_data_rows = []
        for row in data_rows:
            if row is None:
                _new_data_rows.append(row)
                _posted_col_values.append(None)
            else:
                row = list(row)
                _posted_col_values.append(row[_posted_idx] if _posted_idx < len(row) else None)
                _new_data_rows.append(tuple(row[:_posted_idx] + row[_posted_idx + 1:]))
        data_rows = _new_data_rows

    # PostingReference (2026-08-12) -- pulled out the SAME way
    # POSTED_COLUMN_NAME is just above (find it, remove it from header/
    # keep_indices, capture each row's existing value positionally aligned
    # with data_rows) so its STICKY existing values (see
    # POSTING_REFERENCE_COLUMN_NAME's own comment) survive being relocated
    # to its new fixed position (right after Posting Type, computed below)
    # regardless of where it sat in the file before. _existing_posting_
    # references stays None (not just "all blank") when the column doesn't
    # exist in this file at all yet -- distinguishing "never computed"
    # (None -- every row needs fresh computation) from "computed but blank
    # for this specific row" (empty string/None entry inside a real list --
    # tier 2/3 both came up empty last time; still recomputed every run
    # until something actually fills it in, same as a genuinely new row).
    _existing_posting_references = None
    _header_lower_for_posting_ref = [str(h).strip().lower() if h is not None else "" for h in header]
    if POSTING_REFERENCE_COLUMN_NAME.lower() in _header_lower_for_posting_ref:
        _pr_idx = _header_lower_for_posting_ref.index(POSTING_REFERENCE_COLUMN_NAME.lower())
        header = header[:_pr_idx] + header[_pr_idx + 1:]
        keep_indices = keep_indices[:_pr_idx] + keep_indices[_pr_idx + 1:]
        _existing_posting_references = []
        _new_data_rows = []
        for row in data_rows:
            if row is None:
                _new_data_rows.append(row)
                _existing_posting_references.append(None)
            else:
                row = list(row)
                _existing_posting_references.append(row[_pr_idx] if _pr_idx < len(row) else None)
                _new_data_rows.append(tuple(row[:_pr_idx] + row[_pr_idx + 1:]))
        data_rows = _new_data_rows

    header_lower = [str(h).strip().lower() if h is not None else "" for h in header]
    nar_idx = find_column(header_lower, NARRATION_HEADER_ALIASES)
    debit_idx = find_column(header_lower, DEBIT_HEADER_ALIASES)
    credit_idx = find_column(header_lower, CREDIT_HEADER_ALIASES)
    # Statement's OWN raw Reference-style column (2026-08-12, added for
    # PostingReference's tier-3 fallback) -- same REFERENCE_HEADER_ALIASES
    # detection extract_postable_rows() below has always used, just also
    # needed HERE now. Optional, like extract_postable_rows()'s own
    # reference_idx -- None just means tier 3 has nothing to fall back to
    # for this file (not an error; PostingReference for an affected row
    # would then come out blank only if tiers 1 and 2 ALSO found nothing).
    ref_idx = find_column(header_lower, REFERENCE_HEADER_ALIASES)
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

    # SUPPORTING_REQUIRED_COLUMN_NAME (2026-08-12, still later, per
    # Ravindra: "add a new column Supporting doc required from
    # customization module field(api name:cf_supporting_required) before
    # the category column if not already added") is inserted FIRST in this
    # list, i.e. immediately BEFORE "Category" -- unlike
    # add_supporting_required_column() further below (which only ever
    # APPENDS this same column at the very end, because IT runs against an
    # already-categorized file whose Debit Account/Credit Account/Posting
    # Type formulas already exist and reference Category by column letter),
    # inserting it before Category is completely safe HERE: this whole
    # function builds `out_wb`/`out_ws` from scratch every call and writes
    # every formula fresh, at whatever column it lands on this run -- there
    # is no pre-existing formula for an insert to silently break. Reuses
    # the SAME column name/constant add_supporting_required_column() uses
    # (rather than a second, differently-named column) so a file that later
    # goes through /attach-supporting-docs finds this column already
    # present and just refreshes it in place instead of appending a
    # duplicate at the end.
    # Egypt entity paired bank-charge detection (2026-08-13) -- see
    # _detect_paired_bank_charges()'s own docstring and this module's
    # "EGYPT ENTITY: PAIRED SALARY + BANK-CHARGE ITEMIZED POSTING" comment
    # block above. Needs each row's DESTINATION Excel row number precomputed
    # independent of rule-matching (destination row numbers only depend on
    # which rows are blank, not on Category) -- done here, before the main
    # per-row loop below, so "Itemized With" can be written correctly on the
    # very first pass over each row rather than needing a second pass.
    _itemized_role = {}
    _itemized_group_dest_row = {}
    if detect_paired_bank_charges:
        _dest_row_by_offset = {}
        _row_num_cursor = len(metadata_rows) + 1
        for _offset, _row in enumerate(data_rows):
            if _row is None or all(v is None or str(v).strip() == "" for v in _row):
                continue
            _row_num_cursor += 1
            _dest_row_by_offset[_offset] = _row_num_cursor
        _itemized_role, _itemized_group_dest_row = _detect_paired_bank_charges(
            data_rows, nar_idx, debit_idx, credit_idx, _dest_row_by_offset
        )
        print(f"[categorize_from_module] categorize_workbook: paired bank-charge detection ON -- "
              f"{len(_itemized_group_dest_row)} row(s) across "
              f"{len(set(_itemized_group_dest_row.values()))} group(s) affected.", flush=True)

    new_header = list(header) + [SUPPORTING_REQUIRED_COLUMN_NAME, "Category", "Debit Account", "Credit Account",
                                  "Posting Type", POSTING_REFERENCE_COLUMN_NAME]
    if detect_paired_bank_charges:
        new_header = new_header + [ITEMIZED_WITH_COLUMN_NAME]
    if _posted_col_values is not None:
        new_header = new_header + [POSTED_COLUMN_NAME]
    _copy_row(header_row_idx + 1, len(metadata_rows) + 1, new_header)

    # Merged cells + any embedded image (e.g. a bank's letterhead logo) living
    # in the metadata block -- see _copy_metadata_merges_and_images()'s
    # docstring. 2026-08-03, per Ravindra's "significantly different from
    # original" report.
    _copy_metadata_merges_and_images(ws, out_ws, keep_indices, len(metadata_rows))

    # Column numbers (1-based) of the 6 appended columns, and the statement's
    # OWN Debit/Credit columns -- needed as Excel column letters so each
    # row's Debit Account/Credit Account/Posting Type formula can reference
    # its own row's Category/Debit/Credit cells (see _account_formula()/
    # _posting_type_formula()). posting_reference_col (2026-08-12) is a
    # PLAIN value column, not a formula -- see POSTING_REFERENCE_COLUMN_
    # NAME's own comment for why (Gemini calls can't be Excel formulas) --
    # so it needs no _letter/formula-building counterpart the other three do.
    # supporting_required_col sits BEFORE category_col (see new_header just
    # above) -- every offset below is shifted by 1 to make room for it.
    supporting_required_col = len(header) + 1
    category_col = len(header) + 2
    posting_col = len(header) + 5
    posting_reference_col = len(header) + 6
    category_col_letter = get_column_letter(category_col)
    stmt_debit_col_letter = get_column_letter(debit_idx + 1)
    stmt_credit_col_letter = get_column_letter(credit_idx + 1)

    # The hidden lookup sheet every row's formula below points at -- written
    # BEFORE the data rows since the formulas reference it by name, though
    # sheet creation order doesn't actually matter to Excel; done here just
    # to keep the sheet's own reasoning (account_number, rules) close to
    # where it's first used.
    _write_rules_lookup_sheet(out_wb, rules, account_number)

    # The REAL "Bank charges"-style rule for each detected group -- resolved
    # PER GROUP (not once for the whole file), from the org's own live
    # Categorization module data, matching the group's PRIMARY row's own
    # entity suffix (e.g. a primary matched "Salary - EGP" pairs with
    # "Bank charges - EGP") -- see _resolve_group_bank_charges_rules()'s own
    # docstring for the full reasoning. 2026-08-13, per Ravindra: "the bank
    # charges category for the smaller amount must be from the
    # categorization tables Bank charge - EGP like that not just a hard
    # coded string" -- this REPLACES an earlier version of this code that
    # matched category text EXACTLY against the bare BANK_CHARGES_CATEGORY
    # constant, which could never match a real, entity-suffixed category
    # like "Bank charges - EGP" at all (the same class of bug this
    # codebase's own _category_base()/VENDOR_PAYMENT_CATEGORY comment
    # already documents having happened once before).
    _group_bank_charges_rules = (
        _resolve_group_bank_charges_rules(data_rows, nar_idx, rules, _itemized_role, _itemized_group_dest_row)
        if detect_paired_bank_charges else {}
    )

    stats = {"total": 0, "others": 0, "by_category": {}, "itemized_pairs_detected": len(set(_itemized_group_dest_row.values()))}
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
        matched, pattern_matched_text = classify_rule(row[nar_idx], rules)

        # Egypt entity paired bank-charge override (2026-08-13) -- this row
        # is a "charge" row in a group _detect_paired_bank_charges() found
        # above: force its category to the group's REAL, resolved Bank
        # charges rule (from _group_bank_charges_rules -- e.g. "Bank charges
        # - EGP", not a hardcoded string) so Debit Account/Credit Account
        # still resolve a real account, not just display text. If no real
        # rule could be resolved for this group (see
        # _resolve_group_bank_charges_rules()'s own docstring for why that
        # can happen), this row is LEFT ALONE -- its normal classify_rule()
        # result stands, same "never write a category that doesn't really
        # exist in the org's own Categorization module" reasoning as
        # everywhere else in this feature. Either way, "Itemized With" was
        # already set for this row above (independent of whether the
        # category override itself could happen), so it's still visibly
        # flagged as part of a group for manual review. The group's PRIMARY
        # row is deliberately untouched here either way -- its normal
        # classify_rule() result stands as-is.
        if _itemized_role.get(src_offset) == "charge":
            _group_rule = _group_bank_charges_rules.get(_itemized_group_dest_row[src_offset])
            if _group_rule:
                print(f"[categorize_from_module] categorize_workbook: row {current_row_num} -- paired bank-charge "
                      f"override: classify_rule() matched {matched['category']!r}, forcing to "
                      f"{_group_rule['category']!r} instead (Itemized With -> row "
                      f"{_itemized_group_dest_row[src_offset]}).", flush=True)
                matched = _group_rule
            else:
                print(f"[categorize_from_module] categorize_workbook: row {current_row_num} -- paired bank-charge "
                      f"role is 'charge' but no real Bank charges rule was resolved for this group -- leaving "
                      f"this row's own classify_rule() result ({matched['category']!r}) untouched (Itemized With "
                      f"still -> row {_itemized_group_dest_row[src_offset]} for manual review).", flush=True)

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

        # PostingReference (2026-08-12) -- see POSTING_REFERENCE_COLUMN_
        # NAME's own comment for the full lookup this implements and the
        # STICKY behavior (an existing non-blank value is reused verbatim,
        # never recomputed) it starts with -- EXCEPT for Vendor Payment
        # rows, which are handled entirely separately below and never go
        # through STICKY at all (2026-08-12, LATER SAME DAY, per Ravindra:
        # "for vendor payment category i dont want a reference col or
        # gemini or any other check it should take just from the patteren
        # match only" -- a direct follow-up after Ravindra reported Vendor
        # Payment rows weren't picking up a fresh pattern match on re-
        # categorize, and traced it himself to STICKY blocking Tier 0 for
        # any row that already had SOME PostingReference value from before
        # this Vendor Payment tier existed). Unlike Debit Account/Credit
        # Account/Posting Type above, this is written as a PLAIN value, not
        # a formula -- a Gemini call can't be an Excel formula, so (unlike
        # those three) this does NOT live-recalculate if a row's Category
        # is later changed by hand; re-running /categorize is the only way
        # to refresh it (for a NON-Vendor-Payment row, only while it's
        # still blank -- see the STICKY note; a Vendor Payment row instead
        # ALWAYS refreshes, every run -- see below).
        _is_vendor_payment_category = _category_base(matched["category"]).lower() == VENDOR_PAYMENT_CATEGORY.lower()
        if _is_vendor_payment_category:
            # Vendor Payment rows are handled ENTIRELY OUTSIDE the sticky/
            # tier system below -- per Ravindra's exact words above, this is
            # the SOLE source for these rows: no STICKY (an existing value,
            # even one written before this branch existed, is overwritten
            # every single /categorize run -- this is what actually fixes
            # the "not updated" symptom Ravindra reported: STICKY was
            # finding SOME old value and never even reaching Tier 0 below),
            # no rule cf_reference, no Gemini, no raw statement Reference
            # column fallback. A Vendor Payment row with no pattern match
            # this run (pattern_matched_text is None -- e.g. it matched via
            # a literal keyword instead, or its narration simply doesn't
            # contain anything the category's pattern(s) recognize) is left
            # BLANK -- deliberately not falling back to anything else,
            # since Ravindra was explicit that no other check should ever
            # apply to this category.
            posting_reference = pattern_matched_text
            _pr_source = ("regex pattern match (Vendor Payment category -- sole source, always recomputed, "
                          f"never sticky) -> {pattern_matched_text!r}" if pattern_matched_text else
                          "no pattern matched this row (Vendor Payment category never falls back to "
                          "cf_reference/Gemini/raw column/an existing sticky value) -> left blank")
            print(f"[categorize_from_module] categorize_workbook: row {current_row_num} PostingReference "
                  f"(Vendor Payment, always fresh) -- {_pr_source}", flush=True)
        else:
            _existing_pr = _existing_posting_references[src_offset] if _existing_posting_references is not None else None
            _existing_pr_text = str(_existing_pr).strip() if _existing_pr is not None else ""
            if _existing_pr_text:
                posting_reference = _existing_pr_text
                _pr_source = "existing value (sticky -- not recomputed)"
            else:
                _rule_reference = str(matched.get("reference") or "").strip()
                if _rule_reference:
                    # Tier 1: the matched category's own cf_reference.
                    posting_reference = _rule_reference
                    _pr_source = f"rule cf_reference (category={matched['category']!r})"
                elif gemini_client and ai_prompt_template:
                    # Tier 2: Gemini, via the SAME function/reasoning-leak
                    # defenses posting_service.py's own (now-superseded) AI
                    # Reference feature used -- see this file's top-of-function
                    # import comment. Only this row's narration is sent, same
                    # narration-in/suggestion-out shape as before; `context`
                    # (accepted but ignored by that function) is passed as None.
                    _narration_val = row[nar_idx] if nar_idx is not None and nar_idx < len(row) else None
                    _ai_result = generate_ai_reference_preview(
                        gemini_client, ai_prompt_template, {"narration": _narration_val}, None
                    ) or {}
                    _ai_suggestion = _ai_result.get("suggested_narration")
                    if not _ai_result.get("error") and _ai_suggestion:
                        posting_reference = _ai_suggestion
                        _pr_source = "Gemini"
                    else:
                        # Tier 3 fallback -- Gemini failed/errored/was flagged
                        # as a reasoning leak (or returned blank).
                        _raw_ref_val = row[ref_idx] if ref_idx is not None and ref_idx < len(row) else None
                        _raw_ref_text = str(_raw_ref_val).strip() if _raw_ref_val is not None else ""
                        posting_reference = _raw_ref_text or None
                        _pr_source = f"raw statement Reference column (Gemini unavailable -- {_ai_result.get('error')})"
                else:
                    # Tier 3 -- no gemini_client/ai_prompt_template available
                    # for this run at all (e.g. GEMINI_API_KEY not configured,
                    # or cf_ai_prompt blank/missing in Zoho), same "AI hiccup
                    # never blocks the real work" degradation as the rest of
                    # this project's Gemini usage.
                    _raw_ref_val = row[ref_idx] if ref_idx is not None and ref_idx < len(row) else None
                    _raw_ref_text = str(_raw_ref_val).strip() if _raw_ref_val is not None else ""
                    posting_reference = _raw_ref_text or None
                    _pr_source = "raw statement Reference column (no Gemini client/prompt available this run)"
                print(f"[categorize_from_module] categorize_workbook: row {current_row_num} PostingReference "
                      f"(fresh) -- source={_pr_source} -> {posting_reference!r}", flush=True)

        # Supporting Required (2026-08-12, still later) -- a PLAIN value,
        # like PostingReference, not a formula: cf_supporting_required
        # isn't a per-row statement fact a formula could recompute from
        # other cells, it's this row's MATCHED CATEGORY's own flag off the
        # Categorization module -- same `matched` dict classify_rule()
        # already returned for Category itself, just reading a different
        # key off it. `matched` is OTHERS_RULE for an unmatched ("Others")
        # row, which has no "supporting_required" key at all -- .get()
        # defaults that to None/falsy, i.e. "No", same tolerant "can't find
        # a rule -> nothing required" default add_supporting_required_
        # column() already uses.
        supporting_required_value = "Yes" if matched.get("supporting_required") else "No"

        row_out = list(row) + [
            supporting_required_value,
            matched["category"],
            _account_formula(category_cell, stmt_debit_cell, stmt_credit_cell),
            _account_formula(category_cell, stmt_credit_cell, stmt_debit_cell),
            _posting_type_formula(category_cell),
            posting_reference,
        ]
        if detect_paired_bank_charges:
            row_out = row_out + [_itemized_group_dest_row.get(src_offset)]
        if _posted_col_values is not None:
            row_out = row_out + [_posted_col_values[src_offset]]
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


def _normalize_narration_for_pairing(text) -> str:
    """Whitespace-collapsed, casefolded exact-comparison text -- same
    normalization posting_service._normalize_text_for_dup_check() applies,
    duplicated here (not imported) to keep this module's existing zero-
    dependency-on-posting_service.py shape intact. Used ONLY by
    _detect_paired_bank_charges() below to group rows by identical
    narration text."""
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _amount_as_float_for_pairing(value) -> Optional[float]:
    """Same tolerant numeric parse as posting_service._amount_as_float() --
    duplicated here for the same zero-dependency reason as
    _normalize_narration_for_pairing() just above. Returns None (not 0) for
    anything that doesn't parse, so a row with an unusable amount is simply
    left out of pairing consideration rather than treated as a real zero."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _detect_paired_bank_charges(data_rows: list, nar_idx: int, debit_idx: int, credit_idx: int,
                                 dest_row_by_offset: dict) -> Tuple[dict, dict]:
    """Egypt entity paired salary+bank-charge detection -- see this
    module's own "EGYPT ENTITY: PAIRED SALARY + BANK-CHARGE ITEMIZED
    POSTING" comment block (right after _BANK_VAT_CHARGE_CATEGORIES_LOWER
    above) for the full, confirmed design. Groups `data_rows` by exact
    normalized narration text; for every group of 2+ rows whose amounts
    aren't all identical, the single largest amount is "primary" and every
    other row in the group is a "charge" row -- UNLESS more than one row
    ties for the largest amount, in which case the whole group is skipped
    (never guessed which tied row is the real primary).

    `dest_row_by_offset` is {src_offset: destination Excel row number},
    precomputed by the caller (categorize_workbook()) independent of any
    rule-matching, since destination row numbers only depend on which rows
    are blank -- needed here so "Itemized With" can record each row's
    group's primary DESTINATION row number, not its source-data offset.

    Returns (itemized_role, itemized_group_dest_row) -- both keyed by
    src_offset, present ONLY for rows that ended up in a qualifying group.
    itemized_role[src_offset] is "primary" or "charge".
    itemized_group_dest_row[src_offset] is the group's primary row's
    destination Excel row number (written into EVERY row in the group,
    including the primary itself, pointing at its own row)."""
    groups_by_narration = {}
    for src_offset, row in enumerate(data_rows):
        if src_offset not in dest_row_by_offset:
            continue  # blank row, already excluded from dest_row_by_offset by the caller
        nar_val = row[nar_idx] if nar_idx is not None and nar_idx < len(row) else None
        nar_norm = _normalize_narration_for_pairing(nar_val)
        if not nar_norm:
            continue
        debit_val = row[debit_idx] if debit_idx is not None and debit_idx < len(row) else None
        credit_val = row[credit_idx] if credit_idx is not None and credit_idx < len(row) else None
        amount = debit_val if _has_amount(debit_val) else credit_val
        amount_val = _amount_as_float_for_pairing(amount)
        if amount_val is None:
            continue
        groups_by_narration.setdefault(nar_norm, []).append((src_offset, amount_val))

    itemized_role = {}
    itemized_group_dest_row = {}
    for nar_norm, members in groups_by_narration.items():
        if len(members) < 2:
            continue
        amounts_seen = {round(a, 2) for _, a in members}
        if len(amounts_seen) < 2:
            continue  # every amount in this group is identical -- not a primary+charge pattern
        max_amount = max(a for _, a in members)
        primary_candidates = [offset for offset, a in members if round(a, 2) == round(max_amount, 2)]
        if len(primary_candidates) > 1:
            print(f"[categorize_from_module] _detect_paired_bank_charges: narration group {nar_norm!r} has "
                  f"{len(primary_candidates)} rows tied for the largest amount ({max_amount!r}) -- can't tell "
                  f"which one is the real primary, skipping this whole group (no override, no pairing).",
                  flush=True)
            continue
        primary_offset = primary_candidates[0]
        primary_dest_row = dest_row_by_offset[primary_offset]
        itemized_role[primary_offset] = "primary"
        itemized_group_dest_row[primary_offset] = primary_dest_row
        charge_offsets = [offset for offset, _ in members if offset != primary_offset]
        for charge_offset in charge_offsets:
            itemized_role[charge_offset] = "charge"
            itemized_group_dest_row[charge_offset] = primary_dest_row
        print(f"[categorize_from_module] _detect_paired_bank_charges: narration group {nar_norm!r} -- primary="
              f"row (dest {primary_dest_row}, amount {max_amount!r}), {len(charge_offsets)} charge row(s) -- "
              f"which real Bank charges rule (if any) to use for them is resolved separately, see "
              f"_resolve_group_bank_charges_rules().", flush=True)

    return itemized_role, itemized_group_dest_row


def _resolve_group_bank_charges_rules(data_rows: list, nar_idx: int, rules: List[dict],
                                       itemized_role: dict, itemized_group_dest_row: dict) -> dict:
    """For every group _detect_paired_bank_charges() found, resolves the
    ACTUAL "Bank charges"-style rule from `rules` (the org's own live
    Categorization module data) that the group's charge row(s) should be
    force-categorized to -- 2026-08-13, per Ravindra: "the bank charges
    category for the smaller amount must be from the categorization tables
    Bank charge - EGP like that not just a hard coded string". Categories in
    this codebase carry a per-entity ' - <SUFFIX>' tag (see _category_base()/
    _category_suffix()'s own comments -- "Bank charges - CBK", "Bank charges
    - EGP", etc., one real row per entity in the SAME org's module) -- a
    fixed BANK_CHARGES_CATEGORY constant comparison alone can't tell which
    entity's Bank charges rule is the right one, or even find a suffixed one
    at all if there's no bare "Bank charges" row.

    Resolved PER GROUP, not once for the whole file: classify_rule() runs
    (cheap, pure, no side effects) on the group's own PRIMARY row's
    narration to see what THAT row matched (e.g. "Salary - EGP"), then looks
    for a rule whose _category_base() is BANK_CHARGES_CATEGORY (case-
    insensitive) AND whose _category_suffix() matches the primary's own
    suffix (also case-insensitive; both "" if neither has one) -- so a
    primary matched to the EGP entity pairs with "Bank charges - EGP", not
    some other entity's "Bank charges - CBK" that might also exist in the
    same org's module. Done in this separate pre-pass (not inline in
    categorize_workbook()'s main per-row loop) specifically so it works
    regardless of whether a "charge" row happens to appear BEFORE its
    "primary" row in the sheet -- the main loop processes rows in file
    order, but this resolution needs the primary's own classify_rule()
    result available up front either way.

    Returns {group_dest_row: rule_or_None} -- keyed by the SAME "Itemized
    With" destination row number itemized_group_dest_row already uses for
    every row in that group, so the main loop can look a group's resolved
    rule up directly from a charge row's own itemized_group_dest_row entry.
    None means either the primary's own category has no suffix and no bare
    "Bank charges" rule exists, or MORE than one rule matched the same base+
    suffix combination (ambiguous -- never guessed, same "never guess"
    convention as _detect_paired_bank_charges()'s own tied-max-amount
    guard) -- either way, the caller must NOT force a category text that
    isn't a real row in the org's own Categorization module."""
    primary_offsets = [offset for offset, role in itemized_role.items() if role == "primary"]
    group_rules = {}
    for primary_offset in primary_offsets:
        group_dest_row = itemized_group_dest_row[primary_offset]
        primary_row = data_rows[primary_offset]
        primary_narration = primary_row[nar_idx] if nar_idx is not None and nar_idx < len(primary_row) else None
        primary_matched, _ = classify_rule(primary_narration, rules)
        target_suffix = _category_suffix(primary_matched["category"]).lower()
        candidates = [
            r for r in rules
            if _category_base(r["category"]).strip().lower() == BANK_CHARGES_CATEGORY.lower()
            and _category_suffix(r["category"]).lower() == target_suffix
        ]
        if len(candidates) == 1:
            group_rules[group_dest_row] = candidates[0]
            print(f"[categorize_from_module] _resolve_group_bank_charges_rules: group (primary dest "
                  f"{group_dest_row}, primary matched {primary_matched['category']!r}) -> resolved Bank charges "
                  f"rule {candidates[0]['category']!r}.", flush=True)
        elif len(candidates) > 1:
            group_rules[group_dest_row] = None
            print(f"[categorize_from_module] _resolve_group_bank_charges_rules: WARNING -- group (primary dest "
                  f"{group_dest_row}, primary matched {primary_matched['category']!r}, suffix {target_suffix!r}) "
                  f"has {len(candidates)} Bank charges rules tied for the same suffix "
                  f"({[c['category'] for c in candidates]!r}) -- can't tell which is right, not overriding this "
                  f"group's charge row(s) at all.", flush=True)
        else:
            group_rules[group_dest_row] = None
            print(f"[categorize_from_module] _resolve_group_bank_charges_rules: WARNING -- group (primary dest "
                  f"{group_dest_row}, primary matched {primary_matched['category']!r}, suffix {target_suffix!r}) "
                  f"-- no Bank charges rule found in this org's Categorization module for suffix {target_suffix!r} "
                  f"-- not overriding this group's charge row(s) (leaving their own classify_rule() result "
                  f"standing) rather than writing a category that doesn't really exist in the module.",
                  flush=True)
    return group_rules


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


def _is_duplicate_reference(value) -> bool:
    """True if a result text is specifically a "duplicate detected" NOT
    POSTED note (2026-08-11, per Ravindra's live report that a file
    containing duplicate rows never got marked .COMPLETED even once posting
    was otherwise fully done) -- as opposed to any OTHER "NOT POSTED: ..."
    reason (a missing account, unresolved vendor, bad date, unsupported
    posting type -- all genuine data gaps that SHOULD keep blocking file
    completion, see main.py's /post-transactions) or a real "ERROR: ...".

    posting_service.py's _check_for_duplicate()/_check_duplicate_description()
    both always write this exact literal prefix
    ("NOT POSTED: duplicate detected -- ...") on a match -- this function
    just recognizes it, same "match the exact convention those functions
    write" approach as _is_success_reference() above.

    Why this matters: a duplicate-detected row means the underlying
    transaction ALREADY EXISTS in Zoho under some other reference -- there's
    nothing left to DO for this row (it isn't a data gap waiting on a fix),
    so it shouldn't count as "still needs work" for file-completion purposes
    the way an unresolved error does. Before this function existed,
    /post-transactions' "is this file fully done" check treated a duplicate
    note exactly like any other NOT POSTED reason, which meant a file with
    even one duplicate row could never be marked .COMPLETED -- the check
    kept re-finding the same duplicate every single run, forever."""
    text = str(value).strip() if value is not None else ""
    return text.upper().startswith("NOT POSTED: DUPLICATE DETECTED")


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

    BANK CHARGES / VAT ON BANK CHARGES (2026-08-10, per Ravindra -- see the
    module-level comment block above match_bank_charges_to_vat()): a row
    categorized Bank charges or VAT on bank charges is NEVER included in
    "rows" -- unconditionally, regardless of any "include bank charges"
    setting, since neither category is ever posted as an individual entry.
    Such a row (if not already posted from an earlier consolidation -- see
    below) is instead collected into the new "bank_charge_rows" list, with
    the same shape as a normal postable row (including its already-resolved
    debit_account/credit_account, so a consolidated entry can reuse them
    without re-deriving anything). Counted in the new "bank_charges_excluded"
    stat, kept separate from "no_category" (a row IS categorized here, it's
    just deliberately never posted row-by-row). Once a row has been folded
    into a consolidated posting (main.py's /post-bank-charges writes a real
    Zoho reference into that row's OWN Posted column, same convention as any
    other posting), it's indistinguishable from any other already-posted
    row on the next run -- picked up by the ordinary already-posted check
    above, not re-collected here. A row whose consolidated posting instead
    came back "duplicate detected" (2026-08-2x fix) is ALSO not re-collected
    here -- it's settled the same way a genuine post is, just counted
    separately (see "bank_charges_resolved" below) since it never got its
    own Zoho reference.

    ITEMIZED WITH / EGP paired bank-charge groups (2026-08-13, Phase 2 of
    the Egypt entity feature -- see categorize_workbook()'s own
    "detect_paired_bank_charges" comment block for Phase 1, which writes
    the "Itemized With" column this reads): a row with a non-blank
    "Itemized With" value is handled ENTIRELY SEPARATELY from every path
    described above (no_category/already_posted/bank_charges_excluded/
    "rows") -- it never lands in "rows" as an individual postable row, and
    it's exempt from the bank_charge_rows exclusion too even though its own
    Category is typically "Bank charges - <SUFFIX>" (Phase 1's whole point
    was to force that category onto it) -- checked FIRST, before any of
    those other branches even run, specifically so it can't be swept into
    the unrelated month-end Bank Charges/VAT consolidation path. Every row
    sharing the same "Itemized With" value (the group's primary row's own
    destination Excel row -- see ITEMIZED_WITH_COLUMN_NAME's own comment)
    is collected into ONE group, which ends up in exactly one of three new
    return keys depending on the group's current state:
      "itemized_groups": [...] -- groups ready to post as ONE combined
        itemized Zoho Expense (posting_service.py's new
        post_itemized_expense_group()). Each entry: {"group_key": int (the
        primary's destination row), "members": [{excel_row, category,
        posting_type, debit_account, credit_account, account_number,
        amount, narration, reference_number, txn_date, debit_amount}, ...]}
        -- same per-member field shape as a normal "rows" entry, just
        grouped instead of flat.
      "itemized_groups_already_posted": [...] -- a group where AT LEAST ONE
        member already carries a genuine Zoho reference (from an earlier,
        possibly-interrupted run) -- 2026-08-13, per Ravindra's confirmed
        answer ("Any row posted = whole group treated as posted... write
        that same reference onto any row in the group still missing it, to
        repair the partial state"): the WHOLE group is treated as already
        posted (never re-posted), and every member still missing that
        reference gets it "repaired" onto their own row by main.py. Each
        entry: {"group_key": int, "members": [...], "zoho_reference": str
        (the reference found on whichever member already had one),
        "rows_needing_repair": [excel_row, ...]}.
      "itemized_groups_blocked": [...] -- a group where at least one member
        has no usable category (blank/"Others", including a "charge" row
        Phase 1 couldn't resolve a real Bank charges rule for -- see
        categorize_workbook()'s own comment) or couldn't resolve a Debit/
        Credit Account -- NEVER attempted against Zoho; main.py writes a
        clear "NOT POSTED: ..." reason instead, same retry-until-fixed
        convention as every other data-gap reason in this codebase. Each
        entry: {"group_key": int, "members": [...], "reason": str}.

    Returns {"rows": [...], "header_row": int (0-based), "posted_col":
    int|None (0-based, None if the column doesn't exist yet), "header": list,
    "already_posted": int, "no_category": int, "already_posted_rows": [...],
    "bank_charge_rows": [...], "bank_charges_excluded": int,
    "bank_charges_resolved": int (bank charge/VAT rows settled via a
    "duplicate detected" consolidated posting -- see above),
    "bank_charges_resolved_rows": [...] (same shape as
    "already_posted_rows" but with "note" instead of "zoho_reference"),
    "itemized_groups": [...], "itemized_groups_already_posted": [...],
    "itemized_groups_blocked": [...] (all three described just above)}.
    Each entry in "rows" is a dict: excel_row (1-based sheet row, for writing
    the result back), category, posting_type (from the matched RULE's
    cf_type_of_transaction, NOT the sheet's Posting Type formula cell),
    debit_account, credit_account (from _resolve_row_accounts()),
    account_number (passed through, for telling paid-through apart from
    expense account in posting_service.py), amount (whichever of the
    statement's own Debit/Credit is non-zero), narration, reference_number,
    txn_date. reference_number's SOURCE CHANGED 2026-08-12: now reads the
    PostingReference column categorize_workbook() writes (rule cf_reference
    -> Gemini -> the statement's own raw Reference column, resolved once at
    categorization time -- see POSTING_REFERENCE_COLUMN_NAME's own comment),
    falling back to the statement's raw Reference column directly only for
    a file categorized before this feature existed (no PostingReference
    column present at all). is_vendor_payment_category (bool) and
    debit_amount (2026-08-12, later same day) -- added purely for
    posting_service.py's Vendor Payments RECONCILIATION feature (see
    post_row()'s and _reconcile_vendor_payment()'s own docstrings there):
    is_vendor_payment_category is True iff this row's category (base name,
    suffix stripped via _category_base()) equals VENDOR_PAYMENT_CATEGORY --
    when True, posting_service.py never creates a new Zoho record for this
    row, it only looks up and matches an existing one. debit_amount is this
    row's own raw statement Debit-column value (same value amount would
    hold if this row's direction happens to be "debit" -- kept as its own
    key regardless of direction, since the reconciliation check is always
    against the statement's Debit column specifically, per Ravindra's
    spec, not whichever side amount resolved to).

    "already_posted_rows" (2026-08-09, per Ravindra's "post to Zoho Books"
    feature -- live status + a Delete button against each posting): a row
    that's ALREADY posted (skipped from "rows" above, counted in
    already_posted) is not simply dropped anymore -- enough of it is
    captured here (excel_row, category, narration, amount, zoho_reference)
    for the Post widget to still show and offer a Delete button against it,
    even on a run that posts nothing new. Without this, a postings the
    widget already knows about (from an earlier run) would vanish from the
    table on the next run, and there'd be no way to delete them short of
    re-running the exact posting call that created them."""
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
    # reference_idx (the statement's OWN raw Reference-style column) is now
    # ONLY a fallback -- see posting_reference_idx just below, 2026-08-12.
    reference_idx = find_column(header_lower, REFERENCE_HEADER_ALIASES)
    # PostingReference (2026-08-12, per Ravindra: "I want the creating of
    # the reference to happen while doing the catergorization not while
    # posting ... The actual posting should post the reference value from
    # the new column of the statement") -- SUPERSEDES reference_idx above
    # as the primary source for row_out["reference_number"] below.
    # categorize_workbook() (see POSTING_REFERENCE_COLUMN_NAME's own
    # comment) already resolved each row's reference through its 3-tier
    # lookup (rule cf_reference -> Gemini -> the statement's own raw
    # Reference column) and wrote the result here as a plain value -- this
    # function's job is now just to READ it, not decide it. Optional
    # (`find_column`, exact name match via header_lower.index() below would
    # also work since this is an exact, not alias-based, name -- using
    # find_column for consistency with every other column lookup in this
    # function) -- None only for a file that's never been through
    # categorize_workbook() under this feature (an older file categorized
    # before 2026-08-12, or one that's somehow never had this column
    # written) -- see the fallback logic where row_out is built below.
    posting_reference_idx = find_column(header_lower, {POSTING_REFERENCE_COLUMN_NAME.lower()})
    # Itemized With (2026-08-13, Phase 2 of the Egypt entity paired bank-
    # charge feature -- see ITEMIZED_WITH_COLUMN_NAME's own comment block
    # and this function's docstring update below for the full story). None
    # for a file that's never been through Phase 1's detect_paired_bank_
    # charges=True categorize run -- every row then behaves exactly as
    # before this feature existed.
    itemized_with_idx = find_column(header_lower, {ITEMIZED_WITH_COLUMN_NAME.lower()})

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
    already_posted_rows = []
    no_category = 0
    bank_charge_rows = []
    bank_charges_excluded = 0
    bank_charges_resolved = 0
    bank_charges_resolved_rows = []
    itemized_group_members = {}  # group_key (primary's dest excel row) -> [member dict, ...]
    header_row_excel = header_row_idx + 1  # 1-based Excel row number of the header itself
    for offset, row in enumerate(rows[header_row_idx + 1:]):
        excel_row = header_row_excel + 1 + offset
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue

        category = str(row[category_idx]).strip() if category_idx < len(row) and row[category_idx] is not None else ""

        # Itemized-group row (2026-08-13, Phase 2) -- checked BEFORE the
        # no_category/already_posted/bank_charges branches below, so a
        # group member is NEVER swept into any of those (see this
        # function's own docstring for why -- most importantly, never into
        # the unrelated bank_charge_rows exclusion, even though its Category
        # is typically "Bank charges - <SUFFIX>"). category is read here
        # even if blank/"Others" -- an unresolved member still needs to be
        # collected so the WHOLE group can be evaluated together and
        # reported as one blocked group with a clear reason, rather than
        # silently vanishing (see itemized_group_members's post-loop
        # handling below).
        itemized_with_raw = (row[itemized_with_idx]
                              if itemized_with_idx is not None and itemized_with_idx < len(row) else None)
        itemized_with_text = str(itemized_with_raw).strip() if itemized_with_raw is not None else ""
        itemized_group_key = None
        if itemized_with_text:
            try:
                itemized_group_key = int(float(itemized_with_text))
            except (ValueError, TypeError):
                print(f"[categorize_from_module] extract_postable_rows: row {excel_row} has an unparseable "
                      f"{ITEMIZED_WITH_COLUMN_NAME!r} value {itemized_with_raw!r} -- treating as NOT part of a "
                      f"group (falls through to normal per-row handling below).", flush=True)

        if itemized_group_key is not None:
            posted_val = row[posted_col_idx] if posted_col_idx is not None and posted_col_idx < len(row) else None
            rule = rules_by_category.get(category)
            stmt_debit_val = row[debit_idx] if debit_idx < len(row) else None
            stmt_credit_val = row[credit_idx] if credit_idx < len(row) else None
            debit_account, credit_account = _resolve_row_accounts(rule, stmt_debit_val, stmt_credit_val, account_number)
            amount = stmt_debit_val if _has_amount(stmt_debit_val) else stmt_credit_val
            _posting_reference_val = (row[posting_reference_idx]
                                       if posting_reference_idx is not None and posting_reference_idx < len(row)
                                       else None)
            _posting_reference_text = str(_posting_reference_val).strip() if _posting_reference_val is not None else ""
            if posting_reference_idx is not None:
                member_reference_number = _posting_reference_text or None
            else:
                member_reference_number = (str(row[reference_idx]).strip()
                                            if reference_idx is not None and reference_idx < len(row)
                                            and row[reference_idx] is not None else None)
            member = {
                "excel_row": excel_row,
                "category": category or None,
                "posting_type": (rule.get("type_of_posting") if rule else None) or "",
                "debit_account": debit_account,
                "credit_account": credit_account,
                "account_number": account_number or None,
                "amount": amount,
                "narration": row[nar_idx] if nar_idx is not None and nar_idx < len(row) else None,
                "reference_number": member_reference_number,
                "txn_date": row[date_idx] if date_idx is not None and date_idx < len(row) else None,
                "debit_amount": stmt_debit_val,
                "zoho_reference": str(posted_val).strip() if posted_val is not None and str(posted_val).strip() else None,
                "already_posted": _is_success_reference(posted_val),
            }
            itemized_group_members.setdefault(itemized_group_key, []).append(member)
            continue

        if not category or category.lower() == FALLBACK_CATEGORY.lower():
            no_category += 1
            continue
        posted_val = row[posted_col_idx] if posted_col_idx is not None and posted_col_idx < len(row) else None
        if _is_success_reference(posted_val):
            already_posted += 1
            _stmt_debit_val = row[debit_idx] if debit_idx < len(row) else None
            _stmt_credit_val = row[credit_idx] if credit_idx < len(row) else None
            already_posted_rows.append({
                "excel_row": excel_row,
                "category": category,
                "narration": row[nar_idx] if nar_idx is not None and nar_idx < len(row) else None,
                "amount": _stmt_debit_val if _has_amount(_stmt_debit_val) else _stmt_credit_val,
                "zoho_reference": str(posted_val).strip(),
            })
            continue
        # Bank charges/VAT on bank charges rows whose consolidated posting
        # (main.py's /post-bank-charges) came back "duplicate detected" --
        # 2026-08-2x fix, per Ravindra ("Successful processing of posts
        # whether duplicate or success it should change to COMPLETED as
        # before"). Unlike an ORDINARY row's duplicate note (which is
        # deliberately re-checked every run forever -- see
        # _is_duplicate_reference()'s docstring), a consolidated Bank
        # charges/VAT entry is a SUMMED aggregate across every contributing
        # row in the bucket; re-pooling an already-duplicate-resolved row
        # into a future month's aggregate would change that aggregate's
        # total out from under a check that already ran, so once resolved
        # (this run or an earlier one) it's settled -- excluded from
        # "bank_charge_rows" (never re-pooled) and counted here instead of
        # "bank_charges_excluded", so main.py's /post-transactions
        # completion check (which runs right after /post-bank-charges for
        # the same files -- see that route's docstring) can treat it as
        # resolved for ".COMPLETED" eligibility even though it never got an
        # individual Zoho reference. A genuine failure reason (bad date, tax
        # lookup failure, etc) is NOT covered by this branch -- it's still
        # written into the same cell as its own "NOT POSTED: ..." text, but
        # falls through below into "bank_charge_rows"/"bank_charges_excluded"
        # exactly as before, so it keeps retrying (and keeps blocking
        # completion) every run until whatever's actually wrong is fixed.
        if _category_base(category).lower() in _BANK_VAT_CHARGE_CATEGORIES_LOWER and _is_duplicate_reference(posted_val):
            bank_charges_resolved += 1
            _stmt_debit_val = row[debit_idx] if debit_idx < len(row) else None
            _stmt_credit_val = row[credit_idx] if credit_idx < len(row) else None
            bank_charges_resolved_rows.append({
                "excel_row": excel_row,
                "category": category,
                "narration": row[nar_idx] if nar_idx is not None and nar_idx < len(row) else None,
                "amount": _stmt_debit_val if _has_amount(_stmt_debit_val) else _stmt_credit_val,
                "note": str(posted_val).strip(),
            })
            continue

        rule = rules_by_category.get(category)
        stmt_debit_val = row[debit_idx] if debit_idx < len(row) else None
        stmt_credit_val = row[credit_idx] if credit_idx < len(row) else None
        debit_account, credit_account = _resolve_row_accounts(rule, stmt_debit_val, stmt_credit_val, account_number)
        amount = stmt_debit_val if _has_amount(stmt_debit_val) else stmt_credit_val

        # reference_number (2026-08-12, SOURCE CHANGED): now reads from the
        # PostingReference column categorize_workbook() already resolved
        # (rule cf_reference -> Gemini -> raw statement Reference column --
        # see that function's own comment) -- posting_service.py's post_row()
        # dispatch functions send THIS value as the actual Zoho
        # reference_number field, unchanged from before (they don't know or
        # care where it came from). Falls back to reference_idx (the raw
        # statement column directly, the OLD source) only if
        # posting_reference_idx is None -- i.e. this file was never through
        # categorize_workbook() under this feature at all (an older file
        # categorized before 2026-08-12) -- so an already-categorized file
        # doesn't regress to a blank reference just because it predates this
        # column existing. A file that HAS the column but left a specific
        # row's cell blank (all 3 tiers came up empty for that row) posts
        # with reference_number=None for that row, same as before this
        # feature existed for a row with no raw reference either.
        _posting_reference_val = (
            row[posting_reference_idx]
            if posting_reference_idx is not None and posting_reference_idx < len(row)
            else None
        )
        _posting_reference_text = str(_posting_reference_val).strip() if _posting_reference_val is not None else ""
        if posting_reference_idx is not None:
            reference_number = _posting_reference_text or None
        else:
            reference_number = (str(row[reference_idx]).strip()
                                 if reference_idx is not None and reference_idx < len(row) and row[reference_idx] is not None
                                 else None)

        row_out = {
            "excel_row": excel_row,
            "category": category,
            "posting_type": (rule.get("type_of_posting") if rule else None) or "",
            "debit_account": debit_account,
            "credit_account": credit_account,
            "account_number": account_number or None,
            "amount": amount,
            "narration": row[nar_idx] if nar_idx is not None and nar_idx < len(row) else None,
            "reference_number": reference_number,
            "txn_date": row[date_idx] if date_idx < len(row) else None,
            # "debit" if THIS statement's own Debit column had the amount,
            # "credit" if its Credit column did, else None (both/neither --
            # see the "both"/"neither" tally a few lines below). Added
            # 2026-08-2x for the AI narration-classification feature (per
            # Ravindra's "Bank Narration Extraction Prompt" spec, which
            # takes `direction` as one of its per-row inputs) -- purely
            # additive, nothing existing reads or depends on this key's
            # absence.
            "direction": ("debit" if _has_amount(stmt_debit_val) else
                           "credit" if _has_amount(stmt_credit_val) else None),
            # is_vendor_payment_category / debit_amount (2026-08-12, LATER
            # SAME DAY, per Ravindra: "when posting logic runs, for the
            # category vendor payments, we dont want to post anything but
            # needs to check the following...") -- both added purely for
            # posting_service.py's new Vendor Payment RECONCILIATION
            # feature (see that module's _reconcile_vendor_payment() for
            # the full writeup). Computed HERE, not in posting_service.py,
            # specifically to avoid a circular import: this module already
            # imports generate_ai_reference_preview() FROM posting_service.py
            # (one-directional, see the top-of-file import comment) --
            # posting_service.py importing _category_base()/
            # VENDOR_PAYMENT_CATEGORY back FROM this module would create a
            # cycle. Precomputing the boolean here (where _category_base()
            # and VENDOR_PAYMENT_CATEGORY already live) and just handing it
            # across in the row dict sidesteps that entirely -- same
            # pattern as "direction" just above.
            #
            # is_vendor_payment_category: True if this row's Category base
            # name (entity suffix stripped) is VENDOR_PAYMENT_CATEGORY
            # ("Vendor Payments", plural -- see that constant's own comment)
            # -- posting_service.py's post_row() checks this FIRST, before
            # posting_type, and routes to reconciliation instead of a real
            # create_* call when True, regardless of whatever posting_type
            # this rule happens to have.
            #
            # debit_amount: the statement's OWN raw Debit column value for
            # this row (stmt_debit_val, unparsed) -- deliberately NOT the
            # same as "amount" above (which can come from either the Debit
            # OR Credit column, whichever is non-zero) -- Ravindra's literal
            # wording was "amount in debit column matches the amount in the
            # post", so this is scoped to the Debit column specifically,
            # even though in practice a Vendor Payment row's amount is
            # expected to always be on the Debit side anyway (money leaving
            # the account). A Vendor Payment row whose amount unexpectedly
            # landed in the Credit column instead reports "no usable Debit
            # amount" rather than silently substituting the Credit value.
            "is_vendor_payment_category": _category_base(category).lower() == VENDOR_PAYMENT_CATEGORY.lower(),
            "debit_amount": stmt_debit_val,
        }

        # Bank charges / VAT on bank charges -- excluded from individual
        # posting unconditionally, see this function's docstring and the
        # module-level comment block above match_bank_charges_to_vat().
        # _category_base() strips a per-entity " - CBK"/" - NFT" suffix
        # before comparing -- see that function's docstring for why a plain
        # substring check would be wrong here (would wrongly also match
        # "VAT on bank charges - CBK" against BANK_CHARGES_CATEGORY).
        if _category_base(category).lower() in _BANK_VAT_CHARGE_CATEGORIES_LOWER:
            bank_charges_excluded += 1
            bank_charge_rows.append(row_out)
            continue

        postable.append(row_out)

    # Resolve every itemized_group_members group into exactly one of
    # itemized_groups / itemized_groups_already_posted / itemized_groups_
    # blocked -- see this function's own docstring for the full shape of
    # each. Order of these two checks matters: "already posted" is checked
    # BEFORE "blocked", so a group with a genuine Zoho reference on one
    # member is never blocked just because ANOTHER member's category
    # happens to be unresolved (that member's own row is still repaired
    # with the shared reference below, in main.py -- its category no longer
    # matters once the group's real posting already exists in Zoho).
    itemized_groups = []
    itemized_groups_already_posted = []
    itemized_groups_blocked = []
    for group_key, members in itemized_group_members.items():
        posted_members = [m for m in members if m["already_posted"]]
        if posted_members:
            itemized_groups_already_posted.append({
                "group_key": group_key,
                "members": members,
                "zoho_reference": posted_members[0]["zoho_reference"],
                "rows_needing_repair": [m["excel_row"] for m in members if not m["already_posted"]],
            })
            continue
        unresolved = [m for m in members
                      if not m["category"] or m["category"].lower() == FALLBACK_CATEGORY.lower()
                      or not m["debit_account"] or not m["credit_account"]]

        # The group_key equals the primary member's own excel_row by
        # construction (see _detect_paired_bank_charges() in Phase 1, which
        # points every member -- including the primary itself -- at the
        # primary's destination row). Use that to find the primary member so
        # we can catch a second, more subtle failure mode: Phase 1's
        # override only fires when _resolve_group_bank_charges_rules() finds
        # exactly one Bank-charges rule matching the primary's own entity
        # suffix. If it finds none (or more than one), it leaves the
        # "charge" row's category untouched -- and since that row's
        # narration is IDENTICAL to the primary's, classify_rule() naturally
        # re-matches it to the SAME rule as the primary. The result is a
        # group where every member "looks" resolved (real category, real
        # accounts) but a non-primary row has literally the same category as
        # the primary -- which would post the bank charge as a duplicate
        # line item of the primary's own category instead of as a charge.
        # That's never valid for this feature, so treat it as blocked too.
        primary_member = next((m for m in members if m["excel_row"] == group_key), None)
        unresolved_same_as_primary = []
        if primary_member is not None:
            unresolved_same_as_primary = [
                m for m in members
                if m["excel_row"] != group_key and m["category"] == primary_member["category"]
            ]

        if unresolved or unresolved_same_as_primary:
            unresolved_rows = sorted(set(
                [m["excel_row"] for m in unresolved] +
                [m["excel_row"] for m in unresolved_same_as_primary]
            ))
            reason = (f"NOT POSTED: itemized group blocked -- row(s) {unresolved_rows} in this group have "
                      f"no usable category and/or Debit Account/Credit Account (a 'charge' row Phase 1 "
                      f"couldn't resolve a real Bank charges rule for, or a primary row with no rule match)")
            if unresolved_same_as_primary:
                reason += (f", or a 'charge' row ended up with the exact same category as its primary "
                           f"(row {group_key}) -- meaning no matching Bank-charges rule exists for that "
                           f"category's entity suffix, so Phase 1 left the charge row's own narration-based "
                           f"match in place instead of overriding it")
            reason += " -- fix the Categorization module and re-run /categorize before this group can post."
            itemized_groups_blocked.append({
                "group_key": group_key,
                "members": members,
                "reason": reason,
            })
            continue
        itemized_groups.append({"group_key": group_key, "members": members})

    return {
        "rows": postable,
        "header_row": header_row_idx,
        "posted_col": posted_col_idx,
        "header": header,
        "already_posted": already_posted,
        "no_category": no_category,
        "already_posted_rows": already_posted_rows,
        "bank_charge_rows": bank_charge_rows,
        "bank_charges_excluded": bank_charges_excluded,
        "bank_charges_resolved": bank_charges_resolved,
        "bank_charges_resolved_rows": bank_charges_resolved_rows,
        "itemized_groups": itemized_groups,
        "itemized_groups_already_posted": itemized_groups_already_posted,
        "itemized_groups_blocked": itemized_groups_blocked,
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


def add_supporting_required_column(wb, rules: List[dict]) -> dict:
    """Adds/refreshes the 'Supporting Required' Yes/No column on an
    ALREADY-CATEGORIZED statement -- step (a) of Ravindra's Supporting
    Document Attachment feature: "Extract the cf_supporting_required
    field from categorization and add it as column ... as Yes/No."

    DELIBERATELY APPENDED, NOT INSERTED BEFORE 'Category' (2026-08-12):
    Ravindra's own wording asked for this column positioned immediately
    before Category. That is NOT what this does, on purpose. Debit
    Account/Credit Account/Posting Type are live Excel FORMULAS (see
    categorize_workbook()'s module docstring / _account_formula()/
    _posting_type_formula()) that reference the Category cell by its
    CURRENT column letter (e.g. "F2"). openpyxl's ws.insert_cols() shifts
    cell CONTENT rightward but does not rewrite formula TEXT to match --
    every formula referencing a column at or after the insertion point
    would silently start pointing at the wrong cell on a file that already
    has these formulas baked in. (This is different from
    categorize_workbook() itself, which is safe because it writes those
    formulas fresh, at whatever position they land, every run -- there's
    nothing pre-existing to break.) Every other generated column in this
    codebase (Category, Debit Account, Credit Account, Posting Type,
    PostingReference, Zoho Posting Reference) is ALWAYS appended for
    exactly this reason -- this one follows the same convention rather than
    risking a corrupted sheet. A true before-Category placement would need
    a bigger, separate change (rewriting every formula's cell references
    after insert) -- flagged here rather than silently done differently
    from what was asked.

    Looks up each row's OWN existing Category cell value against `rules`
    (exact match, same {category: rule} dict extract_postable_rows() and
    categorize_workbook() already build) -- this does NOT reclassify
    anything, it only reads the category a row was already assigned and
    checks THAT category's own cf_supporting_required flag. A row whose
    category doesn't match any current rule (e.g. the rule was deleted/
    renamed in Zoho since this file was categorized) gets 'No' rather than
    an error -- same tolerant "can't find a rule -> nothing required"
    default used everywhere else in this module.

    Idempotent -- reuses the column if it already exists (from an earlier
    run of this same widget against this same file) instead of appending a
    duplicate; every row's value is recomputed and overwritten every call,
    same as every other generated column in this codebase, so a category
    whose cf_supporting_required changed in Zoho since the last run is
    picked up correctly rather than staying stuck on a stale Yes/No.

    Returns {"column_index": <1-based Excel column>, "yes_count": int,
    "no_count": int}."""
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
    category_idx = find_column(header_lower, {"category"})
    if category_idx is None:
        raise ValueError(
            "No 'Category' column found -- this file hasn't been through /categorize yet."
        )

    header_row_excel = header_row_idx + 1
    if SUPPORTING_REQUIRED_COLUMN_NAME.lower() in header_lower:
        col_idx0 = header_lower.index(SUPPORTING_REQUIRED_COLUMN_NAME.lower())
    else:
        col_idx0 = len(header)  # 0-based -- next free column, same convention as POSTED_COLUMN_NAME above
        ws.cell(row=header_row_excel, column=col_idx0 + 1, value=SUPPORTING_REQUIRED_COLUMN_NAME)
    col_idx_excel = col_idx0 + 1

    rules_by_category = {r["category"]: r for r in rules}
    yes_count = 0
    no_count = 0
    for offset, row in enumerate(rows[header_row_idx + 1:]):
        excel_row = header_row_excel + 1 + offset
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        category = (str(row[category_idx]).strip()
                    if category_idx < len(row) and row[category_idx] is not None else "")
        if not category or category.lower() == FALLBACK_CATEGORY.lower():
            continue
        rule = rules_by_category.get(category)
        is_required = bool(rule and rule.get("supporting_required"))
        ws.cell(row=excel_row, column=col_idx_excel, value=("Yes" if is_required else "No"))
        if is_required:
            yes_count += 1
        else:
            no_count += 1

    return {"column_index": col_idx_excel, "yes_count": yes_count, "no_count": no_count}


def extract_attachment_check_rows(wb, rules: List[dict]) -> dict:
    """Reads an already-categorized, already-posted (or partially posted)
    statement -- call AFTER add_supporting_required_column() has been run
    on the SAME wb object -- and returns every row ready for the Supporting
    Document Attachment step: steps 1-2 of Ravindra's (scoped-down) spec
    ("scan entries in a completed/posted statement and take only those
    entries where the Supporting Required column ... is Yes").

    A row qualifies only if ALL of:
      1. Its Category is set and isn't 'Others'.
      2. Its Supporting Required cell (written by add_supporting_required_
         column(), called first) is 'Yes'.
      3. Its Zoho Posting Reference column holds a genuine SUCCESS
         reference (_is_success_reference()) -- this feature only ever
         attaches to rows that are ALREADY POSTED; an unposted/failed row
         has nothing in Zoho yet to attach anything to.
      4. Its Attachment Status cell (if that column exists at all, from an
         earlier run of THIS widget) does NOT already start with
         'Attached' -- same idempotent "don't redo settled work" convention
         as every other posted-status column in this codebase. A blank
         status, or a previous 'NOT ATTACHED: .../ERROR: ...' status, IS
         re-included -- same retry-until-fixed convention
         _is_success_reference() already establishes for NOT POSTED/ERROR
         text elsewhere in this module.

    Returns {"rows": [...], "header": [...], "header_row": int (0-based),
    "total_supporting_required": int, "already_attached": int}. Each entry
    in "rows": excel_row, category, narration (used to find the matching
    Zoho record -- see main.py's /attach-supporting-docs docstring),
    reference_number -- the invoice-file search key. SOURCE CHANGED
    2026-08-13, per Ravindra ("it should consider the Transaction reference
    column from banks statement not posting reference column"): now reads
    the statement's OWN RAW reference column (REFERENCE_HEADER_ALIASES --
    e.g. "Transaction Reference"/"Reference"/"Reference Number"/"Cheque
    Number", same alias set extract_postable_rows() falls back to), NOT the
    PostingReference column categorize_workbook() resolves (rule
    cf_reference -> Gemini -> raw statement column). Those two can differ a
    lot: PostingReference's tier-1 source is the matched RULE's own fixed
    cf_reference value (e.g. a rule literally configured with reference
    "Test"), which has nothing to do with any real per-transaction number
    and will never match an actual invoice filename in OneDrive -- only the
    statement's own raw per-row reference genuinely identifies which
    invoice belongs to which row. Falls back to the PostingReference column
    only if the statement has no raw reference-style column at all (rare;
    keeps this from going fully blank on such a file). txn_date,
    debit_amount (statement's own raw Debit-column value, informational
    only), zoho_reference (the posted reference text, e.g. "Expense 12345"
    -- informational; the actual record to attach to is found fresh via
    narration matching, not parsed from this)."""
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

    category_idx = find_column(header_lower, {"category"})
    nar_idx = find_column(header_lower, NARRATION_HEADER_ALIASES)
    debit_idx = find_column(header_lower, DEBIT_HEADER_ALIASES)
    date_idx = find_column(header_lower, DATE_HEADER_ALIASES)
    # reference_idx: the statement's OWN raw Reference-style column -- this
    # is now the PRIMARY source for reference_number below (2026-08-13, per
    # Ravindra -- see this function's own docstring). posting_reference_idx
    # (the resolved/computed PostingReference column) is kept only as a
    # fallback for a statement that has no raw reference column at all.
    reference_idx = find_column(header_lower, REFERENCE_HEADER_ALIASES)
    posting_reference_idx = find_column(header_lower, {POSTING_REFERENCE_COLUMN_NAME.lower()})
    supporting_required_idx = find_column(header_lower, {SUPPORTING_REQUIRED_COLUMN_NAME.lower()})
    attachment_status_idx = find_column(header_lower, {ATTACHMENT_STATUS_COLUMN_NAME.lower()})
    posted_col_idx = (header_lower.index(POSTED_COLUMN_NAME.lower())
                       if POSTED_COLUMN_NAME.lower() in header_lower else None)

    if category_idx is None:
        raise ValueError("No 'Category' column found -- this file hasn't been through /categorize yet.")
    if supporting_required_idx is None:
        raise ValueError(
            f"No {SUPPORTING_REQUIRED_COLUMN_NAME!r} column found -- call add_supporting_required_column() on "
            "this same workbook first."
        )

    rules_by_category = {r["category"]: r for r in rules}  # noqa: F841 -- kept for parity/debuggability, not read below
    header_row_excel = header_row_idx + 1
    postable = []
    total_supporting_required = 0
    already_attached = 0
    for offset, row in enumerate(rows[header_row_idx + 1:]):
        excel_row = header_row_excel + 1 + offset
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        category = (str(row[category_idx]).strip()
                    if category_idx < len(row) and row[category_idx] is not None else "")
        if not category or category.lower() == FALLBACK_CATEGORY.lower():
            continue
        supporting_required_val = (str(row[supporting_required_idx]).strip().lower()
                                    if supporting_required_idx < len(row)
                                    and row[supporting_required_idx] is not None else "")
        if supporting_required_val != "yes":
            continue
        total_supporting_required += 1

        posted_val = row[posted_col_idx] if posted_col_idx is not None and posted_col_idx < len(row) else None
        if not _is_success_reference(posted_val):
            continue  # not posted yet (or failed) -- nothing to attach a document to

        existing_status = (str(row[attachment_status_idx]).strip()
                            if attachment_status_idx is not None and attachment_status_idx < len(row)
                            and row[attachment_status_idx] is not None else "")
        if existing_status.lower().startswith("attached"):
            already_attached += 1
            continue

        # reference_number: raw statement reference column first (see this
        # function's own docstring, 2026-08-13 change), falling back to the
        # resolved PostingReference column only if the statement has no raw
        # reference-style column at all.
        _raw_reference_text = (str(row[reference_idx]).strip()
                                if reference_idx is not None and reference_idx < len(row)
                                and row[reference_idx] is not None else "")
        if reference_idx is not None:
            reference_number = _raw_reference_text or None
        else:
            reference_number = ((str(row[posting_reference_idx]).strip()
                                  if posting_reference_idx is not None and posting_reference_idx < len(row)
                                  and row[posting_reference_idx] is not None else "") or None)

        postable.append({
            "excel_row": excel_row,
            "category": category,
            "narration": row[nar_idx] if nar_idx is not None and nar_idx < len(row) else None,
            "reference_number": reference_number,
            "txn_date": row[date_idx] if date_idx is not None and date_idx < len(row) else None,
            "debit_amount": row[debit_idx] if debit_idx is not None and debit_idx < len(row) else None,
            "zoho_reference": str(posted_val).strip(),
        })

    return {
        "rows": postable,
        "header": header,
        "header_row": header_row_idx,
        "total_supporting_required": total_supporting_required,
        "already_attached": already_attached,
    }


def write_attachment_results(wb, results: dict) -> int:
    """Writes each row's Supporting Document Attachment outcome into the
    'Attachment Status' column (appended if it doesn't already exist --
    same safe append-only convention as write_posting_results()/
    POSTED_COLUMN_NAME above, never inserted, so zero formula risk).
    `results` is {excel_row: status_text, ...}. Returns the 1-based Excel
    column the status ended up in."""
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    header_row_idx = find_header_row(rows) if rows else None
    if header_row_idx is None:
        raise ValueError("Sheet is empty or has no header row.")
    header = list(rows[header_row_idx])
    header_lower = [str(h).strip().lower() if h is not None else "" for h in header]
    header_row_excel = header_row_idx + 1

    if ATTACHMENT_STATUS_COLUMN_NAME.lower() in header_lower:
        col_idx0 = header_lower.index(ATTACHMENT_STATUS_COLUMN_NAME.lower())
    else:
        col_idx0 = len(header)
        ws.cell(row=header_row_excel, column=col_idx0 + 1, value=ATTACHMENT_STATUS_COLUMN_NAME)
    col_idx_excel = col_idx0 + 1

    for excel_row, text in results.items():
        ws.cell(row=excel_row, column=col_idx_excel, value=text)

    return col_idx_excel


def clear_posting_reference(wb, excel_rows: List[int]) -> Optional[int]:
    """Blanks the 'Zoho Posting Reference' cell for each given excel_row
    (1-based sheet row), after that row's posting has actually been deleted
    from Zoho Books (2026-08-09, per Ravindra's "post to Zoho Books" feature
    -- Delete button behavior confirmed: "Delete in Zoho + clear reference").
    Clearing this cell is what makes the row eligible to post again on the
    next run -- extract_postable_rows() only ever skips a row when this
    column holds a genuine SUCCESS reference (see _is_success_reference()); a
    blank cell is indistinguishable from a row that was never posted at all.

    Independent of build_rules()/rules entirely -- unlike extract_postable_
    rows()/write_posting_results(), this doesn't need to know which
    Categorization rule matched each row, since deleting a posting doesn't
    require recomputing anything, just locating the one column and blanking
    specific rows in it. This is deliberate: main.py's /delete-postings can
    call this directly without first fetching rules for whichever
    organization the file's rows happen to belong to.

    Returns the 1-based Excel column number the Posted Reference column was
    found in, or None if this workbook doesn't have that column at all
    (nothing to clear -- not an error, since a row that was never posted has
    no reference to blank in the first place). Writes directly into the
    existing cell (ws.cell(...).value = ""), same as write_posting_results()
    -- leaves every other cell, including any live Debit Account/Credit
    Account/Posting Type FORMULAS, completely untouched."""
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return None
    header_row_idx = find_header_row(rows)
    if header_row_idx is None:
        return None
    header = list(rows[header_row_idx])
    header_lower = [str(h).strip().lower() if h is not None else "" for h in header]
    if POSTED_COLUMN_NAME.lower() not in header_lower:
        return None
    posted_col_excel = header_lower.index(POSTED_COLUMN_NAME.lower()) + 1
    header_row_excel = header_row_idx + 1
    for excel_row in excel_rows:
        if excel_row <= header_row_excel:
            continue  # never touch the header row itself
        ws.cell(row=excel_row, column=posted_col_excel, value="")
    return posted_col_excel