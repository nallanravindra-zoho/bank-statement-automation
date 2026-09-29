/* ============================================================================
 * CONFIG -- same GCP deployment every other widget in this extension talks
 * to (see widget.js's own CONFIG block for the full history). This widget
 * is READ-ONLY -- it never categorizes, posts, or writes back to any Zoho
 * record, so it doesn't need MODULE_API_NAME/STATUS_FIELD/ZFAPPS.get(record)
 * at all, only the organization + bank-account pickers every other widget
 * already has, plus the new /reconcile endpoint.
 * ==========================================================================*/
const GCP_BASE_URL = "https://bank-statement-automation-184927294160.us-central1.run.app";
const GCP_ORGANIZATIONS_ENDPOINT = GCP_BASE_URL + "/organizations";
const GCP_ORG_FOLDER_MAP_ENDPOINT = GCP_BASE_URL + "/org-folder-map";
const GCP_BANK_ACCOUNTS_ENDPOINT = GCP_BASE_URL + "/bank-accounts";
const GCP_RECONCILE_ENDPOINT = GCP_BASE_URL + "/reconcile";
// Must match the GCP service's CATEGORIZE_API_KEY env var, same shared key
// every other widget in this extension already uses.
const GCP_API_KEY = "3dc32b352e8ec01e3958407d3d77f296226a0a7d316ccc1b";

let orgList = [];
let orgFolderMap = {};
let currentOrgAccounts = [];
let checkedAccountIds = {};

document.addEventListener("DOMContentLoaded", init);

function init() {
  console.log("reconciliation_widget.js: DOMContentLoaded fired, calling ZFAPPS.extension.init()...");

  var initWatchdog = setTimeout(function () {
    console.error("reconciliation_widget.js: ZFAPPS.extension.init() did not call back within 8s.");
    showError(
      "The Zoho widget SDK (zf_sdk.js) never finished initializing (no error, it just never called back). " +
      "Check the Network tab for a request to zf_sdk.js and confirm it returned 200."
    );
  }, 8000);

  ZFAPPS.extension.init()
    .then(function () {
      clearTimeout(initWatchdog);
      // Enlarged 2026-08-13 (later same day), per Ravindra: "make the
      // window bigger ... use the rest of the screen for results with no
      // scrolling as much as possible" -- 560x760 was sized for the org/
      // account picker alone (before any results existed); this needs real
      // room for several accounts' result cards side by side (see
      // reconciliation_widget.css's .result-cards grid) without forcing a
      // scroll for the common case. Same RESIZE mechanism/timing (right
      // after ZFAPPS.extension.init() resolves) as post_widget.js's own
      // 1180x920 enlargement.
      ZFAPPS.invoke("RESIZE", { width: "1400px", height: "900px" });
      return Promise.all([
        fetchOrganizationsSafely(),
        fetchOrgFolderMapSafely(),
      ]);
    })
    .then(function (results) {
      orgList = results[0];
      orgFolderMap = results[1];
      console.log("reconciliation_widget.js: " + orgList.length + " org(s), folder map -> " +
        JSON.stringify(orgFolderMap, null, 2));
      if (!orgList.length) {
        throw new Error(
          "No Zoho Books organizations were returned for this login (GET /organizations came back empty, or " +
          "failed -- see console)."
        );
      }
      showOrgSelectPanel();
    })
    .catch(function (err) {
      clearTimeout(initWatchdog);
      console.error("reconciliation_widget.js: error during init/load ->", err);
      showError("Couldn't load this widget: " + (err && err.message ? err.message : err) +
        ". See the browser console for the raw response.");
    });

  document.getElementById("close-btn").addEventListener("click", function () {
    ZFAPPS.closeModal();
  });
}

/* ----------------------------------------------------------------------
 * Org / folder-map / bank-accounts fetches -- identical shape to widget.js's
 * own fetchOrganizations()/fetchOrgFolderMap()/fetchBankAccountsForOrg(),
 * duplicated here rather than shared (this extension's widgets don't share
 * a JS module today -- each <script> tag loads only its own file).
 * --------------------------------------------------------------------*/
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
    console.error("reconciliation_widget.js: couldn't fetch /organizations ->", err);
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
    console.error("reconciliation_widget.js: couldn't fetch /org-folder-map ->", err);
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
    console.error("reconciliation_widget.js: couldn't fetch /bank-accounts for organization_id=" +
      organizationIdToFetch + " ->", err);
    return [];
  });
}

function showError(message) {
  var statusEl = document.getElementById("status");
  statusEl.hidden = false;
  statusEl.className = "status error";
  statusEl.textContent = message;
}

/* ----------------------------------------------------------------------
 * Org-select panel
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

  // Default the month picker to the CURRENT month -- most reconciliation
  // runs will be for the month that just closed, so this saves a click for
  // the common case; still fully editable.
  var monthEl = document.getElementById("month-year-select");
  var now = new Date();
  monthEl.value = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0");

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
        "record for organization_id " + org.organization_id + " in the org-folder-mapping module " +
        "(cm_orgidonedrivemap) before running this organization.");
      return;
    }

    var monthValue = monthEl.value; // "YYYY-MM"
    if (!monthValue) {
      alert("Pick a month to reconcile.");
      return;
    }
    var parts = monthValue.split("-");
    var year = Number(parts[0]);
    var month = Number(parts[1]);

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
      alert("Select at least one bank account to reconcile.");
      return;
    }
    var missingNumber = accounts.filter(function (a) { return !a.account_number; });
    if (missingNumber.length) {
      alert("These selected accounts have no account number on file in Zoho, which this feature needs to find " +
        "the right statement file: " + missingNumber.map(function (a) { return a.label; }).join(", "));
      return;
    }

    document.getElementById("org-start-btn").removeEventListener("click", onOrgStart);
    startReconciliation(accounts, folderPath, org, month, year);
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

  fetchBankAccountsForOrgSafely(org.organization_id).then(function (accounts) {
    var stillSelected = orgList[Number(orgSelectEl.value)] === org;
    if (!stillSelected) return;
    currentOrgAccounts = accounts;
    renderAccountChecklist("");
  });
}

function renderAccountChecklist(filterText) {
  var listEl = document.getElementById("org-accounts-list");
  var filter = (filterText || "").trim().toLowerCase();
  var visible = currentOrgAccounts.filter(function (a) {
    if (!filter) return true;
    var label = ((a.account_name || "") + " " + (a.account_number || "")).toLowerCase();
    return label.indexOf(filter) !== -1;
  });

  if (!currentOrgAccounts.length) {
    listEl.innerHTML = '<div class="account-checklist-empty">No bank accounts found for this organization.</div>';
    return;
  }
  if (!visible.length) {
    listEl.innerHTML = '<div class="account-checklist-empty">No accounts match "' + escapeHtml(filterText) + '".</div>';
    return;
  }

  listEl.innerHTML = "";
  visible.forEach(function (account) {
    var label = document.createElement("label");
    label.className = "account-checkbox-row" + (checkedAccountIds[account.account_id] ? " checked" : "");

    var checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = !!checkedAccountIds[account.account_id];
    checkbox.addEventListener("change", function () {
      if (checkbox.checked) {
        checkedAccountIds[account.account_id] = true;
      } else {
        delete checkedAccountIds[account.account_id];
      }
      label.className = "account-checkbox-row" + (checkbox.checked ? " checked" : "");
      renderSelectedAccountsSummary();
    });

    var text = document.createElement("span");
    text.textContent = (account.account_name || "(unnamed account)") +
      (account.account_number ? " -- " + account.account_number : "");

    label.appendChild(checkbox);
    label.appendChild(text);
    listEl.appendChild(label);
  });
  renderSelectedAccountsSummary();
}

var MAX_SUMMARY_CHIPS = 5;
function renderSelectedAccountsSummary() {
  var summaryEl = document.getElementById("selected-accounts-summary");
  var selected = currentOrgAccounts.filter(function (a) { return checkedAccountIds[a.account_id]; });
  if (!selected.length) {
    summaryEl.hidden = true;
    summaryEl.innerHTML = "";
    return;
  }
  summaryEl.hidden = false;
  var shown = selected.slice(0, MAX_SUMMARY_CHIPS);
  var extra = selected.length - shown.length;
  summaryEl.innerHTML = shown.map(function (a) {
    return '<span class="chip">' + escapeHtml(a.account_name || a.account_number || "account") + "</span>";
  }).join("") + (extra > 0 ? '<span class="chip">+' + extra + " more</span>" : "");
}

function escapeHtml(str) {
  var div = document.createElement("div");
  div.textContent = str == null ? "" : str;
  return div.innerHTML;
}

/* ----------------------------------------------------------------------
 * Reconciliation run -- one /reconcile call per selected account, all in
 * parallel (same "don't make the user wait for the slowest one" pattern as
 * widget.js's own runCategorization()), each rendering its own result card
 * live as it resolves.
 * --------------------------------------------------------------------*/
function startReconciliation(accounts, folderPath, org, month, year) {
  // 2026-08-14 (later still), per Ravindra: "clear out the selection screen
  // and use the whole screen, we dont need to disable and keep the screen
  // with disabled options. use the full screen for the results." --
  // previously the org-select-panel stayed visible (just disabled) once a
  // run started, sitting above results-panel; since both are flex:1
  // .content-panel siblings inside the same column layout (see
  // reconciliation_widget.css's .panel/.content-panel rules), that meant
  // the two panels SPLIT the available height instead of results getting
  // the whole screen. Now the selection screen is hidden outright the
  // moment a run starts, so results-panel is the only .content-panel left
  // and gets the full height to itself -- no disabling needed since the
  // controls aren't visible/reachable at all anymore.
  document.getElementById("org-select-panel").hidden = true;

  document.getElementById("results-panel").hidden = false;
  document.getElementById("result-banner").hidden = true;

  renderResultsContextBar(org, accounts);
  renderResultCardShells(accounts);
  updateOverallProgress(0, accounts.length);

  var doneCount = 0;
  var anyFail = false;

  Promise.all(accounts.map(function (account, i) {
    return callGcpReconcile(account, folderPath, org, month, year)
      .then(function (result) {
        // renderResultCard() returns its own displayPass (balance+aggregate
        // only, check 3 no longer shown -- see that function's own
        // 2026-08-14 comment) -- used here instead of result.overall_pass
        // so the top summary banner never disagrees with what the
        // individual cards actually show.
        var displayPass = renderResultCard(i, account, result);
        if (!result.success || !displayPass) anyFail = true;
      })
      .catch(function (err) {
        renderResultCardError(i, account, err);
        anyFail = true;
      })
      .then(function () {
        doneCount++;
        updateOverallProgress(doneCount, accounts.length);
      });
  })).then(function () {
    showDoneBanner(!anyFail, accounts.length);
  });
}

// "show the selected org and account at the top" (2026-08-13, later same
// day) -- populated once, right as a run starts, from the same `org`/
// `accounts` values startReconciliation() already resolved from the
// org-select-panel's own picker/checklist -- no separate fetch needed.
function renderResultsContextBar(org, accounts) {
  document.getElementById("results-context-org").textContent =
    org.name + " (" + org.organization_id + ")";
  document.getElementById("results-context-accounts").textContent =
    accounts.map(function (a) { return a.label; }).join(", ");
  document.getElementById("results-context-bar").hidden = false;
}

function renderResultCardShells(accounts) {
  var cardsEl = document.getElementById("result-cards");
  cardsEl.innerHTML = "";
  accounts.forEach(function (account, i) {
    var card = document.createElement("div");
    card.className = "result-card is-processing";
    card.id = "result-card-" + i;
    card.innerHTML =
      '<div class="result-card-header">' +
        '<div class="result-card-icon"><div class="spinner"></div></div>' +
        '<div class="result-card-title-block">' +
          '<div class="result-card-account">' + escapeHtml(account.label) + "</div>" +
          '<div class="result-card-file">Reconciling&hellip;</div>' +
        "</div>" +
      "</div>";
    cardsEl.appendChild(card);
  });
}

function updateOverallProgress(done, total) {
  var pct = total === 0 ? 0 : Math.round((done / total) * 100);
  document.getElementById("overall-progress-fill").style.width = pct + "%";
  document.getElementById("overall-progress-pct").textContent = pct + "%";
  document.getElementById("overall-progress-label").textContent =
    "Reconciled " + done + " of " + total + " account" + (total === 1 ? "" : "s") + "…";
}

function callGcpReconcile(account, folderPath, org, month, year) {
  var body = {
    folder_path: folderPath,
    bank_account_number: account.account_number,
    account_id: account.account_id,
    month: month,
    year: year,
    organization_id: org.organization_id,
  };
  return fetch(GCP_RECONCILE_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Api-Key": GCP_API_KEY },
    body: JSON.stringify(body),
  })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /reconcile (HTTP " + resp.status + ")");
      });
    });
}

function fmtNum(n) {
  if (n === null || n === undefined || isNaN(n)) return "--";
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/* Renders one account's FAILED-TO-CALL-THE-BACKEND-AT-ALL state (network
   error, non-JSON response) -- distinct from a successful call that came
   back success:false (a matching/routing problem the backend understood
   and explained -- see renderResultCard() below for that case). */
function renderResultCardError(i, account, err) {
  var card = document.getElementById("result-card-" + i);
  if (!card) return;
  card.className = "result-card is-error";
  card.innerHTML =
    '<div class="result-card-header">' +
      '<div class="result-card-icon">✕</div>' +
      '<div class="result-card-title-block">' +
        '<div class="result-card-account">' + escapeHtml(account.label) + "</div>" +
        '<div class="result-card-file">Couldn\'t reach the reconciliation service</div>' +
      "</div>" +
    "</div>" +
    '<div class="result-card-detail">' + escapeHtml(err && err.message ? err.message : String(err)) + "</div>";
}

function renderResultCard(i, account, result) {
  var card = document.getElementById("result-card-" + i);
  if (!card) return;

  if (!result.success) {
    // A clean, understood failure from the backend -- file not found,
    // ambiguous match, missing fields, bad statement shape. Show the exact
    // reason (and candidates_checked/matched, if present) rather than a
    // generic error, same "never guess, always explain" convention the
    // route itself follows.
    card.className = "result-card is-error";
    var extra = "";
    if (result.candidates_checked && result.candidates_checked.length) {
      extra = '<div class="result-card-detail">Files found in the folder: ' +
        escapeHtml(result.candidates_checked.join(", ")) + "</div>";
    }
    if (result.matched && result.matched.length) {
      extra = '<div class="result-card-detail">Matched files: ' + escapeHtml(result.matched.join(", ")) + "</div>";
    }
    card.innerHTML =
      '<div class="result-card-header">' +
        '<div class="result-card-icon">✕</div>' +
        '<div class="result-card-title-block">' +
          '<div class="result-card-account">' + escapeHtml(account.label) + "</div>" +
          '<div class="result-card-file">Not reconciled</div>' +
        "</div>" +
      "</div>" +
      '<div class="result-card-detail">' + escapeHtml(result.error || "Unknown error.") + "</div>" +
      extra;
    return;
  }

  var checks = result.checks;
  var balance = checks.balance_match;
  var aggregate = checks.aggregate_match;
  var summary = result.statement_summary || {};

  // "as of <date>" (2026-08-13, later still) -- per Ravindra's live report
  // that the closing balance looked like it came from "mid of the
  // statement" rather than the statement's own declared end -- now always
  // shows exactly which date the statement-side number reflects, right
  // next to the number itself, instead of only in a caveat someone has to
  // expand to find. See reconciliation_service.py's parse_month_end_
  // statement() "period_end AWARENESS" docstring for the underlying fix.
  var asOfText = summary.closing_balance_computed_date
    ? " (as of " + escapeHtml(summary.closing_balance_computed_date) + ")"
    : "";

  var balanceRow =
    '<div class="check-row ' + (balance.pass ? "pass" : "fail") + '">' +
      '<div class="check-row-icon">' + (balance.pass ? "✓" : "✕") + "</div>" +
      '<div class="check-row-body">' +
        '<div class="check-row-title">Month-end balance matches Zoho</div>' +
        '<div class="check-row-detail">Statement' + asOfText + ': <span class="num">' + fmtNum(balance.statement_balance) +
        '</span> &nbsp;|&nbsp; Zoho: <span class="num">' + fmtNum(balance.zoho_balance) + "</span>" +
        (balance.pass ? "" : ' &nbsp;|&nbsp; Difference: <span class="num">' + fmtNum(balance.difference) + "</span>") +
        (summary.rows_after_period_end ? '<br/><span class="check-row-note">' + summary.rows_after_period_end +
          " row(s) dated after this statement's own declared period end (" + escapeHtml(summary.period_end || "?") +
          ") were excluded from this balance.</span>" : "") +
        "</div>" +
      "</div>" +
    "</div>";

  // Deliberately CROSS-mapped display (2026-08-14, per Ravindra's live
  // report that this check compared the wrong sides): a bank statement's
  // own Debit/Credit columns are from the BANK's point of view (Debit =
  // money OUT), while Zoho's ledger records this same account as an ASSET
  // from the BUSINESS's point of view (Debit = money IN) -- opposite
  // conventions for the same money movement. So "Statement Debit" (money
  // out) is compared against "Zoho Credit" (also money out), and
  // "Statement Credit" (money in) against "Zoho Debit" (also money in) --
  // shown explicitly here so the pairing is visible, not implied same-side.
  // See reconciliation_service.py's run_reconciliation() for the matching
  // server-side comment -- don't "fix" this back to same-side.
  // Zoho transactions that could ACCOUNT FOR the discrepancy -- 2026-08-14
  // (later still), per Ravindra: "if there is any discrepancy in statement
  // Vs Zoho, only show those entries where the difference could be and not
  // all the transactions." Only rendered when the check actually FAILS (a
  // passing check has nothing to explain); when it does fail, filtered to
  // just the UNCLASSIFIED entries -- a correctly classified entry is, by
  // definition, already counted correctly in the totals above and isn't
  // "where the difference could be", whereas an unclassified entry is
  // excluded from both totals entirely and is a concrete, specific reason
  // the numbers might not match.
  var zohoDebugHtml = "";
  var zohoCount = aggregate.zoho_transactions_count;
  if (!aggregate.pass) {
    if (typeof zohoCount === "number" && zohoCount === 0) {
      zohoDebugHtml = '<div class="check-row-note">Zoho returned 0 transaction records for this ' +
        "account/period -- that's why both Zoho totals above are 0.00.</div>";
    } else {
      var unclassifiedEntries = (aggregate.zoho_transactions_debug || []).filter(function (e) {
        return e.classification === "unclassified";
      });
      if (unclassifiedEntries.length) {
        zohoDebugHtml = '<details class="unmatched-list"><summary>' + unclassifiedEntries.length +
          " Zoho transaction(s) of an unrecognized shape (excluded from the totals above, possibly " +
          "part of the difference) -- click to view</summary>" +
          unclassifiedEntries.map(function (e) {
            var desc = e.description || e.reference_number || "";
            return '<div class="unmatched-row">' + escapeHtml(e.date || "no date") + " -- " +
              escapeHtml(e.transaction_type || "unknown type") +
              (desc ? " -- " + escapeHtml(desc) : "") +
              ': <span class="amt">raw amount ' + fmtNum(e.raw_amount) + "</span></div>";
          }).join("") +
          "</details>";
      }
    }
  }

  var aggregateRow =
    '<div class="check-row ' + (aggregate.pass ? "pass" : "fail") + '">' +
      '<div class="check-row-icon">' + (aggregate.pass ? "✓" : "✕") + "</div>" +
      '<div class="check-row-body">' +
        '<div class="check-row-title">Aggregate credits/debits match Zoho</div>' +
        '<div class="check-row-detail">Statement Debit ↔ Zoho Credit (money out): <span class="num">' +
        fmtNum(aggregate.statement_total_debit) + "</span> vs <span class=\"num\">" +
        fmtNum(aggregate.zoho_total_credit) + "</span>" +
        (aggregate.pass ? "" : " &nbsp;|&nbsp; Difference: <span class=\"num\">" +
          fmtNum(aggregate.statement_debit_vs_zoho_credit_difference) + "</span>") + "<br/>" +
        'Statement Credit ↔ Zoho Debit (money in): <span class="num">' +
        fmtNum(aggregate.statement_total_credit) + "</span> vs <span class=\"num\">" +
        fmtNum(aggregate.zoho_total_debit) + "</span>" +
        (aggregate.pass ? "" : " &nbsp;|&nbsp; Difference: <span class=\"num\">" +
          fmtNum(aggregate.statement_credit_vs_zoho_debit_difference) + "</span>") +
        (aggregate.zoho_unclassified_count ? "<br/>" + aggregate.zoho_unclassified_count +
          " Zoho transaction(s) of an unrecognized shape were excluded from the Zoho totals above." : "") +
        zohoDebugHtml +
        "</div>" +
      "</div>" +
    "</div>";

  // "Every statement entry has been posted" (check 3) row REMOVED from the
  // widget 2026-08-14 (later still), per Ravindra: "can you remove this
  // section, everything looks fine" -- referring to this check, which was
  // showing a lot of false-negative "not found" rows (a separate, already-
  // noted issue: some Bank Transfer detail lookups this check depends on
  // were hitting "invalid ID"/"does not exist" errors from Zoho -- see the
  // 2026-08-14 changelog entry logged right after that was spotted in the
  // Cloud Run logs -- not yet root-caused/fixed). Backend still computes
  // and returns checks.all_posted in full (matched_count/unmatched_rows/
  // etc, plus its own pass/fail and unmatched_reason per row) for API
  // consumers/logs -- this is a display-only removal, same as the Notes
  // block removal right below.
  //
  // displayPass() (2026-08-14, later still): since check 3 is no longer
  // shown, the card's own pass/fail icon and border color are now driven
  // ONLY by the two checks still visible (balance, aggregate) -- NOT the
  // backend's raw result.overall_pass, which still factors in check 3 and
  // would otherwise show a red card even when everything the user can
  // actually see has passed. showResultCard()'s caller (below) mirrors
  // this same displayPass() logic for the top summary banner, so the
  // banner and the individual cards never disagree with each other.
  var displayPass = balance.pass && aggregate.pass;
  card.className = "result-card " + (displayPass ? "is-pass" : "is-fail");

  // Notes/caveats block removed from the widget 2026-08-14 (later still),
  // per Ravindra: "also remove the notes row from the bottom of the
  // widget." result.caveats is still computed and returned by the backend
  // (reconciliation_service.py) for logs/API consumers -- this is a
  // display-only removal, not a removal of the underlying diagnostics.
  card.innerHTML =
    '<div class="result-card-header">' +
      '<div class="result-card-icon">' + (displayPass ? "✓" : "✕") + "</div>" +
      '<div class="result-card-title-block">' +
        '<div class="result-card-account">' + escapeHtml(account.label) + "</div>" +
        '<div class="result-card-file">' + escapeHtml(result.file_name || "") + "</div>" +
      "</div>" +
    "</div>" +
    '<div class="check-rows">' + balanceRow + aggregateRow + "</div>";
  return displayPass;
}

function showDoneBanner(allPassed, totalAccounts) {
  var banner = document.getElementById("result-banner");
  banner.hidden = false;
  banner.className = "result-banner" + (allPassed ? "" : " has-errors");
  document.getElementById("result-icon").textContent = allPassed ? "✓" : "!";
  document.getElementById("result-title").textContent = allPassed
    ? "All accounts reconciled cleanly"
    : "Some accounts have discrepancies";
  document.getElementById("result-subtitle").textContent = allPassed
    ? "Every check passed for all " + totalAccounts + " selected account(s) -- see the cards above for the full numbers."
    : "Review the card(s) above marked with a red cross for the specific check(s) that didn't match.";
}
