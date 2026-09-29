/* ============================================================================
 * CONFIG -- same GCP deployment as widget.js. Fill GCP_API_KEY in before
 * packaging (same value as widget.js's GCP_API_KEY).
 * ==========================================================================*/

const CATEGORIZATION_MODULE_API_NAME = "cm_categorisation";

const GCP_BASE_URL = "https://bank-statement-automation-184927294160.us-central1.run.app";
const GCP_LIST_FILES_ENDPOINT = GCP_BASE_URL + "/list-files";
const GCP_CATEGORIZATION_RULES_ENDPOINT = GCP_BASE_URL + "/categorization-rules";
const GCP_BANK_ACCOUNTS_ENDPOINT = GCP_BASE_URL + "/bank-accounts";
const GCP_ORGANIZATIONS_ENDPOINT = GCP_BASE_URL + "/organizations";
const GCP_ORG_FOLDER_MAP_ENDPOINT = GCP_BASE_URL + "/org-folder-map";
const GCP_POST_TRANSACTIONS_ENDPOINT = GCP_BASE_URL + "/post-transactions";
const GCP_POST_BANK_CHARGES_ENDPOINT = GCP_BASE_URL + "/post-bank-charges";
const GCP_DELETE_POSTINGS_ENDPOINT = GCP_BASE_URL + "/delete-postings";
const GCP_API_KEY = "3dc32b352e8ec01e3958407d3d77f296226a0a7d316ccc1b";

/* ==========================================================================
 * Widget logic -- this widget deliberately runs INDEPENDENTLY of the
 * "Start Categorizing" widget (widget.js): it picks its OWN organization and
 * accounts (2026-08-09, per Ravindra's confirmed design answer, "Own org +
 * account picker"), rather than reusing whatever a prior categorize run in
 * the same Zoho session happened to select. Same org-picker/account-checklist
 * pattern as widget.js, duplicated here rather than shared, since these are
 * two separate widget pages (post_widget.html has its own <script>, not a
 * second button inside widget.html) -- mirrors the "Start Categorizing"
 * button's own structure, same as the original July 26 design.
 * ==========================================================================*/

let orgList = [];
let orgFolderMap = {};
let categorizationRules = [];
let categorizationRulesError = null;
let currentOrgAccounts = [];
let checkedAccountIds = {};
let selectedOrg = null;

// Every posting row seen across the whole run (from every /post-transactions
// call, across every selected account/file) -- each entry augmented with
// `deleted` (set true once a successful delete comes back) so the table can
// grey it out in place instead of re-fetching anything. Indexed by array
// position; DOM row ids are "posting-row-<index>".
let allPostingRows = [];

// Pagination (2026-08-09, per Ravindra: "i need a full view of the rows
// may be 20 rows and paginated if more" -- replaces the old fixed-height
// inner-scrollbox table, which he flagged as clumsy). 0-based page index;
// reset to 0 whenever a fresh posting run starts (see startPosting()).
// Delete actions do NOT reset the page or remove rows from allPostingRows
// (a deleted row stays visible, marked "Deleted", per the existing
// "keep history" design) -- so page count stays stable across deletes.
var POSTINGS_PAGE_SIZE = 20;
var postingsCurrentPage = 0;

// True from startPosting() until every account/file has finished streaming
// (2026-08-09, later still) -- used only to pick the right empty-state
// message in renderPostingsPanel() ("posting in progress, rows will appear
// as they post" vs. the old "no postable rows found" message, which only
// makes sense once a run has actually finished with zero rows).
var postingRunActive = false;

// True from deleteRows() until its whole delete run (single row or "Delete
// All") finishes streaming (2026-08-09, still later) -- per Ravindra:
// "delete all option should also show the live status/count as it is
// deleting, currently it is deleting silently and updating at the end
// only." Drives renderPostingsPanel()'s count-label text (shows live
// "Deleting X of Y..." progress instead of the normal row-count summary)
// and disables every Delete button while a run is in flight, so a second
// delete can't be kicked off (and interleave its own events/rows) before
// the first one finishes.
var deleteRunActive = false;
var deleteRunProgress = { done: 0, total: 0 };

document.addEventListener("DOMContentLoaded", init);

function init() {
  console.log("post_widget.js: DOMContentLoaded fired, calling ZFAPPS.extension.init()...");

  var initWatchdog = setTimeout(function () {
    console.error("post_widget.js: ZFAPPS.extension.init() did not call back within 8s.");
    showError(
      "The Zoho widget SDK (zf_sdk.js) never finished initializing (no error, it just never called back). " +
      "Check the Network tab for a request to zf_sdk.js and confirm it returned 200."
    );
  }, 8000);

  ZFAPPS.extension.init()
    .then(function () {
      clearTimeout(initWatchdog);
      console.log("post_widget.js: ZFAPPS.extension.init() resolved successfully.");
      // Enlarged again 2026-08-09 (later the same day) -- Ravindra asked for
      // "a full view of the rows may be 20 rows... make the widget bigger
      // enough to see atleaset 20 rows will all the columns visible",
      // replacing the old fixed-height inner-scrollbox table (see
      // post_widget.css). 820x820 was sized for the org/account picker
      // alone; this needs real room for a ~20-row table with 7 columns on
      // top of that.
      ZFAPPS.invoke("RESIZE", { width: "1180px", height: "920px" });

      return Promise.all([
        fetchOrganizationsSafely(),
        fetchOrgFolderMapSafely(),
      ]);
    })
    .then(function (results) {
      orgList = results[0];
      orgFolderMap = results[1];
      console.log("post_widget.js: organizations -> " + orgList.length + " org(s).");
      console.log("post_widget.js: org -> OneDrive folder map -> " + JSON.stringify(orgFolderMap, null, 2));

      if (!orgList.length) {
        throw new Error(
          "No Zoho Books organizations were returned for this login (GET /organizations came back empty, or " +
          "failed -- see console). Nothing to show in the organization picker."
        );
      }

      showOrgSelectPanel();
    })
    .catch(function (err) {
      clearTimeout(initWatchdog);
      console.error("post_widget.js: error during init/load ->", err);
      showError(
        "Couldn't load organizations/accounts: " + (err && err.message ? err.message : err) +
        ". See the browser console for the raw response."
      );
    });

  document.getElementById("close-btn").addEventListener("click", function () {
    ZFAPPS.closeModal();
  });
}

function fetchOrganizations() {
  return fetch(GCP_ORGANIZATIONS_ENDPOINT, { method: "GET", headers: { "X-Api-Key": GCP_API_KEY } })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /organizations (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) throw new Error(result.error || "Unknown error from /organizations");
      return result.organizations || [];
    });
}
function fetchOrganizationsSafely() {
  return fetchOrganizations().catch(function (err) {
    console.error("post_widget.js: couldn't fetch /organizations ->", err);
    return [];
  });
}

function fetchOrgFolderMap() {
  return fetch(GCP_ORG_FOLDER_MAP_ENDPOINT, { method: "GET", headers: { "X-Api-Key": GCP_API_KEY } })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /org-folder-map (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) throw new Error(result.error || "Unknown error from /org-folder-map");
      return result.map || {};
    });
}
function fetchOrgFolderMapSafely() {
  return fetchOrgFolderMap().catch(function (err) {
    console.error("post_widget.js: couldn't fetch /org-folder-map ->", err);
    return {};
  });
}

function fetchBankAccountsForOrg(organizationIdToFetch) {
  var url = GCP_BANK_ACCOUNTS_ENDPOINT + "?organization_id=" + encodeURIComponent(organizationIdToFetch);
  return fetch(url, { method: "GET", headers: { "X-Api-Key": GCP_API_KEY } })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /bank-accounts (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) throw new Error(result.error || "Unknown error from /bank-accounts");
      return result.accounts || [];
    });
}
function fetchBankAccountsForOrgSafely(organizationIdToFetch) {
  return fetchBankAccountsForOrg(organizationIdToFetch).catch(function (err) {
    console.error("post_widget.js: couldn't fetch /bank-accounts for organization_id=" + organizationIdToFetch + " ->", err);
    return [];
  });
}

// /post-transactions needs the SAME rules the statement was categorized with
// (to resolve each row's accounts/posting type) -- fetched per-org, same
// lifecycle/endpoint as widget.js's fetchCategorizationRulesForOrg().
function fetchCategorizationRulesForOrg(orgId) {
  var url = GCP_CATEGORIZATION_RULES_ENDPOINT +
    "?module_api_name=" + encodeURIComponent(CATEGORIZATION_MODULE_API_NAME) +
    "&organization_id=" + encodeURIComponent(orgId);
  return fetch(url, { method: "GET", headers: { "X-Api-Key": GCP_API_KEY } })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /categorization-rules (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) throw new Error(result.error || "Unknown error from /categorization-rules");
      var records = result.records || [];
      if (!records.length) {
        throw new Error("No Categorization rules found for organization_id " + orgId + " -- nothing to post against.");
      }
      return records;
    });
}

function showError(message) {
  var statusEl = document.getElementById("status");
  statusEl.hidden = false;
  statusEl.className = "status error";
  statusEl.textContent = message;
}

/* ----------------------------------------------------------------------
 * Org-select popup -- same shape as widget.js's, just posting instead of
 * categorizing.
 * --------------------------------------------------------------------*/
function showOrgSelectPanel() {
  document.getElementById("status").hidden = true;

  var orgSelectEl = document.getElementById("org-select");
  orgSelectEl.innerHTML = "";
  orgList.forEach(function (org, i) {
    var option = document.createElement("option");
    option.value = String(i);
    option.textContent = org.name + " (" + org.organization_id + ")";
    orgSelectEl.appendChild(option);
  });

  document.getElementById("org-select-panel").hidden = false;

  orgSelectEl.addEventListener("change", function () {
    loadDataForSelectedOrg();
  });
  loadDataForSelectedOrg();

  document.getElementById("account-search").addEventListener("input", function (e) {
    renderAccountChecklist(e.target.value);
  });

  document.getElementById("org-start-btn").addEventListener("click", function onOrgStart() {
    var org = orgList[Number(orgSelectEl.value)];
    if (!org) {
      alert("No organization selected.");
      return;
    }
    var folderPath = orgFolderMap[org.organization_id];
    if (!folderPath) {
      alert("No OneDrive folder is mapped for \"" + org.name + "\" (" + org.organization_id + ") yet -- add a " +
        "record for organization_id " + org.organization_id + " in the org-folder-mapping module (cm_orgidonedrivemap) " +
        "before running this organization.");
      return;
    }
    if (categorizationRulesError) {
      alert("Categorization rules for \"" + org.name + "\" (" + org.organization_id + ") couldn't be loaded -- " +
        (categorizationRulesError.message || categorizationRulesError) + " Fix this and reselect the organization to retry.");
      return;
    }
    if (!categorizationRules.length) {
      alert("Categorization rules for \"" + org.name + "\" (" + org.organization_id + ") are still loading -- " +
        "wait a moment and try again.");
      return;
    }

    var accounts = currentOrgAccounts
      .filter(function (a) { return checkedAccountIds[a.account_id]; })
      .map(function (a) {
        return {
          label: (a.account_name || "") + (a.account_number ? " " + a.account_number : ""),
          account_id: a.account_id,
          account_number: a.account_number,
        };
      });
    if (!accounts.length) {
      alert("Select at least one bank account to process.");
      return;
    }

    document.getElementById("org-start-btn").removeEventListener("click", onOrgStart);

    // Bank Charges/VAT consolidation (2026-08-10, per Ravindra) -- default
    // UNCHECKED ("Excluded"), read fresh at click time same as every other
    // org-select-panel field above. See post_widget.html's checkbox and
    // runPosting()'s use of this flag below.
    var includeBankChargesEl = document.getElementById("include-bank-charges-checkbox");
    var includeBankCharges = !!(includeBankChargesEl && includeBankChargesEl.checked);

    selectedOrg = org;
    startPosting(accounts, folderPath, org, categorizationRules, includeBankCharges);
  });
}

function loadDataForSelectedOrg() {
  var orgSelectEl = document.getElementById("org-select");
  var listEl = document.getElementById("org-accounts-list");
  var org = orgList[Number(orgSelectEl.value)];
  if (!org) return;

  listEl.innerHTML = '<div class="account-checklist-empty">Loading accounts…</div>';
  checkedAccountIds = {};
  var searchEl = document.getElementById("account-search");
  if (searchEl) searchEl.value = "";
  categorizationRules = [];
  categorizationRulesError = null;

  var accountsPromise = fetchBankAccountsForOrgSafely(org.organization_id);
  var rulesPromise = fetchCategorizationRulesForOrg(org.organization_id)
    .catch(function (err) {
      console.error("post_widget.js: couldn't fetch /categorization-rules for organization_id=" +
        org.organization_id + " ->", err);
      categorizationRulesError = err;
      return [];
    });

  Promise.all([accountsPromise, rulesPromise]).then(function (results) {
    var stillSelected = orgList[Number(orgSelectEl.value)] === org;
    if (!stillSelected) return;

    currentOrgAccounts = results[0];
    categorizationRules = results[1];
    renderAccountChecklist("");
  });
}

function renderAccountChecklist(filterText) {
  var listEl = document.getElementById("org-accounts-list");

  if (!currentOrgAccounts.length) {
    listEl.innerHTML = '<div class="account-checklist-empty">No Bank-type accounts with a resolvable ' +
      "account_id were found in this organization.</div>";
    renderSelectedAccountsSummary();
    return;
  }

  var needle = (filterText || "").trim().toLowerCase();
  var filtered = !needle ? currentOrgAccounts : currentOrgAccounts.filter(function (account) {
    var label = ((account.account_name || "") + " " + (account.account_number || "")).toLowerCase();
    return label.indexOf(needle) !== -1;
  });

  listEl.innerHTML = "";
  if (!filtered.length) {
    listEl.innerHTML = '<div class="account-checklist-empty">No accounts match &ldquo;' +
      escapeHtml(filterText) + "&rdquo;.</div>";
    renderSelectedAccountsSummary();
    return;
  }

  filtered.forEach(function (account, i) {
    var row = document.createElement("label");
    row.className = "account-checkbox-row";
    var checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.id = "org-account-" + i;
    checkbox.dataset.accountId = account.account_id;
    checkbox.checked = !!checkedAccountIds[account.account_id];
    row.classList.toggle("checked", checkbox.checked);
    checkbox.addEventListener("change", function () {
      if (checkbox.checked) {
        checkedAccountIds[account.account_id] = true;
      } else {
        delete checkedAccountIds[account.account_id];
      }
      row.classList.toggle("checked", checkbox.checked);
      renderSelectedAccountsSummary();
    });
    var label = (account.account_name || "") + (account.account_number ? " " + account.account_number : "");
    row.appendChild(checkbox);
    var span = document.createElement("span");
    span.textContent = label;
    row.appendChild(span);
    listEl.appendChild(row);
  });

  renderSelectedAccountsSummary();
}

var MAX_SUMMARY_CHIPS = 5;
function renderSelectedAccountsSummary() {
  var el = document.getElementById("selected-accounts-summary");
  if (!el) return;
  var selected = currentOrgAccounts.filter(function (a) { return checkedAccountIds[a.account_id]; });

  updateStartButtonLabel(selected.length);

  if (!selected.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = "";
  selected.slice(0, MAX_SUMMARY_CHIPS).forEach(function (account) {
    var chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = account.account_name || account.account_number || "Account";
    el.appendChild(chip);
  });
  var extra = selected.length - MAX_SUMMARY_CHIPS;
  if (extra > 0) {
    var moreChip = document.createElement("span");
    moreChip.className = "chip";
    moreChip.textContent = "+" + extra + " more";
    el.appendChild(moreChip);
  }
}

function updateStartButtonLabel(count) {
  var btn = document.getElementById("org-start-btn");
  if (!btn) return;
  btn.textContent = "OK, Start Posting" + (count ? " (" + count + ")" : "");
}

function escapeHtml(str) {
  var div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

/* ----------------------------------------------------------------------
 * Posting run -- same "list files once, match client-side, fire per-
 * account calls in parallel" shape as widget.js's runCategorization(), just
 * calling /post-transactions instead of /categorize, and accumulating every
 * row's detail into allPostingRows for the results table below instead of
 * stamping any record fields.
 * --------------------------------------------------------------------*/
function startPosting(accounts, folderPath, org, rules, includeBankCharges) {
  // Fully hidden, not just disabled (2026-08-09, later still) -- per
  // Ravindra: "you can utilize the whole screen just show the name of the
  // selected org and account(s) at the top". Nothing left to pick once a
  // run is underway, and this widget doesn't offer a "back" flow -- reopen
  // the widget from Zoho to start a fresh run.
  document.getElementById("org-select-panel").hidden = true;

  document.getElementById("result-banner").hidden = true;
  allPostingRows = [];
  postingsCurrentPage = 0;
  postingRunActive = true;

  showRunHeader(org, accounts);

  // Shown immediately, not at the end -- per Ravindra: "start showing the
  // postings info live as they get updated not at the end." Renders its
  // "posting in progress" empty state until the first row/already-posted
  // event arrives.
  document.getElementById("postings-panel").hidden = false;
  renderPostingsPanel();

  runPosting(accounts, folderPath, org, rules, includeBankCharges);
}

// Compact header (2026-08-09, later still) -- replaces the old per-account
// progress-panel entirely. Just the org name, the selected account
// label(s), and a one-line status that setRunHeaderStatus() keeps updated
// live as each file streams in.
function showRunHeader(org, accounts) {
  document.getElementById("run-header-org").textContent = org.name || "";
  document.getElementById("run-header-accounts").textContent =
    accounts.map(function (a) { return a.label; }).join(", ");
  document.getElementById("run-header-status").textContent = "Starting…";
  document.getElementById("run-header").hidden = false;
}

function setRunHeaderStatus(text) {
  var el = document.getElementById("run-header-status");
  if (el) el.textContent = text;
}

function fetchOneDriveFileList(folderPath) {
  var url = GCP_LIST_FILES_ENDPOINT;
  if (folderPath) url += "?folder_path=" + encodeURIComponent(folderPath);
  return fetch(url, { method: "GET", headers: { "X-Api-Key": GCP_API_KEY } })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /list-files (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) throw new Error(result.error || "Unknown error from /list-files");
      return result.files || [];
    });
}

// Same digit-run matching as widget.js's matchFilesToAccounts().
function matchFilesToAccounts(accounts, allFiles) {
  return accounts.map(function (account) {
    var digitsOnly = (account.account_number || "").replace(/[^0-9]/g, "");
    if (!digitsOnly) digitsOnly = (account.label || "").replace(/[^0-9]/g, "");
    if (!digitsOnly) return [];
    return allFiles.filter(function (fileName) {
      return fileName.toLowerCase().indexOf(digitsOnly.toLowerCase()) !== -1;
    });
  });
}

// Parses one line of an NDJSON stream -- returns the parsed object, or null
// for a blank line (the trailing "" after the final \n) or a line that
// fails to parse (logged, not thrown, so one bad line can't kill the whole
// stream read).
function parseNdjsonLine(line) {
  var trimmed = (line || "").trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed);
  } catch (e) {
    console.error("post_widget.js: couldn't parse NDJSON line ->", line, e);
    return null;
  }
}

// Fallback for environments without ReadableStream support -- reads the
// whole NDJSON body at once and replays every line through onEvent in
// order. Functionally identical to the streaming path below, just not
// actually live (same "degrades gracefully" reasoning as main.py's
// UNVERIFIED LIVE note about Cloud Run's own buffering behavior -- either
// way the widget ends up with the same final state).
function consumeNdjsonText(text, onEvent) {
  var finalEvent = null;
  text.split("\n").forEach(function (line) {
    var evt = parseNdjsonLine(line);
    if (!evt) return;
    onEvent(evt);
    if (evt.type === "done" || evt.type === "error") finalEvent = evt;
  });
  return finalEvent;
}

// Generic NDJSON-streaming POST helper (2026-08-09, still later) -- shared
// by both callGcpPostTransactionsStreaming() (posting) and
// callGcpDeletePostingsStreaming() (deleting) below, since both endpoints
// now stream one JSON event per line in exactly the same shape (see each
// route's own docstring in main.py for its specific event types). Calls
// onEvent(evt) for every NDJSON line AS SOON as it arrives, and resolves
// with the final "done"/"error" event once the stream ends. Rejects only
// on a genuine network/pre-stream failure (bad input, a server-side error
// that happened before streaming even started, etc -- still a normal
// one-shot JSON error response with a real HTTP status, per each route's
// own "prep work fails normally, only the stream itself can't" reasoning).
function streamNdjsonRequest(url, body, onEvent) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Api-Key": GCP_API_KEY },
    body: JSON.stringify(body),
  }).then(function (resp) {
    var contentType = resp.headers.get("Content-Type") || "";
    if (contentType.indexOf("application/x-ndjson") === -1) {
      // Failed before streaming started -- still a normal one-shot JSON
      // error response with a real HTTP status.
      return resp.json().catch(function () {
        throw new Error("Non-JSON, non-NDJSON response (HTTP " + resp.status + ")");
      }).then(function (result) {
        throw new Error((result && result.error) || ("HTTP " + resp.status));
      });
    }

    if (!resp.body || !resp.body.getReader) {
      return resp.text().then(function (text) { return consumeNdjsonText(text, onEvent); });
    }

    var reader = resp.body.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    var finalEvent = null;

    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.value) {
          buffer += decoder.decode(chunk.value, { stream: !chunk.done });
          var lines = buffer.split("\n");
          buffer = lines.pop(); // last element is a partial (or empty) line -- held for next chunk
          lines.forEach(function (line) {
            var evt = parseNdjsonLine(line);
            if (!evt) return;
            onEvent(evt);
            if (evt.type === "done" || evt.type === "error") finalEvent = evt;
          });
        }
        if (chunk.done) {
          var trailing = parseNdjsonLine(buffer);
          if (trailing) {
            onEvent(trailing);
            if (trailing.type === "done" || trailing.type === "error") finalEvent = trailing;
          }
          return finalEvent;
        }
        return pump();
      });
    }

    return pump();
  });
}

// Streaming replacement for the old callGcpPostTransactions() (2026-08-09,
// later still), per Ravindra: "i want to see live posting of each post as
// it updates and count also updates live". Posts fileName's rows via
// /post-transactions, calling onEvent(evt) for every NDJSON line AS SOON as
// it arrives ({"type": "start"|"row"|"done"|"error", ...} -- see main.py's
// /post-transactions docstring for the full event shapes).
function callGcpPostTransactionsStreaming(fileName, accountId, folderPath, rules, organizationId, onEvent) {
  var body = {
    file_name: fileName,
    rules: rules,
    account_number: accountId,
    organization_id: organizationId,
  };
  if (folderPath) body.folder_path = folderPath;
  return streamNdjsonRequest(GCP_POST_TRANSACTIONS_ENDPOINT, body, onEvent);
}

// Bank Charges/VAT consolidation (2026-08-10, per Ravindra -- see
// post_widget.html's checkbox and main.py's /post-bank-charges docstring for
// the full feature). Plain one-shot JSON, NOT NDJSON -- this route makes at
// most two Zoho calls total, not one per row, so there's nothing meaningful
// to stream live. Called ONCE PER ACCOUNT with every OneDrive file matched
// to that account this run (never once per file) -- see runPosting() below
// for why (a matching Bank charges/VAT pair can straddle two files).
function callGcpPostBankCharges(fileNames, accountId, folderPath, rules, organizationId) {
  var body = {
    file_names: fileNames,
    rules: rules,
    account_number: accountId,
    organization_id: organizationId,
  };
  if (folderPath) body.folder_path = folderPath;
  return fetch(GCP_POST_BANK_CHARGES_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Api-Key": GCP_API_KEY },
    body: JSON.stringify(body),
  }).then(function (resp) {
    return resp.json().catch(function () {
      throw new Error("Non-JSON response from /post-bank-charges (HTTP " + resp.status + ")");
    }).then(function (result) {
      if (!result.success) throw new Error(result.error || "Unknown error from /post-bank-charges");
      return result;
    });
  });
}

// Turns one /post-bank-charges response's vat_consolidated/non_vat_consolidated
// bucket into a row-detail object matching the SAME shape every other entry
// in allPostingRows already has (see runPosting()'s onEvent handler above),
// so it renders in the same table via the exact same rowClass()/
// referenceClass()/computeStats() logic -- no separate UI path needed.
// `consolidated: true` is the one extra field: excel_row is null (this
// entry isn't any single sheet row -- it's a SUM across many, possibly
// across more than one file), which renderPostingsPanel() uses to disable
// its own per-row Delete button (deleting a consolidated entry would need
// to also un-mark every contributing row across however many files it
// touched -- not something the existing single-row delete_posting() call
// can do; see that function's own note below for what to do instead).
function bankChargeBucketToRow(bucket, label, accounts, accountId) {
  var account = accounts.filter(function (a) { return a.account_id === accountId; })[0];
  return {
    file_name: "(consolidated -- " + (account ? account.label : accountId) + ")",
    folder_path: null,
    excel_row: null,
    category: "Bank charges",
    posting_type: "Expense",
    amount: bucket.posted ? bucket.amount : null,
    narration: label,
    txn_date: bucket.posted ? bucket.txn_date : null,
    zoho_reference: bucket.posted ? bucket.reference : ("NOT POSTED: " + (bucket.reason || "unknown reason")),
    posted: !!bucket.posted,
    newly_posted: true,
    consolidated: true,
    contributing_rows: bucket.contributing_rows || 0,
  };
}

// 2026-08-09 (later still), per Ravindra: "i want to see live posting of
// each post as it updates and count also updates live ... start showing
// the postings info live as they get updated not at the end." Each
// account's matched file(s) still post in parallel (Promise.all, same
// concurrency as before this change) -- only the PER-FILE detail is new:
// instead of waiting for one file's whole JSON response, every row (and
// the already-posted-earlier rows) streams in and gets pushed into
// allPostingRows + re-rendered the instant its own NDJSON line arrives, via
// callGcpPostTransactionsStreaming()'s onEvent callback below. Concurrent
// files' rows can interleave in the table as a result -- harmless, since
// every row already carries its own file_name column.
function runPosting(accounts, folderPath, org, rules, includeBankCharges) {
  var anyError = false;
  var totalFiles = 0;
  var doneFiles = 0;
  var noFileNotes = [];

  function updateFileProgress() {
    setRunHeaderStatus(totalFiles
      ? ("Posting " + doneFiles + " of " + totalFiles + " file(s)…")
      : "No matching files found for any selected account.");
  }

  fetchOneDriveFileList(folderPath)
    .then(function (allFiles) {
      var matchedFilesByIndex = matchFilesToAccounts(accounts, allFiles);
      totalFiles = matchedFilesByIndex.reduce(function (sum, files) { return sum + files.length; }, 0);
      updateFileProgress();

      var perAccountPromises = accounts.map(function (account, i) {
        var matchedFiles = matchedFilesByIndex[i] || [];
        if (!matchedFiles.length) {
          anyError = true;
          noFileNotes.push('No file found in OneDrive for "' + account.label + '" (searched ' +
            (folderPath || "the default folder") + ").");
          return Promise.resolve();
        }

        var accountId = account.account_id || "";

        // Bank Charges/VAT consolidation (2026-08-10, per Ravindra) -- if
        // checked, run ONCE per account, with EVERY matched file for that
        // account, and MUST finish (and its own upload(s) land) before this
        // account's normal per-file /post-transactions calls below start
        // downloading -- both routes can modify and re-upload the SAME
        // OneDrive file(s), so running them concurrently risks one save
        // clobbering the other's write (see main.py's /post-bank-charges
        // docstring). Not run at all when the checkbox is unchecked -- Bank
        // charges/VAT rows still get excluded from individual posting
        // either way (that part is unconditional, on the backend), this
        // only skips the EXTRA consolidated-posting step.
        var bankChargesStep = includeBankCharges
          ? callGcpPostBankCharges(matchedFiles, accountId, folderPath, rules, org.organization_id)
            .then(function (result) {
              allPostingRows.push(bankChargeBucketToRow(result.vat_consolidated, "Bank Charges - VAT", accounts, accountId));
              allPostingRows.push(bankChargeBucketToRow(result.non_vat_consolidated, "Bank Charges Non-VAT", accounts, accountId));
              renderPostingsPanel();
            })
            .catch(function (err) {
              anyError = true;
              var msg = 'Bank Charges consolidation failed for "' + account.label + '": ' +
                ((err && err.message) ? err.message : String(err));
              console.error("post_widget.js: /post-bank-charges failed ->", err);
              noFileNotes.push(msg);
            })
          : Promise.resolve();

        return bankChargesStep.then(function () {
          return Promise.all(matchedFiles.map(function (fileName) {
            return callGcpPostTransactionsStreaming(
              fileName, accountId, folderPath, rules, org.organization_id,
              function onEvent(evt) {
                if (evt.type === "start") {
                  (evt.already_posted_rows || []).forEach(function (row) {
                    allPostingRows.push(Object.assign({}, row, { deleted: false }));
                  });
                  renderPostingsPanel();
                } else if (evt.type === "row") {
                  allPostingRows.push(Object.assign({}, evt.row, { deleted: false }));
                  renderPostingsPanel();
                }
                // "done"/"error" events carry final stats, not a row -- the
                // table's own live-accumulated rows are already the source
                // of truth for counts (see computeStats()), so nothing else
                // to do with them here beyond the finalEvent check below.
              }
            )
              .then(function (finalEvent) {
                doneFiles++;
                updateFileProgress();
                if (!finalEvent || finalEvent.type !== "done") {
                  anyError = true;
                  var msg = (finalEvent && finalEvent.error) || "stream ended without a 'done' event";
                  console.error("post_widget.js: /post-transactions for " + fileName + " did not finish cleanly ->", msg);
                  noFileNotes.push(fileName + ": " + msg);
                } else if (finalEvent.errors > 0) {
                  // Same "any row errored -> flag the whole run" reasoning as
                  // the pre-streaming code's totalErrors check.
                  anyError = true;
                }
                // 2026-08-2x, per Ravindra's live report that .COMPLETED
                // wasn't firing even after the backend fix -- main.py's
                // /post-transactions "done" event now always explains WHY a
                // file wasn't marked complete (not_marked_complete_reason/
                // rename_error/completion_debug), instead of that reason
                // only ever going to Cloud Run logs Ravindra can't easily
                // check himself. Surfaced here as a console.log (not an
                // error -- a file simply not being eligible yet is normal,
                // not a failure) so it's visible in the browser's own
                // DevTools console on the next live run, without needing
                // server-side log access to diagnose.
                if (finalEvent && finalEvent.type === "done" && !finalEvent.file_marked_complete) {
                  console.log("post_widget.js: " + fileName + " not marked .COMPLETED -- " +
                    (finalEvent.not_marked_complete_reason || "(no reason given)"),
                    finalEvent.completion_debug || {});
                }
              })
              .catch(function (err) {
                doneFiles++;
                anyError = true;
                updateFileProgress();
                console.error("post_widget.js: streaming post failed for", fileName, "->", err);
                noFileNotes.push(fileName + ": " + ((err && err.message) ? err.message : String(err)));
              });
          }));
        });
      });

      return Promise.all(perAccountPromises);
    })
    .catch(function (err) {
      anyError = true;
      var msg = "Couldn't list the OneDrive folder: " + ((err && err.message) ? err.message : String(err));
      setRunHeaderStatus(msg);
      noFileNotes.push(msg);
    })
    .then(function () {
      postingRunActive = false;
      showDonePanel(!anyError, noFileNotes);
      renderPostingsPanel();
    });
}

function showDonePanel(allSucceeded, notes) {
  setRunHeaderStatus(allSucceeded ? "Done." : "Finished with some errors.");

  var banner = document.getElementById("result-banner");
  banner.hidden = false;
  banner.className = "result-banner" + (allSucceeded ? "" : " has-errors");
  document.getElementById("result-icon").textContent = allSucceeded ? "✓" : "!";
  document.getElementById("result-title").textContent = allSucceeded ? "All done!" : "Finished with some errors";
  document.getElementById("result-subtitle").textContent = allSucceeded
    ? "Posting finished for every selected account -- see the table below for each posting, with a Delete button against each."
    : "Some accounts had errors -- see the notes below and the table for every posting's outcome.";

  var notesEl = document.getElementById("result-notes");
  if (notes && notes.length) {
    notesEl.hidden = false;
    notesEl.innerHTML = notes.map(function (n) { return "<div>" + escapeHtml(n) + "</div>"; }).join("");
  } else {
    notesEl.hidden = true;
    notesEl.innerHTML = "";
  }
}

/* ----------------------------------------------------------------------
 * Live postings table -- stats bar, per-row Delete, and Delete All.
 * 2026-08-09, per Ravindra: "i want a live status of each of the postings
 * showing the counts and other relevant statistics. Also i want a delete
 * button against each posting and also a delete all button at the top."
 * --------------------------------------------------------------------*/
function computeStats() {
  // 2026-08-10, per Ravindra: "when they are already posted the posted
  // count should not be updated but the not posted should be updated as
  // these are not posted." -- an already-posted row (posted this run's
  // OWN activity is what "Posted" is meant to reflect, not this account's
  // cumulative Zoho state) is skipped, not newly posted here -- see
  // row.newly_posted (main.py sets this false on already_posted_rows,
  // true on every freshly-streamed "row" event). Only a row that was
  // GENUINELY posted (newly_posted true, or no newly_posted info at all --
  // e.g. a hypothetical future caller that doesn't set it) counts toward
  // "Posted"; everything else not-yet-posted, errored, OR already-posted-
  // earlier now counts toward "Not posted". The table itself still marks
  // already-posted rows distinctly (see rowClass()/referenceClass() and the
  // already-posted capsule in renderPostingsPanel()) so they're never
  // confused with a genuine failure despite sharing this bucket.
  var stats = { total: allPostingRows.length, posted: 0, notPosted: 0, deleted: 0 };
  allPostingRows.forEach(function (row) {
    if (row.deleted) {
      stats.deleted++;
    } else if (row.posted && row.newly_posted !== false) {
      stats.posted++;
    } else {
      stats.notPosted++;
    }
  });
  return stats;
}

function renderStatsBar() {
  var stats = computeStats();
  var el = document.getElementById("postings-stats");
  el.innerHTML =
    '<div class="postings-stat"><div class="postings-stat-value">' + stats.total + '</div><div class="postings-stat-label">Total rows</div></div>' +
    '<div class="postings-stat stat-posted"><div class="postings-stat-value">' + stats.posted + '</div><div class="postings-stat-label">Posted</div></div>' +
    '<div class="postings-stat stat-not-posted"><div class="postings-stat-value">' + stats.notPosted + '</div><div class="postings-stat-label">Not posted</div></div>' +
    '<div class="postings-stat"><div class="postings-stat-value">' + stats.deleted + '</div><div class="postings-stat-label">Deleted</div></div>';
}

function referenceClass(row) {
  if (row.deleted) return "";
  if (row.posted) return "is-posted";
  var text = (row.zoho_reference || "").toUpperCase();
  return text.indexOf("ERROR") === 0 ? "is-error" : "is-not-posted";
}

function rowClass(row) {
  if (row.deleted) return "row-deleted";
  if (row.posted) return "row-posted";
  var text = (row.zoho_reference || "").toUpperCase();
  return text.indexOf("ERROR") === 0 ? "row-error" : "row-not-posted";
}

// Total page count for the current allPostingRows (always >= 1, even for 0
// rows, so "Page 1 of 1" is a sane label rather than "Page 1 of 0").
function totalPostingsPages() {
  return Math.max(1, Math.ceil(allPostingRows.length / POSTINGS_PAGE_SIZE));
}

function renderPostingsPagination() {
  var wrap = document.getElementById("postings-pagination");
  var totalPages = totalPostingsPages();

  if (allPostingRows.length <= POSTINGS_PAGE_SIZE) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;

  var startRow = postingsCurrentPage * POSTINGS_PAGE_SIZE + 1;
  var endRow = Math.min(allPostingRows.length, startRow + POSTINGS_PAGE_SIZE - 1);
  document.getElementById("postings-page-label").textContent =
    "Page " + (postingsCurrentPage + 1) + " of " + totalPages + " (rows " + startRow + "–" + endRow +
    " of " + allPostingRows.length + ")";

  var prevBtn = document.getElementById("postings-prev-btn");
  var nextBtn = document.getElementById("postings-next-btn");
  prevBtn.disabled = postingsCurrentPage <= 0;
  nextBtn.disabled = postingsCurrentPage >= totalPages - 1;
}

function renderPostingsPanel() {
  document.getElementById("postings-panel").hidden = false;
  renderStatsBar();

  var stats = computeStats();
  document.getElementById("postings-count-label").textContent = deleteRunActive
    // 2026-08-09, still later -- overrides the normal summary text while a
    // delete run (single row or "Delete All") is in flight, so the count
    // visibly ticks up live instead of only updating once the whole batch
    // is done. deleteRunProgress.done is bumped from deleteRows()'s own
    // per-"item"-event handler, right before each such call into here.
    ? "Deleting " + deleteRunProgress.done + " of " + deleteRunProgress.total + " posting(s)…"
    : stats.total + " row" + (stats.total === 1 ? "" : "s") + " total -- " + stats.posted + " currently posted";

  // Clamp the current page in case rows changed (shouldn't normally shrink
  // the row count, but this keeps the page index sane defensively).
  var totalPages = totalPostingsPages();
  if (postingsCurrentPage > totalPages - 1) postingsCurrentPage = totalPages - 1;
  if (postingsCurrentPage < 0) postingsCurrentPage = 0;

  var tbody = document.getElementById("postings-tbody");
  tbody.innerHTML = "";

  if (!allPostingRows.length) {
    var emptyRow = document.createElement("tr");
    var emptyMessage = postingRunActive
      // 2026-08-09, later still -- shown for the brief window between
      // startPosting() and the first row/already-posted-row event actually
      // arriving, instead of the "no rows found" message (which only makes
      // sense once a run has actually finished with nothing to show).
      ? "Posting in progress&hellip; rows will appear here live as each one is posted."
      : 'No postable rows found for the selected account(s) -- every row was either not yet categorized, categorized as "Others", or has no usable Posting Type.';
    emptyRow.innerHTML = '<td colspan="8"><div class="postings-empty">' + emptyMessage + "</div></td>";
    tbody.appendChild(emptyRow);
  }

  var pageStart = postingsCurrentPage * POSTINGS_PAGE_SIZE;
  var pageEnd = pageStart + POSTINGS_PAGE_SIZE;

  allPostingRows.forEach(function (row, index) {
    if (index < pageStart || index >= pageEnd) return; // only render the current page's rows

    var tr = document.createElement("tr");
    tr.id = "posting-row-" + index;
    tr.className = rowClass(row);

    var amountText = (row.amount === null || row.amount === undefined) ? "" : String(row.amount);
    var deleteCell;
    if (row.deleted) {
      deleteCell = '<span class="row-deleted-label">Deleted</span>';
    } else if (row.consolidated) {
      // Bank Charges/VAT consolidation (2026-08-10) -- this ONE Zoho entry
      // was built from summing possibly many contributing statement rows,
      // possibly across more than one OneDrive file (see
      // bankChargeBucketToRow()'s comment above) -- the existing per-row
      // Delete flow (/delete-postings) only knows how to delete a single
      // Zoho record AND clear ONE row's own Posted-column cell in ONE file,
      // so it can't correctly unwind a consolidated entry. Deliberately no
      // Delete button here rather than a half-correct one: delete the entry
      // directly in Zoho Books if needed, then clear the affected rows'
      // "Zoho Posting Reference" cells by hand before re-running
      // /post-bank-charges for them.
      deleteCell = row.posted ? '<span class="row-deleted-label">Delete in Zoho directly</span>' : "";
    } else if (row.posted) {
      // Disabled while ANY delete run is in flight (2026-08-09, still
      // later) -- prevents a second overlapping delete request (whose
      // events would interleave with the first's on screen) rather than
      // just disabling the specific button that was clicked.
      deleteCell = '<button type="button" class="row-delete-btn" data-index="' + index + '"' +
        (deleteRunActive ? " disabled" : "") + ">Delete</button>";
    } else {
      deleteCell = "";
    }

    tr.innerHTML =
      "<td>" + escapeHtml(row.file_name || "") + "</td>" +
      '<td class="postings-narration">' + escapeHtml(row.narration || "") + "</td>" +
      "<td>" + escapeHtml(row.category || "") + "</td>" +
      "<td>" + escapeHtml(row.posting_type || "") + "</td>" +
      "<td>" + escapeHtml(amountText) + "</td>" +
      '<td class="postings-reference ' + referenceClass(row) + '">' + escapeHtml(row.zoho_reference || "") +
      // 2026-08-10, per Ravindra: re-posting an already-posted file showed
      // "all successfully posted" with no way to tell that apart from a
      // freshly-posted row -- confusing when testing the new duplicate
      // checks, since a row skipped via the LOCAL "Zoho Posting Reference"
      // cell (never even re-attempted against Zoho -- see
      // extract_postable_rows()'s already_posted_rows) looks identical to
      // one that genuinely posted just now. The backend has always sent
      // `newly_posted` on every row (main.py's already_posted_rows_detail
      // sets it false, each streamed "row" event sets it true) -- this was
      // simply never read here before. Only shown for POSTED rows -- a
      // not-yet-posted or errored row has no "already posted" state to
      // distinguish. 2026-08-10, later same day, per Ravindra ("not
      // highlighted significantly ... show as a capsule with a coloured
      // border or something like before") -- upgraded from a plain inline
      // italic label to a pill/capsule badge, reusing this project's
      // existing .chip visual language (widget.css's org-account-summary
      // chips) rather than inventing a new style from scratch.
      (row.posted && row.newly_posted === false
        ? '<div class="already-posted-chip">Already posted</div>' : "") +
      "</td>" +
      // PostingReference now resolved at /categorize time, not here
      // (2026-08-12, LATER SAME DAY, per Ravindra: "I want the creating of
      // the reference to happen while doing the catergorization not while
      // posting" -- see post_widget.html's matching <th> comment and
      // main.py's /post-transactions for the full history). row.
      // posting_reference is simply the reference text this row actually
      // posted with -- no live AI call happens at posting time any more,
      // so there's no separate "applied"/"fallback" distinction to show
      // here the way the brief live-at-posting-time version of this
      // feature had; whatever categorize_workbook() decided (rule
      // cf_reference / Gemini / the statement's own raw Reference column)
      // is just displayed as-is. Blank for an already-posted row (from an
      // earlier run, via the "start" event -- see runPosting(), which
      // doesn't send this field) or any row whose PostingReference cell
      // was itself blank, same "just show nothing rather than an error"
      // spirit as an empty zoho_reference.
      '<td class="postings-ai-preview">' + escapeHtml(row.posting_reference || "") + "</td>" +
      "<td>" + deleteCell + "</td>";
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll(".row-delete-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var index = Number(btn.dataset.index);
      deleteRows([index]);
    });
  });

  renderPostingsPagination();
  var prevBtn = document.getElementById("postings-prev-btn");
  var nextBtn = document.getElementById("postings-next-btn");
  // Re-bind fresh each render, same clone-to-clear-listeners pattern as
  // delete-all-btn below -- avoids stacking duplicate handlers.
  var freshPrev = prevBtn.cloneNode(true);
  prevBtn.parentNode.replaceChild(freshPrev, prevBtn);
  freshPrev.addEventListener("click", function () {
    if (postingsCurrentPage > 0) {
      postingsCurrentPage--;
      renderPostingsPanel();
    }
  });
  var freshNext = nextBtn.cloneNode(true);
  nextBtn.parentNode.replaceChild(freshNext, nextBtn);
  freshNext.addEventListener("click", function () {
    if (postingsCurrentPage < totalPostingsPages() - 1) {
      postingsCurrentPage++;
      renderPostingsPanel();
    }
  });

  var deleteAllBtn = document.getElementById("delete-all-btn");
  // Consolidated Bank Charges/VAT entries are excluded from "Delete All" --
  // see the deleteCell branch above for why the per-row delete flow can't
  // handle them.
  var deletableCount = allPostingRows.filter(function (r) { return r.posted && !r.deleted && !r.consolidated; }).length;
  deleteAllBtn.disabled = deletableCount === 0 || deleteRunActive;
  deleteAllBtn.textContent = deleteRunActive
    ? "Deleting " + deleteRunProgress.done + " of " + deleteRunProgress.total + "…"
    : "Delete All" + (deletableCount ? " (" + deletableCount + ")" : "");
  // Re-bind fresh each render (element persists across renders here, so
  // remove any previous listener first via cloning) -- avoids stacking
  // duplicate handlers across multiple renderPostingsPanel() calls.
  var freshBtn = deleteAllBtn.cloneNode(true);
  deleteAllBtn.parentNode.replaceChild(freshBtn, deleteAllBtn);
  freshBtn.addEventListener("click", function () {
    // "Delete All" deletes every deletable row across ALL pages, not just
    // the current page -- it's a global action, same as the stats bar
    // above it summarizing the whole run, not just the visible page.
    var indices = allPostingRows
      .map(function (r, i) { return i; })
      .filter(function (i) { return allPostingRows[i].posted && !allPostingRows[i].deleted && !allPostingRows[i].consolidated; });
    if (!indices.length) return;
    if (!confirm("Delete all " + indices.length + " posting(s) from Zoho Books? This cannot be undone.")) return;
    deleteRows(indices);
  });
}

// Streaming replacement for the old callGcpDeletePostings() (2026-08-09,
// still later), per Ravindra: "delete all option should also show the live
// status/count as it is deleting, currently it is deleting silently and
// updating at the end only". Calls onEvent(evt) for every NDJSON line AS
// SOON as it arrives ({"type": "start"|"item"|"warning"|"done"|"error",
// ...} -- see main.py's /delete-postings docstring for the full event
// shapes).
function callGcpDeletePostingsStreaming(items, organizationId, onEvent) {
  return streamNdjsonRequest(GCP_DELETE_POSTINGS_ENDPOINT, { organization_id: organizationId, items: items }, onEvent);
}

// indices: array of positions into allPostingRows to delete (1 for a single
// row's Delete button, many for "Delete All"). triggerBtn is no longer used
// to track/disable the clicked button directly (2026-08-09, still later) --
// renderPostingsPanel() itself now disables EVERY Delete button while
// deleteRunActive is true, which also survives the delete-all-btn/
// row-delete-btn elements being torn down and rebuilt by renderPostingsPanel()
// mid-run (each "item" event triggers exactly that rebuild, live).
function deleteRows(indices) {
  if (deleteRunActive) return; // a run is already in flight -- ignore a stray double-click

  var items = indices.map(function (i) {
    var row = allPostingRows[i];
    return {
      file_name: row.file_name,
      folder_path: row.folder_path,
      excel_row: row.excel_row,
      zoho_reference: row.zoho_reference,
    };
  });

  deleteRunActive = true;
  deleteRunProgress = { done: 0, total: items.length };
  renderPostingsPanel(); // shows "Deleting 0 of N..." and disables every Delete button immediately

  var anyFailed = false;
  var sawDone = false;

  callGcpDeletePostingsStreaming(items, selectedOrg ? selectedOrg.organization_id : null, function onEvent(evt) {
    if (evt.type === "item") {
      deleteRunProgress.done++;
      var result = evt.result;
      // Match the event back to its row by file_name + excel_row (the same
      // pair uniquely identifies a row, same as the backend's own grouping
      // key) -- mark it deleted only on a genuine per-item success.
      var idx = indices.find(function (i) {
        var row = allPostingRows[i];
        return row.file_name === result.file_name && row.excel_row === result.excel_row;
      });
      if (idx !== undefined) {
        if (result.success) {
          allPostingRows[idx].deleted = true;
        } else {
          anyFailed = true;
          console.error("post_widget.js: delete failed for row", allPostingRows[idx], "->", result.message);
        }
      }
      renderPostingsPanel(); // live: row flips to "Deleted", stats/count update immediately
    } else if (evt.type === "warning") {
      // A row's Zoho delete already succeeded (and already rendered as
      // Deleted above), but clearing its local reference/re-uploading the
      // file failed afterward -- the delete itself is fine, just flag it.
      anyFailed = true;
      console.error("post_widget.js: /delete-postings warning ->", evt.message);
    } else if (evt.type === "done") {
      sawDone = true;
    }
  })
    .then(function (finalEvent) {
      if (!sawDone || !finalEvent || finalEvent.type !== "done") {
        anyFailed = true;
        console.error("post_widget.js: /delete-postings stream did not finish cleanly ->", finalEvent);
      }
    })
    .catch(function (err) {
      anyFailed = true;
      console.error("post_widget.js: delete request failed ->", err);
      alert("Couldn't delete: " + (err && err.message ? err.message : err));
    })
    .then(function () {
      deleteRunActive = false;
      if (anyFailed) {
        alert("Some postings couldn't be deleted -- see the browser console for details. Rows that succeeded " +
          "have been marked Deleted; the rest are unchanged and can be retried.");
      }
      renderPostingsPanel();
    });
}
