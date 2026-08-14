"""
main.py -- GCP Cloud Run service behind the original Zoho Books widget
(zoho-books-extension/).

BACKGROUND: an earlier iteration of this project (2026-08-01) briefly moved
the trigger to a standalone web dashboard and the rules to a customer-owned
OneDrive workbook, for reselling to customers without Zoho Books Premium.
REMOVED 2026-08-02, per Ravindra ("i want to remove the dashboard logic as i
am not hosting any self maintained dashboard") -- this deployment goes back
to being driven entirely by the Zoho widget: /dashboard and its static
assets are gone, and /categorize's `rules` parameter is required again (no
more silent fallback to an OneDrive Rules workbook) -- the widget always
fetches rules itself from the "Categorization" custom module
(/categorization-rules) and sends them straight through. rules_config.py
and gcp_categorize_service/static/ are now orphaned/unused on disk -- safe
to delete from your deployment folder if you want, nothing here imports
them anymore.

Endpoints:

  GET  /list-files             -- ONE Graph "what's in this folder" call per
                                 run. Callers match account numbers to file
                                 names themselves (client-side), then fire
                                 the per-account /categorize calls in
                                 parallel. Accepts an optional `folder_path`
                                 query param (defaults to
                                 MS_STATEMENTS_FOLDER_PATH) -- this is what
                                 lets a caller point it at a specific
                                 entity's own OneDrive folder (see /entities
                                 below) instead of always reading one fixed
                                 folder.

  GET  /entities                -- LEGACY, superseded 2026-08-01 (later) by
                                 /organizations + /org-folder-map below, once
                                 a single Zoho login was confirmed live to
                                 see multiple real orgs -- kept only for
                                 back-compat, the widget no longer calls it.
                                 Originally: returned every configured
                                 entity/org (Qatar, Saudi, EGP, ...) and the
                                 OneDrive folder its statements live in, read
                                 from a small "Entities" custom module in CBK
                                 (ENTITIES_MODULE_API_NAME, default
                                 "cm_entities").

  GET  /organizations           -- (2026-08-01, later, per Ravindra) returns
                                 every real Zoho Books organization this
                                 deployment's login can see (GET
                                 /organizations against Zoho itself, no
                                 custom module involved) -- confirmed live
                                 that one Self Client login sees more than
                                 one org. Backs the widget's "which
                                 organization" popup with live data instead
                                 of a hand-maintained entity list.

  GET  /org-folder-map          -- (2026-08-01, later, per Ravindra) returns
                                 the org_id -> OneDrive-folder-path mapping
                                 from the cm_orgidonedrivemap custom module
                                 in CBK (ORG_FOLDER_MODULE_API_NAME) -- pairs
                                 with /organizations: the widget can only
                                 start a run for an org that ALSO has a
                                 folder mapped here.

  POST /categorize             -- given an EXACT file_name (no listing/
                                 searching here at all -- downloads it
                                 directly via Graph's path addressing), runs
                                 the keyword-matching categorization pass
                                 (categorize_from_module.categorize_workbook
                                 -- Category, Debit Account, Credit Account,
                                 Posting Type, PostingReference columns) and
                                 re-uploads the result to OneDrive,
                                 overwriting the original file in place. Safe
                                 to re-run on the same file -- Category/Debit
                                 Account/Credit Account/Posting Type are
                                 replaced fresh every run; PostingReference is
                                 STICKY instead (a row that already has one
                                 keeps it -- see POSTING_REFERENCE_COLUMN_NAME
                                 in categorize_from_module.py). `rules` in the
                                 request body is REQUIRED (the widget fetches
                                 them itself via /categorization-rules below
                                 and sends them straight through). Also builds
                                 a Gemini client + fetches the AI reference
                                 prompt (2026-08-12, LATER SAME DAY, per
                                 Ravindra: "I want the creating of the
                                 reference to happen while doing the
                                 catergorization not while posting") -- see
                                 categorize_workbook()'s own docstring for the
                                 3-tier PostingReference lookup this feeds,
                                 and posting_service.py's "AI REFERENCE --
                                 MOVED TO CATEGORIZATION TIME" module-level
                                 section header for why this moved here from
                                 /post-transactions. After saving, runs a
                                 headless LibreOffice recalculation pass
                                 (_recalculate_formulas(), 2026-08-02) so
                                 Debit Account/Credit Account/Posting Type's
                                 live formulas show a real value in any viewer
                                 immediately, not just once someone opens the
                                 file for real in Excel -- non-fatal if it
                                 fails (the formulas are still correct either
                                 way; requires libreoffice-calc in this
                                 service's container, see Dockerfile/
                                 DEPLOY.md).

  GET  /categorization-rules    -- reads Zoho's "Categorization" custom
                                 module and returns only the records that
                                 apply to a specific organization -- this is
                                 where the widget gets the `rules` it passes
                                 to /categorize. Requires BOTH
                                 `module_api_name` and `organization_id`
                                 query params (2026-08-06, per Ravindra:
                                 categorization rules can differ per
                                 organization now, via a new mandatory
                                 `cf_entity` field on every Categorization
                                 record -- see CATEGORIZATION_ENTITY_FIELD
                                 below). A record whose cf_entity doesn't
                                 match the requested organization_id is
                                 filtered out server-side, so the widget
                                 only ever sees -- and only ever sends to
                                 /categorize -- the rules meant for the
                                 organization actually being processed.

  GET  /bank-accounts           -- ONE call per run (like /list-files) that
                                 returns every real account in the org via
                                 Zoho's native Banking module (account_id +
                                 account_number -- a standard, every-plan-
                                 including-Free endpoint, not a custom
                                 module), filtered to only accounts that
                                 actually have an account_id AND whose
                                 account_type is "bank" (2026-08-02, per
                                 Ravindra: "i want only the bank accounts
                                 with the type Bank only" -- excludes Cash-
                                 type accounts like Petty Cash/Undeposited
                                 Funds that the Banking module also lists
                                 alongside real bank accounts). Used to
                                 resolve each statement's own bank account to
                                 its real Zoho account_id and to match it
                                 against OneDrive files by account number.
                                 Accepts an optional `organization_id` query
                                 param (2026-08-01, later) to list a
                                 DIFFERENT org's accounts than this
                                 deployment's own default -- see
                                 /organizations above.

  POST /update-status           -- sets a field (e.g. cf_status, cf_date,
                                 cf_entity_org, cf_bank_accounts) on one Zoho
                                 Books custom module record -- how the widget
                                 reports a run's outcome back onto the
                                 "Bank Transaction Testing" record.

  POST /post-transactions       -- RESTORED 2026-08-09, per Ravindra: "i am
                                 done with the categorization now i want to
                                 post these entries to books based on the
                                 posting type column of the statement."
                                 Downloads an already-categorized file (same
                                 file_name/folder_path shape as /categorize),
                                 extracts every postable row
                                 (categorize_from_module.extract_postable_rows,
                                 unchanged), posts each one to Zoho Books
                                 (posting_service.post_rows, NEW --
                                 organization_id threaded through so the Post
                                 widget's own org picker, independent from
                                 any prior categorize run, controls which org
                                 gets posted into), writes the outcome back
                                 into the "Zoho Posting Reference" column
                                 (write_posting_results, unchanged), and
                                 re-uploads. Idempotent: a row with a genuine
                                 success reference already in that column is
                                 silently skipped (see extract_postable_rows'
                                 own docstring) -- safe to re-run/click more
                                 than once. Returns per-row detail (for the
                                 widget's live status table) plus counts.
                                 `rules` in the body is REQUIRED, same reason
                                 as /categorize -- the accounts/posting type
                                 for each row come from the SAME rules the
                                 statement was categorized with.

  POST /delete-postings         -- NEW 2026-08-09, per Ravindra's confirmed
                                 Delete-button behavior ("Delete in Zoho +
                                 clear reference (Recommended)"): given a
                                 list of {file_name, folder_path, excel_row,
                                 zoho_reference} items (one row's posting per
                                 item -- both a single row's Delete button and
                                 the widget's "Delete All" button send this
                                 same shape, just with 1 item vs many),
                                 deletes each one from Zoho Books
                                 (posting_service.delete_posting) and, only on
                                 success, blanks that row's "Zoho Posting
                                 Reference" cell (categorize_from_module.
                                 clear_posting_reference) so it becomes
                                 eligible to post again. Items are grouped by
                                 file so each file is downloaded/re-uploaded
                                 ONCE regardless of how many of its rows are
                                 being deleted in the same call -- important
                                 for "Delete All", which can span every file
                                 from a whole posting run. One item's failure
                                 (Zoho rejects the delete, or the file can't
                                 be reached) doesn't abort the rest -- every
                                 item gets its own per-item success/failure in
                                 the response.

Auth: a shared-secret header (X-Api-Key), checked against CATEGORIZE_API_KEY,
on every endpoint below. This is deliberately simple, not a full OAuth flow --
give the key out like a password, and rotate CATEGORIZE_API_KEY if it's ever
shared more broadly than intended.

Local run:
    pip install -r requirements.txt
    export MS_TENANT_ID=... MS_CLIENT_ID=... MS_CLIENT_SECRET=... MS_DRIVE_ID=...
    export ZOHO_CLIENT_ID=... ZOHO_CLIENT_SECRET=... ZOHO_REFRESH_TOKEN=... ZOHO_ORGANIZATION_ID=...
    export CATEGORIZE_API_KEY=some-shared-secret
    python main.py
Then hit these endpoints from the Zoho widget (zoho-books-extension/), not a browser page.

Deploy: see DEPLOY.md in this folder.
"""
import calendar
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from typing import Optional

import openpyxl
from flask import Flask, jsonify, request, Response, stream_with_context

import posting_service
from categorize_from_module import (
    build_rules,
    categorize_workbook,
    clear_posting_reference,
    extract_postable_rows,
    write_posting_results,
    _is_success_reference,
    _is_duplicate_reference,
    # Bank Charges/VAT consolidation (2026-08-10) -- see
    # categorize_from_module.py's module-level comment block above
    # match_bank_charges_to_vat() and posting_service.py's module docstring
    # for the full feature. Only used by the new /post-bank-charges route
    # below.
    match_bank_charges_to_vat,
    BANK_CHARGES_CATEGORY,
    # 2026-08-1x, per Ravindra: cf_categorisation values now carry a
    # per-entity suffix ("Bank charges - CBK", "Bank charges - NFT", ...) --
    # see categorize_from_module.py's _category_base() docstring for why
    # this route's own bank_rows_found count below needs the same
    # suffix-stripping comparison extract_postable_rows()/
    # match_bank_charges_to_vat() already use internally.
    _category_base,
    # Supporting Document Attachment (2026-08-12, later same day; scoped
    # down the same day -- see the module-level comment above
    # SUPPORTING_REQUIRED_COLUMN_NAME there, and the /attach-supporting-docs
    # route below, for the full feature.
    add_supporting_required_column,
    extract_attachment_check_rows,
    write_attachment_results,
)

# Bank Charges/VAT consolidation tax_treatment values (2026-08-11 REWORK --
# the first version of these two constants was the literal strings Ravindra
# described in words, "VAT Registered Standard 5%" / "Out of Scope", sent
# straight as tax_treatment. Zoho rejected BOTH live with
# `{"code":2,"message":"Invalid value passed for tax_treatment"}`.
# Ravindra then sent screenshots of his org's actual Expense form, which
# showed the real shape: Tax Treatment and Tax (the rate) are TWO SEPARATE
# fields, and Tax Treatment is a short snake_case enum -- see
# zoho_books_client.py's create_expense() docstring for the full corrected
# writeup, including the still-UNVERIFIED-LIVE flag on "out_of_scope"'s
# exact spelling. The tax RATE ("Standard Rate [5%]") is no longer a
# constant here at all -- it's resolved live via ZohoBooksClient.find_tax_id()
# in the /post-bank-charges route below, since a tax_id is an opaque
# per-organization GUID this codebase can't know in advance.
_TAX_TREATMENT_VAT_MATCHED = "vat_registered"
_TAX_TREATMENT_NON_VAT = "out_of_scope"

# The tax RATE to apply to the VAT-matched "Bank Charges - VAT" consolidated
# entry, resolved live via ZohoBooksClient.find_tax_id(name_contains=...,
# percentage=...) rather than a hardcoded tax_id -- matches Ravindra's
# screenshot showing "Standard Rate [5%]" in his org's Tax dropdown.
# Configurable via env var in case the exact rate name differs in another
# org this same deployment might serve later.
_VAT_MATCHED_TAX_RATE_NAME = os.environ.get("BANK_CHARGES_VAT_TAX_RATE_NAME", "Standard Rate")
_VAT_MATCHED_TAX_RATE_PERCENTAGE = float(os.environ.get("BANK_CHARGES_VAT_TAX_RATE_PERCENTAGE", "5"))

# ---------------------------------------------------------------------------
# SUPPORTING DOCUMENT ATTACHMENT (2026-08-12, later same day -- SCOPE CUT
# 2026-08-12, still later, per Ravindra: "i want to park the document
# extraction and validation aside and want only to attach the required
# document to the expense entry ... Please discard all the previous changes
# made for validation and gemini pdf doc reading. Make only the changes i
# mentioned in this."). See /attach-supporting-docs below for the full
# route and its module-level comment block for the discarded-vs-kept
# history; nothing from the earlier Gemini/VAT-correction design (which
# used to live here as EXPECTED_INVOICE_BILL_TO_NAME/SUPPORTING_DOC_
# AMOUNT_TOLERANCE) is needed any more -- this feature no longer reads or
# compares invoice content at all, it only finds the right Zoho record (by
# narration-vs-notes/description match) and attaches a file to it.

from onedrive_client import OneDriveClient, OneDriveConfig, completed_file_name, is_marked_complete
from zoho_books_client import ZohoBooksClient, ZohoConfig
from gemini_client import GeminiClient, GeminiConfig
import reconciliation_service

# AI reference-number substitution -- NOW LIVE (2026-08-12, per Ravindra:
# "i want to now post these reference no to the respective entries not just
# preview"). Originally DRY RUN ONLY (2026-08-2x, per Ravindra: "I am
# planning to fill the reference number for an entry(All types) while
# posting by running its narration thru a gemini AI prompt ... As of now
# please give me the prompt response ... which i will inspect") -- he
# reviewed enough of those previews (plus a reasoning-leak bug found and
# fixed the same day it went live -- see posting_service.py's
# _clean_suggested_narration()/_looks_like_reasoning_leak()) to ask for it
# to actually apply now. The prompt TEXT itself still lives in Zoho, on the
# "cm_referenceaiprompt" custom module's cf_ai_prompt field -- same "config
# lives in a Zoho custom module, not hardcoded here" convention as
# ENTITIES_MODULE_API_NAME/ORG_FOLDER_MODULE_API_NAME above. See
# _fetch_ai_prompt_template() and /post-transactions below for where this
# is actually used, and posting_service.post_rows_streaming()'s docstring
# for exactly when/how the AI suggestion overwrites row["reference_number"]
# before a row posts (and the fallback behavior when it can't).
REFERENCE_AI_PROMPT_MODULE_API_NAME = os.environ.get("REFERENCE_AI_PROMPT_MODULE_API_NAME", "cm_referenceaiprompt")
REFERENCE_AI_PROMPT_FIELD = os.environ.get("REFERENCE_AI_PROMPT_FIELD", "cf_ai_prompt")

# CLASSIFICATION CONTEXT for the AI feature above (2026-08-2x, SAME DAY,
# once Ravindra sent his actual "Bank Narration Extraction Prompt — v1"
# spec): that prompt's CLASSIFICATION ORDER steps 1-3 (own-account transfer /
# intercompany transfer / loan provider) each match the transaction's
# counterparty against one of these three lists -- see
# posting_service._build_ai_classification_input()'s docstring for the full
# "Inputs to pass with each call" shape.
#
# related_entities / loan_providers are seeded EXACTLY from Ravindra's own
# message -- NOT guessed: "loan_providers ... currently: WeFi, TCS/PNC, NBF"
# and "sister/group companies like CBK Gulf, CBK Egypt, Knights for Telecom
# and Information" (his own examples of is_same_legal_entity=false entities).
# Overridable via env var (a JSON array) without a code change once Ravindra
# has the FULL real lists (his spec's own related_entities shape also wants
# `aliases` per entity, which aren't populated here yet -- blank for now).
#
# own_accounts is NOT a constant here -- built LIVE per request in
# _build_ai_classification_context() below, from ZohoBooksClient.
# list_bank_accounts() (real account_number/account_name for whichever
# organization is currently being posted to). bank name/IBAN/currency are
# UNVERIFIED -- this codebase has never needed those specific fields off
# Zoho's own /bankaccounts response before (only account_id/account_name/
# account_number, see GET /bank-accounts above), so they're left blank
# rather than guessed at a field name that might be wrong and silently feed
# the model bad data. Also only covers ONE organization's own accounts per
# call (whichever org this run is posting to), not every CBK entity's
# accounts at once -- true cross-entity "own account" matching (Ravindra's
# spec: "accounts under the *same* legal entity at a different bank") needs
# every relevant org's bank accounts pooled together, which isn't wired up
# yet either.
RELATED_ENTITIES_JSON = os.environ.get("RELATED_ENTITIES_JSON") or json.dumps([
    {"entity_name": "CBK Gulf", "aliases": [], "is_same_legal_entity": False},
    {"entity_name": "CBK Egypt", "aliases": [], "is_same_legal_entity": False},
    {"entity_name": "Knights for Telecom and Information", "aliases": [], "is_same_legal_entity": False},
])
LOAN_PROVIDERS_JSON = os.environ.get("LOAN_PROVIDERS_JSON") or json.dumps(["WeFi", "TCS/PNC", "NBF"])

# RESTORED 2026-08-09, per Ravindra ("i am done with the categorization now i
# want to post these entries to books..."). Posting logic was deliberately
# UNWIRED from this file on 2026-08-02 ("i dont want any posting logic as of
# now ... i will use it later") after a real deploy crashed on startup with
# "ImportError: cannot import name 'extract_postable_rows' from
# 'categorize_from_module'" (a version mismatch between an older deployed
# categorize_from_module.py and this file's imports -- same class of bug as
# the July 23 stale-deploy issue). That risk doesn't apply this time: this
# same change updates categorize_from_module.py (adds
# clear_posting_reference(), already_posted_rows) in lockstep with this
# file, so both are always the same version together -- see /post-transactions
# and /delete-postings below for the restored + new routes.

app = Flask(__name__)

API_KEY = os.environ.get("CATEGORIZE_API_KEY")  # shared secret the widget must send back
DEFAULT_FOLDER_PATH = os.environ.get("MS_STATEMENTS_FOLDER_PATH", "BankStatements/Dubai")

# The "Entities" custom module in CBK (2026-08-01, per Ravindra) -- one
# record per legal entity/org (Qatar, Saudi, EGP, ...), holding which
# OneDrive folder that entity's bank statements live in. This is what lets
# the "Start Categorizing" button pick the right folder instead of always
# reading MS_STATEMENTS_FOLDER_PATH -- see GET /entities below and its
# docstring for the exact field shape and for why organization_id is
# carried through even though nothing posts cross-organization yet.
#
# SUPERSEDED (2026-08-01, later): once GET /organizations (below) confirmed a
# single Zoho login can see multiple real orgs live, Ravindra built a
# simpler replacement module -- cm_orgidonedrivemap (ORG_FOLDER_* below) --
# that only needs an org_id -> OneDrive-folder mapping, since the entity's
# NAME now comes straight from Zoho's own live org list instead of being
# typed into a separate field. /entities and cm_entities are left in place,
# untouched, purely for back-compat with anything already pointed at them --
# the widget itself no longer calls /entities.
ENTITIES_MODULE_API_NAME = os.environ.get("ENTITIES_MODULE_API_NAME", "cm_entities")

# The "org id -> OneDrive folder" custom module in CBK (cm_orgidonedrivemap,
# 2026-08-01, per Ravindra) -- one record per Zoho Books organization this
# deployment's login can see (per GET /organizations), holding which
# OneDrive folder THAT org's bank statements live in. Two fields, both
# plain text, API names as Ravindra actually created them in Zoho (NOT
# necessarily matching this module's own name):
#   - ORG_FOLDER_ORGID_FIELD  -- the organization_id (as a string, e.g.
#     "861870866") this record's folder path applies to.
#   - ORG_FOLDER_PATH_FIELD   -- the OneDrive folder path for that org's
#     statements (e.g. "BankStatements/Qatar").
# See GET /org-folder-map below.
ORG_FOLDER_MODULE_API_NAME = os.environ.get("ORG_FOLDER_MODULE_API_NAME", "cm_orgidonedrivemap")
ORG_FOLDER_ORGID_FIELD = os.environ.get("ORG_FOLDER_ORGID_FIELD", "cf_orgidonedrivemap")
ORG_FOLDER_PATH_FIELD = os.environ.get("ORG_FOLDER_PATH_FIELD", "cf_one_drive_folder")

# /bank-accounts (2026-08-02, per Ravindra) -- account_type value(s) that
# count as a "real bank account" for the widget's checkbox list, excluding
# Cash/Credit-Card-type entries that Zoho's Banking module (GET /bankaccounts)
# also returns alongside them. Configurable via env var (comma-separated) in
# case Zoho's actual raw value turns out not to be lowercase "bank" once
# checked live -- see /bank-accounts' docstring comment for how to verify.
BANK_ACCOUNT_TYPE_VALUES = {
    v.strip().lower()
    for v in os.environ.get("BANK_ACCOUNT_TYPE_VALUES", "bank").split(",")
    if v.strip()
}

# /categorization-rules (2026-08-06, per Ravindra: "categorization rules can
# be different for different orgs ... instead of loading all the rules now
# we have to load rules of the initially selected orgid from the categorize
# module") -- the field on each cm_categorisation record that says which
# organization_id that rule applies to. Ravindra confirmed this field is
# MANDATORY on every record (there is no "leave it blank to apply to every
# org" case, unlike e.g. the old config_loader.py's bank_id="ALL") -- so
# filtering is a plain exact-match against organization_id, and a record
# with a blank/missing cf_entity is simply excluded (never matches any real
# organization_id) rather than treated as a wildcard. Configurable via env
# var in case the field's real API name ever needs to change without a
# redeploy of this constant.
CATEGORIZATION_ENTITY_FIELD = os.environ.get("CATEGORIZATION_ENTITY_FIELD", "cf_entity")

# Real shape of cf_entity confirmed live (2026-08-06, screenshot from
# Ravindra): it's a lookup-style dropdown built off Zoho's own organization
# list, and its stored value renders as "<organization_id> - <organization
# name>" (e.g. "861870866 - Cyber Knight Gulf LLC"), NOT a bare
# organization_id string as originally assumed when this field was first
# added. A plain exact-match against organization_id would therefore never
# match anything. _extract_org_id_from_cf_entity() pulls just the leading
# digit run out of whatever raw value Zoho returns, so the comparison works
# against the real data shape -- and still works unchanged for a record
# whose cf_entity genuinely IS just a bare organization_id (the leading-digit
# extraction is a no-op in that case), so this isn't a breaking change for
# any record set up either way.
_ENTITY_LEADING_ID_RE = re.compile(r"^\s*(\d+)")


def _extract_org_id_from_cf_entity(raw_value):
    """Extracts the organization_id portion out of a cf_entity value.

    Handles the confirmed-live "<org_id> - <org name>" dropdown format by
    taking the leading run of digits. Falls back to the raw (stripped) value
    itself if it doesn't start with digits at all, so a record with some
    other/unexpected shape doesn't just silently disappear -- it'll compare
    (and fail to match, visibly, via the debug log below) rather than being
    mistaken for a blank one.
    """
    if raw_value is None:
        return ""
    text = str(raw_value).strip()
    if not text:
        return ""
    m = _ENTITY_LEADING_ID_RE.match(text)
    if m:
        return m.group(1)
    return text


def _fetch_ai_prompt_template(zoho_client) -> Optional[str]:
    """Reads cf_ai_prompt off the cm_referenceaiprompt custom module -- the
    prompt TEXT Ravindra maintains directly in Zoho (see this feature's
    module-level comment above REFERENCE_AI_PROMPT_MODULE_API_NAME) for the
    AI reference-number preview. Unlike the Categorization module, this one
    is NOT expected to be scoped per-entity (no cf_entity-style filter) --
    it's a single shared prompt used for every row regardless of which
    organization/entity is being posted to, same as ENTITIES_MODULE_API_NAME/
    ORG_FOLDER_MODULE_API_NAME's records live in one shared "CBK" org.

    Returns the prompt text, or None if it couldn't be read/found for ANY
    reason (module missing, field blank, Zoho API error, ...) -- never
    raises. Every caller treats None as "AI preview unavailable this run",
    never as a reason to fail the whole posting request; this is a DRY-RUN
    preview feature, not something real posting depends on.

    2026-08-11, REVISED after a live report from Ravindra ("all this was
    there when i run the code still i got that error" -- the "blank/missing
    field" log line -- even after he'd confirmed, via a screenshot, that a
    record with real prompt text existed in the module): the ORIGINAL
    version below always used records[0] -- the FIRST record Zoho's list API
    returns -- and only ever warned, never actually looked, if more than one
    record existed. Fixed to scan every record for a non-blank field instead
    -- but a FOLLOW-UP live report showed that fix alone wasn't enough: with
    exactly ONE record in the module, the log still showed the field as
    0 chars via the LIST response.

    2026-08-11, REVISED AGAIN, same day: this module's cf_ai_prompt is a
    long "Text Box (Multi-line)" field -- the exact same category of field
    (a long free-text field) that was already found, earlier this same
    session, to be silently omitted from Zoho's LIST responses for Journal/
    Expense/Vendor Payment/Bank Transaction (their "notes"/"description"
    fields -- see get_journal()'s docstring in zoho_books_client.py for that
    earlier incident). Applying the same fix here: when the LIST response's
    field comes back blank for every record, this now falls back to a full
    single-record GET per record (get_custom_module_record(), same file) --
    the record's own detail endpoint may return the long field's real value
    even where the list endpoint didn't. This part is UNVERIFIED LIVE (see
    get_custom_module_record()'s docstring for exactly what's guessed vs.
    confirmed) -- if it's still wrong, the diagnostic logging at the very
    end of this function (every record's raw available keys, plus any key
    that merely CONTAINS "prompt") gives enough to build an exact fix next
    without guessing a fourth time.

    2026-08-11, REVISED YET AGAIN, same day: a live detail-GET response
    (once the id-resolution fix above let it actually run) came back with
    an entirely different shape than expected -- layout/approval/sharing
    metadata (approvers_list, blueprint_field_id, layout, module_fields,
    ...) with NO cf_ai_prompt at the top level. `module_fields` looks like
    where the actual field data lives on a detail response -- see
    _extract_field_value() below, which now also checks there (several
    plausible shapes) instead of only a flat top-level lookup.

    Returns the prompt text, or None if it couldn't be read/found for ANY
    reason -- never raises. Every caller treats None as "AI preview
    unavailable this run", never as a reason to fail the whole posting
    request; this is a DRY-RUN preview feature, not something real posting
    depends on."""
    try:
        records = zoho_client.list_custom_module_records(REFERENCE_AI_PROMPT_MODULE_API_NAME)
    except Exception as e:
        print(f"[main] _fetch_ai_prompt_template: couldn't read module "
              f"{REFERENCE_AI_PROMPT_MODULE_API_NAME!r} -> {e}", flush=True)
        return None
    if not records:
        print(f"[main] _fetch_ai_prompt_template: module {REFERENCE_AI_PROMPT_MODULE_API_NAME!r} has no "
              f"records -- AI reference preview unavailable this run.", flush=True)
        return None

    def _record_label(rec):
        # "record_name" CONFIRMED live 2026-08-11 (Ravindra pasted a raw
        # keys dump showing exactly this key, holding "MyPrompt") -- tried
        # first. The rest are speculative fallbacks kept in case a
        # differently-shaped module ever lands here. Only ever used for
        # logging, never for matching logic, so a wrong guess here just
        # makes one log line less readable -- it can't cause a wrong record
        # to be selected.
        for key in ("record_name", "name", "Name", REFERENCE_AI_PROMPT_MODULE_API_NAME + "_name"):
            val = rec.get(key)
            if val:
                return str(val)
        return "?"

    def _resolve_record_id(rec):
        # "module_record_id" CONFIRMED live 2026-08-11 (same raw-keys dump
        # as _record_label() above) -- tried first; this is what actually
        # lets the get_custom_module_record() detail-GET fallback below run
        # at all (it silently couldn't find an id before this fix, since
        # the original guesses -- record_id/id -- don't exist on a real
        # record). The rest are kept as speculative fallbacks only.
        for key in ("module_record_id", "record_id", "id", REFERENCE_AI_PROMPT_MODULE_API_NAME + "_id"):
            val = rec.get(key)
            if val:
                return str(val)
        return None

    def _extract_field_value(detail, field_api_name):
        # 2026-08-11, added after a live detail-GET response (record
        # '2122051000102832150') came back with NEITHER field_api_name NOR
        # REFERENCE_AI_PROMPT_FIELD at the top level -- instead a
        # 'module_fields' key sits alongside layout/approval/sharing
        # metadata (approvers_list, blueprint_field_id, layout_details,
        # etc.), strongly suggesting the actual field DATA lives nested
        # under there rather than flat on the response, unlike every other
        # get_*() single-record method in zoho_books_client.py (Journal/
        # Expense/...) where the field sits flat on the unwrapped object.
        # UNVERIFIED LIVE: module_fields' own shape (a dict keyed by field
        # api name? a list of {api_name/field_name, value} objects?) isn't
        # confirmed -- this tries the plausible shapes and returns the
        # first non-blank match. Returns None if nothing matches, with the
        # caller logging module_fields' own real shape so a next attempt,
        # if needed, is exact rather than another guess.
        detail = detail or {}
        val = str(detail.get(field_api_name) or "").strip()
        if val:
            return val
        module_fields = detail.get("module_fields")
        if isinstance(module_fields, dict):
            val = str(module_fields.get(field_api_name) or "").strip()
            if val:
                return val
        elif isinstance(module_fields, list):
            for item in module_fields:
                if not isinstance(item, dict):
                    continue
                candidate_name = (item.get("api_name") or item.get("field_api_name")
                                  or item.get("field_name") or item.get("name"))
                if candidate_name == field_api_name:
                    val = str(item.get("value") or item.get("field_value") or item.get("data") or "").strip()
                    if val:
                        return val
        return None

    non_blank = [(rec, str(rec.get(REFERENCE_AI_PROMPT_FIELD) or "").strip()) for rec in records]
    if len(records) > 1:
        summary = ", ".join(f"{_record_label(rec)}={len(text)} chars" for rec, text in non_blank)
        print(f"[main] _fetch_ai_prompt_template: module {REFERENCE_AI_PROMPT_MODULE_API_NAME!r} has "
              f"{len(records)} records ({summary}) -- expected exactly 1 (the prompt). Using the first "
              f"one with a non-blank {REFERENCE_AI_PROMPT_FIELD!r} field; consider cleaning up the extras "
              f"in Zoho if this wasn't intentional.", flush=True)

    chosen = next(((rec, text) for rec, text in non_blank if text), None)
    if chosen is not None:
        rec, prompt_text = chosen
        print(f"[main] _fetch_ai_prompt_template: using record {_record_label(rec)!r} (from the list "
              f"response) -- prompt is {len(prompt_text)} chars.", flush=True)
        return prompt_text

    # Nothing in the LIST response had a non-blank field -- try a full
    # single-record GET per record (see this function's docstring +
    # get_custom_module_record()'s docstring for why).
    for rec in records:
        record_id = _resolve_record_id(rec)
        if not record_id:
            print(f"[main] _fetch_ai_prompt_template: record {_record_label(rec)!r} has a blank "
                  f"{REFERENCE_AI_PROMPT_FIELD!r} in the list response, and no id field found to try a "
                  f"detail GET (checked record_id/id/{REFERENCE_AI_PROMPT_MODULE_API_NAME + '_id'!r}) -- "
                  f"skipping.", flush=True)
            continue
        try:
            detail = zoho_client.get_custom_module_record(REFERENCE_AI_PROMPT_MODULE_API_NAME, record_id)
        except Exception as e:
            print(f"[main] _fetch_ai_prompt_template: detail GET for record {record_id!r} failed -> {e}",
                  flush=True)
            continue
        detail_text = _extract_field_value(detail, REFERENCE_AI_PROMPT_FIELD)
        if detail_text:
            print(f"[main] _fetch_ai_prompt_template: record {record_id!r}'s {REFERENCE_AI_PROMPT_FIELD!r} "
                  f"was blank/absent in the list response but {len(detail_text)} chars via a full "
                  f"single-record GET -- using that.", flush=True)
            return detail_text
        module_fields = (detail or {}).get("module_fields")
        if isinstance(module_fields, dict):
            module_fields_shape = f"dict, keys: {sorted(module_fields.keys())}"
        elif isinstance(module_fields, list):
            first_item_keys = sorted(module_fields[0].keys()) if module_fields and isinstance(module_fields[0], dict) else None
            module_fields_shape = f"list of {len(module_fields)}, first item keys: {first_item_keys}"
        else:
            module_fields_shape = f"{type(module_fields).__name__} ({module_fields!r})" if module_fields is not None else "(not present)"
        print(f"[main] _fetch_ai_prompt_template: record {record_id!r}'s {REFERENCE_AI_PROMPT_FIELD!r} is "
              f"ALSO blank via a full single-record GET, not just the list response -- detail response "
              f"keys: {sorted((detail or {}).keys())} -- module_fields shape: {module_fields_shape}",
              flush=True)

    # Still nothing -- print full diagnostics (every record's raw available
    # keys, plus any key that merely CONTAINS "prompt" in case the real
    # field name/casing differs from REFERENCE_AI_PROMPT_FIELD) so the next
    # step is precise, not another guess.
    for rec in records:
        keys = sorted(rec.keys())
        prompt_like = {k: len(str(rec.get(k) or "")) for k in keys if "prompt" in k.lower()}
        print(f"[main] _fetch_ai_prompt_template: record {_record_label(rec)!r} raw list-response keys: "
              f"{keys} -- prompt-like keys/lengths: {prompt_like or '(none found)'}", flush=True)
    print(f"[main] _fetch_ai_prompt_template: none of the {len(records)} record(s) in module "
          f"{REFERENCE_AI_PROMPT_MODULE_API_NAME!r} have a non-blank {REFERENCE_AI_PROMPT_FIELD!r} field, "
          f"in either the list response or a single-record GET -- AI reference preview unavailable this "
          f"run.", flush=True)
    return None


def _build_gemini_client() -> Optional[GeminiClient]:
    """Constructs a GeminiClient from GEMINI_API_KEY, or None (never raises)
    if that env var isn't set -- same "an optional preview feature must
    never break a real posting run" reasoning as _fetch_ai_prompt_template()
    above. main.py's /post-transactions treats a None gemini_client exactly
    like a None ai_prompt_template: the AI preview step is just skipped for
    this run, every real posting call proceeds completely unaffected."""
    try:
        return GeminiClient(GeminiConfig.from_env())
    except Exception as e:
        print(f"[main] _build_gemini_client: GEMINI_API_KEY not configured or invalid -- AI reference preview "
              f"unavailable this run: {e}", flush=True)
        return None


def _build_ai_classification_context(zoho_client, organization_id) -> dict:
    """Builds {"own_accounts", "related_entities", "loan_providers"} -- the
    three lists posting_service.generate_ai_reference_preview() sends with
    EVERY row this run (see RELATED_ENTITIES_JSON/LOAN_PROVIDERS_JSON above
    for related_entities/loan_providers -- static per this deployment,
    parsed once here). own_accounts is fetched LIVE via
    ZohoBooksClient.list_bank_accounts() for `organization_id` (the org
    currently being posted to) -- see this feature's module-level comment
    above RELATED_ENTITIES_JSON for exactly what's real here (account_number/
    account_name) vs not yet wired (bank name/IBAN/currency, and every OTHER
    CBK entity's own accounts).

    Never raises -- a failure fetching bank accounts (or a bad JSON env var)
    degrades that one list to empty rather than failing the whole AI preview
    step, same "optional feature must never break a real posting run"
    reasoning as _fetch_ai_prompt_template()/_build_gemini_client() above."""
    try:
        related_entities = json.loads(RELATED_ENTITIES_JSON)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[main] _build_ai_classification_context: RELATED_ENTITIES_JSON is invalid JSON, using []: {e}", flush=True)
        related_entities = []
    try:
        loan_providers = json.loads(LOAN_PROVIDERS_JSON)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[main] _build_ai_classification_context: LOAN_PROVIDERS_JSON is invalid JSON, using []: {e}", flush=True)
        loan_providers = []

    own_accounts = []
    try:
        accounts = zoho_client.list_bank_accounts(organization_id=organization_id)
        own_accounts = [
            {
                "entity": None,  # not wired up -- see module-level comment above RELATED_ENTITIES_JSON
                "bank": None,    # not wired up
                "account_number": a.get("account_number"),
                "iban": None,    # not wired up
                "currency": None,  # not wired up
                "account_name": a.get("account_name"),  # extra, beyond the spec's own shape -- harmless context for the model
            }
            for a in accounts
            if a.get("account_number")
        ]
    except Exception as e:
        print(f"[main] _build_ai_classification_context: couldn't fetch bank accounts for organization_id="
              f"{organization_id!r} -- own_accounts will be empty this run: {e}", flush=True)

    return {"own_accounts": own_accounts, "related_entities": related_entities, "loan_providers": loan_providers}


@app.after_request
def add_cors_headers(resp):
    # The widget runs inside a Zoho-hosted iframe and calls this endpoint
    # cross-origin via fetch() -- these headers are required for the browser
    # to accept the response (and for the preflight OPTIONS below to succeed).
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Key"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _recalculate_formulas(path: str) -> None:
    """Runs the just-generated workbook through a headless LibreOffice Calc
    round trip (load -> recalculate -> save as .xlsx) so Debit Account/
    Credit Account/Posting Type -- written as live FORMULAS by
    categorize_from_module.py, not static values, so they keep updating if a
    row's Category is edited by hand later -- get a real CACHED value baked
    in too, not just the bare formula string.

    Why this is needed (2026-08-02, per Ravindra: "In one statement it is
    able to show the debit/credit account column with values but none of the
    other statements. The columns are created but empty values"): openpyxl
    (which writes these formula cells) has no formula engine of its own -- a
    formula cell it writes has NO cached value at all until some real
    spreadsheet application opens, recalculates, and re-saves the file.
    Confirmed directly in this environment: reading a freshly-generated file
    back with openpyxl's own data_only=True mode returns None for every one
    of these cells, even though the formula itself is completely correct.
    Whichever ONE statement showed values was almost certainly opened for
    real in Excel at some point (which recalculates and caches on save,
    exactly like this function does) -- the others were probably only ever
    looked at in a quick preview/viewer that doesn't run a full recalculation
    itself. Doing this recalculation HERE, once, right after generation,
    means every viewer sees the correct value immediately with no dependency
    on what opens the file next -- the formulas themselves are UNCHANGED and
    still live/recalculate further if a Category cell is edited afterward in
    real Excel.

    Requires the `libreoffice-calc` package in this service's container (see
    Dockerfile) -- adds real image size/cold-start time, flagged in DEPLOY.md.
    Deliberately non-fatal if it fails (see the /categorize route): the
    formulas are still 100% correct either way, this step is purely about
    making their value visible without an extra manual open-and-resave step."""
    directory = os.path.dirname(path) or "."
    outdir = os.path.join(directory, "_recalculated")
    os.makedirs(outdir, exist_ok=True)
    result = subprocess.run(
        ["soffice", "--headless", "--norestore", "--convert-to", "xlsx", "--outdir", outdir, path],
        capture_output=True, text=True, timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(f"soffice exited {result.returncode}: {(result.stderr or result.stdout)[:500]}")
    converted_path = os.path.join(outdir, os.path.basename(path))
    if not os.path.exists(converted_path) or os.path.getsize(converted_path) == 0:
        raise RuntimeError(f"soffice did not produce {converted_path!r} -- stdout: {result.stdout[:300]}")
    shutil.move(converted_path, path)


def _convert_legacy_xls_to_xlsx(path: str) -> str:
    """Converts a legacy binary .xls file (Excel 97-2003 format) to a real
    .xlsx file via the same headless LibreOffice binary already required for
    _recalculate_formulas() above. openpyxl (used for every read/write in
    this service) can only handle the modern .xlsx format -- opening a
    genuine .xls file with it raises openpyxl's own verbatim error
    ("openpyxl does not support the old .xls file format, please use xlrd to
    read this file, or convert it to the more recent .xlsx file format.")
    (2026-08-03, per Ravindra: a real live statement, 'CBQ QAR 3001 JUN
    2026.xls', hit exactly this and failed the whole account's run).

    Rather than adding a second library (xlrd) just to READ .xls -- which
    still wouldn't solve WRITING the categorized output back out, since
    openpyxl can only ever save .xlsx, and this pipeline's Debit/Credit
    Account/Posting Type columns are live formulas .xls can't hold anyway --
    this reuses the LibreOffice dependency already in the container (added
    for the recalculation step) to produce a real .xlsx copy FIRST. The rest
    of the pipeline (categorize_workbook(), _recalculate_formulas(), upload)
    then runs completely unchanged against that converted copy.

    Returns the path to the converted .xlsx file, written to a separate
    subdirectory alongside the original -- the original .xls is left
    completely untouched on disk (and, per the /categorize route below,
    never overwritten in OneDrive either). Raises RuntimeError with the raw
    soffice output on failure, same pattern as _recalculate_formulas()."""
    directory = os.path.dirname(path) or "."
    outdir = os.path.join(directory, "_xls_converted")
    os.makedirs(outdir, exist_ok=True)
    result = subprocess.run(
        ["soffice", "--headless", "--norestore", "--convert-to", "xlsx", "--outdir", outdir, path],
        capture_output=True, text=True, timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(f"soffice exited {result.returncode}: {(result.stderr or result.stdout)[:500]}")
    base_no_ext = os.path.splitext(os.path.basename(path))[0]
    converted_path = os.path.join(outdir, base_no_ext + ".xlsx")
    if not os.path.exists(converted_path) or os.path.getsize(converted_path) == 0:
        raise RuntimeError(f"soffice did not produce {converted_path!r} -- stdout: {result.stdout[:300]}")
    return converted_path


def _check_auth():
    if API_KEY and request.headers.get("X-Api-Key") != API_KEY:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    return None


def _prefer_xlsx_over_legacy_xls(file_names: list) -> list:
    """Drops a legacy `.xls` file from the listing if a `.xlsx` file with the
    SAME base name is also present -- e.g. once "CBQ QAR 3001 JUN 2026.xls"
    has been through _convert_legacy_xls_to_xlsx() and its categorized result
    uploaded as "CBQ QAR 3001 JUN 2026.xlsx" (see that function's docstring),
    BOTH files sit in the same OneDrive folder forever afterward, since the
    original .xls is deliberately left untouched rather than deleted.

    Without this, every future /list-files call returns both names, and
    widget.js's matchFilesToAccounts() matches on a simple digit-run
    substring against every file in the folder -- both names contain the
    same account digits, so BOTH get processed on every subsequent run:
    the .xls gets reconverted and its .xlsx re-overwritten (harmless but
    wasteful), while the ALREADY-categorized .xlsx also gets reprocessed
    from scratch (categorize_workbook()'s _strip_previously_generated_
    columns() means this replaces rather than piles up a second set of
    Category/Debit Account/etc. columns within that one file, so it isn't
    silently corrupted) -- but the widget's own summary ends up reporting 2
    files/double the row count for what is really one statement, and -- more
    importantly -- if/when the not-yet-wired-up posting feature
    (posting_service.py) starts calling Zoho Books from this same per-file
    loop, processing both names would mean posting every one of these
    transactions TWICE.

    2026-08-03, per Ravindra: "when you create a new file with the same name
    next time when i run the categorization again will it process both the
    files?" -- yes, without this fix. The `.xlsx` twin is always the
    complete, already-converted, already-categorized version, so it's always
    the one worth keeping -- comparing base names case-insensitively (a
    OneDrive/Windows-originated folder can't have two files differing only
    in case anyway, but a stray case mismatch shouldn't defeat this) is
    enough; no need to inspect either file's contents."""
    xlsx_base_names = {
        os.path.splitext(name)[0].lower()
        for name in file_names
        if name and name.lower().endswith(".xlsx")
    }
    kept = []
    dropped = []
    for name in file_names:
        if name and name.lower().endswith(".xls") and not name.lower().endswith(".xlsx"):
            if os.path.splitext(name)[0].lower() in xlsx_base_names:
                dropped.append(name)
                continue
        kept.append(name)
    if dropped:
        print(f"[main] _prefer_xlsx_over_legacy_xls: dropping {dropped!r} from the listing -- each already has "
              f"a converted .xlsx twin present, which is what should be processed instead.", flush=True)
    return kept


@app.route("/list-files", methods=["OPTIONS"])
@app.route("/categorize", methods=["OPTIONS"])
@app.route("/categorization-rules", methods=["OPTIONS"])
@app.route("/entities", methods=["OPTIONS"])
@app.route("/organizations", methods=["OPTIONS"])
@app.route("/org-folder-map", methods=["OPTIONS"])
@app.route("/bank-accounts", methods=["OPTIONS"])
@app.route("/update-status", methods=["OPTIONS"])
@app.route("/post-transactions", methods=["OPTIONS"])
@app.route("/post-bank-charges", methods=["OPTIONS"])
@app.route("/delete-postings", methods=["OPTIONS"])
@app.route("/attach-supporting-docs", methods=["OPTIONS"])
@app.route("/reconcile", methods=["OPTIONS"])
def preflight():
    return ("", 204)


@app.route("/list-files", methods=["GET"])
def list_files():
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    folder_path = request.args.get("folder_path") or DEFAULT_FOLDER_PATH
    try:
        client = OneDriveClient(OneDriveConfig.from_env())
        items = client.list_files_in_folder(folder_path)
    except Exception as e:
        print(f"[main] /list-files folder={folder_path!r}: FAILED -> {e}", flush=True)
        return jsonify({"success": False, "error": f"Couldn't list OneDrive folder {folder_path!r}: {e}"}), 502

    all_file_names = [item.get("name") for item in items]

    # "Don't reprocess an already-fully-posted file" (2026-08-10, per
    # Ravindra: see onedrive_client.py's completed_file_name()/
    # is_marked_complete() docstrings for the full convention). Filtered out
    # HERE, server-side, before the widget's own account-matching ever sees
    # these names -- a completed file is never downloaded/categorized/posted
    # again, not merely re-processed-and-skipped-again at the row level.
    file_names = [name for name in all_file_names if name and not is_marked_complete(name)]
    completed_file_names = [name for name in all_file_names if name and is_marked_complete(name)]

    file_names = _prefer_xlsx_over_legacy_xls(file_names)
    # Debug logging (2026-08-02, per Ravindra -- diagnosing 2 specific files
    # that exist in the mapped folder but were reported as "not found" by
    # the widget's client-side matching). Printed with flush=True so it
    # shows up immediately in `gcloud run services logs read` -- this is
    # the one place to look to confirm whether Graph is even RETURNING
    # these files at all (if a file is missing from this list, the problem
    # is server-side/Graph -- indexing lag, a sync conflict, etc; if it's
    # present here but the widget still says "not found", the problem is in
    # matchFilesToAccounts()'s digit-matching, and this list is exactly what
    # to compare its computed digitsOnly value against).
    print(f"[main] /list-files folder={folder_path!r}: {len(file_names)} file(s) returned, "
          f"{len(completed_file_names)} already-completed file(s) filtered out: {file_names} "
          f"(completed, skipped: {completed_file_names})", flush=True)

    return jsonify({
        "success": True,
        "folder_path": folder_path,
        "files": file_names,
        # Not consumed by the widget today -- included for visibility/
        # debugging (e.g. confirming a file you expected to be skipped
        # actually got marked complete) without requiring a Cloud Run log
        # lookup every time.
        "completed_files_skipped": completed_file_names,
    })


@app.route("/categorize", methods=["POST"])
def categorize():
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    body = request.get_json(force=True, silent=True) or {}
    file_name = str(body.get("file_name", "")).strip()
    folder_path = body.get("folder_path") or DEFAULT_FOLDER_PATH
    # REQUIRED (2026-08-02, per Ravindra -- dashboard/OneDrive-Rules-workbook
    # fallback removed): the widget fetches these itself via
    # GET /categorization-rules (the "Categorization" custom module) and
    # sends the raw records straight through here.
    raw_rules = body.get("rules") or []
    # Despite the field name, this must be the current statement's own bank
    # account's real Zoho account_id (2026-07-25, per Ravindra) -- NOT its
    # bank account number. Used to fill in whichever of Debit Account/Credit
    # Account represents "this account" for each row (see
    # categorize_from_module.py's IMPORTANT docstring note). Optional -- if
    # omitted, that side is just left blank instead of guessed.
    account_number = str(body.get("account_number", "")).strip()
    # DEBUG (2026-08-02, per Ravindra -- "debit/credit account not populating
    # for one specific account, other files fine, please add log statements"):
    # this is the single most important value to see per-request, since a
    # blank account_number here means "this account"'s own side of every
    # entry is guaranteed to come out blank downstream (by design, not a bug
    # -- see categorize_workbook()'s docstring) -- print it plainly so a
    # Cloud Run log read immediately shows whether THIS request ever received
    # a real account_id for THIS file/account, before looking any further.
    _account_number_note = "" if account_number else " -- BLANK, this account's side will be left blank downstream"
    print(f"[main] /categorize {file_name!r}: account_number (resolved account_id, from widget.js's "
          f"account.account_id) = {account_number!r}{_account_number_note}", flush=True)

    # Egypt entity paired salary+bank-charge itemized posting, Phase 1
    # wiring (2026-08-13, per Ravindra's follow-up "where do i get the
    # checkbox for this kind of processing" -- Phase 1 only added the
    # categorize_from_module.py logic behind a parameter nothing called yet;
    # THIS is what actually lets the widget turn it on). OPT-IN, default
    # False -- see categorize_from_module.py's own "EGYPT ENTITY: PAIRED
    # SALARY + BANK-CHARGE ITEMIZED POSTING" comment block and the project's
    # egypt-paired-bank-charge-itemized-posting-spec.md for the full design.
    # Accepts a real JSON boolean (what widget.js sends) or common truthy
    # string/number shapes, same tolerant parsing as every other boolean
    # flag this route already reads off request bodies elsewhere in this
    # project.
    _raw_detect_pairs = body.get("detect_paired_bank_charges")
    if isinstance(_raw_detect_pairs, bool):
        detect_paired_bank_charges = _raw_detect_pairs
    else:
        detect_paired_bank_charges = str(_raw_detect_pairs or "").strip().lower() in ("true", "1", "yes", "on")
    print(f"[main] /categorize {file_name!r}: detect_paired_bank_charges={detect_paired_bank_charges!r}", flush=True)

    if not file_name:
        return jsonify({"success": False, "error": "file_name is required (from a prior /list-files call)"}), 400
    if not raw_rules:
        return jsonify({"success": False, "file_name": file_name,
                         "error": "rules is required -- fetch them from GET /categorization-rules "
                                  "and pass the records straight through."}), 400

    rules = build_rules(raw_rules)
    if not rules:
        return jsonify({"success": False, "file_name": file_name,
                         "error": "No usable categorization rules found in the records passed -- nothing to "
                                  "match categories against."}), 400

    try:
        client = OneDriveClient(OneDriveConfig.from_env())
        # AI reference -- MOVED HERE (2026-08-12, LATER SAME DAY, per
        # Ravindra: "I want the creating of the reference to happen while
        # doing the catergorization not while posting"). Fetched/built ONCE
        # per file, same pattern /post-transactions used to use before this
        # move -- see REFERENCE_AI_PROMPT_MODULE_API_NAME's own module-level
        # comment for where ai_prompt_template comes from, and
        # categorize_from_module.py's POSTING_REFERENCE_COLUMN_NAME comment
        # for exactly how these two get used inside categorize_workbook()
        # below (tier 2 of its 3-tier PostingReference lookup). Neither
        # _fetch_ai_prompt_template() nor _build_gemini_client() ever
        # raises -- a missing GEMINI_API_KEY or an empty/missing
        # cf_ai_prompt just silently skips tier 2 for every row this run
        # (falls straight to tier 3, the statement's own raw Reference
        # column) rather than failing the whole categorize request. A
        # SEPARATE ZohoBooksClient from the one OneDriveClient uses above --
        # this route never needed a Zoho connection before this feature.
        zoho_client_for_ai = ZohoBooksClient(ZohoConfig.from_env())
        ai_prompt_template = _fetch_ai_prompt_template(zoho_client_for_ai)
        gemini_client = _build_gemini_client() if ai_prompt_template else None
        with tempfile.TemporaryDirectory() as tmp:
            # download_file_by_path -- NOT find_file_by_name -- deliberately:
            # it hits Graph's path-addressed content endpoint directly, no
            # folder listing involved, since the caller already knows the
            # exact name (from /list-files, matched client-side).
            print(f"[main] /categorize {file_name!r}: downloading...", flush=True)
            local_path = client.download_file_by_path(folder_path, file_name, os.path.join(tmp, file_name))
            print(f"[main] /categorize {file_name!r}: downloaded, loading workbook...", flush=True)

            # Legacy .xls (Excel 97-2003 binary format) support (2026-08-03,
            # per Ravindra -- see _convert_legacy_xls_to_xlsx()'s docstring):
            # openpyxl can only read/write .xlsx, so a genuine .xls input is
            # converted to a real .xlsx FIRST, via the same LibreOffice
            # binary the recalculation step below already requires. The
            # categorized OUTPUT is uploaded under a new .xlsx-suffixed name
            # rather than overwriting the original .xls in OneDrive (which
            # can't hold the live Debit/Credit Account formulas this
            # pipeline writes anyway) -- the original .xls is left untouched,
            # same "unwire/leave in place, don't delete" convention used
            # elsewhere in this project.
            upload_file_name = file_name
            if file_name.lower().endswith(".xls"):
                print(f"[main] /categorize {file_name!r}: legacy .xls format detected, converting to .xlsx "
                      f"via LibreOffice first...", flush=True)
                try:
                    local_path = _convert_legacy_xls_to_xlsx(local_path)
                except Exception as conv_err:
                    print(f"[main] /categorize {file_name!r}: .xls->.xlsx conversion FAILED -> {conv_err}",
                          flush=True)
                    return jsonify({
                        "success": False, "file_name": file_name,
                        "error": f"This file is in the old .xls format, and converting it to .xlsx failed: "
                                 f"{conv_err}. Try re-saving it as .xlsx manually in Excel and re-uploading.",
                    }), 500
                upload_file_name = os.path.splitext(file_name)[0] + ".xlsx"
                print(f"[main] /categorize {file_name!r}: converted OK, result will be uploaded as "
                      f"{upload_file_name!r} (the original .xls is left untouched in OneDrive)", flush=True)

            wb = openpyxl.load_workbook(local_path, data_only=True)
            print(f"[main] /categorize {file_name!r}: workbook loaded, categorizing "
                  f"({len(rules)} rule(s))...", flush=True)
            out_wb, stats = categorize_workbook(
                wb, rules, account_number,
                gemini_client=gemini_client, ai_prompt_template=ai_prompt_template,
                detect_paired_bank_charges=detect_paired_bank_charges,
            )
            print(f"[main] /categorize {file_name!r}: categorized ({stats}), saving...", flush=True)
            out_wb.save(local_path)
            # Recalculate Debit Account/Credit Account/Posting Type's live
            # formulas so they show a real value in ANY viewer, not just once
            # someone opens the file for real in Excel (2026-08-02, per
            # Ravindra -- see _recalculate_formulas()'s docstring). Non-fatal
            # on failure -- the formulas are still fully correct and will
            # compute properly the moment the file IS opened in a real
            # spreadsheet app; this step is purely a convenience so that's not
            # a required extra step.
            print(f"[main] /categorize {file_name!r}: saved, recalculating formulas...", flush=True)
            try:
                _recalculate_formulas(local_path)
                print(f"[main] /categorize {file_name!r}: recalculation OK", flush=True)
            except Exception as recalc_err:
                print(f"[main] /categorize {file_name!r}: recalculation FAILED (non-fatal -- formulas are "
                      f"still correct, just won't show a value until opened in a real spreadsheet app) -> "
                      f"{recalc_err}", flush=True)
            print(f"[main] /categorize {file_name!r}: uploading back to OneDrive as {upload_file_name!r}...",
                  flush=True)
            client.upload_file(local_path, folder_path, upload_file_name)
            print(f"[main] /categorize {file_name!r}: upload SUCCEEDED (as {upload_file_name!r})", flush=True)
    except FileNotFoundError as e:
        print(f"[main] /categorize {file_name!r}: FileNotFoundError -> {e}", flush=True)
        return jsonify({"success": False, "file_name": file_name, "error": str(e)}), 404
    except Exception as e:
        # traceback.format_exc() (not just str(e)) so the Cloud Run log shows
        # the exact line that raised, not just the exception's message --
        # str(e) alone can't distinguish "download failed" from "upload
        # failed" when both raise the same kind of Graph HTTPError text.
        import traceback
        print(f"[main] /categorize {file_name!r}: EXCEPTION -> {e}\n{traceback.format_exc()}", flush=True)
        return jsonify({"success": False, "file_name": file_name, "error": str(e)}), 500

    # uploaded_file_name is only ever different from file_name when a legacy
    # .xls input was converted to .xlsx above (2026-08-03) -- included so the
    # widget/Ravindra can tell there's a NEW file to look at, not the
    # original .xls, without needing to check the Cloud Run logs for it.
    return jsonify({"success": True, "file_name": file_name, "uploaded_file_name": upload_file_name, **stats})


@app.route("/post-transactions", methods=["POST"])
def post_transactions():
    """Posts every postable row of an already-categorized statement to Zoho
    Books -- see this file's docstring for the full route description.

    STREAMING (2026-08-09, later still), per Ravindra: "currently all the
    records are updated on the widget after posting is complete. but i want
    to see live posting of each post as it updates and count also updates
    live ... start showing the postings info live as they get updated not
    at the end." Everything through extraction (download, load workbook,
    extract_postable_rows) still happens up front and can still fail with a
    normal JSON error response + real HTTP status (400/404/500), same as
    before -- nothing about THAT changed. Only the actual posting step is
    now streamed: once posting starts, the response becomes NDJSON
    (newline-delimited JSON, one event object per line, mimetype
    application/x-ndjson) instead of one big JSON blob at the end, built
    from a generator that drains posting_service.post_rows_streaming() and
    yields one line per row AS SOON as that row's own Zoho call returns.

    Event shapes (one JSON object per line):
      {"type": "start", "success": true, "file_name", "folder_path",
       "already_posted", "no_category", "already_posted_rows": [...same
       per-row shape as "row" events' "row" below, but posted=true,
       newly_posted=false...], "total_to_post": <int>, "bank_charges_excluded":
       <int> -- 2026-08-10, count of Bank charges/VAT on bank charges rows
       this file has that are NEVER posted individually (see
       categorize_from_module.py's extract_postable_rows()); informational
       only, not an error. See the separate /post-bank-charges route below
       for the month-end consolidated posting flow those rows feed into.}
      {"type": "row", "row": {file_name, folder_path, excel_row, category,
       posting_type, amount, narration, txn_date, zoho_reference, posted,
       newly_posted: true, posting_reference: str|None} -- posting_reference
       is simply row["reference_number"] (2026-08-12, LATER SAME DAY, per
       Ravindra: "I want the creating of the reference to happen while
       doing the catergorization not while posting" -- this SUPERSEDES an
       even-shorter-lived version of this route that called Gemini live,
       right here, per row, before dispatch; see posting_service.py's "AI
       REFERENCE -- MOVED TO CATEGORIZATION TIME" module-level section
       header for the full two-moves-in-one-day history). No Gemini call
       happens in this route at all any more -- every row's reference was
       already resolved once, during /categorize (rule's own cf_reference
       -> Gemini -> the statement's raw Reference column -- see
       categorize_from_module.py's POSTING_REFERENCE_COLUMN_NAME comment),
       and extract_postable_rows() read that resolved value straight into
       row["reference_number"] before this row ever reached post_row().
       posting_reference is included here purely so the widget can show
       what reference text this row actually posted with.}, "stats":
       {total_attempted, posted, not_posted, errors, by_type}}
      {"type": "done", "success": true, "file_name", "uploaded_file_name",
       stats: {...final...}}
      {"type": "error", "success": false, "file_name", "error": "..."} --
       ONLY way to signal a failure once streaming has started, since the
       HTTP status line/headers are already sent by then (can't switch to a
       500 mid-stream) -- the widget must check "type" on every line, not
       just rely on the HTTP status.

    UNVERIFIED LIVE (flag until confirmed against a real Cloud Run deploy):
    whether Cloud Run's own front-end/reverse-proxy layer passes this
    response through to the browser incrementally or buffers the whole
    thing before delivering it. If it buffers, nothing breaks -- the widget
    just receives every NDJSON line at once instead of incrementally,
    degrading gracefully to "live" being not-actually-live rather than
    crashing (same parsing code either way). X-Accel-Buffering: no is set
    below as a best-effort hint to any nginx-style proxy in the path; Cloud
    Run's own behavior here hasn't been confirmed one way or the other."""
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    body = request.get_json(force=True, silent=True) or {}
    file_name = str(body.get("file_name", "")).strip()
    folder_path = body.get("folder_path") or DEFAULT_FOLDER_PATH
    raw_rules = body.get("rules") or []
    # This statement's own bank account's real Zoho account_id -- same
    # meaning/param name as /categorize's account_number (NOT a bank account
    # number -- see categorize_from_module.py's IMPORTANT docstring note).
    # Needed here too so posting_service.py can tell "this account"'s side
    # of an Expense row apart from the expense GL account.
    account_number = str(body.get("account_number", "")).strip()
    # The Post widget's OWN organization picker (2026-08-09, per Ravindra's
    # confirmed design answer: "Own org + account picker" -- run independently
    # of whichever org a prior categorize run used) -- threaded through to
    # every Zoho Books write this run makes, via ZohoBooksClient's existing
    # organization_id override (see zoho_books_client.py's _request()).
    # Optional: omitted/blank means this client's own configured
    # ZOHO_ORGANIZATION_ID is used, same as every other org-scoped endpoint
    # here when organization_id isn't passed.
    organization_id = str(body.get("organization_id", "")).strip() or None

    print(f"[main] /post-transactions {file_name!r}: account_number={account_number!r}, "
          f"organization_id={organization_id!r}", flush=True)

    if not file_name:
        return jsonify({"success": False, "error": "file_name is required (from a prior /list-files call)"}), 400
    if not raw_rules:
        return jsonify({"success": False, "file_name": file_name,
                         "error": "rules is required -- fetch them from GET /categorization-rules "
                                  "and pass the records straight through, same as /categorize."}), 400

    rules = build_rules(raw_rules)
    if not rules:
        return jsonify({"success": False, "file_name": file_name,
                         "error": "No usable categorization rules found in the records passed -- nothing to "
                                  "post against."}), 400

    # Prep work (download/load/extract) still happens synchronously, up
    # front, with normal JSON-error-response-on-failure behavior -- ONLY
    # the posting loop below is streamed. Uses tempfile.mkdtemp() (not the
    # `with tempfile.TemporaryDirectory()` context manager used pre-2026-08-09)
    # because local_path needs to stay alive for the generator below, which
    # Flask doesn't actually run until AFTER this view function returns --
    # a `with` block here would delete the temp dir out from under it. The
    # generator's own `finally` cleans it up once streaming ends (success,
    # client disconnect, or error) instead.
    tmp = tempfile.mkdtemp()
    try:
        onedrive_client = OneDriveClient(OneDriveConfig.from_env())
        zoho_client = ZohoBooksClient(ZohoConfig.from_env())
        # AI reference -- MOVED TO /categorize (2026-08-12, LATER SAME DAY,
        # per Ravindra: "I want the creating of the reference to happen
        # while doing the catergorization not while posting"). This route
        # no longer fetches ai_prompt_template/builds a gemini_client at
        # all -- by the time a file reaches /post-transactions, every
        # row's reference was already resolved once, during /categorize
        # (see categorize_from_module.py's categorize_workbook() and its
        # POSTING_REFERENCE_COLUMN_NAME comment), and
        # extract_postable_rows() below reads that resolved value straight
        # into row["reference_number"] -- posting_service.py's post_row()
        # dispatch functions send it exactly as before, with zero AI
        # awareness of their own. See posting_service.py's "AI REFERENCE --
        # MOVED TO CATEGORIZATION TIME" module-level section header for the
        # full history of this feature's two moves in one day.
        print(f"[main] /post-transactions {file_name!r}: downloading...", flush=True)
        local_path = onedrive_client.download_file_by_path(folder_path, file_name, os.path.join(tmp, file_name))

        # IMPORTANT: NOT data_only=True -- extract_postable_rows() needs
        # every formula cell's FORMULA intact (not just its cached value),
        # and this same workbook object gets saved again below to add the
        # Zoho Posting Reference column. Loading with data_only=True here
        # would silently strip the live Debit Account/Credit Account/
        # Posting Type formulas on save -- see extract_postable_rows()'s
        # own docstring for the full reasoning.
        wb = openpyxl.load_workbook(local_path)
        print(f"[main] /post-transactions {file_name!r}: workbook loaded, extracting postable rows "
              f"({len(rules)} rule(s))...", flush=True)
        extraction = extract_postable_rows(wb, rules, account_number)
        print(f"[main] /post-transactions {file_name!r}: {len(extraction['rows'])} postable row(s), "
              f"{extraction['already_posted']} already posted, {extraction['no_category']} with no usable "
              f"category, {extraction['bank_charges_excluded']} Bank charges/VAT on bank charges row(s) "
              f"excluded from individual posting (see /post-bank-charges for the month-end consolidated "
              f"posting flow) -- posting...", flush=True)
    except FileNotFoundError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"[main] /post-transactions {file_name!r}: FileNotFoundError -> {e}", flush=True)
        return jsonify({"success": False, "file_name": file_name, "error": str(e)}), 404
    except ValueError as e:
        # extract_postable_rows() raises ValueError for things like "no
        # Category column" / "no Debit/Credit columns" / "no date column" --
        # a clear, specific message worth surfacing as-is rather than behind
        # a generic 500.
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"[main] /post-transactions {file_name!r}: ValueError -> {e}", flush=True)
        return jsonify({"success": False, "file_name": file_name, "error": str(e)}), 400
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        import traceback
        print(f"[main] /post-transactions {file_name!r}: EXCEPTION -> {e}\n{traceback.format_exc()}", flush=True)
        return jsonify({"success": False, "file_name": file_name, "error": str(e)}), 500

    # "Already posted" rows (from an earlier run) are known up front and
    # don't get re-attempted -- sent once, in the "start" event, same shape
    # as each "row" event's row dict but posted=true/newly_posted=false, so
    # the widget can show them immediately alongside the live rows about to
    # stream in.
    already_posted_rows_detail = [
        {
            "file_name": file_name,
            "folder_path": folder_path,
            "excel_row": row["excel_row"],
            "category": row["category"],
            "posting_type": None,
            "amount": row["amount"],
            "narration": row["narration"],
            "txn_date": None,
            "zoho_reference": row["zoho_reference"],
            # 2026-08-12 (later same day), per Ravindra: a Vendor Payment
            # reconciliation MATCH from an earlier run is not something
            # this pipeline ever posted to Zoho -- only validated against
            # an existing record -- so it shouldn't read as "posted" here
            # either (this also correctly keeps the widget from offering a
            # Delete button for it -- delete_posting() has nothing to
            # delete for a reconciliation match). See posting_service.
            # is_vendor_payment_reconciliation_result()'s own docstring for
            # why this is a separate notion from _is_success_reference()
            # (which still treats this row as settled/complete).
            "posted": not posting_service.is_vendor_payment_reconciliation_result(row["zoho_reference"]),
            "newly_posted": False,
        }
        for row in extraction["already_posted_rows"]
    ]
    # Egypt itemized-group handling (2026-08-13, Phase 2 -- see
    # categorize_from_module.py's extract_postable_rows() docstring for the
    # full "itemized_groups"/"itemized_groups_already_posted"/
    # "itemized_groups_blocked" shape). A group with AT LEAST ONE member
    # already posted is treated as ENTIRELY already posted (per Ravindra's
    # confirmed "any row posted = whole group treated as posted" answer) --
    # folded into the SAME already_posted_rows_detail list normal already-
    # posted rows use, so every member shows up immediately in the "start"
    # event, gets the shared reference, and (via post_widget.js's existing
    # Delete-button wiring, unchanged) can be deleted like any other posted
    # row. itemized_already_posted_repairs collects just the members that
    # DON'T yet carry that reference in their own cell -- fed into
    # posting_results below so write_posting_results() repairs them onto
    # the sheet even though this run never re-posts anything for this group.
    itemized_already_posted_repairs = {}  # excel_row -> zoho_reference text
    for group in extraction.get("itemized_groups_already_posted", []):
        zoho_reference = group["zoho_reference"]
        for member in group["members"]:
            if not member["already_posted"]:
                itemized_already_posted_repairs[member["excel_row"]] = zoho_reference
            already_posted_rows_detail.append({
                "file_name": file_name,
                "folder_path": folder_path,
                "excel_row": member["excel_row"],
                "category": member["category"],
                "posting_type": None,
                "amount": member["amount"],
                "narration": member["narration"],
                "txn_date": None,
                "zoho_reference": zoho_reference,
                "posted": True,
                "newly_posted": False,
            })
    rows_by_excel_row = {row["excel_row"]: row for row in extraction["rows"]}

    def generate():
        # Seeded with the itemized "already posted" repairs computed above
        # (2026-08-13, Phase 2) -- write_posting_results() below writes
        # every key in posting_results onto its own row's cell, so this
        # repairs a member's still-blank Zoho Posting Reference cell onto
        # the shared group reference even though nothing gets re-posted for
        # this group this run. Every subsequent write into posting_results
        # (the ordinary per-row loop, then the itemized blocked/postable
        # loops below) simply adds more keys -- no collision is possible,
        # since a group's own members' excel_row values never overlap with
        # extraction["rows"]'s (extract_postable_rows() routes itemized rows
        # entirely separately -- see that function's docstring).
        posting_results = dict(itemized_already_posted_repairs)
        stats = {"total_attempted": 0, "posted": 0, "not_posted": 0, "errors": 0, "by_type": {}}

        # Incremental checkpoint saving (2026-08-13, added per Ravindra's live
        # report: "stream ended without a 'done' event ... missing the last
        # post and not posting it. and it is not posting back the zoho
        # posting reference for any transactions"). Root cause was gunicorn's
        # worker --timeout killing the request mid-stream (see Dockerfile's
        # own comment) -- SIGKILL can't be caught, so the ONE
        # write_posting_results()/wb.save()/upload_file() call this route used
        # to make, right at the very end of generate(), never ran at all, even
        # for rows that had already posted successfully to Zoho moments
        # earlier. Raising the timeout (Dockerfile) fixes the immediate cause,
        # but this is a second, independent safety net: periodically writing
        # whatever's in posting_results SO FAR back onto the workbook and
        # re-uploading it means a future crash/timeout/OOM -- from THIS cause
        # or any other -- loses at most a handful of rows' worth of
        # references instead of the entire file's. write_posting_results() is
        # purely additive/idempotent (just sets cell values keyed by
        # excel_row -- see its own docstring), so calling it repeatedly with
        # a growing dict is safe. Deliberately NOT checkpointed after every
        # single ordinary row -- that would add an extra OneDrive upload
        # round-trip per row, which for a large statement would make the
        # overall run meaningfully slower (working against the very timeout
        # this is meant to protect against) -- every CHECKPOINT_INTERVAL-th
        # ordinary row instead, plus after each itemized group (few in
        # number, but each one is already multiple extra Zoho API calls, so
        # the relative overhead of one more upload is small). A checkpoint
        # failure is logged and swallowed, never allowed to take down the
        # run itself -- the final save/upload below (unchanged) still runs
        # normally and is what actually determines .COMPLETED eligibility.
        CHECKPOINT_INTERVAL = 10

        def _checkpoint(label):
            try:
                write_posting_results(wb, extraction, posting_results)
                wb.save(local_path)
                onedrive_client.upload_file(local_path, folder_path, file_name)
                print(f"[main] /post-transactions {file_name!r}: checkpoint saved ({label}) -- "
                      f"{len(posting_results)} reference(s) written so far", flush=True)
            except Exception as checkpoint_err:
                print(f"[main] /post-transactions {file_name!r}: checkpoint save FAILED ({label}) -- "
                      f"{checkpoint_err} -- continuing the run regardless (final save at the end will "
                      f"still retry)", flush=True)

        try:
            itemized_groups = extraction.get("itemized_groups", [])
            itemized_groups_blocked = extraction.get("itemized_groups_blocked", [])
            itemized_group_member_count = sum(len(g["members"]) for g in itemized_groups) + \
                sum(len(g["members"]) for g in itemized_groups_blocked)
            yield json.dumps({
                "type": "start",
                "success": True,
                "file_name": file_name,
                "folder_path": folder_path,
                "already_posted": extraction["already_posted"],
                "no_category": extraction["no_category"],
                "already_posted_rows": already_posted_rows_detail,
                "total_to_post": len(extraction["rows"]) + itemized_group_member_count,
                # Bank Charges/VAT consolidation (2026-08-10) -- informational
                # only here; these rows are simply excluded from this run's
                # individual posting, not an error. See /post-bank-charges
                # for the separate, checkbox-gated consolidated posting flow.
                # "bank_charges_excluded" (2026-08-2x: now only genuinely
                # still-pending rows) vs "bank_charges_resolved" (settled via
                # a "duplicate detected" consolidated posting -- see
                # extract_postable_rows()'s docstring) -- also informational.
                "bank_charges_excluded": extraction["bank_charges_excluded"],
                "bank_charges_resolved": extraction.get("bank_charges_resolved", 0),
                # Egypt itemized-group counts (2026-08-13, Phase 2) --
                # informational only, same spirit as bank_charges_excluded
                # above; the groups themselves post automatically in the
                # same run (see the itemized loops below), no separate
                # checkbox/route needed (confirmed with Ravindra).
                "itemized_groups_detected": len(itemized_groups) + len(itemized_groups_blocked) +
                    len(extraction.get("itemized_groups_already_posted", [])),
                "itemized_groups_blocked": len(itemized_groups_blocked),
            }) + "\n"

            # Shared for the WHOLE run (2026-08-13, later still, per
            # Ravindra: "the posting logic has become very slow ... if i
            # have a lot of transactions it will take for ever") -- one
            # dict passed into both the ordinary-row loop below AND every
            # itemized-group post further down, so posting_service.py's
            # per-(type, date) duplicate-check cache (see post_rows_
            # streaming()'s own docstring) is shared across this entire
            # file's rows, not rebuilt from scratch for each. See
            # posting_service.py's _check_duplicate_description() docstring
            # for the actual mechanics/cost this fixes.
            dup_check_cache = {}

            for excel_row, result_text, stats_snapshot in posting_service.post_rows_streaming(
                zoho_client, extraction["rows"], organization_id, dup_check_cache=dup_check_cache
            ):
                posting_results[excel_row] = result_text
                stats = stats_snapshot
                row = rows_by_excel_row[excel_row]
                # posting_reference (2026-08-12, LATER SAME DAY -- replaces
                # the old ai_reference_preview/ai_reference_applied/
                # ai_classification fields this route used to build here).
                # No Gemini call happens at posting time any more -- this is
                # simply row["reference_number"], which extract_postable_
                # rows() already read from the categorized file's
                # PostingReference column (resolved once, during
                # /categorize -- see categorize_from_module.py's
                # POSTING_REFERENCE_COLUMN_NAME comment) before this row
                # ever reached post_row(). Included here purely so the
                # widget can show WHAT reference text this row actually
                # posted with, same visibility the old ai_reference_preview
                # column gave, just without a live AI call backing it.
                row_detail = {
                    "file_name": file_name,
                    "folder_path": folder_path,
                    "excel_row": excel_row,
                    "category": row["category"],
                    "posting_type": row["posting_type"],
                    "amount": row["amount"],
                    "narration": row["narration"],
                    "txn_date": str(row["txn_date"]) if row["txn_date"] is not None else None,
                    "zoho_reference": result_text,
                    # 2026-08-12 (later same day), per Ravindra: "when
                    # vendor payment is reconciled dont consider this as
                    # posted, count should go towards not posted only as we
                    # are not posting anything just validating." -- a
                    # Vendor Payment reconciliation MATCH still counts as a
                    # success for _is_success_reference() (so it correctly
                    # feeds `newly_succeeded`/.COMPLETED eligibility below --
                    # "if validated successfully document name changed to
                    # Completed"), but is deliberately excluded here so the
                    # widget's live Posted/Not-posted counters (computed
                    # client-side from this exact field) put it in the "Not
                    # posted" bucket instead -- nothing was actually posted
                    # to Zoho for this row, only validated against a record
                    # that already existed. See posting_service.
                    # is_vendor_payment_reconciliation_result()'s own
                    # docstring for the full rationale.
                    "posted": (_is_success_reference(result_text)
                               and not posting_service.is_vendor_payment_reconciliation_result(result_text)),
                    "newly_posted": True,
                    "posting_reference": row.get("reference_number"),
                }
                yield json.dumps({"type": "row", "row": row_detail, "stats": stats_snapshot}) + "\n"
                if len(posting_results) % CHECKPOINT_INTERVAL == 0:
                    _checkpoint(f"ordinary rows, {len(posting_results)} so far")

            # Egypt itemized groups -- BLOCKED (2026-08-13, Phase 2 -- see
            # categorize_from_module.py's extract_postable_rows() docstring).
            # Never attempted against Zoho at all -- a clear "NOT POSTED: ..."
            # reason is written onto EVERY member's own row (same retry-
            # until-fixed convention as any other data-gap reason elsewhere
            # in this file), and a "row" event is emitted per member so the
            # widget's live table/counters reflect it exactly like an
            # ordinary NOT POSTED row would (post_widget.js's rendering and
            # stats code has no itemized-specific branch at all -- it just
            # consumes "row" events, so nothing there needed to change).
            for group in itemized_groups_blocked:
                reason = group["reason"]
                stats["total_attempted"] += len(group["members"])
                stats["not_posted"] += len(group["members"])
                stats = {**stats, "by_type": dict(stats["by_type"])}
                for member in group["members"]:
                    posting_results[member["excel_row"]] = reason
                    row_detail = {
                        "file_name": file_name,
                        "folder_path": folder_path,
                        "excel_row": member["excel_row"],
                        "category": member["category"],
                        "posting_type": member["posting_type"],
                        "amount": member["amount"],
                        "narration": member["narration"],
                        "txn_date": str(member["txn_date"]) if member["txn_date"] is not None else None,
                        "zoho_reference": reason,
                        "posted": False,
                        "newly_posted": True,
                        "posting_reference": member.get("reference_number"),
                    }
                    yield json.dumps({"type": "row", "row": row_detail, "stats": stats}) + "\n"
            if itemized_groups_blocked:
                _checkpoint("itemized groups blocked")

            # Egypt itemized groups -- ready to post (2026-08-13, Phase 2):
            # ONE combined itemized Zoho Expense per group
            # (posting_service.post_itemized_expense_group()) instead of one
            # posting per member -- the whole point of this feature. Posted
            # automatically in this SAME /post-transactions run, right after
            # the ordinary per-row loop above -- 2026-08-13, per Ravindra's
            # confirmed answer ("Automatic, in the same Start Posting run" --
            # no separate checkbox/button for this). The SAME result text is
            # written onto EVERY member's own row (so each row's Zoho
            # Posting Reference cell shows the shared Expense reference, and
            # a "charge" row becomes indistinguishable from a normal
            # already-posted row on the next run's extraction), and a "row"
            # event is emitted per member so the widget's live table updates
            # for both rows, not just the primary.
            for group in itemized_groups:
                result_text = posting_service.post_itemized_expense_group(
                    zoho_client, group, organization_id, dup_check_cache=dup_check_cache
                )
                print(f"[main] /post-transactions {file_name!r}: itemized group (primary excel_row="
                      f"{group['group_key']}) -> {result_text}", flush=True)
                stats["total_attempted"] += len(group["members"])
                if result_text.upper().startswith("NOT POSTED"):
                    stats["not_posted"] += len(group["members"])
                elif result_text.upper().startswith("ERROR"):
                    stats["errors"] += len(group["members"])
                else:
                    stats["posted"] += len(group["members"])
                stats = {**stats, "by_type": dict(stats["by_type"])}
                for member in group["members"]:
                    posting_results[member["excel_row"]] = result_text
                    row_detail = {
                        "file_name": file_name,
                        "folder_path": folder_path,
                        "excel_row": member["excel_row"],
                        "category": member["category"],
                        "posting_type": member["posting_type"],
                        "amount": member["amount"],
                        "narration": member["narration"],
                        "txn_date": str(member["txn_date"]) if member["txn_date"] is not None else None,
                        "zoho_reference": result_text,
                        "posted": _is_success_reference(result_text),
                        "newly_posted": True,
                        "posting_reference": member.get("reference_number"),
                    }
                    yield json.dumps({"type": "row", "row": row_detail, "stats": stats}) + "\n"
                # Checkpointed after EVERY group (not just every
                # CHECKPOINT_INTERVAL-th, unlike the ordinary-row loop above)
                # -- these are the most expensive items per iteration (a
                # duplicate-check call plus a create-itemized-expense call
                # each), so they're also the likeliest single place to run
                # long/fail, and groups are typically few in number so the
                # extra upload overhead per group is small.
                _checkpoint(f"itemized group primary excel_row={group['group_key']}")

            write_posting_results(wb, extraction, posting_results)
            print(f"[main] /post-transactions {file_name!r}: posted ({stats}), saving...", flush=True)
            wb.save(local_path)
            print(f"[main] /post-transactions {file_name!r}: uploading back to OneDrive...", flush=True)
            upload_result = onedrive_client.upload_file(local_path, folder_path, file_name)
            print(f"[main] /post-transactions {file_name!r}: upload SUCCEEDED", flush=True)

            # "Don't reprocess an already-fully-posted file" (2026-08-10, per
            # Ravindra: "I dont want to process the files i already
            # processed/posted ... Is it good to rename the file with
            # .COMPLETED at the end"). Eligible once EVERY postable row this
            # file has -- posted in an earlier run or just now -- has a real
            # Zoho posting reference, AND at least one row has ever actually
            # posted or was resolved as a duplicate (a file where every row
            # is "Others"/uncategorized is NOT marked complete -- that's
            # "nothing matched a rule yet", not "done", and hiding it via a
            # rename would make it invisible to a future categorization
            # fix). See onedrive_client.py's completed_file_name()/
            # is_marked_complete()/rename_file() for the marker convention
            # and the actual Graph rename call.
            #
            # FIX 2026-08-11, per Ravindra's live report ("when a file is
            # reprocessed and it only contained duplicates or partial
            # duplicates ... the file name is not changed to completed"):
            # a "NOT POSTED: duplicate detected -- ..." row used to count
            # toward `still_unposted` exactly like any other unresolved
            # NOT POSTED/ERROR reason -- so a file with even one duplicate
            # row could NEVER be marked complete, since the same duplicate
            # note gets rewritten every single run forever (by design --
            # see _check_for_duplicate()'s docstring for why a duplicate
            # note is deliberately NOT treated as "already posted" on
            # re-extraction). A duplicate-detected row means the underlying
            # transaction ALREADY EXISTS in Zoho under some other reference
            # -- there's nothing left to DO for it, unlike a genuine data
            # gap (missing account, unresolved vendor, bad date, blank
            # posting type) -- so it's now excluded from `still_unposted`
            # and counted alongside `newly_succeeded` for the "at least one
            # row was actually resolved this run" eligibility guard (a file
            # where EVERY row turned out to be a duplicate, and nothing was
            # ever posted before either, should still be markable complete
            # -- see `newly_duplicate` below). Genuine NOT POSTED/ERROR
            # reasons are UNCHANGED -- they still correctly keep a file out
            # of `.COMPLETED` until whatever's actually wrong gets fixed.
            newly_succeeded = sum(1 for text in posting_results.values() if _is_success_reference(text))
            newly_duplicate = sum(1 for text in posting_results.values() if _is_duplicate_reference(text))
            still_unposted = len(posting_results) - newly_succeeded - newly_duplicate
            total_ever_posted = extraction["already_posted"] + newly_succeeded
            # Bank charges/VAT on bank charges rows (2026-08-2x fix, per
            # Ravindra -- same request as the duplicate fix above, extended
            # to cover the consolidated Bank Charges/VAT posting path too):
            # a row settled as "duplicate detected" by a consolidated posting
            # (main.py's /post-bank-charges, which runs BEFORE this route for
            # the same files -- see that route's docstring) had nothing
            # counted anywhere as "resolved", so a file whose only remaining
            # content was such rows could never reach .COMPLETED -- now
            # counted via `extraction["bank_charges_resolved"]`.
            #
            # REVERTED 2026-08-2x (same day), per Ravindra's live report right
            # after this shipped: "my file not changing to COMPLETED in any
            # path with checkbox checked or unchecked" -- an EARLIER version
            # of this fix also required `extraction["bank_charges_excluded"]
            # == 0` (no Bank charges/VAT row left un-consolidated at all)
            # before allowing .COMPLETED, reasoning that a file with
            # genuinely-pending bank charge rows shouldn't be hidden from
            # future /post-bank-charges runs. That reasoning wasn't wrong in
            # theory, but broke real usage badly: an orphan VAT-on-bank-
            # charges row with no matching Bank charges code (which
            # match_bank_charges_to_vat() leaves unmatched FOREVER by
            # design -- see its docstring) would permanently block EVERY
            # file that ever contains one, even with the checkbox checked;
            # and with the checkbox OFF, `bank_charges_excluded` never drops
            # to 0 at all for any file with a Bank charges/VAT row, since
            # nothing ever consolidates them. That's a strictly worse
            # regression than the gap it was meant to close, and directly
            # contradicts "as before" -- a file with unresolved bank charges
            # rows completing once its ORDINARY rows are done is the
            # existing, intended behavior (see the 2026-08-10 Bank Charges/
            # VAT feature's own docstrings: those rows are simply excluded
            # from individual posting, not an error). Restored to the exact
            # original condition, with only `bank_charges_resolved` added.
            bank_charges_resolved = extraction.get("bank_charges_resolved", 0)
            file_marked_complete = False
            new_file_name = None
            not_marked_complete_reason = None
            rename_error = None
            # 2026-08-2x, per Ravindra's follow-up live report ("i still dont
            # see my statement name changed to .COMPLETED yet after all the
            # posting sdone yet") -- after fixing the over-strict guard above,
            # the file STILL wasn't completing, live, in a way the mocked
            # test suite can't reproduce (no real Zoho/OneDrive credentials
            # in this dev environment -- see this repo's standing
            # constraint). Rather than guess a third time, this now always
            # reports WHY a file wasn't marked complete (or why the rename
            # attempt itself failed) directly in the "done" event, instead of
            # only ever printing it to Cloud Run logs Ravindra may not have
            # access to check -- so the next live report can point straight
            # at the actual cause (a genuinely-unresolved row still blocking
            # `still_unposted`, a Graph permissions/rename failure, a missing
            # upload response "id", etc.) instead of another round of
            # guessing from a plausible-sounding theory.
            eligible = still_unposted == 0 and (total_ever_posted > 0 or newly_duplicate > 0 or bank_charges_resolved > 0)
            if not eligible:
                if still_unposted > 0:
                    not_marked_complete_reason = (
                        f"{still_unposted} row(s) in this file still have a genuine NOT POSTED/ERROR reason "
                        f"(not a duplicate) -- fix whatever's wrong with them and re-run.")
                else:
                    not_marked_complete_reason = (
                        "no row in this file has ever posted, been resolved as a duplicate, or been resolved "
                        "via a consolidated Bank charges/VAT posting -- nothing to mark complete yet.")
                print(f"[main] /post-transactions {file_name!r}: NOT marking complete -- {not_marked_complete_reason} "
                      f"(still_unposted={still_unposted}, total_ever_posted={total_ever_posted}, "
                      f"newly_duplicate={newly_duplicate}, bank_charges_resolved={bank_charges_resolved})", flush=True)
            else:
                new_file_name = completed_file_name(file_name)
                item_id = upload_result.get("id")
                if item_id:
                    try:
                        onedrive_client.rename_file(item_id, new_file_name)
                        file_marked_complete = True
                        print(f"[main] /post-transactions {file_name!r}: every postable row now has a Zoho "
                              f"reference or resolved as a duplicate ({total_ever_posted} posted, "
                              f"{newly_duplicate} newly resolved as duplicates this run, "
                              f"{bank_charges_resolved} Bank charges/VAT row(s) resolved via consolidated "
                              f"posting, 0 still unposted) -- renamed to {new_file_name!r} so future "
                              f"/list-files calls skip it.", flush=True)
                    except Exception as e:
                        rename_error = str(e)
                        not_marked_complete_reason = f"rename to {new_file_name!r} failed: {rename_error}"
                        print(f"[main] /post-transactions {file_name!r}: posting itself succeeded, but couldn't "
                              f"rename to {new_file_name!r} to mark it complete -- will be reprocessed (harmlessly "
                              f"-- every row already has its own duplicate protection) on the next run: {e}",
                              flush=True)
                        new_file_name = None
                else:
                    not_marked_complete_reason = (
                        f"upload response had no 'id' field -- can't rename to mark complete "
                        f"(raw upload response: {upload_result})")
                    print(f"[main] /post-transactions {file_name!r}: upload response had no 'id' -- can't rename "
                          f"to mark complete (raw response: {upload_result})", flush=True)

            yield json.dumps({
                "type": "done",
                "success": True,
                "file_name": file_name,
                "uploaded_file_name": file_name,
                "file_marked_complete": file_marked_complete,
                "completed_file_name": new_file_name,
                # Diagnostics (2026-08-2x) -- see the block above for why
                # these are now always included instead of only ever being
                # printed to Cloud Run logs: lets a live report point
                # straight at the actual cause instead of guessing.
                "not_marked_complete_reason": not_marked_complete_reason,
                "rename_error": rename_error,
                "completion_debug": {
                    "still_unposted": still_unposted,
                    "newly_succeeded": newly_succeeded,
                    "newly_duplicate": newly_duplicate,
                    "already_posted_before_this_run": extraction["already_posted"],
                    "total_ever_posted": total_ever_posted,
                    "bank_charges_resolved": bank_charges_resolved,
                    "bank_charges_excluded_pending": extraction.get("bank_charges_excluded", 0),
                    "upload_result_had_id": bool(upload_result.get("id")) if isinstance(upload_result, dict) else False,
                },
                **stats,
            }) + "\n"
        except Exception as e:
            import traceback
            print(f"[main] /post-transactions {file_name!r}: EXCEPTION during streaming posting -> {e}\n"
                  f"{traceback.format_exc()}", flush=True)
            # Can't switch to an HTTP 500 mid-stream -- the status/headers
            # already went out with the first yielded line above. This
            # "error" event is the ONLY way the widget finds out something
            # went wrong once streaming has started; see the route
            # docstring's Event shapes section.
            yield json.dumps({"type": "error", "success": False, "file_name": file_name, "error": str(e)}) + "\n"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            # Best-effort hint for any nginx-style proxy in the path to not
            # buffer this response -- see the route docstring's UNVERIFIED
            # LIVE note re: Cloud Run's own buffering behavior.
            "X-Accel-Buffering": "no",
        },
    )


# =============================================================================
# SUPPORTING DOCUMENT ATTACHMENT (2026-08-12, later same day; SCOPE CUT
# 2026-08-12, still later, per Ravindra's own words: "i want to park the
# document extraction and validation aside and want only to attach the
# required document to the expense entry. Just like other widgets this
# widget also opens up entities and respective accounts and opens up the
# document with the account selected. for every entry marked as supporting
# required=Yes, based on the orgid onedrive map folder it goes in to
# docs/<account no>/ and attaches the pdf file with the reference no read
# from the statement entry. This file is now attached to the post for that
# entry in the zoho books. It has to find the entry using the notes/desc
# field compared with the narration. Please discard all the previous
# changes made for validation and gemini pdf doc reading. Make only the
# changes i mentioned in this."
#
# WHAT THIS SUPERSEDES: an earlier, much larger "Supporting Document
# VERIFICATION" design built the same day (Gemini PDF extraction of
# vendor/invoice-to/amount/VAT fields, an invoice-to-name check, an amount-
# match check, a VAT-strip-and-update of the posted Expense, a NEW
# cf_invoice_folder_path field on the Entities module) -- ALL of that is
# now discarded, never having run live. gemini_client.py's
# extract_json_from_pdf() and zoho_books_client.py's update_expense() were
# deleted; categorize_from_module.py's _category_entity_suffix() (only
# needed for the per-entity invoice-folder lookup) was deleted too.
#
# WHAT THIS FEATURE ACTUALLY DOES NOW, end to end:
#   a. add_supporting_required_column() (categorize_from_module.py, kept
#      unchanged from the earlier design) still adds the "Supporting
#      Required" Yes/No column, from cf_supporting_required on the
#      Categorization module -- APPENDED at the end of the sheet, not
#      inserted before Category, for the same live-formula-corruption
#      reason documented on that function itself (Debit Account/Credit
#      Account/Posting Type are formulas that reference Category by column
#      letter; openpyxl's insert_cols() doesn't rewrite formula text).
#   1. extract_attachment_check_rows() (categorize_from_module.py) takes
#      only rows where that column is "Yes" AND the row is already posted
#      (a genuine _is_success_reference() in the Zoho Posting Reference
#      column) AND not already marked "Attached" in this feature's own
#      "Attachment Status" column.
#   2. The invoice PDF is looked up in OneDrive under
#      "<org's mapped folder>/docs/<bank account number>/" -- the org's
#      mapped folder is the SAME /org-folder-map (cm_orgidonedrivemap) this
#      deployment already uses for bank statements (per Ravindra: "based on
#      the orgid onedrive map folder"); the bank account number is whichever
#      account the widget's caller selected (per Ravindra: "this widget
#      also opens up entities and respective accounts" -- same org+account
#      picker as post_widget.js, see supporting_docs_widget.js). Matched by
#      the row's own PostingReference value (loose, alnum-normalized
#      "contains" match via onedrive_client.find_files_containing() -- same
#      mechanism the discarded design already used for this one piece).
#   3. The Zoho record to attach to is identified directly from the sheet's
#      own "Zoho Posting Reference" cell (e.g. "Expense 12345") --
#      REVISED 2026-08-13, per Ravindra ("why is it even using date it has
#      to just check the notes.desc field right"): the original design here
#      searched Zoho fresh by comparing narration against every same-day
#      record's notes/description text (per the earlier instruction "It has
#      to find the entry using the notes/desc field compared with the
#      narration"), narrowed to the same calendar day only because Zoho's
#      API has no "search by free-text field" endpoint -- date range was
#      never part of the actual match, just how the candidate list stayed
#      small enough to fetch. That's now unnecessary: this row's own
#      already-posted "Zoho Posting Reference" cell already says EXACTLY
#      which Zoho record it is (it's literally what post_row() posted it
#      as), so posting_service.parse_zoho_reference() just parses that
#      "<Type> <id>" text directly -- no search, no date scoping, no text
#      comparison, and no risk of a false negative from a date-format
#      mismatch (see this function's own history/comments for the exact bug
#      that caused). See _attach_supporting_doc_row() below.
#   8. If the matched record is an Expense, the PDF is attached via
#      zoho_client.attach_receipt_to_expense() (kept from the earlier
#      design, still UNVERIFIED LIVE -- see that method's own docstring).
#      SCOPE NOTE: attachment is Expense-only in this first version, same
#      as the discarded design's own step-8 scope -- this codebase has no
#      confirmed Zoho attach mechanism for Journal/Vendor Payment/Bank
#      Transfer yet, so a row that matches one of those types gets a clear
#      "found, but attachment isn't supported for that posting type yet"
#      status rather than a silent no-op or a guessed API call.
#
# Steps 2-3 ("scan the invoice with Gemini"), 4 ("check billed to"), 5
# ("check the sub amount"), 6-7 ("strip VAT, correct the amount, set Tax
# Treatment") of the ORIGINAL 8-step spec are gone entirely -- no PDF is
# read for its CONTENT any more, it's simply located and attached as-is.
# =============================================================================


def _attach_supporting_doc_row(row: dict, onedrive_client, zoho_client, organization_id: str,
                                docs_folder: str, tmp_dir: str) -> tuple:
    """Runs the Supporting Document Attachment pipeline against ONE
    already-posted statement row (a dict from categorize_from_module.
    extract_attachment_check_rows()'s "rows" list): find the invoice PDF in
    `docs_folder` by this row's raw statement reference, identify the Zoho
    record to attach to DIRECTLY from this row's own "Zoho Posting
    Reference" text (row["zoho_reference"], e.g. "Expense 12345" -- the
    exact record post_row() posted this row as), attach the PDF if that
    record is an Expense. Never raises -- every failure mode becomes a
    clear status string, same RESULT TEXT CONVENTION as
    posting_service.post_row(): a status starting with "Attached" is the
    only success shape (checked by extract_attachment_check_rows()'s own
    idempotency filter on the next run); "NOT ATTACHED: ..." for anything
    this function could check but didn't find/match; "ERROR: ..." for an
    actual API/network failure. Returns (status_text, detail_dict) --
    detail_dict carries the matched record type/id and invoice file name
    purely for the widget's per-row detail columns, never used for the
    status logic itself.

    REVISED 2026-08-13, per Ravindra ("why is it even using date it has to
    just check the notes.desc field right"): this used to search Zoho fresh
    for a same-day record whose notes/description text matched this row's
    narration (posting_service._ALL_DESCRIPTION_SEARCHES /
    _normalize_text_for_dup_check()) -- date range was only ever a
    candidate-narrowing mechanism (Zoho's API has no "search by free-text
    field" endpoint), never part of the actual match, and it turned out
    fragile in practice (a live false-negative traced to row["txn_date"]
    not being formatted before hitting Zoho's date_start/date_end filter).
    Since this row's own "Zoho Posting Reference" cell already says EXACTLY
    which Zoho record it is, posting_service.parse_zoho_reference() now
    just parses that directly -- no search, no date scoping, no text
    comparison, and no way for this step to disagree with what actually got
    posted."""
    reference_number = row.get("reference_number")
    if not reference_number:
        return ("NOT ATTACHED: no reference value on this row -- nothing to search OneDrive for an "
                "invoice with.", {})

    try:
        candidates = onedrive_client.find_files_containing(docs_folder, reference_number)
    except Exception as e:
        return (f"ERROR: couldn't search OneDrive folder {docs_folder!r} for reference {reference_number!r} "
                f"-- {e}", {})

    if not candidates:
        return (f"NOT ATTACHED: no invoice file found in {docs_folder!r} matching reference "
                f"{reference_number!r}.", {})
    if len(candidates) > 1:
        names = [c.get("name") for c in candidates]
        return (f"NOT ATTACHED: {len(candidates)} candidate invoice files matched reference "
                f"{reference_number!r} in {docs_folder!r} ({names}) -- ambiguous, not auto-picking one.", {})

    invoice_item = candidates[0]
    invoice_file_name = invoice_item.get("name") or "invoice.pdf"
    local_invoice_path = os.path.join(tmp_dir, f"row{row['excel_row']}_{invoice_file_name}")
    try:
        onedrive_client.download_file(invoice_item, local_invoice_path)
    except Exception as e:
        return (f"ERROR: found {invoice_file_name!r} but couldn't download it from OneDrive -- {e}",
                {"invoice_file_name": invoice_file_name})

    record_type, record_id = posting_service.parse_zoho_reference(row.get("zoho_reference"))
    if not record_id:
        return (f"NOT ATTACHED: found invoice {invoice_file_name!r}, but couldn't tell which Zoho record this "
                f"row was posted as (Zoho Posting Reference: {row.get('zoho_reference')!r}).",
                {"invoice_file_name": invoice_file_name})

    if record_type != posting_service._EXPENSE_PREFIX:
        return (f"NOT ATTACHED: found invoice {invoice_file_name!r} -- this row was posted as {record_type} "
                f"{record_id}, but attachment is only supported for Expense postings so far.",
                {"invoice_file_name": invoice_file_name, "record_type": record_type, "record_id": record_id})

    try:
        zoho_client.attach_receipt_to_expense(
            record_id, local_invoice_path, file_name=invoice_file_name, organization_id=organization_id
        )
    except Exception as e:
        return (f"ERROR: found matching Expense {record_id} but attaching {invoice_file_name!r} failed -- {e}",
                {"invoice_file_name": invoice_file_name, "record_type": record_type, "record_id": record_id})

    status_text = f"Attached: {invoice_file_name} -> Expense {record_id}"
    return (status_text, {"invoice_file_name": invoice_file_name, "record_type": record_type, "record_id": record_id})


@app.route("/attach-supporting-docs", methods=["POST"])
def attach_supporting_docs():
    """Supporting Document Attachment -- see the module-level comment block
    above (starting "SUPPORTING DOCUMENT ATTACHMENT") for the full,
    scoped-down feature and what it supersedes. Same overall shape as
    /post-transactions: prep work (download, load workbook, add the
    Supporting Required column, extract the rows that need an attachment)
    happens synchronously up front with a normal JSON-error-response-on-
    failure; the actual per-row find/attach work is STREAMED as NDJSON (one
    event object per line, mimetype application/x-ndjson), same "live
    status" UX as every other long-running widget action in this project.

    Request body: {file_name, folder_path, rules, organization_id,
    bank_account_number} -- file_name/folder_path/rules/organization_id are
    the same fields/meaning as /post-transactions (rules is the raw
    Categorization module records, rebuilt server-side via build_rules(),
    same as there; folder_path is the SELECTED ORG's own mapped OneDrive
    folder, from /org-folder-map, resolved client-side same as every other
    endpoint that takes it). bank_account_number is NEW here -- the real
    bank account number (NOT the Zoho account_id /post-transactions' own
    confusingly-named "account_number" field actually holds -- see that
    route's own comment) of whichever account the widget's caller selected,
    used to build the "docs/<bank_account_number>/" subfolder this route
    searches for invoices under `folder_path`.

    Event shapes (one JSON object per line):
      {"type": "start", "success": true, "file_name", "folder_path",
       "docs_folder", "supporting_required_yes": <int>,
       "supporting_required_no": <int>, "already_attached": <int -- rows
       this run is skipping because an earlier run already attached their
       document>, "total_to_check": <int>}
      {"type": "row", "row": {excel_row, category, narration,
       reference_number, txn_date, debit_amount, zoho_reference, status,
       invoice_file_name, record_type, record_id}, "stats": {total_to_check,
       attached, not_attached, errors}}
      {"type": "done", "success": true, "file_name", "uploaded_file_name",
       "stats": {...final...}}
      {"type": "error", "success": false, "file_name", "error": "..."} --
       same "can't switch to a 500 mid-stream" reasoning as
       /post-transactions' own docstring.

    UNVERIFIED LIVE (flagged like every other new endpoint in this
    project): the Zoho attach endpoint
    (zoho_books_client.attach_receipt_to_expense()) and the docs/<account>/
    OneDrive folder convention this route depends on have NOT been run
    against a real live Zoho org/OneDrive yet -- see that method's own
    docstring for the specific assumption flagged there; this whole route
    needs a real end-to-end run before being trusted unattended."""
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    body = request.get_json(force=True, silent=True) or {}
    file_name = str(body.get("file_name", "")).strip()
    folder_path = body.get("folder_path") or DEFAULT_FOLDER_PATH
    raw_rules = body.get("rules") or []
    organization_id = str(body.get("organization_id", "")).strip() or None
    bank_account_number = str(body.get("bank_account_number", "")).strip()

    print(f"[main] /attach-supporting-docs {file_name!r}: organization_id={organization_id!r}, "
          f"bank_account_number={bank_account_number!r}", flush=True)

    if not file_name:
        return jsonify({"success": False, "error": "file_name is required (from a prior /list-files call)"}), 400
    if not bank_account_number:
        return jsonify({"success": False, "file_name": file_name,
                         "error": "bank_account_number is required -- the real bank account number this "
                                  "statement belongs to, used to find its docs/<account>/ OneDrive subfolder."}), 400
    if not raw_rules:
        return jsonify({"success": False, "file_name": file_name,
                         "error": "rules is required -- fetch them from GET /categorization-rules and pass the "
                                  "records straight through, same as /categorize."}), 400

    rules = build_rules(raw_rules)
    if not rules:
        return jsonify({"success": False, "file_name": file_name,
                         "error": "No usable categorization rules found in the records passed."}), 400

    # "based on the orgid onedrive map folder it goes in to docs/<account
    # no>/" -- folder_path is the org's own mapped OneDrive folder (already
    # resolved client-side, same as /post-transactions); this route just
    # appends the fixed "docs/<bank_account_number>" subpath under it.
    docs_folder = f"{folder_path.strip('/')}/docs/{bank_account_number}"

    tmp = tempfile.mkdtemp()
    try:
        onedrive_client = OneDriveClient(OneDriveConfig.from_env())
        zoho_client = ZohoBooksClient(ZohoConfig.from_env())

        print(f"[main] /attach-supporting-docs {file_name!r}: downloading...", flush=True)
        local_path = onedrive_client.download_file_by_path(folder_path, file_name, os.path.join(tmp, file_name))

        # NOT data_only=True -- same reasoning as /post-transactions: this
        # workbook gets saved again below (Supporting Required + Attachment
        # Status columns), and the live Debit Account/Credit Account/
        # Posting Type formulas must survive that save untouched.
        wb = openpyxl.load_workbook(local_path)
        sr_result = add_supporting_required_column(wb, rules)
        print(f"[main] /attach-supporting-docs {file_name!r}: Supporting Required column -- "
              f"{sr_result['yes_count']} Yes, {sr_result['no_count']} No.", flush=True)
        extraction = extract_attachment_check_rows(wb, rules)
        rows = extraction["rows"]
        print(f"[main] /attach-supporting-docs {file_name!r}: {len(rows)} row(s) to attach "
              f"(total_supporting_required={extraction['total_supporting_required']}, "
              f"already_attached={extraction['already_attached']}), docs_folder={docs_folder!r}.", flush=True)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"[main] /attach-supporting-docs {file_name!r}: prep failed -- {e}", flush=True)
        return jsonify({"success": False, "file_name": file_name, "error": str(e)}), 400

    def generate():
        results = {}
        stats = {"total_to_check": len(rows), "attached": 0, "not_attached": 0, "errors": 0}
        try:
            yield json.dumps({
                "type": "start", "success": True, "file_name": file_name, "folder_path": folder_path,
                "docs_folder": docs_folder,
                "supporting_required_yes": sr_result["yes_count"], "supporting_required_no": sr_result["no_count"],
                "already_attached": extraction["already_attached"], "total_to_check": len(rows),
            }) + "\n"

            for row in rows:
                status_text, detail = _attach_supporting_doc_row(
                    row, onedrive_client, zoho_client, organization_id, docs_folder, tmp
                )
                results[row["excel_row"]] = status_text
                if status_text.lower().startswith("attached"):
                    stats["attached"] += 1
                elif status_text.upper().startswith("ERROR"):
                    stats["errors"] += 1
                else:
                    stats["not_attached"] += 1
                print(f"[main] /attach-supporting-docs {file_name!r}: row {row['excel_row']} "
                      f"(category={row.get('category')!r}) -> {status_text}", flush=True)
                yield json.dumps({
                    "type": "row",
                    "row": {
                        "excel_row": row["excel_row"], "category": row.get("category"),
                        "narration": row.get("narration"), "reference_number": row.get("reference_number"),
                        "txn_date": str(row["txn_date"]) if row.get("txn_date") is not None else None,
                        "debit_amount": row.get("debit_amount"), "zoho_reference": row.get("zoho_reference"),
                        "status": status_text, "invoice_file_name": detail.get("invoice_file_name"),
                        "record_type": detail.get("record_type"), "record_id": detail.get("record_id"),
                    },
                    "stats": dict(stats),
                }) + "\n"

            write_attachment_results(wb, results)
            print(f"[main] /attach-supporting-docs {file_name!r}: checked ({stats}), saving...", flush=True)
            wb.save(local_path)
            print(f"[main] /attach-supporting-docs {file_name!r}: uploading back to OneDrive...", flush=True)
            onedrive_client.upload_file(local_path, folder_path, file_name)
            print(f"[main] /attach-supporting-docs {file_name!r}: upload SUCCEEDED", flush=True)

            yield json.dumps({
                "type": "done", "success": True, "file_name": file_name, "uploaded_file_name": file_name,
                "stats": stats,
            }) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "success": False, "file_name": file_name, "error": str(e)}) + "\n"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/reconcile", methods=["POST"])
def reconcile():
    """Month-end bank statement reconciliation -- new feature, 2026-08-13,
    per Ravindra's "i want to write a reconciliation system ... select a
    predefined month end statement and does various checks." See
    reconciliation_service.py's own module docstring for the full design
    (the 3 confirmed checks, what "the books" means for each, and the
    UNVERIFIED LIVE Zoho endpoints this depends on).

    Unlike /post-transactions or /attach-supporting-docs, this is a single
    file's worth of read-only computation (no workbook is modified or
    re-uploaded) -- plain one-shot JSON, not streamed.

    Request body: {folder_path, bank_account_number, account_id, month
    (1-12), year, organization_id, file_name (optional)}.
      - bank_account_number: the statement's own real bank account number
        (NOT the Zoho account_id -- same distinction /attach-supporting-
        docs' own bank_account_number field draws), used both to find the
        right OneDrive file (find_month_end_statement_file()'s digit-run
        match) and for the statement-vs-expected account-number sanity
        check reconciliation_service.py's own docstring describes.
      - account_id: this account's real Zoho account_id (what /post-
        transactions' own confusingly-named "account_number" field
        actually holds elsewhere in this codebase) -- used for checks 1
        and 2's two live Zoho calls (get_bank_account_balance()/find_bank_
        account_transactions()). Check 3 (2026-08-13, later same day
        redesign -- narration-first matching, see reconciliation_service.
        py's match_statement_rows_to_zoho() docstring) searches Zoho
        directly via `zoho_client`/`organization_id` instead and does NOT
        use account_id at all -- it isn't restricted to this specific bank
        account.
      - file_name (optional): skip find_month_end_statement_file()'s
        filename matching entirely and use this exact file -- for when the
        automatic match is ambiguous or wrong and the caller already knows
        which file they want (the widget surfaces this as a manual picker
        once an ambiguous-match error comes back, rather than a dead end).

    Response: {"success": true, "file_name", "folder_path", ...every key
    reconciliation_service.run_reconciliation() returns spread in} on
    success. On a "file not found"/"ambiguous match" outcome specifically,
    returns {"success": false, "error", "candidates_checked": [...every
    file name in the folder...], "matched": [...whatever DID match, if
    ambiguous...]} -- listed rather than guessed, same convention as
    onedrive_client.find_file_by_name()'s own FileNotFoundError."""
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    body = request.get_json(force=True, silent=True) or {}
    folder_path = body.get("folder_path") or DEFAULT_FOLDER_PATH
    bank_account_number = str(body.get("bank_account_number", "")).strip()
    account_id = str(body.get("account_id", "")).strip()
    organization_id = str(body.get("organization_id", "")).strip() or None
    explicit_file_name = str(body.get("file_name", "")).strip()
    try:
        month = int(body.get("month"))
        year = int(body.get("year"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "month (1-12) and year (e.g. 2026) are required integers."}), 400
    if not (1 <= month <= 12):
        return jsonify({"success": False, "error": f"month must be 1-12, got {month!r}."}), 400

    print(f"[main] /reconcile: bank_account_number={bank_account_number!r}, account_id={account_id!r}, "
          f"month={month!r}, year={year!r}, organization_id={organization_id!r}, "
          f"explicit_file_name={explicit_file_name!r}", flush=True)

    if not bank_account_number:
        return jsonify({"success": False, "error": "bank_account_number is required -- used to find the right "
                                                     "month-end statement file and for the account-number sanity check."}), 400
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required -- this account's real Zoho account_id, "
                                                     "used for the live balance/transactions calls."}), 400

    try:
        onedrive_client = OneDriveClient(OneDriveConfig.from_env())
        items = onedrive_client.list_files_in_folder(folder_path)
    except Exception as e:
        print(f"[main] /reconcile: couldn't list OneDrive folder {folder_path!r} -> {e}", flush=True)
        return jsonify({"success": False, "error": f"Couldn't list OneDrive folder {folder_path!r}: {e}"}), 502
    all_file_names = [item.get("name") for item in items if item.get("name")]

    if explicit_file_name:
        if explicit_file_name not in all_file_names:
            return jsonify({
                "success": False,
                "error": f"{explicit_file_name!r} not found in OneDrive folder {folder_path!r}.",
                "candidates_checked": all_file_names,
            }), 404
        file_name = explicit_file_name
    else:
        match = reconciliation_service.find_month_end_statement_file(all_file_names, bank_account_number, month, year)
        if len(match["matched"]) == 0:
            return jsonify({
                "success": False,
                "error": (f"No month-end statement file found for account {bank_account_number!r}, "
                          f"{calendar.month_name[month]} {year} in folder {folder_path!r} -- looked for a "
                          f"filename containing the account number's digits, the year {match['year_token']!r}, "
                          f"and one of {match['month_tokens_tried']}."),
                "candidates_checked": all_file_names,
            }), 404
        if len(match["matched"]) > 1:
            return jsonify({
                "success": False,
                "error": (f"{len(match['matched'])} files matched account {bank_account_number!r}, "
                          f"{calendar.month_name[month]} {year} -- ambiguous, not guessing. Pass file_name "
                          f"explicitly to pick one."),
                "matched": match["matched"],
            }), 409
        file_name = match["matched"][0]

    tmp = tempfile.mkdtemp()
    try:
        print(f"[main] /reconcile: downloading {file_name!r}...", flush=True)
        local_path = onedrive_client.download_file_by_path(folder_path, file_name, os.path.join(tmp, file_name))
        # data_only=True is fine (unlike /post-transactions' own workbook
        # load) -- this file is never saved/re-uploaded, only read.
        wb = openpyxl.load_workbook(local_path, data_only=True)

        zoho_client = ZohoBooksClient(ZohoConfig.from_env())
        print(f"[main] /reconcile: fetching Zoho balance for account_id={account_id!r}...", flush=True)
        balance_info = zoho_client.get_bank_account_balance(account_id, organization_id=organization_id)

        date_start = f"{year:04d}-{month:02d}-01"
        last_day = calendar.monthrange(year, month)[1]
        date_end = f"{year:04d}-{month:02d}-{last_day:02d}"
        # 2026-08-14 (later still), per Ravindra: "can you actually pull the
        # transactions for the specified time period using Journal records
        # in Zoho where you get all the debit and credit entries for each
        # of the transactions for the selected account" -- primary source
        # is now list_account_transactions() (Zoho's own General Ledger
        # view of this account, GET /chartofaccounts/{account_id}/
        # transactions), NOT find_bank_account_transactions()'s per-object
        # feed. If that new (UNVERIFIED LIVE) endpoint call itself fails
        # outright -- wrong path/shape entirely, not just an unrecognized
        # record shape -- falls back to the old feed rather than taking the
        # whole /reconcile call down, same "defense in depth, don't let an
        # unverified guess break a working feature" reasoning as this
        # service's other fallback paths.
        print(f"[main] /reconcile: fetching Zoho general-ledger transactions for account_id={account_id!r}, "
              f"{date_start}..{date_end}...", flush=True)
        try:
            zoho_transactions = zoho_client.list_account_transactions(
                account_id, date_start, date_end, organization_id=organization_id
            )
            zoho_transactions_source = "list_account_transactions (General Ledger)"
        except Exception as e:
            print(f"[main] /reconcile: list_account_transactions() failed ({e!r}) -- falling back to "
                  f"find_bank_account_transactions()...", flush=True)
            zoho_transactions = zoho_client.find_bank_account_transactions(
                account_id, date_start, date_end, organization_id=organization_id
            )
            zoho_transactions_source = "find_bank_account_transactions (fallback, per-object feed)"
        print(f"[main] /reconcile: Zoho returned {len(zoho_transactions)} record(s) via "
              f"{zoho_transactions_source} for account_id={account_id!r}, {date_start}..{date_end}", flush=True)

        result = reconciliation_service.run_reconciliation(
            wb, balance_info, zoho_transactions, zoho_client, organization_id, account_id,
            bank_account_number, month, year
        )
        print(f"[main] /reconcile: {file_name!r} -> overall_pass={result['overall_pass']}, "
              f"balance_pass={result['checks']['balance_match']['pass']}, "
              f"aggregate_pass={result['checks']['aggregate_match']['pass']} "
              f"(zoho_total_debit={result['checks']['aggregate_match']['zoho_total_debit']}, "
              f"zoho_total_credit={result['checks']['aggregate_match']['zoho_total_credit']}, "
              f"zoho_unclassified_count={result['checks']['aggregate_match']['zoho_unclassified_count']}), "
              f"all_posted_pass={result['checks']['all_posted']['pass']} "
              f"({result['checks']['all_posted']['unmatched_count']} unmatched)", flush=True)
        return jsonify({"success": True, "file_name": file_name, "folder_path": folder_path, **result})
    except FileNotFoundError as e:
        return jsonify({"success": False, "file_name": file_name, "error": str(e)}), 404
    except ValueError as e:
        # reconciliation_service.py raises ValueError for a genuinely
        # unrecognizable statement shape (no header/Debit/Credit/Date/
        # Running Balance column) -- surfaced as-is, same convention as
        # /post-transactions' own extract_postable_rows() ValueError handling.
        return jsonify({"success": False, "file_name": file_name, "error": str(e)}), 400
    except Exception as e:
        import traceback
        print(f"[main] /reconcile: EXCEPTION -> {e}\n{traceback.format_exc()}", flush=True)
        return jsonify({"success": False, "file_name": file_name, "error": str(e)}), 500
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.route("/post-bank-charges", methods=["POST"])
def post_bank_charges():
    """Month-end Bank Charges/VAT consolidation -- 2026-08-10, per Ravindra:
    "when checked (usually i do it at the month end with the complete bank
    statement) i will add all the bank charges and make just 1 entry with
    latest entry date and ER#/description as Bank Charges - VAT (debit
    amount) ... The posting should also find those bank entries which do
    not have corresponding VAT entries ... made a similar entry ... as Bank
    Charges Non-VAT with tax treatment as Out of Scope."

    Deliberately a SEPARATE route from /post-transactions, not a flag on it
    -- see categorize_from_module.py's extract_postable_rows() docstring:
    Bank charges/VAT on bank charges rows are NEVER posted individually
    through /post-transactions, checkbox or not. This route is only ever
    called when the widget's "Bank Charges Included/Excluded" checkbox is
    checked, and only ONCE PER ACCOUNT (not once per file) -- takes a LIST
    of every OneDrive file matched to that account this run (Ravindra: "There
    can be multiple statements for a single bank account with multiple date
    ranges within a month"), since a Bank charges row and its corresponding
    VAT row can land in two different files. The widget is responsible for
    calling this BEFORE calling /post-transactions for the same account's
    files this same run (see post_widget.js's runPosting()) -- both routes
    download, modify, and re-upload the SAME OneDrive file(s), so running
    them concurrently for the same file risks one save clobbering the
    other's write; sequencing this route first (and awaiting its response)
    before /post-transactions downloads its own fresh copy avoids that.

    NOT streamed (plain one-shot JSON, unlike /post-transactions/
    /delete-postings) -- this route makes at most three Zoho API calls total
    (one GET /settings/taxes to resolve the VAT-matched entry's tax_id via
    find_tax_id(), added 2026-08-11, plus one consolidated Expense each for
    the VAT-matched and non-VAT buckets), not one call per row, so there's
    no meaningful "live per-row progress" to stream; a normal JSON response
    with a real HTTP status on error is simpler and sufficient here.

    Request body: {"file_names": [...], "folder_path", "account_number",
    "organization_id", "rules"} -- same "rules" shape as /categorize and
    /post-transactions (the raw Categorization module records; fetch via
    GET /categorization-rules and pass straight through).

    Response: {"success": true, "bank_rows_found": int, "vat_rows_found":
    int, "matched_pairs": int, "unmatched_bank_rows": int,
    "vat_consolidated": {"posted": bool, "reference": str|None, "amount":
    float|None, "contributing_rows": int, "reason": str (only if posted is
    false)}, "non_vat_consolidated": {...same shape...}, "files_updated":
    [file_name, ...] -- files this route actually re-uploaded (only those
    with at least one contributing row; a file with no Bank charges/VAT
    rows this run is left untouched)}."""
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    body = request.get_json(force=True, silent=True) or {}
    file_names = body.get("file_names") or []
    folder_path = body.get("folder_path") or DEFAULT_FOLDER_PATH
    raw_rules = body.get("rules") or []
    account_number = str(body.get("account_number", "")).strip()
    organization_id = str(body.get("organization_id", "")).strip() or None

    print(f"[main] /post-bank-charges: file_names={file_names!r}, account_number={account_number!r}, "
          f"organization_id={organization_id!r}", flush=True)

    if not file_names:
        return jsonify({"success": False, "error": "file_names is required -- a non-empty list of every "
                                                      "OneDrive file matched to this account this run."}), 400
    if not raw_rules:
        return jsonify({"success": False, "error": "rules is required -- fetch them from GET "
                                                      "/categorization-rules and pass the records straight "
                                                      "through, same as /categorize and /post-transactions."}), 400

    rules = build_rules(raw_rules)
    if not rules:
        return jsonify({"success": False, "error": "No usable categorization rules found in the records "
                                                      "passed -- nothing to post against."}), 400

    tmp = tempfile.mkdtemp()
    try:
        onedrive_client = OneDriveClient(OneDriveConfig.from_env())
        zoho_client = ZohoBooksClient(ZohoConfig.from_env())

        # Download + extract EVERY file up front (not streamed) -- pooling
        # every file's bank_charge_rows together is the whole point (see
        # this route's docstring): a matching pair can straddle two files.
        # per_file_state keeps each file's own already-open workbook +
        # extraction result alive so the write-back loop below can go
        # straight back into the SAME objects, instead of re-downloading.
        per_file_state = []
        pooled_bank_charge_rows = []
        for file_name in file_names:
            file_name = str(file_name or "").strip()
            if not file_name:
                continue
            print(f"[main] /post-bank-charges: downloading {file_name!r}...", flush=True)
            local_path = onedrive_client.download_file_by_path(folder_path, file_name, os.path.join(tmp, file_name))
            # Same NOT-data_only=True reasoning as /post-transactions -- this
            # workbook gets saved again below (if it contributes any rows),
            # and loading with data_only=True would silently strip the live
            # Debit Account/Credit Account/Posting Type formulas on save.
            wb = openpyxl.load_workbook(local_path)
            extraction = extract_postable_rows(wb, rules, account_number)
            for row in extraction["bank_charge_rows"]:
                row["_file_name"] = file_name  # tags each row with its own source file for the write-back loop below
            pooled_bank_charge_rows.extend(extraction["bank_charge_rows"])
            per_file_state.append({"file_name": file_name, "local_path": local_path, "wb": wb, "extraction": extraction})
            print(f"[main] /post-bank-charges: {file_name!r} -- {len(extraction['bank_charge_rows'])} Bank "
                  f"charges/VAT on bank charges row(s) not yet posted.", flush=True)
    except FileNotFoundError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"[main] /post-bank-charges: FileNotFoundError -> {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 404
    except ValueError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"[main] /post-bank-charges: ValueError -> {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        import traceback
        print(f"[main] /post-bank-charges: EXCEPTION -> {e}\n{traceback.format_exc()}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500

    try:
        bank_rows_found = sum(1 for r in pooled_bank_charge_rows if _category_base(r["category"]).lower() == BANK_CHARGES_CATEGORY.lower())
        vat_rows_found = len(pooled_bank_charge_rows) - bank_rows_found
        match_result = match_bank_charges_to_vat(pooled_bank_charge_rows)
        matched_pairs = match_result["matched_pairs"]
        unmatched_bank_rows = match_result["unmatched_bank_rows"]
        print(f"[main] /post-bank-charges: {bank_rows_found} Bank charges row(s), {vat_rows_found} VAT on bank "
              f"charges row(s) pooled across {len(per_file_state)} file(s) -- {len(matched_pairs)} matched "
              f"pair(s), {len(unmatched_bank_rows)} unmatched Bank charges row(s).", flush=True)

        # excel_row_result_by_file: {file_name: {excel_row: result_text}} --
        # accumulated below from whichever of the two consolidated postings
        # actually succeeds, then written back into each file's own
        # workbook in one pass at the end (see write_posting_results()).
        excel_row_result_by_file = defaultdict(dict)

        def _post_consolidated(contributing_rows, label, tax_treatment, tax_id=None):
            """Builds and posts ONE synthetic consolidated Expense row for
            `contributing_rows` (a non-empty list of real Bank charges row
            dicts, all sharing the same account+category) -- reuses
            posting_service.post_row() exactly as an ordinary Bank charges
            row would, so duplicate-detection, the notes/description field,
            and every other _post_expense() behavior apply unchanged. Debit
            Account/Credit Account are copied from the FIRST contributing
            row rather than re-derived -- every row here is the same
            category on the same statement account, so they're already
            identical. tax_id (2026-08-11) is only ever passed for the
            VAT-matched call -- see create_expense()'s docstring for why the
            Non-VAT/out_of_scope call leaves it unset.

            Returns (result_dict, result_text) -- result_text is now ALWAYS
            the real outcome text (success reference, "NOT POSTED: duplicate
            detected -- ...", or any other "NOT POSTED: ...") rather than
            None on anything but success (2026-08-2x fix, per Ravindra:
            "Successful processing of posts whether duplicate or success it
            should change to COMPLETED as before"). Previously this returned
            None for every non-success outcome, so the write-back loop below
            (which only writes a row when `result_text` is truthy) silently
            never recorded a "duplicate detected" outcome into the
            contributing rows' own Posted cells at all -- unlike an ordinary
            /post-transactions row, where every outcome (success, duplicate,
            or a genuine failure reason) always gets written. That meant a
            file whose ONLY remaining content was duplicate-resolved Bank
            charges/VAT rows had nothing on record marking them settled, so
            extract_postable_rows() kept re-collecting them into
            "bank_charge_rows" forever and /post-transactions' completion
            check (which has no visibility into bank charge rows at all)
            could never see them as resolved -- the file could never reach
            .COMPLETED. Now every outcome is written, and
            extract_postable_rows() recognizes a written "duplicate
            detected" note on a Bank charges/VAT row as resolved (see its
            "bank_charges_resolved" stat) without re-pooling it -- see that
            function's docstring for why a consolidated aggregate is treated
            as settled rather than re-checked every run the way an ordinary
            row's duplicate is."""
            amount = sum((posting_service._amount_as_float(r["amount"]) or 0.0) for r in contributing_rows)
            dates = [posting_service.format_txn_date(r["txn_date"]) for r in contributing_rows]
            dates = [d for d in dates if d]
            latest_date = max(dates) if dates else None
            sample = contributing_rows[0]
            synthetic_row = {
                "category": BANK_CHARGES_CATEGORY,
                "posting_type": sample.get("posting_type"),
                "debit_account": sample.get("debit_account"),
                "credit_account": sample.get("credit_account"),
                "account_number": sample.get("account_number"),
                "amount": amount,
                "narration": label,
                "reference_number": label,
                "txn_date": latest_date,
                "tax_treatment": tax_treatment,
                "tax_id": tax_id,
            }
            if not latest_date:
                reason = (f"NOT POSTED: Couldn't determine a valid date from any of the "
                          f"{len(contributing_rows)} contributing row(s).")
                return {"posted": False, "reason": reason}, reason
            result_text = posting_service.post_row(zoho_client, synthetic_row, organization_id)
            if _is_success_reference(result_text):
                return {"posted": True, "reference": result_text, "amount": amount,
                        "contributing_rows": len(contributing_rows), "txn_date": latest_date}, result_text
            return {"posted": False, "reason": result_text}, result_text

        vat_consolidated = {"posted": False, "reason": "No Bank charges row had a matching VAT on bank "
                                                         "charges row this run."}
        if matched_pairs:
            contributing_bank_rows = [pair[0] for pair in matched_pairs]
            # Resolve the "Standard Rate [5%]" tax_id live rather than
            # hardcoding a guessed GUID -- see zoho_books_client.py's
            # find_tax_id() docstring. Failing this lookup (rate renamed,
            # missing, or ambiguous in this org) is reported as a clean
            # NOT POSTED reason instead of posting an Expense with a wrong
            # or missing tax rate.
            try:
                vat_tax_id = zoho_client.find_tax_id(
                    _VAT_MATCHED_TAX_RATE_NAME, percentage=_VAT_MATCHED_TAX_RATE_PERCENTAGE,
                    organization_id=organization_id,
                )
                vat_consolidated, result_text = _post_consolidated(
                    contributing_bank_rows, "Bank Charges - VAT", _TAX_TREATMENT_VAT_MATCHED,
                    tax_id=vat_tax_id,
                )
            except ValueError as e:
                reason = (f"NOT POSTED: Couldn't resolve this org's {_VAT_MATCHED_TAX_RATE_NAME!r} tax rate: {e}")
                vat_consolidated, result_text = {"posted": False, "reason": reason}, reason
            if result_text:
                # BOTH sides of every matched pair are marked consumed --
                # the VAT row contributed to this same consolidated total
                # exactly as much as its Bank charges partner did, even
                # though only the Bank charges side's AMOUNT was summed
                # (see this route's docstring / posting_service.py's module
                # docstring for why). Written for EVERY outcome now (success,
                # duplicate, or a genuine failure reason), not just success
                # -- see _post_consolidated()'s docstring for why (2026-08-2x
                # fix, ".COMPLETED" for duplicate-resolved bank charges).
                for bank_row, vat_row in matched_pairs:
                    excel_row_result_by_file[bank_row["_file_name"]][bank_row["excel_row"]] = result_text
                    excel_row_result_by_file[vat_row["_file_name"]][vat_row["excel_row"]] = result_text

        non_vat_consolidated = {"posted": False, "reason": "Every Bank charges row this run had a matching "
                                                             "VAT on bank charges row -- nothing left over."}
        if unmatched_bank_rows:
            non_vat_consolidated, result_text = _post_consolidated(unmatched_bank_rows, "Bank Charges Non-VAT",
                                                                     _TAX_TREATMENT_NON_VAT)
            if result_text:
                for bank_row in unmatched_bank_rows:
                    excel_row_result_by_file[bank_row["_file_name"]][bank_row["excel_row"]] = result_text

        files_updated = []
        for state in per_file_state:
            rows_to_write = excel_row_result_by_file.get(state["file_name"])
            if not rows_to_write:
                continue  # this file contributed nothing to either successful posting -- leave it untouched
            write_posting_results(state["wb"], state["extraction"], rows_to_write)
            state["wb"].save(state["local_path"])
            onedrive_client.upload_file(state["local_path"], folder_path, state["file_name"])
            files_updated.append(state["file_name"])
            print(f"[main] /post-bank-charges: wrote {len(rows_to_write)} consolidated reference(s) into "
                  f"{state['file_name']!r} and re-uploaded it.", flush=True)

        return jsonify({
            "success": True,
            "bank_rows_found": bank_rows_found,
            "vat_rows_found": vat_rows_found,
            "matched_pairs": len(matched_pairs),
            "unmatched_bank_rows": len(unmatched_bank_rows),
            "vat_consolidated": vat_consolidated,
            "non_vat_consolidated": non_vat_consolidated,
            "files_updated": files_updated,
        })
    except Exception as e:
        import traceback
        print(f"[main] /post-bank-charges: EXCEPTION -> {e}\n{traceback.format_exc()}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.route("/delete-postings", methods=["POST"])
def delete_postings():
    """Deletes one or more postings from Zoho Books and clears their
    statement's "Zoho Posting Reference" cell -- see this file's docstring
    for the full route description (used by both a single row's Delete
    button and the widget's "Delete All" button).

    STREAMING (2026-08-09, still later), per Ravindra: "delete all option
    should also show the live status/count as it is deleting, currently it
    is deleting silently and updating at the end only" -- same NDJSON
    streaming shape as /post-transactions (see that route's docstring for
    the full reasoning): validation (`items` required) still returns a
    normal JSON 400 on failure, but once deletion starts, the response
    becomes one JSON event per line:
      {"type": "start", "total": <int>} -- sent once, immediately.
      {"type": "item", "result": {file_name, excel_row, zoho_reference,
       success, message}, "counts": {deleted, failed, total_attempted}} --
       one per item, the instant THAT item's own Zoho delete call returns
       (not batched per file the way the old one-shot response was).
      {"type": "warning", "file_name", "excel_row", "message"} -- for the
       one edge case a per-item event can't express: this item's Zoho
       delete already succeeded (and its own "item" event already went out)
       before a LATER failure in the same file group (clearing the
       "Zoho Posting Reference" cell / re-uploading) -- the delete itself
       is NOT undone, just its local bookkeeping didn't complete. Can't
       retroactively change an "item" event already sent, so this is a
       separate, explicit heads-up instead of silently dropping it.
      {"type": "done", "success": true, "results": [...every item...],
       "deleted", "failed"} -- results is included again in full here for
       any client that only wants the final state, same as before this
       update.
      {"type": "error", ...} -- an unexpected exception outside the normal
       per-group error handling below; same "can't switch to a real HTTP
       error status once streaming has started" reasoning as
       /post-transactions."""
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    body = request.get_json(force=True, silent=True) or {}
    organization_id = str(body.get("organization_id", "")).strip() or None
    items = body.get("items") or []
    if not items:
        return jsonify({
            "success": False,
            "error": "items is required -- a list of {file_name, folder_path, excel_row, zoho_reference} to delete.",
        }), 400

    # Grouped by (file_name, folder_path) so each file is downloaded/
    # re-uploaded ONCE per call regardless of how many of its rows are being
    # deleted together -- important for "Delete All", which can span every
    # file processed across a whole posting run.
    groups = {}
    group_order = []
    for item in items:
        key = (str(item.get("file_name", "")).strip(), item.get("folder_path") or DEFAULT_FOLDER_PATH)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(item)

    zoho_client = ZohoBooksClient(ZohoConfig.from_env())
    onedrive_client = OneDriveClient(OneDriveConfig.from_env())

    def generate():
        results = []
        deleted_count = 0
        failed_count = 0

        def emit_item(file_name, excel_row, zoho_reference, success, message):
            nonlocal deleted_count, failed_count
            result = {"file_name": file_name, "excel_row": excel_row, "zoho_reference": zoho_reference,
                      "success": success, "message": message}
            results.append(result)
            if success:
                deleted_count += 1
            else:
                failed_count += 1
            return json.dumps({
                "type": "item",
                "result": result,
                "counts": {"deleted": deleted_count, "failed": failed_count,
                           "total_attempted": deleted_count + failed_count},
            }) + "\n"

        try:
            yield json.dumps({"type": "start", "total": len(items)}) + "\n"

            for (file_name, folder_path) in group_order:
                group_items = groups[(file_name, folder_path)]
                if not file_name:
                    for item in group_items:
                        yield emit_item(file_name, item.get("excel_row"), item.get("zoho_reference"),
                                         False, "file_name is required for each item.")
                    continue

                try:
                    # `with` is safe here (unlike /post-transactions'
                    # tempdir) since the whole download/delete/save/upload
                    # sequence for this group -- yields included -- runs
                    # inside this one block, all within a single pass of
                    # this generator; nothing needs the directory to
                    # outlive it.
                    with tempfile.TemporaryDirectory() as tmp:
                        local_path = onedrive_client.download_file_by_path(folder_path, file_name, os.path.join(tmp, file_name))
                        # NOT data_only=True -- same reasoning as
                        # /post-transactions: this workbook is saved again
                        # below, and must not strip the live Debit
                        # Account/Credit Account/Posting Type formulas.
                        wb = openpyxl.load_workbook(local_path)

                        rows_to_clear = []
                        for item in group_items:
                            excel_row = item.get("excel_row")
                            zoho_reference = item.get("zoho_reference")
                            print(f"[main] /delete-postings {file_name!r} row {excel_row!r}: deleting "
                                  f"{zoho_reference!r}...", flush=True)
                            del_result = posting_service.delete_posting(zoho_client, zoho_reference, organization_id)
                            print(f"[main] /delete-postings {file_name!r} row {excel_row!r}: {del_result}", flush=True)
                            if del_result["success"] and isinstance(excel_row, int):
                                rows_to_clear.append(excel_row)
                            yield emit_item(file_name, excel_row, zoho_reference,
                                             del_result["success"], del_result["message"])

                        if rows_to_clear:
                            clear_posting_reference(wb, rows_to_clear)
                            wb.save(local_path)
                            onedrive_client.upload_file(local_path, folder_path, file_name)
                            print(f"[main] /delete-postings {file_name!r}: cleared {len(rows_to_clear)} row(s)' "
                                  f"reference and re-uploaded.", flush=True)
                except Exception as e:
                    import traceback
                    print(f"[main] /delete-postings {file_name!r}: EXCEPTION -> {e}\n{traceback.format_exc()}", flush=True)
                    # Only items from this group that don't already have a
                    # result (a delete-from-Zoho call that succeeded before
                    # this exception -- e.g. during download/save/upload --
                    # keeps its own real per-item result, already streamed,
                    # rather than being overwritten as failed).
                    already_reported_rows = {r["excel_row"] for r in results if r["file_name"] == file_name}
                    for item in group_items:
                        if item.get("excel_row") not in already_reported_rows:
                            yield emit_item(file_name, item.get("excel_row"), item.get("zoho_reference"),
                                             False, f"Couldn't process {file_name!r}: {e}")
                        else:
                            # This item's own "item" event (a genuine
                            # success) already went out above -- can't
                            # retroactively change it, so flag the
                            # reference-clear/re-upload failure separately
                            # instead of silently losing it.
                            yield json.dumps({
                                "type": "warning", "file_name": file_name, "excel_row": item.get("excel_row"),
                                "message": f"Deleted from Zoho, but couldn't clear its Zoho Posting Reference "
                                           f"cell / re-upload {file_name!r}: {e}. The Zoho-side delete itself "
                                           "succeeded -- safe to ignore, or manually blank that row's reference "
                                           "cell so it's eligible to post again.",
                            }) + "\n"

            yield json.dumps({
                "type": "done",
                "success": True,
                "results": results,
                "deleted": deleted_count,
                "failed": failed_count,
            }) + "\n"
        except Exception as e:
            import traceback
            print(f"[main] /delete-postings: EXCEPTION during streaming -> {e}\n{traceback.format_exc()}", flush=True)
            yield json.dumps({"type": "error", "success": False, "error": str(e)}) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/categorization-rules", methods=["GET"])
def categorization_rules():
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    module_api_name = request.args.get("module_api_name")
    if not module_api_name:
        return jsonify({"success": False, "error": "module_api_name query param is required"}), 400

    # organization_id is now REQUIRED (2026-08-06, per Ravindra -- see
    # CATEGORIZATION_ENTITY_FIELD above): every Categorization record must
    # have cf_entity set to a real organization_id, and rules are always
    # scoped to whichever organization is actually being processed. Rather
    # than silently falling back to "every record" when this is omitted
    # (which would defeat the whole point -- a caller that forgot to pass it
    # would get every org's rules mixed together again), this 400s clearly
    # so a missing param is caught immediately instead of surfacing later as
    # a confusing wrong-category result.
    organization_id = request.args.get("organization_id")
    if not organization_id:
        return jsonify({
            "success": False,
            "error": "organization_id query param is required -- Categorization rules are scoped per "
                     f"organization via each record's {CATEGORIZATION_ENTITY_FIELD!r} field.",
        }), 400

    try:
        client = ZohoBooksClient(ZohoConfig.from_env())
        all_records = client.list_custom_module_records(module_api_name)
    except Exception as e:
        return jsonify({"success": False, "error": f"Couldn't read module {module_api_name!r}: {e}"}), 502

    wanted_org_id = str(organization_id).strip()
    records = []
    skipped_blank = 0
    skipped_mismatch_raw = []  # (raw cf_entity, extracted id) for non-matching, non-blank records -- diagnostic only
    for rec in all_records:
        raw_entity = rec.get(CATEGORIZATION_ENTITY_FIELD)
        if not str(raw_entity or "").strip():
            skipped_blank += 1
            continue
        extracted_org_id = _extract_org_id_from_cf_entity(raw_entity)
        if extracted_org_id == wanted_org_id:
            records.append(rec)
        else:
            skipped_mismatch_raw.append((raw_entity, extracted_org_id))

    # Debug logging, same style as this project's other per-org filters
    # (/org-folder-map, /bank-accounts) -- printed so a Cloud Run log read
    # immediately shows whether a low/zero rule count for an org is because
    # (a) that org genuinely has no rules yet, (b) some records have
    # cf_entity blank/unset and are being silently excluded, or (c) cf_entity
    # is set but didn't parse down to the requested org_id (shows the raw
    # value + what was extracted from it, so a real shape mismatch -- e.g.
    # neither a bare id nor the confirmed "id - name" format -- is visible
    # immediately instead of looking identical to "no rules configured").
    print(f"[main] /categorization-rules module={module_api_name!r} organization_id={wanted_org_id!r}: "
          f"{len(records)}/{len(all_records)} record(s) matched (cf_entity field="
          f"{CATEGORIZATION_ENTITY_FIELD!r}); {skipped_blank} record(s) have a blank/missing "
          f"{CATEGORIZATION_ENTITY_FIELD!r}; {len(skipped_mismatch_raw)} record(s) have it set but it didn't "
          f"match this org -- raw/extracted samples: {skipped_mismatch_raw[:5]!r}", flush=True)

    return jsonify({
        "success": True,
        "module_api_name": module_api_name,
        "organization_id": organization_id,
        "records": records,
        "total_records_in_module": len(all_records),
    })


def _normalize_entities(records: list) -> list:
    """Normalizes raw "Entities" custom-module records (one per legal
    entity/org -- Qatar, Saudi, EGP, ... -- see main.py's docstring) into the
    plain shape the widget's entity picker needs. Blank/malformed rows
    (no cf_entity_name) are skipped rather than failing the whole list, same
    tolerant approach as build_rules() in categorize_from_module.py.

    cf_organization_id is carried through even though nothing POSTS against
    a different Zoho organization yet -- that's a separate, not-yet-built
    phase (posting an entity's transactions into ITS OWN Zoho Books org
    rather than the one this service's credentials point at). Capturing the
    field here now means the Entities module doesn't need a schema change
    later when that phase starts -- only new backend code that starts
    reading a field that's already sitting there, blank or not.

    entity_id is just the entity's own name -- there's no need for Zoho's
    internal record ID anywhere in this feature (nothing looks an entity
    record up or updates it by ID), so using the name as the stable
    selection key avoids relying on an unconfirmed field name for something
    that isn't actually needed.

    NOTE (2026-08-12, later same day): this docstring used to also describe
    an invoice_folder_path field (cf_invoice_folder_path, a per-entity
    OneDrive invoice folder) added for the original Supporting Document
    VERIFICATION design. That design was discarded the same day in favor of
    a simpler Supporting Document ATTACHMENT feature that finds invoices
    under the EXISTING org-folder-map's own mapped folder (see main.py's
    /attach-supporting-docs) instead of a separate per-entity field -- so
    cf_invoice_folder_path is no longer read here; no new Entities-module
    field is needed for this feature at all."""
    entities = []
    for rec in records:
        name = str(rec.get("cf_entity_name") or "").strip()
        if not name:
            continue
        entities.append({
            "entity_id": name,
            "entity_name": name,
            "onedrive_folder_path": str(rec.get("cf_onedrive_folder_path") or "").strip(),
            "organization_id": str(rec.get("cf_organization_id") or "").strip() or None,
        })
    return entities


@app.route("/entities", methods=["GET"])
def entities():
    """Returns every configured entity/org (Qatar, Saudi, EGP, ... -- one
    record per entity in the "Entities" custom module, ENTITIES_MODULE_API_NAME
    -- default "cm_entities") with its OneDrive folder path, so the "Start
    Categorizing" widget can let the person pick which entity's statements
    to process THIS run, instead of always reading the one hardcoded
    MS_STATEMENTS_FOLDER_PATH folder. Pass ?module_api_name=... to override
    which module is read, same pattern as /categorization-rules.

    Returns {"success": true, "entities": []} (not an error) if the module
    is empty or doesn't exist yet -- a customer who hasn't set this module
    up yet should see "no entities configured", and the widget falls back
    to the old hardcoded-folder behavior in that case (see widget.js's
    fetchEntitiesSafely()), not a broken run."""
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    module_api_name = request.args.get("module_api_name") or ENTITIES_MODULE_API_NAME
    try:
        client = ZohoBooksClient(ZohoConfig.from_env())
        raw_records = client.list_custom_module_records(module_api_name)
    except Exception as e:
        return jsonify({"success": False, "error": f"Couldn't read entities module {module_api_name!r}: {e}"}), 502

    return jsonify({
        "success": True,
        "module_api_name": module_api_name,
        "entities": _normalize_entities(raw_records),
    })


@app.route("/organizations", methods=["GET"])
def organizations():
    """Returns every Zoho Books organization this deployment's login can see
    (GET /organizations, no organization_id needed) -- 2026-08-01, per
    Ravindra, confirmed live against the real tenant. Backs the widget's
    live "which organization" popup: no separate credential set per entity,
    just whichever orgs this one login is already a member of. An org this
    login was never added to as a user simply won't be in this list -- fix
    that in Zoho (add the user to that org), not here."""
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    try:
        client = ZohoBooksClient(ZohoConfig.from_env())
        orgs = client.list_organizations()
    except Exception as e:
        return jsonify({"success": False, "error": f"Couldn't list organizations: {e}"}), 502

    return jsonify({
        "success": True,
        "organizations": [
            {
                "organization_id": str(o.get("organization_id") or ""),
                "name": o.get("name"),
                "plan_name": o.get("plan_name"),
                "country": o.get("country"),
                "currency_code": o.get("currency_code"),
            }
            for o in orgs
        ],
    })


@app.route("/org-folder-map", methods=["GET"])
def org_folder_map():
    """Returns the org_id -> OneDrive-folder-path mapping from the
    cm_orgidonedrivemap custom module in CBK (ORG_FOLDER_MODULE_API_NAME) --
    2026-08-01, per Ravindra. Pairs with GET /organizations: the widget's
    popup shows every org that call returns, but can only actually START a
    run for an org that ALSO has a folder mapped here -- one row per
    organization_id, holding ORG_FOLDER_ORGID_FIELD (the org_id as text) and
    ORG_FOLDER_PATH_FIELD (its OneDrive folder path). Rows with a blank
    org-id field are skipped rather than failing the whole list, same
    tolerant approach as every other module reader in this file.

    Returns {"success": true, "map": {}} (not an error) if the module is
    empty or doesn't exist yet -- the widget treats "no mapping for this
    org" as "can't run this org until one's added", not a broken page."""
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    module_api_name = request.args.get("module_api_name") or ORG_FOLDER_MODULE_API_NAME
    try:
        client = ZohoBooksClient(ZohoConfig.from_env())
        raw_records = client.list_custom_module_records(module_api_name)
    except Exception as e:
        return jsonify({"success": False, "error": f"Couldn't read org-folder-map module {module_api_name!r}: {e}"}), 502

    org_folder_map_dict = {}
    for rec in raw_records:
        org_id = str(rec.get(ORG_FOLDER_ORGID_FIELD) or "").strip()
        if not org_id:
            continue
        org_folder_map_dict[org_id] = str(rec.get(ORG_FOLDER_PATH_FIELD) or "").strip()

    return jsonify({
        "success": True,
        "module_api_name": module_api_name,
        "map": org_folder_map_dict,
    })


@app.route("/bank-accounts", methods=["GET"])
def bank_accounts():
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    # Optional (2026-08-01, per Ravindra): pull a DIFFERENT org's bank
    # accounts than this deployment's own default -- lets the widget's
    # org-picker popup show real accounts for whichever organization was
    # selected (see GET /organizations above). Omitted -> unchanged, uses
    # this client's own configured organization_id, same as before this
    # param existed.
    organization_id = request.args.get("organization_id") or None

    try:
        client = ZohoBooksClient(ZohoConfig.from_env())
        accounts = client.list_bank_accounts(organization_id=organization_id)
    except Exception as e:
        return jsonify({"success": False, "error": f"Couldn't read bank accounts: {e}"}), 502

    # Trimmed to just what the widget needs to resolve a statement's own
    # account number to its real account_id client-side -- no reason to ship
    # balances/routing numbers/etc. across the wire for this. Filtered to
    # only accounts that:
    #   1. actually have an account_id (2026-08-01, per Ravindra: "i want
    #      only the real bank accounts with an account id") -- skips
    #      anything Zoho's Banking module returns without one, rather than
    #      showing an unselectable/broken checkbox for it in the widget.
    #   2. have account_type == "bank" (2026-08-02, per Ravindra: "i want
    #      only the bank accounts with the type Bank only") -- Zoho's
    #      Banking module (GET /bankaccounts) lists Bank, Cash, and Credit
    #      Card type accounts together (confirmed from Ravindra's own Chart
    #      of Accounts screenshot: "Petty Cash"/"Undeposited Funds" are
    #      account_type "cash" and were showing up in the picker alongside
    #      genuine bank accounts like "Payment Clearing Account"/"Commercial
    #      Bank Qatar USD"). NOT yet confirmed live what Zoho's raw
    #      account_type string value actually is for a "Bank"-type account
    #      (assumed lowercase "bank", matching the lowercase/underscore
    #      convention Zoho uses elsewhere, e.g. "other_current_asset") -- if
    #      this filters out accounts that SHOULD show, or still lets Cash
    #      accounts through, the fix is checking the raw `account_type`
    #      value in a direct `/bank-accounts` curl response (per DEPLOY.md)
    #      and adjusting BANK_ACCOUNT_TYPE_VALUES below to match.
    return jsonify({
        "success": True,
        "organization_id": organization_id,
        "accounts": [
            {
                "account_id": a.get("account_id"),
                "account_name": a.get("account_name"),
                "account_number": a.get("account_number"),
            }
            for a in accounts
            if a.get("account_id")
            and str(a.get("account_type") or "").strip().lower() in BANK_ACCOUNT_TYPE_VALUES
        ],
    })


@app.route("/update-status", methods=["POST"])
def update_status():
    unauthorized = _check_auth()
    if unauthorized:
        return unauthorized

    body = request.get_json(force=True, silent=True) or {}
    module_api_name = str(body.get("module_api_name", "")).strip()
    record_id = str(body.get("record_id", "")).strip()
    fields = body.get("fields") or {}

    if not module_api_name or not record_id:
        return jsonify({"success": False, "error": "module_api_name and record_id are required"}), 400
    if not fields:
        return jsonify({"success": False, "error": "fields is required, e.g. {\"cf_status\": \"Categorization Complete\"}"}), 400

    try:
        client = ZohoBooksClient(ZohoConfig.from_env())
        result = client.update_custom_module_record(module_api_name, record_id, fields)
    except Exception as e:
        return jsonify({"success": False, "error": f"Couldn't update {module_api_name}/{record_id}: {e}"}), 502

    return jsonify({"success": True, "module_api_name": module_api_name, "record_id": record_id, "result": result})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))