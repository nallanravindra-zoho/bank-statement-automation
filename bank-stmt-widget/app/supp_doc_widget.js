/* ============================================================================
 * CONFIG -- same GCP deployment as widget.js/post_widget.js. Fill
 * GCP_API_KEY in before packaging (same value as the other two widgets').
 * ==========================================================================*/

const CATEGORIZATION_MODULE_API_NAME = "cm_categorisation";

const GCP_BASE_URL = "https://bank-statement-automation-184927294160.us-central1.run.app";
const GCP_LIST_FILES_ENDPOINT = GCP_BASE_URL + "/list-files";
const GCP_CATEGORIZATION_RULES_ENDPOINT = GCP_BASE_URL + "/categorization-rules";
const GCP_BANK_ACCOUNTS_ENDPOINT = GCP_BASE_URL + "/bank-accounts";
const GCP_ORGANIZATIONS_ENDPOINT = GCP_BASE_URL + "/organizations";
const GCP_ORG_FOLDER_MAP_ENDPOINT = GCP_BASE_URL + "/org-folder-map";
const GCP_ATTACH_SUPPORTING_DOCS_ENDPOINT = GCP_BASE_URL + "/attach-supporting-docs";
const GCP_API_KEY = "3dc32b352e8ec01e3958407d3d77f296226a0a7d316ccc1b";

/* ==========================================================================
 * Widget logic -- REWRITTEN 2026-08-12, later same day, per Ravindra's
 * scope cut: "i want to park the document extraction and validation aside
 * and want only to attach the required document to the expense entry.
 * Just like other widgets this widget also opens up entities and
 * respective accounts and opens up the document with the account
 * selected." The earlier version of this file drove a single-statement-
 * file picker for the (now discarded) Gemini-based verification feature --
 * this version instead uses the SAME org + bank-account checklist pattern
 * as post_widget.js (own org picker, own account checklist, 2026-08-09
 * "Own org + account picker" convention already established for every
 * widget in this extension that runs independently), then finds each
 * selected account's own already-posted statement file(s) the same way
 * post_widget.js finds its posting targets (matchFilesToAccounts()).
 * ==========================================================================*/

let orgList = [];
let orgFolderMap = {};
let categorizationRules = [];
let categorizationRulesError = null;
let currentOrgAccounts = [];
let checkedAccountIds = {};
let selectedOrg = null;

// Every "row" event seen this run, in arrival order, each tagged with which
// file it came from (the server's own "row" event doesn't carry file_name --
// this widget can process more than one matched file/account per run, unlike
// the single-file design this replaced) -- rendered straight into the table,
// no pagination (the qualifying-row count here is always a small subset of a
// full statement -- only rows whose category is marked Supporting Required).
let allCheckRows = [];
let checkRunActive = false;

document.addEventListener("DOMContentLoaded", init);

function init() {
  console.log("supporting_docs_widget.js: DOMContentLoaded fired, calling ZFAPPS.extension.init()...");

  var initWatchdog = setTimeout(function () {
    console.error("supporting_docs_widget.js: ZFAPPS.extension.init() did not call back within 8s.");
    showError(
      "The Zoho widget SDK (zf_sdk.js) never finished initializing (no error, it just never called back). " +
      "Check the Network tab for a request to zf_sdk.js and confirm it returned 200."
    );
  }, 8000);

  ZFAPPS.extension.init()
    .then(function () {
      clearTimeout(initWatchdog);
      console.log("supporting_docs_widget.js: ZFAPPS.extension.init() resolved successfully.");
      ZFAPPS.invoke("RESIZE", { width: "1180px", height: "920px" });

      return Promise.all([
        fetchOrganizationsSafely(),
        fetchOrgFolderMapSafely(),
      ]);
    })
    .then(function (results) {
      orgList = results[0];
      orgFolderMap = results[1];
      console.log("supporting_docs_widget.js: organizations -> " + orgList.length + " org(s).");

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
      console.error("supporting_docs_widget.js: error during init/load ->", err);
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
    console.error("supporting_docs_widget.js: couldn't fetch /organizations ->", err);
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
    console.error("supporting_docs_widget.js: couldn't fetch /org-folder-map ->", err);
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
    console.error("supporting_docs_widget.js: couldn't fetch /bank-accounts for organization_id=" + organizationIdToFetch + " ->", err);
    return [];
  });
}

// /attach-supporting-docs needs the SAME rules the statement was categorized
// with (to resolve cf_supporting_required per category server-side, via
// add_supporting_required_column()) -- fetched per-org, same lifecycle/
// endpoint as widget.js/post_widget.js's own fetchCategorizationRulesForOrg().
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
        throw new Error("No Categorization rules found for organization_id " + orgId + " -- nothing to check against.");
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

function escapeHtml(str) {
  return String(str == null ? "" : str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* ----------------------------------------------------------------------
 * Org + account picker -- identical shape to post_widget.js's own
 * org-select-panel (see that file for the original comments this mirrors).
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
    if (accounts.some(function (a) { return !a.account_number; })) {
      alert("At least one selected account has no real account number on file -- this feature needs each " +
        "account's own account number to find its docs/<account number>/ OneDrive subfolder. Unselect it or " +
        "fix its account number in Zoho first.");
      return;
    }

    document.getElementById("org-start-btn").removeEventListener("click", onOrgStart);

    selectedOrg = org;
    startChecking(accounts, folderPath, org, categorizationRules);
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
      console.error("supporting_docs_widget.js: couldn't fetch /categorization-rules for organization_id=" +
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
  btn.textContent = "OK, Attach Supporting Documents" + (count ? " (" + count + ")" : "");
}

/* ----------------------------------------------------------------------
 * NDJSON streaming -- identical helpers to post_widget.js's own
 * (parseNdjsonLine/consumeNdjsonText/streamNdjsonRequest); duplicated here
 * rather than shared for the same "separate widget pages" reason given at
 * the top of this file.
 * --------------------------------------------------------------------*/
function parseNdjsonLine(line) {
  var trimmed = (line || "").trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed);
  } catch (e) {
    console.error("supporting_docs_widget.js: couldn't parse NDJSON line ->", line, e);
    return null;
  }
}

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

function streamNdjsonRequest(url, body, onEvent) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Api-Key": GCP_API_KEY },
    body: JSON.stringify(body),
  }).then(function (resp) {
    var contentType = resp.headers.get("Content-Type") || "";
    if (contentType.indexOf("application/x-ndjson") === -1) {
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
          buffer = lines.pop();
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

// /list-files excludes .COMPLETED files from its own "files" list by design
// (so a fresh categorize/post run never re-picks one up), but still reports
// them separately under "completed_files_skipped". THIS widget's natural
// target is an already-fully-POSTED statement (a .COMPLETED file, since
// only posted rows are ever eligible for attachment) -- so, unlike
// post_widget.js's own version of this helper (which only wants NOT-yet-
// complete files, since it POSTS them), both lists are combined here, or a
// fully-completed statement's rows would never be found for attachment at
// all.
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
      return (result.files || []).concat(result.completed_files_skipped || []);
    });
}

// Same digit-run matching as widget.js's/post_widget.js's own
// matchFilesToAccounts() -- matches each selected account's real account
// number (digits only) against each OneDrive file's own name.
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

// 2026-08-13, per Ravindra's live report: the docs/<account number>/
// OneDrive subfolder this feature searches wasn't being found, even though
// the folder genuinely exists (e.g. "BankStatements/Dubai/docs/012556498646/
// 25-368764693-1-151.pdf") -- traced via a real Cloud Run log line showing
// docs_folder='BankStatements/Dubai/docs/xxxx8646', NOT the real account
// number. Root cause: Zoho's own GET /bankaccounts response (this widget's
// only source for account.account_number, via /bank-accounts) returns bank
// account numbers MASKED -- only the last 4 digits, e.g. "XXXX8646" -- for
// security/PCI reasons Ravindra's Books org apparently applies; this was
// never confirmed either way before, since nothing in this codebase had
// needed the FULL account number for anything before this feature (the
// existing account-to-statement-file matching in matchFilesToAccounts()
// above only ever needed a SUBSTRING match, which the masked last-4-digits
// value still satisfies -- that's why file matching itself kept working
// fine while the docs/<account>/ folder path silently didn't).
//
// Fix: once a statement file is matched to an account, the file's own NAME
// already carries the real, unmasked, full account number (bank statement
// filenames in this project have always been named with the real account
// number, e.g. "012556498646 BS Sample 1st to 17th.xlsx" -- same file this
// function itself searches by digit-substring against). So the REAL number
// for the docs/<account number>/ folder is extracted from the matched
// filename itself, not from Zoho's (masked) account.account_number --
// see extractAccountNumberFromFileName() and its use in runCheck() below.
// Falls back to account.account_number (the previous, masked-prone
// behavior) only if no usable digit run is found in the filename, so this
// never regresses to "nothing sent" for a filename that doesn't follow the
// usual leading-account-number convention.
function extractAccountNumberFromFileName(fileName) {
  var m = (fileName || "").match(/\d{6,}/);
  return m ? m[0] : null;
}

// Calls /attach-supporting-docs for ONE file, streaming {"type":
// "start"|"row"|"done"|"error", ...} events as they arrive -- see main.py's
// /attach-supporting-docs docstring for the full event shapes.
// bankAccountNumber is the REAL bank account number (account.account_number,
// NOT account_id) -- used server-side to build the "docs/<account
// number>/" OneDrive subfolder this feature searches for invoices under.
function callGcpAttachSupportingDocsStreaming(fileName, folderPath, rules, organizationId, bankAccountNumber, onEvent) {
  var body = {
    file_name: fileName,
    rules: rules,
    organization_id: organizationId,
    bank_account_number: bankAccountNumber,
  };
  if (folderPath) body.folder_path = folderPath;
  return streamNdjsonRequest(GCP_ATTACH_SUPPORTING_DOCS_ENDPOINT, body, onEvent);
}

/* ----------------------------------------------------------------------
 * Run + results table.
 * --------------------------------------------------------------------*/
function startChecking(accounts, folderPath, org, rules) {
  document.getElementById("org-select-panel").hidden = true;
  document.getElementById("result-banner").hidden = true;
  allCheckRows = [];
  checkRunActive = true;

  document.getElementById("run-header-org").textContent = org.name || "";
  document.getElementById("run-header-accounts").textContent =
    accounts.map(function (a) { return a.label; }).join(", ");
  setRunHeaderStatus("Starting…");
  document.getElementById("run-header").hidden = false;

  document.getElementById("checks-panel").hidden = false;
  renderChecksPanel();

  runCheck(accounts, folderPath, org, rules);
}

function setRunHeaderStatus(text) {
  var el = document.getElementById("run-header-status");
  if (el) el.textContent = text;
}

// Same "list files once, match client-side, fire per-account/per-file calls
// in parallel" shape as post_widget.js's runPosting() -- calling
// /attach-supporting-docs instead of /post-transactions, and accumulating
// every row's detail into allCheckRows for the results table below.
function runCheck(accounts, folderPath, org, rules) {
  var anyError = false;
  var totalFiles = 0;
  var doneFiles = 0;
  var notes = [];
  var finalStats = { attached: 0, not_attached: 0, errors: 0 };

  function updateFileProgress() {
    setRunHeaderStatus(totalFiles
      ? ("Checking " + doneFiles + " of " + totalFiles + " file(s)…")
      : "No matching (already-posted) files found for any selected account.");
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
          notes.push('No file found in OneDrive for "' + account.label + '" (searched ' +
            (folderPath || "the default folder") + ").");
          return Promise.resolve();
        }

        return Promise.all(matchedFiles.map(function (fileName) {
          // Prefer the real account number embedded in the matched
          // filename over account.account_number, which Zoho's own API
          // may return masked (e.g. "XXXX8646") -- see
          // extractAccountNumberFromFileName()'s own comment above for the
          // real bug this fixes. Falls back to account.account_number
          // (whatever Zoho gave us, masked or not) only if the filename
          // itself has no usable digit run to extract.
          var bankAccountNumberForFolder = extractAccountNumberFromFileName(fileName) || account.account_number;
          if (bankAccountNumberForFolder !== account.account_number) {
            console.log("supporting_docs_widget.js: using account number from filename (" +
              bankAccountNumberForFolder + ") instead of Zoho's own account.account_number (" +
              account.account_number + ", likely masked) for " + fileName + "'s docs/<account>/ folder.");
          }
          return callGcpAttachSupportingDocsStreaming(
            fileName, folderPath, rules, org.organization_id, bankAccountNumberForFolder,
            function onEvent(evt) {
              if (evt.type === "row") {
                allCheckRows.push(Object.assign({}, evt.row, { file_name: fileName }));
                renderChecksPanel();
              } else if (evt.type === "done" || evt.type === "start") {
                if (evt.stats) finalStats = evt.stats;
              }
            }
          )
            .then(function (finalEvent) {
              doneFiles++;
              updateFileProgress();
              if (!finalEvent || finalEvent.type !== "done") {
                anyError = true;
                var msg = (finalEvent && finalEvent.error) || "stream ended without a 'done' event";
                console.error("supporting_docs_widget.js: /attach-supporting-docs for " + fileName +
                  " did not finish cleanly ->", msg);
                notes.push(fileName + ": " + msg);
              } else {
                finalStats = finalEvent.stats || finalStats;
                if (finalEvent.stats && finalEvent.stats.errors > 0) anyError = true;
              }
            })
            .catch(function (err) {
              doneFiles++;
              anyError = true;
              updateFileProgress();
              console.error("supporting_docs_widget.js: streaming attach failed for", fileName, "->", err);
              notes.push(fileName + ": " + ((err && err.message) ? err.message : String(err)));
            });
        }));
      });

      return Promise.all(perAccountPromises);
    })
    .catch(function (err) {
      anyError = true;
      var msg = "Couldn't list the OneDrive folder: " + ((err && err.message) ? err.message : String(err));
      setRunHeaderStatus(msg);
      notes.push(msg);
    })
    .then(function () {
      checkRunActive = false;
      setRunHeaderStatus(anyError ? "Finished with some errors." : "Done.");
      renderChecksPanel();
      showDonePanel(!anyError, notes, computeStats());
    });
}

function computeStats() {
  var stats = { total: allCheckRows.length, attached: 0, notAttached: 0, errors: 0 };
  allCheckRows.forEach(function (row) {
    var s = (row.status || "").toLowerCase();
    if (s.indexOf("error") === 0) stats.errors++;
    else if (s.indexOf("attached") === 0) stats.attached++;
    else stats.notAttached++;
  });
  return stats;
}

function showDonePanel(allSucceeded, notes, stats) {
  var banner = document.getElementById("result-banner");
  banner.hidden = false;
  banner.className = "result-banner" + (notes.length ? " has-errors" : "");
  document.getElementById("result-icon").textContent = notes.length ? "!" : "✓";
  document.getElementById("result-title").textContent = notes.length ? "Finished with some errors" : "All done!";
  document.getElementById("result-subtitle").textContent =
    stats.attached + " attached, " + stats.notAttached + " not attached, " + stats.errors +
    " error(s) out of " + stats.total + " row(s) checked.";
  var notesEl = document.getElementById("result-notes");
  notesEl.innerHTML = "";
  notes.forEach(function (n) {
    var div = document.createElement("div");
    div.className = "result-note-line";
    div.textContent = n;
    notesEl.appendChild(div);
  });
}

function statusBadgeClass(status) {
  var s = (status || "").toLowerCase();
  if (s.indexOf("error") === 0) return "row-error";
  if (s.indexOf("attached") === 0) return "row-posted";
  return "row-not-posted"; // "NOT ATTACHED: ..."
}

function invoiceFileBadge(row) {
  if (!row.invoice_file_name) return '<span class="doc-badge badge-na">--</span>';
  return escapeHtml(row.invoice_file_name);
}

function matchedRecordText(row) {
  if (row.record_type && row.record_id) return escapeHtml(row.record_type + " " + row.record_id);
  return "--";
}

function renderChecksStats() {
  var stats = computeStats();
  var statsEl = document.getElementById("checks-stats");
  statsEl.innerHTML =
    '<div class="postings-stat"><div class="postings-stat-value">' + stats.total + '</div><div class="postings-stat-label">Total Checked</div></div>' +
    '<div class="postings-stat stat-posted"><div class="postings-stat-value">' + stats.attached + '</div><div class="postings-stat-label">Attached</div></div>' +
    '<div class="postings-stat stat-not-posted"><div class="postings-stat-value">' + stats.notAttached + '</div><div class="postings-stat-label">Not Attached</div></div>' +
    '<div class="postings-stat stat-errors"><div class="postings-stat-value">' + stats.errors + '</div><div class="postings-stat-label">Errors</div></div>';
}

function renderChecksPanel() {
  renderChecksStats();

  var countLabel = document.getElementById("checks-count-label");
  countLabel.textContent = allCheckRows.length + " row(s) checked so far" + (checkRunActive ? " -- in progress…" : "");

  var tbody = document.getElementById("checks-tbody");
  if (!allCheckRows.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="postings-empty">' +
      (checkRunActive ? "Checking in progress&hellip; rows will appear here live as each one is checked."
                       : "No Supporting Required rows found to attach for the selected account(s).") +
      '</td></tr>';
    return;
  }

  tbody.innerHTML = allCheckRows.map(function (row) {
    return '<tr class="' + statusBadgeClass(row.status) + '">' +
      '<td>' + escapeHtml(row.file_name || "") + '</td>' +
      '<td>' + escapeHtml(row.excel_row) + '</td>' +
      '<td>' + escapeHtml(row.category) + '</td>' +
      '<td class="postings-narration">' + escapeHtml(row.narration || "") + '</td>' +
      '<td class="postings-reference">' + escapeHtml(row.reference_number || "--") + '</td>' +
      '<td>' + invoiceFileBadge(row) + '</td>' +
      '<td class="checks-matched-cell">' + matchedRecordText(row) + '</td>' +
      '<td class="checks-status-cell">' + escapeHtml(row.status) + '</td>' +
      '</tr>';
  }).join("");
}
