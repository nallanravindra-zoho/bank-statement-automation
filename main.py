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
                                 Posting Type columns) and re-uploads the
                                 result to OneDrive, overwriting the original
                                 file in place. Safe to re-run on the same
                                 file -- any columns a previous run already
                                 added are replaced, not duplicated. `rules`
                                 in the request body is REQUIRED (the widget
                                 fetches them itself via /categorization-rules
                                 below and sends them straight through). After
                                 saving, runs a headless LibreOffice
                                 recalculation pass (_recalculate_formulas(),
                                 2026-08-02) so Debit Account/Credit Account/
                                 Posting Type's live formulas show a real
                                 value in any viewer immediately, not just
                                 once someone opens the file for real in
                                 Excel -- non-fatal if it fails (the formulas
                                 are still correct either way; requires
                                 libreoffice-calc in this service's container,
                                 see Dockerfile/DEPLOY.md).

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

  (POST /post-transactions -- REMOVED 2026-08-02, per Ravindra: "i dont want
                                 any posting logic as of now ... i will use it
                                 later." This deployment no longer imports or
                                 exposes any posting code path at all -- see
                                 the note just above this file's imports for
                                 exactly what was removed and why (it's what
                                 was crashing the container). posting_service.py
                                 and categorize_from_module.py's
                                 extract_postable_rows()/write_posting_results()
                                 are untouched on disk for whenever this is
                                 wanted again -- nothing was deleted, just
                                 unwired from this file.)

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
import os
import re
import shutil
import subprocess
import tempfile

import openpyxl
from flask import Flask, jsonify, request

from categorize_from_module import build_rules, categorize_workbook
from onedrive_client import OneDriveClient, OneDriveConfig
from zoho_books_client import ZohoBooksClient, ZohoConfig

# NOTE (2026-08-02, per Ravindra): posting logic (POST /post-transactions,
# posting_service.post_rows(), categorize_from_module.extract_postable_rows()/
# write_posting_results()) is deliberately NOT imported/wired up here right
# now -- Ravindra asked to remove it from what's actually deployed until
# he's ready to use it, since a real deploy just crashed on startup with
# "ImportError: cannot import name 'extract_postable_rows' from
# 'categorize_from_module'" (the deployed categorize_from_module.py on Cloud
# Run was an older copy that predates those two functions -- a version
# mismatch between files, same class of bug as the July 23 stale-deploy
# issue). Removing the import here means main.py no longer depends on
# categorize_from_module.py having those two functions at all, so this
# class of crash can't recur regardless of which exact categorize_from_module.py
# ends up alongside this file. posting_service.py and
# categorize_from_module.py's extract_postable_rows()/write_posting_results()
# are untouched and still on disk -- nothing was deleted, just unwired from
# this file -- so the posting feature can be reconnected later by adding
# the import back and restoring the /post-transactions route (see this
# project's git history / the July 26 update in the project doc for the
# route's exact shape if it's needed again).

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

    file_names = [item.get("name") for item in items]
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
    print(f"[main] /list-files folder={folder_path!r}: {len(file_names)} file(s): {file_names}", flush=True)

    return jsonify({
        "success": True,
        "folder_path": folder_path,
        "files": file_names,
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
            out_wb, stats = categorize_workbook(wb, rules, account_number)
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


# NOTE: POST /post-transactions used to live here (reads an already-
# categorized file's Debit/Credit Account + Posting Type and posts each row
# to Zoho Books). Removed 2026-08-02 per Ravindra ("i dont want any posting
# logic as of now ... i will use it later") -- this is exactly what was
# crashing the deployed container (see the import-removal note above this
# file's imports). The route's full logic isn't lost: posting_service.py
# (post_rows()) and categorize_from_module.py's extract_postable_rows()/
# write_posting_results() are still on disk, untouched -- reintroducing this
# endpoint later is just restoring this route + its two imports.


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
    that isn't actually needed."""
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