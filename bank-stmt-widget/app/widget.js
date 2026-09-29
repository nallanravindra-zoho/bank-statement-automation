/* ============================================================================
 * CONFIG -- fill these in from your Zoho Books org / GCP deployment before
 * packaging.
 * ==========================================================================*/

// Settings > Custom Modules > "Bank Transaction Testing" > module details
// shows its API name. Confirmed from your console output.
const MODULE_API_NAME = "cm_bank_transaction_testing";

// The status picklist field on the same module (Initiated / Categorization
// Complete / Human Review Complete / Posting Complete / Error). Set to
// STATUS_VALUE_ON_SUCCESS once every account has been processed with no
// errors, or STATUS_VALUE_ON_ERROR if any account failed.
const STATUS_FIELD = "cf_status";
const STATUS_VALUE_ON_SUCCESS = "Categorization Complete";
const STATUS_VALUE_ON_ERROR = "Error";

// Custom field on the same module (cm_bank_transaction_testing) that records
// when this record was last successfully processed. Set alongside cf_status,
// but ONLY on a fully successful run (see runCategorization()) -- an errored
// run's cf_status already says "Error", so a "processed on" date next to
// that would misleadingly suggest it finished cleanly.
// CONFIRMED LIVE (2026-07-25): this IS a Date & Time field (Zoho's error
// said "Invalid date time ... specified", not "field not found"), but it
// rejects the bare "Z" UTC suffix a plain toISOString() produces -- Zoho
// wants a signed numeric offset instead (e.g. "+00:00"), same convention
// Zoho CRM's datetime fields use. See currentTimestampForZoho() below.
const DATE_PROCESSED_FIELD = "cf_date";

// New (2026-08-01, later, per Ravindra) -- once a run finishes, the record
// also gets stamped with WHICH organization/entity and WHICH bank accounts
// were actually processed this time (in addition to cf_status/cf_date,
// which already existed). Both API names were given directly by Ravindra:
// "org id apiname: cf_entity_org" and (2026-08-02) "the bank accounts api
// name is cf_bank_accounts" -- confirmed, replacing the earlier best-guess
// cf_bank_accounts_dropdown. Separate from the native "Bank Accounts"
// lookup field, which stays untouched/manually curated by Ravindra.
const ENTITY_ORG_FIELD = "cf_entity_org";
const SELECTED_BANK_ACCOUNTS_FIELD = "cf_bank_accounts"; // CONFIRMED 2026-08-02

// The "Categorization" custom module holding category/account/posting rules
// (one record per category). Confirmed live on 2026-07-23 via a direct
// /categorization-rules curl check -- it's cm_categorisation (cm_ prefix,
// as every other custom module here uses; cf_categorisation was a guess
// that 404'd/failed).
const CATEGORIZATION_MODULE_API_NAME = "cm_categorisation";

// Your deployed GCP Cloud Run service (see gcp_categorize_service/DEPLOY.md).
// GCP_API_KEY must match that service's CATEGORIZE_API_KEY env var exactly.
//
// NOTE: reading the Categorization module and updating cf_status used to go
// through ZFAPPS.request() directly to the Books API -- that turned out to
// require an "API Configuration" set up in Zoho Sigma's extension editor,
// which doesn't exist for a widget that was never registered there. Both of
// those calls now go through this same backend instead (/categorization-rules,
// /update-status), using the Zoho Books OAuth credential already set up for
// the original pipeline. ZFAPPS.get() below is unaffected -- reading
// organization/record context never needed Sigma at all.
const GCP_BASE_URL = "https://bank-statement-automation-184927294160.us-central1.run.app";
const GCP_LIST_FILES_ENDPOINT = GCP_BASE_URL + "/list-files";
const GCP_CATEGORIZE_ENDPOINT = GCP_BASE_URL + "/categorize";
const GCP_CATEGORIZATION_RULES_ENDPOINT = GCP_BASE_URL + "/categorization-rules";
const GCP_BANK_ACCOUNTS_ENDPOINT = GCP_BASE_URL + "/bank-accounts";
const GCP_UPDATE_STATUS_ENDPOINT = GCP_BASE_URL + "/update-status";
// New (2026-08-01, later, per Ravindra) -- SUPERSEDES the earlier
// GCP_ENTITIES_ENDPOINT/cm_entities approach. Confirmed live that a single
// Zoho login can see more than one real organization (GET /organizations),
// so the widget now shows a live org picker straight from Zoho itself,
// instead of a hand-maintained "Entities" module. GCP_ORG_FOLDER_MAP_ENDPOINT
// reads the (much simpler) cm_orgidonedrivemap module, which only needs to
// map an org_id to its OneDrive folder path -- the org's NAME comes from
// Zoho's own live list, not from that module at all.
const GCP_ORGANIZATIONS_ENDPOINT = GCP_BASE_URL + "/organizations";
const GCP_ORG_FOLDER_MAP_ENDPOINT = GCP_BASE_URL + "/org-folder-map";
const GCP_API_KEY = "3dc32b352e8ec01e3958407d3d77f296226a0a7d316ccc1b";

/* ==========================================================================
 * Widget logic
 * ==========================================================================*/

let organizationId = null;
let recordId = null;
// categorizationRules (2026-08-06, per Ravindra: "categorization rules can be
// different for different orgs ... instead of loading all the rules now we
// have to load rules of the initially selected orgid from the categorize
// module") -- used to be fetched ONCE at init(), unfiltered, since rules
// applied to every organization. Now every cm_categorisation record carries a
// mandatory cf_entity (organization_id) field, so this is re-fetched, SCOPED
// to whichever organization is currently selected, every time the org
// dropdown changes (see loadDataForSelectedOrg()) -- same lifecycle as
// currentOrgAccounts below, just for rules instead of bank accounts.
let categorizationRules = []; // raw Categorization module records for the CURRENTLY SELECTED org only
let categorizationRulesError = null; // set if the org-scoped fetch failed -- checked before a run can start
let orgList = []; // [{organization_id, name, plan_name, country, currency_code}, ...] from /organizations
let orgFolderMap = {}; // {organization_id: onedrive_folder_path} from /org-folder-map
let selectedOrg = null; // the org object chosen in the popup, once "OK" is clicked
// currentOrgAccounts/checkedAccountIds (2026-08-02, per Ravindra: "need a
// search on name for account to filter") -- the checkbox list is now
// filterable by name, so it can't rely on querying which checkboxes are
// checked directly from the DOM at click time (a checked box that's been
// filtered OUT of view no longer exists in the DOM at all, once the list is
// re-rendered). currentOrgAccounts holds the FULL unfiltered list for
// whichever organization is currently selected; checkedAccountIds tracks
// selections by account_id across search-filter re-renders, independent of
// which rows happen to be visible right now. See renderAccountChecklist().
let currentOrgAccounts = [];
let checkedAccountIds = {};

document.addEventListener("DOMContentLoaded", init);

function init() {
  console.log("widget.js: DOMContentLoaded fired, calling ZFAPPS.extension.init()...");

  var initWatchdog = setTimeout(function () {
    console.error("widget.js: ZFAPPS.extension.init() did not call back within 8s -- it's hanging, not erroring.");
    showError(
      "The Zoho widget SDK (zf_sdk.js) never finished initializing (no error, it just never called back). " +
      "Check the Network tab for a request to zf_sdk.js and confirm it returned 200."
    );
  }, 8000);

  ZFAPPS.extension.init()
    .then(function () {
      clearTimeout(initWatchdog);
      console.log("widget.js: ZFAPPS.extension.init() resolved successfully.");
      // Enlarged (2026-08-02, per Ravindra: "The text is cut below. Make the
      // window bigger so that what is processed is shown properly") -- the
      // old 460x620 was cutting off progress/summary text, especially once
      // the org-select panel (organization dropdown + account checklist +
      // search box) is showing at the same time as the progress rows below
      // it. The account checklist panel also scrolls internally (see
      // widget.css), so this isn't the only fix, but a bigger modal means
      // less needs to scroll to begin with.
      ZFAPPS.invoke("RESIZE", { width: "560px", height: "760px" });

      return Promise.all([
        ZFAPPS.get("organization"),
        ZFAPPS.get(MODULE_API_NAME),
      ]);
    })
    .then(function (results) {
      var orgData = results[0];
      var recordData = results[1];

      var moduleRecord = recordData && recordData[MODULE_API_NAME];
      var orgRecord = orgData && (orgData.organization || orgData);

      organizationId = orgRecord && orgRecord.organization_id;
      recordId = moduleRecord && moduleRecord.module_record_id;

      console.log("ZFAPPS.get(organization) -> " + JSON.stringify(orgData, null, 2));
      console.log("ZFAPPS.get(" + MODULE_API_NAME + ") -> " + JSON.stringify(recordData, null, 2));

      // organizationId itself isn't forwarded to the GCP backend below -- that
      // service uses its OWN stored ZOHO_ORGANIZATION_ID credential, not one
      // passed from here. It's still validated as a sanity check that
      // ZFAPPS.get("organization") came back in the expected shape; recordId
      // is the one that's actually used (by /update-status).
      if (!organizationId || !recordId) {
        throw new Error("Missing organization_id or module_record_id from ZFAPPS.get() -- see console for the raw response above.");
      }

      // NOTE (2026-08-01, later): this widget no longer reads
      // cf_bank_accounts_formatted to seed the account list -- WHICH
      // accounts get processed is now chosen live in the org-select popup
      // below (real accounts, fetched fresh per organization), not parsed
      // off this record. cf_bank_accounts_formatted and the native "Bank
      // Accounts" lookup field are left completely untouched on the record.

      // Categorization rules are NOT fetched here anymore (2026-08-06) --
      // they're now org-scoped (cf_entity on every cm_categorisation record
      // is mandatory), so there's no single "the rules" to fetch before an
      // organization has even been picked. loadDataForSelectedOrg() fetches
      // them once an org is selected (initially org index 0, and again on
      // every dropdown change) -- see showOrgSelectPanel().
      return Promise.all([
        fetchOrganizationsSafely(),
        fetchOrgFolderMapSafely(),
      ]);
    })
    .then(function (results) {
      var organizations = results[0]; // [] if /organizations itself failed, or this login has no orgs
      var folderMap = results[1]; // {} if /org-folder-map itself failed, or cm_orgidonedrivemap is empty
      orgList = organizations;
      orgFolderMap = folderMap;
      console.log("Zoho organizations -> " + organizations.length + " org(s): " + JSON.stringify(organizations, null, 2));
      console.log("Org -> OneDrive folder map -> " + JSON.stringify(folderMap, null, 2));

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
      console.error("widget.js: error during init/load ->", err);
      showError(
        "Couldn't load this record: " + (err && err.message ? err.message : err) +
        ". See the browser console for the raw response."
      );
    });

  document.getElementById("close-btn").addEventListener("click", function () {
    ZFAPPS.closeModal();
  });
}

// Fetches only the Categorization module records that apply to ONE specific
// organization (cf_category, cf_from_account, cf_to_account, cf_key_words,
// cf_type_of_transaction, cf_entity) via our own GCP backend (see main.py's
// /categorization-rules) -- NOT ZFAPPS.request(), which needs a Sigma API
// Configuration we don't have. Hands them back RAW (same field names); the
// GCP /categorize endpoint normalizes them, so the widget doesn't need to
// know that shape at all.
//
// 2026-08-06, per Ravindra: "categorization rules can be different for
// different orgs ... instead of loading all the rules now we have to load
// rules of the initially selected orgid from the categorize module." Every
// cm_categorisation record now carries a MANDATORY cf_entity field (the
// organization_id it applies to -- confirmed by Ravindra there's no "blank
// means every org" case), so main.py's /categorization-rules now requires
// organization_id and filters server-side -- this only ever returns the
// rules meant for THIS org, not the whole module.
function fetchCategorizationRulesForOrg(orgId) {
  var url = GCP_CATEGORIZATION_RULES_ENDPOINT +
    "?module_api_name=" + encodeURIComponent(CATEGORIZATION_MODULE_API_NAME) +
    "&organization_id=" + encodeURIComponent(orgId);
  return fetch(url, {
    method: "GET",
    headers: { "X-Api-Key": GCP_API_KEY },
  })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /categorization-rules (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) {
        throw new Error(result.error || "Unknown error from /categorization-rules");
      }
      var records = result.records || [];
      if (!records.length) {
        throw new Error("No Categorization rules found for organization_id " + orgId + " in module " +
          CATEGORIZATION_MODULE_API_NAME + " -- nothing to match categories against. Make sure at least one " +
          "record's cf_entity is set to this exact organization_id.");
      }
      return records;
    });
}

// Every real Zoho Books organization this login can see (GET /organizations)
// -- 2026-08-01, later, per Ravindra. Confirmed live against the real Cyber
// Knight tenant that a single Self Client login sees more than one org, so
// this backs a live "which organization" picker with no separate credential
// set needed per entity.
function fetchOrganizations() {
  return fetch(GCP_ORGANIZATIONS_ENDPOINT, {
    method: "GET",
    headers: { "X-Api-Key": GCP_API_KEY },
  })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /organizations (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) {
        throw new Error(result.error || "Unknown error from /organizations");
      }
      return result.organizations || [];
    });
}

function fetchOrganizationsSafely() {
  return fetchOrganizations().catch(function (err) {
    console.error("widget.js: couldn't fetch /organizations ->", err);
    return [];
  });
}

// The org_id -> OneDrive-folder-path map from the cm_orgidonedrivemap
// custom module in CBK -- 2026-08-01, later, per Ravindra. Used to look up
// which folder to read once an organization is picked in the popup; an org
// with no entry here can't be run yet (see showOrgSelectPanel()).
function fetchOrgFolderMap() {
  return fetch(GCP_ORG_FOLDER_MAP_ENDPOINT, {
    method: "GET",
    headers: { "X-Api-Key": GCP_API_KEY },
  })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /org-folder-map (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) {
        throw new Error(result.error || "Unknown error from /org-folder-map");
      }
      return result.map || {};
    });
}

function fetchOrgFolderMapSafely() {
  return fetchOrgFolderMap().catch(function (err) {
    console.error("widget.js: couldn't fetch /org-folder-map -- no organization will have a usable OneDrive " +
      "folder until this is reachable ->", err);
    return {};
  });
}

// Every REAL bank account (i.e. one with an account_id -- main.py's
// /bank-accounts already filters to only those) in a SPECIFIC organization,
// via GET /bank-accounts?organization_id=... -- 2026-08-01, later, per
// Ravindra ("i want only the real bank accounts with an account id").
function fetchBankAccountsForOrg(organizationIdToFetch) {
  var url = GCP_BANK_ACCOUNTS_ENDPOINT + "?organization_id=" + encodeURIComponent(organizationIdToFetch);
  return fetch(url, {
    method: "GET",
    headers: { "X-Api-Key": GCP_API_KEY },
  })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /bank-accounts (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) {
        throw new Error(result.error || "Unknown error from /bank-accounts");
      }
      return result.accounts || [];
    });
}

function fetchBankAccountsForOrgSafely(organizationIdToFetch) {
  return fetchBankAccountsForOrg(organizationIdToFetch).catch(function (err) {
    console.error("widget.js: couldn't fetch /bank-accounts for organization_id=" + organizationIdToFetch + " ->", err);
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
 * Org-select popup (2026-08-01, later, per Ravindra) -- replaces the
 * earlier cm_entities-based entity picker. Step 1: pick a REAL Zoho Books
 * organization (live list from /organizations). Step 2: pick which of
 * THAT org's real bank accounts to process, via checkboxes populated from
 * a live /bank-accounts?organization_id=... call. Clicking "OK" disables
 * both (per Ravindra's ask) rather than hiding them, and starts the same
 * per-account progress UI as before, shown below this panel.
 * --------------------------------------------------------------------*/
function showOrgSelectPanel() {
  document.getElementById("status").hidden = true;

  var orgSelectEl = document.getElementById("org-select");
  orgSelectEl.innerHTML = "";
  orgList.forEach(function (org, i) {
    var option = document.createElement("option");
    option.value = String(i);
    // Name + org_id only (2026-08-02, per Ravindra: no plan/subscription
    // wording in the picker) -- plan_name is still fetched and available on
    // the org object (via /organizations) in case it's needed elsewhere
    // later, just not shown here.
    option.textContent = org.name + " (" + org.organization_id + ")";
    orgSelectEl.appendChild(option);
  });

  document.getElementById("org-select-panel").hidden = false;

  orgSelectEl.addEventListener("change", function () {
    loadDataForSelectedOrg();
  });
  // Populate the account checklist (and load this org's Categorization
  // rules -- 2026-08-06) for whichever org is selected first (the
  // <select>'s own default, index 0) without waiting for the user to
  // explicitly change it.
  loadDataForSelectedOrg();

  // Search-filter box (2026-08-02, per Ravindra: "need a search on name for
  // account to filter") -- re-renders the checklist (from the already-
  // fetched currentOrgAccounts, no re-fetch needed) on every keystroke.
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
    // 2026-08-06, per Ravindra: Categorization rules are now org-scoped
    // (cf_entity, mandatory on every record), loaded by loadDataForSelectedOrg()
    // whenever the org selection changes. If that fetch is still in flight, or
    // failed, or came back empty for THIS org, there's nothing to categorize
    // against -- block starting rather than silently sending an empty rule
    // list through to /categorize (which would 400 there anyway, but with a
    // much less specific error than we can give right here).
    if (categorizationRulesError) {
      alert("Categorization rules for \"" + org.name + "\" (" + org.organization_id + ") couldn't be loaded -- " +
        (categorizationRulesError.message || categorizationRulesError) + " Fix this (e.g. tag a record's " +
        "cf_entity with this organization_id) and reselect the organization to retry.");
      return;
    }
    if (!categorizationRules.length) {
      alert("Categorization rules for \"" + org.name + "\" (" + org.organization_id + ") are still loading -- " +
        "wait a moment and try again.");
      return;
    }

    // Built from checkedAccountIds/currentOrgAccounts, NOT by querying
    // checked checkboxes in the DOM (2026-08-02) -- with the search filter
    // now able to hide rows, a checked account whose row is currently
    // filtered out of view no longer has a checkbox element in the DOM at
    // all, so a direct DOM query would silently drop it from the run.
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

    // Prevent double-firing if clicked more than once before the disable
    // below takes visual effect.
    document.getElementById("org-start-btn").removeEventListener("click", onOrgStart);

    // Egypt entity paired bank-charge detection (2026-08-13, per Ravindra)
    // -- default UNCHECKED (off), read fresh at click time same as every
    // other org-select-panel field above. See widget.html's checkbox and
    // callGcpCategorize()'s use of this flag below.
    var detectPairedChargesEl = document.getElementById("detect-paired-charges-checkbox");
    var detectPairedCharges = !!(detectPairedChargesEl && detectPairedChargesEl.checked);

    selectedOrg = org;
    startCategorization(accounts, folderPath, org, detectPairedCharges);
  });
}

// Re-fetches the bank-account list AND the Categorization rules for
// whichever organization is currently selected in the <select> -- called
// once at popup-open time and again every time the org selection changes.
// Resets the search box and the checked-selection tracking, since switching
// organizations means a completely different set of accounts.
//
// Rules are fetched alongside accounts (2026-08-06, per Ravindra -- see
// fetchCategorizationRulesForOrg()'s docstring): they're now scoped to
// whichever org is selected, same lifecycle as the account checklist, so
// both are loaded together here rather than rules being fetched once at
// init() the way they used to be. The rules fetch failing does NOT block
// rendering the account checklist -- it's tracked separately
// (categorizationRules/categorizationRulesError) and checked at
// "OK, Start Categorizing" time instead, so a rules problem for this org is
// visible without preventing the accounts list itself from showing.
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
      console.error("widget.js: couldn't fetch /categorization-rules for organization_id=" +
        org.organization_id + " ->", err);
      categorizationRulesError = err;
      return [];
    });

  Promise.all([accountsPromise, rulesPromise]).then(function (results) {
    // Only apply the result if the user hasn't already changed the org
    // selection again while these fetches were in flight.
    var stillSelected = orgList[Number(orgSelectEl.value)] === org;
    if (!stillSelected) return;

    currentOrgAccounts = results[0];
    categorizationRules = results[1];
    console.log("Categorization rules for " + org.name + " (" + org.organization_id + ") -> " +
      categorizationRules.length + " rule(s): " + JSON.stringify(categorizationRules, null, 2));
    renderAccountChecklist("");
  });
}

// Renders (or re-renders) the checkbox list from currentOrgAccounts,
// filtered by `filterText` (case-insensitive substring match against the
// account's display label -- name + number) -- 2026-08-02, per Ravindra
// ("need a search on name for account to filter"). Which boxes are checked
// is tracked in checkedAccountIds, NOT read back off the DOM, so a
// selection made before narrowing the search survives even once its row is
// filtered out of view and its checkbox element no longer exists.
function renderAccountChecklist(filterText) {
  var listEl = document.getElementById("org-accounts-list");

  if (!currentOrgAccounts.length) {
    // main.py's /bank-accounts now filters server-side to account_type
    // "bank" only (2026-08-02, per Ravindra) -- an org with real accounts
    // but none of type Bank (e.g. only Cash/Credit Card accounts set up)
    // will legitimately show this empty state.
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

// Live "what's selected so far" reflection (2026-08-03, per Ravindra:
// "intuitively show the selected accounts in the main screen when checked,
// it should not be too cluttered") -- a compact chip row below the
// checklist, built from checkedAccountIds/currentOrgAccounts (not the DOM,
// same reasoning as onOrgStart() -- a checked account whose row is currently
// filtered out of view by the search box still needs to show up here).
// Capped at MAX_SUMMARY_CHIPS individual chips, with a "+N more" chip
// covering the rest, so selecting many accounts still renders as one short
// row rather than growing without bound -- keeping this "not too cluttered"
// even for an org with a long account list. Entirely hidden (no empty bar)
// when nothing is checked yet.
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

// Keeps the start button itself as a second, always-visible confirmation of
// how many accounts are currently selected -- e.g. "OK, Start Categorizing
// (3)" -- without needing to glance at the chip row above at all.
function updateStartButtonLabel(count) {
  var btn = document.getElementById("org-start-btn");
  if (!btn) return;
  btn.textContent = "OK, Start Categorizing" + (count ? " (" + count + ")" : "");
}

// Goes straight from the org-select popup into processing. Unlike the
// earlier version, the org-select panel is NOT hidden here -- it's left
// visible but DISABLED (per Ravindra's explicit ask), with the progress
// panel appearing below it.
//
// accounts: [{label, account_id, account_number}, ...] chosen via
// checkboxes in the popup. folderPath: this organization's OneDrive folder
// (from orgFolderMap). org: the full organization object selected, used at
// the end of the run to stamp ENTITY_ORG_FIELD.
function startCategorization(accounts, folderPath, org, detectPairedCharges) {
  if (!accounts.length) {
    showError("No bank accounts were selected to process.");
    return;
  }

  // Disable (not hide) the org/account selection controls while a run is
  // in progress -- 2026-08-01, later, per Ravindra. The search box (added
  // 2026-08-02) gets the same treatment -- no reason to let it keep
  // filtering a list whose checkboxes are all disabled anyway.
  document.getElementById("org-select").disabled = true;
  document.getElementById("org-start-btn").disabled = true;
  document.getElementById("account-search").disabled = true;
  document.querySelectorAll("#org-accounts-list input[type=checkbox]").forEach(function (box) {
    box.disabled = true;
  });

  document.getElementById("progress-panel").hidden = false;
  // Defensive reset in case this widget instance somehow ran once already
  // without being closed -- keeps a stale result banner from a previous run
  // from showing above this run's fresh rows.
  document.getElementById("result-banner").hidden = true;

  runCategorization(accounts, folderPath, org, detectPairedCharges);
}

/* ----------------------------------------------------------------------
 * Live progress UI
 * --------------------------------------------------------------------*/

function renderProgressRows(accounts) {
  var rowsEl = document.getElementById("account-rows");
  rowsEl.innerHTML = "";
  accounts.forEach(function (account, i) {
    var row = document.createElement("div");
    row.className = "account-row-item";
    row.id = "row-" + i;
    row.innerHTML =
      '<div class="row-icon">' + (i + 1) + "</div>" +
      '<div class="row-text">' +
        '<div class="row-account">' + escapeHtml(account.label) + "</div>" +
        '<div class="row-detail">Waiting&hellip;</div>' +
      "</div>";
    rowsEl.appendChild(row);
  });
}

function setRowState(index, state, detailText) {
  var row = document.getElementById("row-" + index);
  if (!row) return;
  row.className = "account-row-item is-" + state;
  var icon = row.querySelector(".row-icon");
  var detail = row.querySelector(".row-detail");
  if (state === "processing") {
    icon.innerHTML = '<div class="spinner"></div>';
  } else if (state === "success") {
    icon.textContent = "✓"; // checkmark
  } else if (state === "error") {
    icon.textContent = "✕"; // cross
  }
  if (detailText) detail.textContent = detailText;
}

function updateOverallProgress(done, total) {
  var pct = total === 0 ? 0 : Math.round((done / total) * 100);
  document.getElementById("overall-progress-fill").style.width = pct + "%";
  document.getElementById("overall-progress-pct").textContent = pct + "%";
  document.getElementById("overall-progress-label").textContent =
    "Processing " + done + " of " + total + " account" + (total === 1 ? "" : "s") + "…";
}

function escapeHtml(str) {
  var div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

/* ----------------------------------------------------------------------
 * Runs every SELECTED account through the GCP categorization endpoint.
 * This is a two-step design, not one call per account:
 *   1. ONE call to /list-files -- gets every file name in the selected
 *      organization's OneDrive folder, once, no matter how many accounts
 *      there are.
 *   2. Match each account's number against that list CLIENT-SIDE (same
 *      substring logic the backend used to do per-request), then fire the
 *      per-account /categorize calls all AT ONCE (Promise.all, not chained
 *      sequentially) -- each one now takes an exact file_name, so none of
 *      them touch the folder listing at all.
 * Rows still update live and independently as each account's own call
 * resolves -- they just may finish in a different order than they started
 * in, since several are genuinely in flight together now.
 *
 * accounts is now a list of OBJECTS ({label, account_id, account_number}),
 * not raw label strings (2026-08-01, later) -- account_id comes straight
 * from the org-scoped /bank-accounts call the user picked from, so there is
 * no more client-side account_id RESOLUTION step (the old
 * resolveAccountIds()/accountIdMap/ACCOUNT_ID_OVERRIDES machinery is gone
 * entirely -- it's simply the exact record the checkbox represented).
 *
 * folderPath: this organization's OneDrive folder (from orgFolderMap),
 * threaded through to both fetchOneDriveFileList() and every per-account
 * callGcpCategorize() call below. org: stamped into ENTITY_ORG_FIELD once
 * the run finishes.
 * --------------------------------------------------------------------*/
function runCategorization(accounts, folderPath, org, detectPairedCharges) {
  renderProgressRows(accounts);
  updateOverallProgress(0, accounts.length);

  var doneCount = 0;
  var anyError = false;
  // Extra notes NOT already visible in the per-account rows above (field-
  // update failures/soft-failures only) -- 2026-08-03, per Ravindra: this
  // used to also collect a duplicate one-line-per-account summary that got
  // shown on a separate "done" screen once the run finished, which is
  // exactly the "flashing screen overwriting the actual results" he flagged
  // -- each row above already shows its own full result, so nothing about
  // an individual account needs repeating here.
  var noteLines = [];
  // Tracks whether each field-update call actually succeeded (2026-08-02,
  // per Ravindra) -- used so showDonePanel()'s subtitle reflects what
  // genuinely happened instead of a hardcoded claim that's shown regardless
  // of whether the update call succeeded.
  var statusUpdateOk = false;
  // Split into two independently-tracked flags (2026-08-02, later, per
  // Ravindra's live test): bundling cf_entity_org + cf_bank_accounts into
  // ONE PUT was itself still failing, with Zoho returning a generic
  // {"code":1000,"message":"Internal Error"} that doesn't say which field
  // caused it. This is the exact same "one field's problem takes the whole
  // call down" failure mode this project has already hit twice before
  // (cf_date vs cf_status on 2026-07-25; cf_status vs this pair earlier
  // today) -- so it's split one level further, same as those. Whichever of
  // the two now fails alone tells us definitively which field's type/value
  // Zoho is rejecting, instead of guessing.
  var entityOrgUpdateOk = false;
  var bankAccountsUpdateOk = false;

  function recordDone(i, account, state, detail) {
    doneCount++;
    setRowState(i, state, detail);
    updateOverallProgress(doneCount, accounts.length);
  }

  fetchOneDriveFileList(folderPath)
    .then(function (allFiles) {
      var matchedFilesByIndex = matchFilesToAccounts(accounts, allFiles);

      var perAccountPromises = accounts.map(function (account, i) {
        var matchedFiles = matchedFilesByIndex[i] || [];
        if (!matchedFiles.length) {
          // BUG FIXED (2026-08-02, per Ravindra: "When errors are there in
          // processing like [no file found] the status is still
          // Categorization complete") -- this branch used to call
          // recordDone() with state "error" but never actually set
          // `anyError = true`, so cf_status still got set to
          // STATUS_VALUE_ON_SUCCESS at the end of the run even though this
          // account visibly failed. Every OTHER error path in this function
          // already set anyError -- this was the one that was missed.
          anyError = true;
          // Include which folder was actually searched (2026-08-02, per
          // Ravindra: "eventhough file is there with the account no getting
          // file not found error") -- a file existing in OneDrive doesn't
          // help if this run searched a DIFFERENT folder than the one it's
          // actually in (e.g. the wrong organization's mapped folder from
          // cm_orgidonedrivemap) -- this makes that mismatch immediately
          // visible instead of having to guess which folder was checked.
          recordDone(i, account, "error",
            "No file found in OneDrive containing this account's number (searched " +
            (folderPath || "the default folder") + ").");
          return Promise.resolve();
        }

        // Show which file(s) this row is actually working on WHILE it's
        // processing, not just an anonymous spinner -- Ravindra asked to see
        // the file name being processed, not only the account label.
        var fileLabel = matchedFiles.join(", ");
        setRowState(i, "processing", "Processing " + fileLabel + "…");
        // Usually one file per account, but process every match if there's
        // more than one (e.g. more than one statement dropped for the same
        // account) -- all in parallel, same reasoning as across accounts.
        var accountId = account.account_id || "";
        return Promise.all(matchedFiles.map(function (fileName) {
          return callGcpCategorize(fileName, accountId, folderPath, detectPairedCharges);
        }))
          .then(function (results) {
            var succeeded = results.filter(function (r) { return r.success; });
            var failed = results.filter(function (r) { return !r.success; });
            var totalRows = succeeded.reduce(function (sum, r) { return sum + (r.total || 0); }, 0);
            var totalOthers = succeeded.reduce(function (sum, r) { return sum + (r.others || 0); }, 0);
            // itemized_pairs_detected (2026-08-13) -- only ever non-zero when
            // detectPairedCharges was checked for this run; see
            // categorize_from_module.py's categorize_workbook() stats.
            var totalItemizedPairs = succeeded.reduce(function (sum, r) { return sum + (r.itemized_pairs_detected || 0); }, 0);
            // A legacy .xls input gets converted and uploaded under a NEW
            // .xlsx-suffixed name rather than overwriting the original .xls
            // (2026-08-03, per Ravindra -- see main.py's uploaded_file_name
            // note) -- flag this in the summary so it's obvious there's a
            // different file to open, not the one originally selected.
            var renamedFiles = succeeded
              .filter(function (r) { return r.uploaded_file_name && r.uploaded_file_name !== r.file_name; })
              .map(function (r) { return r.file_name + " -> " + r.uploaded_file_name; });

            // Per-file error/success detail keeps the file name attached to
            // its own outcome (main.py's response always includes file_name)
            // rather than falling back to matchedFiles, in case a file
            // couldn't even be identified before the call failed.
            if (failed.length && !succeeded.length) {
              anyError = true;
              recordDone(i, account, "error",
                failed.map(function (r) { return (r.file_name || fileLabel) + ": " + r.error; }).join("; "));
            } else if (failed.length) {
              anyError = true;
              recordDone(i, account, "error",
                succeeded.length + "/" + matchedFiles.length + " file(s) ok (" + fileLabel + "), " + totalRows +
                " row(s) categorized -- but " + failed.length + " file(s) failed: " +
                failed.map(function (r) { return (r.file_name || fileLabel) + ": " + r.error; }).join("; "));
            } else {
              recordDone(i, account, "success",
                matchedFiles.length + " file(s) (" + fileLabel + "), " + totalRows + " row(s) categorized" +
                (totalOthers ? " (" + totalOthers + " as Others)" : "") +
                (totalItemizedPairs ? " (" + totalItemizedPairs + " paired bank-charge group(s) detected)" : "") +
                (renamedFiles.length ? " -- converted from .xls, saved as new file: " +
                  renamedFiles.join(", ") : ""));
            }
          })
          .catch(function (err) {
            anyError = true;
            recordDone(i, account, "error", (err && err.message) ? err.message : String(err));
          });
      });

      return Promise.all(perAccountPromises);
    })
    .catch(function (err) {
      // /list-files itself failed -- nothing could even start. Mark every
      // row as failed rather than leaving them stuck on "Waiting…".
      anyError = true;
      var msg = "Couldn't list the OneDrive folder: " + ((err && err.message) ? err.message : String(err));
      accounts.forEach(function (account, i) { recordDone(i, account, "error", msg); });
    })
    .then(function () {
      // cf_status is now sent in its OWN call, separate from
      // cf_entity_org/cf_bank_accounts (2026-08-02, per Ravindra: "The
      // entity and bank accounts not updated in the bank transaction module
      // along with status and date"). Previously all three were bundled
      // into one PUT -- if Zoho rejected cf_entity_org or cf_bank_accounts
      // for any reason (wrong field type, validation, etc.), the WHOLE call
      // could fail, silently taking cf_status down with it too, exactly the
      // same class of bug cf_date already had its own isolated call for
      // (see trySetProcessedDate()'s comment, 2026-07-25). Splitting this
      // one more time means a problem with either group is now reported
      // separately and specifically, instead of one failure possibly
      // masking as "everything updated" or hiding which field was the
      // actual problem.
      var statusFields = {};
      statusFields[STATUS_FIELD] = anyError ? STATUS_VALUE_ON_ERROR : STATUS_VALUE_ON_SUCCESS;
      return updateRecordFields(statusFields)
        .then(function () {
          statusUpdateOk = true;
        })
        .catch(function (err) {
          console.error("widget.js: failed to update " + STATUS_FIELD + " ->", err);
          noteLines.push("(Note: couldn't update the record's " + STATUS_FIELD + " field -- " +
            (err && err.message ? err.message : err) + ")");
        });
    })
    .then(function () {
      // cf_entity_org, now on its OWN call (2026-08-02, later) -- see the
      // entityOrgUpdateOk/bankAccountsUpdateOk declaration above for why.
      var entityOrgFields = {};
      entityOrgFields[ENTITY_ORG_FIELD] = org.name + " (" + org.organization_id + ")";
      return updateRecordFields(entityOrgFields)
        .then(function () {
          entityOrgUpdateOk = true;
        })
        .catch(function (err) {
          console.error("widget.js: failed to update " + ENTITY_ORG_FIELD + " ->", err);
          noteLines.push("(Note: couldn't update the record's " + ENTITY_ORG_FIELD + " field -- " +
            (err && err.message ? err.message : err) + ")");
        });
    })
    .then(function () {
      // cf_bank_accounts, now on its OWN call too -- isolated from
      // cf_entity_org so that if ONE of these two is what Zoho's "Internal
      // Error" was actually about (e.g. cf_bank_accounts being a
      // restricted-value picklist that can't take this free-text,
      // comma-joined string of account names -- see the reply to Ravindra
      // that flagged this as the leading hypothesis), the OTHER field still
      // updates instead of both failing together.
      var bankAccountsFields = {};
      bankAccountsFields[SELECTED_BANK_ACCOUNTS_FIELD] = accounts.map(function (a) { return a.label; }).join(", ");
      return updateRecordFields(bankAccountsFields)
        .then(function () {
          bankAccountsUpdateOk = true;
        })
        .catch(function (err) {
          console.error("widget.js: failed to update " + SELECTED_BANK_ACCOUNTS_FIELD + " ->", err);
          noteLines.push("(Note: couldn't update the record's " + SELECTED_BANK_ACCOUNTS_FIELD + " field -- " +
            (err && err.message ? err.message : err) + ")");
        });
    })
    .then(function () {
      // Best-effort and independent of the status update above -- only
      // attempted on a clean run (see runCategorization()'s existing
      // anyError check), and its own failure is reported as a soft note
      // rather than anything that blocks the done panel.
      if (anyError) return;
      return trySetProcessedDate().catch(function (err) {
        console.error("widget.js: couldn't set " + DATE_PROCESSED_FIELD + " with any candidate format ->", err);
        noteLines.push("(Note: status updated, but couldn't set " + DATE_PROCESSED_FIELD + " -- " +
          (err && err.message ? err.message : err) + ")");
      });
    })
    .then(function () {
      showDonePanel(!anyError, noteLines.filter(Boolean), statusUpdateOk, entityOrgUpdateOk, bankAccountsUpdateOk);
    });
}

// ONE Graph call per run -- see main.py's docstring for why this is split
// out from /categorize instead of each account call re-listing the folder.
//
// folderPath: the selected organization's OneDrive folder, passed through
// as main.py's existing optional ?folder_path= query param -- omitted
// entirely (not even as an empty string) when falsy, so the backend falls
// back to its own default folder (MS_STATEMENTS_FOLDER_PATH).
function fetchOneDriveFileList(folderPath) {
  var url = GCP_LIST_FILES_ENDPOINT;
  if (folderPath) {
    url += "?folder_path=" + encodeURIComponent(folderPath);
  }
  return fetch(url, {
    method: "GET",
    headers: { "X-Api-Key": GCP_API_KEY },
  })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /list-files (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) {
        throw new Error(result.error || "Unknown error from /list-files");
      }
      return result.files || [];
    });
}

// Matches every account's trailing digit run (from account.account_number)
// against the file list, same substring rule the backend used to apply
// per-request. Returns an array PARALLEL to `accounts` (matched by index,
// not by label text -- 2026-08-01, later -- since two different real
// accounts could in principle share an identical display label, but never
// share an array index): matchedFilesByIndex[i] = [fileName, ...].
function matchFilesToAccounts(accounts, allFiles) {
  return accounts.map(function (account) {
    var digitsOnly = (account.account_number || "").replace(/[^0-9]/g, "");
    // Fallback to account.label (2026-08-02, confirmed with real live
    // data): Zoho's Banking module can leave a real bank account's
    // account_number field COMPLETELY BLANK while the exact same digits
    // are instead typed directly into the account's NAME in Zoho (e.g.
    // account_name = "NBF USD 012001940536", account_number = "").
    // `account.label` (built in onOrgStart, see its own comment) is
    // `account_name + (account_number ? " "+account_number : "")`, so when
    // account_number is blank, label IS just account_name -- extracting
    // digits from it recovers exactly the account number needed, with no
    // extra plumbing.
    if (!digitsOnly) {
      digitsOnly = (account.label || "").replace(/[^0-9]/g, "");
    }
    if (!digitsOnly) {
      return [];
    }
    return allFiles.filter(function (fileName) {
      return fileName.toLowerCase().indexOf(digitsOnly.toLowerCase()) !== -1;
    });
  });
}

// folderPath: the selected organization's OneDrive folder this file lives
// in, passed through as main.py's existing optional folder_path body field
// -- omitted entirely when falsy, so the backend falls back to its own
// default folder, same as fetchOneDriveFileList() above.
function callGcpCategorize(fileName, accountId, folderPath, detectPairedCharges) {
  var body = {
    file_name: fileName,
    rules: categorizationRules,
    // This statement's own Zoho account_id -- comes straight from the
    // org-scoped /bank-accounts call the user picked this account from
    // (2026-08-01, later), NOT a bank account number and no longer
    // resolved client-side at all. categorize_from_module.py uses it to
    // fill in whichever of Debit Account/Credit Account represents "this
    // account" for each row. The wire field name stays "account_number"
    // for backward compatibility with the GCP service's existing request
    // contract (main.py/categorize_from_module.py) -- only the value it
    // carries has changed, from a bank account number to a real account_id.
    account_number: accountId,
    // Egypt entity paired bank-charge detection (2026-08-13, per Ravindra)
    // -- OPT-IN, from widget.html's checkbox (default unchecked/false).
    // See categorize_from_module.py's categorize_workbook() own
    // detect_paired_bank_charges parameter for what this actually does.
    detect_paired_bank_charges: !!detectPairedCharges,
  };
  if (folderPath) {
    body.folder_path = folderPath;
  }
  return fetch(GCP_CATEGORIZE_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Api-Key": GCP_API_KEY,
    },
    body: JSON.stringify(body),
  }).then(function (resp) {
    return resp.json().catch(function () {
      throw new Error("Non-JSON response from categorization service (HTTP " + resp.status + ")");
    });
  });
}

// Candidate string shapes for the current moment, most-likely-correct
// first, all built from the SAME instant so they only differ in format.
// CONFIRMED LIVE (2026-07-25): a plain `new Date().toISOString()` (which
// ends in a bare "Z") was rejected outright -- Zoho's error was "Invalid
// date time 2026-07-25T02:55:20Z specified." -- so a literal "Z" UTC
// designator is out. A follow-up attempt using UTC digits with a numeric
// "+0000"/"+00:00" offset WAS accepted (no error), but then displayed the
// wrong clock time -- e.g. accepted at 02:55 UTC showed as "03:03 AM" in
// Zoho while the widget was actually run at 08:34 local time. That's not a
// timezone-conversion mismatch (03:03 vs 08:34 isn't a round offset) so
// much as evidence this custom field doesn't do real timezone-aware display
// at all -- it just echoes back whatever Y/M/D H:M:S digits were sent,
// offset suffix or not. Fix: build the digits from the BROWSER'S OWN LOCAL
// clock (getHours()/getMinutes()/etc., not the UTC getters) instead of UTC,
// so whatever gets echoed back matches the actual wall-clock moment the
// widget ran, not a UTC instant relabeled as if it were local. The offset
// suffix on candidates 1-2 is now the browser's REAL local offset (via
// localOffsetString()) rather than a hardcoded "+0000" -- harmless if
// Zoho ignores it for display as suspected, but correct if it's ever read
// back programmatically instead of just displayed.
function localOffsetString(date, withColon) {
  var totalMinutes = -date.getTimezoneOffset(); // e.g. +330 for IST, -420 for PDT
  var sign = totalMinutes >= 0 ? "+" : "-";
  var abs = Math.abs(totalMinutes);
  var hh = pad2(Math.floor(abs / 60));
  var mm = pad2(abs % 60);
  return sign + hh + (withColon ? ":" : "") + mm;
}

function pad2(n) {
  return (n < 10 ? "0" : "") + n;
}

function candidateDateFormats(date) {
  var y = date.getFullYear();
  var mo = pad2(date.getMonth() + 1);
  var d = pad2(date.getDate());
  var h = pad2(date.getHours());
  var mi = pad2(date.getMinutes());
  var s = pad2(date.getSeconds());
  return [
    y + "-" + mo + "-" + d + "T" + h + ":" + mi + ":" + s + localOffsetString(date, false), // e.g. +0530, no colon
    y + "-" + mo + "-" + d + "T" + h + ":" + mi + ":" + s + localOffsetString(date, true),  // e.g. +05:30, with colon
    y + "-" + mo + "-" + d + " " + h + ":" + mi + ":" + s,             // space-separated, no offset
    y + "-" + mo + "-" + d,                                             // date-only, last resort
  ];
}

// Tries each candidate format from candidateDateFormats() in turn (as its
// own /update-status call, one at a time -- not in parallel, so we never
// fire more than one PUT at a record simultaneously), stopping at the
// first one Zoho accepts. Rejects only if every candidate fails.
function trySetProcessedDate() {
  var candidates = candidateDateFormats(new Date());
  var i = 0;
  function attempt() {
    if (i >= candidates.length) {
      return Promise.reject(new Error(
        "None of the " + candidates.length + " candidate date formats were accepted -- see console for each one's error."));
    }
    var value = candidates[i];
    i++;
    var fields = {};
    fields[DATE_PROCESSED_FIELD] = value;
    return updateRecordFields(fields)
      .then(function (result) {
        console.log("widget.js: " + DATE_PROCESSED_FIELD + " accepted with format: " + JSON.stringify(value));
        return result;
      })
      .catch(function (err) {
        console.warn("widget.js: " + DATE_PROCESSED_FIELD + " format " + JSON.stringify(value) + " rejected -> " +
          (err && err.message ? err.message : err));
        return attempt();
      });
  }
  return attempt();
}

// Sets one or more fields on this record via our own GCP backend (main.py's
// /update-status) -- NOT ZFAPPS.request(), same reason as fetchCategorizationRulesForOrg().
// `fields` is a plain {api_name: value, ...} map. Called up to THREE times
// per run now (2026-08-02, split one further) -- once for STATUS_FIELD alone,
// once for ENTITY_ORG_FIELD/SELECTED_BANK_ACCOUNTS_FIELD together, and once
// (separately, best-effort, only on a clean run) for DATE_PROCESSED_FIELD via
// trySetProcessedDate() -- each group is isolated so a problem with one
// group's field(s) can't silently take another group down with it (see
// runCategorization()'s comments for the full history of why this keeps
// getting split further). Any failure here throws with whatever main.py's
// /update-status returned, which already includes Zoho's own raw response
// body (see zoho_books_client.py's _request()) -- the full text is what
// actually explains WHY a field didn't update, so make sure it's visible
// (not cut off) in whatever shows this error to the user.
function updateRecordFields(fields) {
  return fetch(GCP_UPDATE_STATUS_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Api-Key": GCP_API_KEY,
    },
    body: JSON.stringify({
      module_api_name: MODULE_API_NAME,
      record_id: recordId,
      fields: fields,
    }),
  })
    .then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("Non-JSON response from /update-status (HTTP " + resp.status + ")");
      });
    })
    .then(function (result) {
      if (!result.success) {
        throw new Error(result.error || "Unknown error from /update-status");
      }
      return result;
    });
}

// statusUpdateOk/entityOrgUpdateOk/bankAccountsUpdateOk (2026-08-02, per
// Ravindra): whether each corresponding /update-status call actually
// succeeded, so this subtitle states what genuinely happened instead of a
// hardcoded claim that used to be shown regardless of whether the update
// call itself succeeded or failed (Ravindra: "The entity and bank accounts
// not updated in the bank transaction module along with status and date" --
// yet the done panel always said "...this record's status is now..." even
// when it wasn't). entityOrgUpdateOk/bankAccountsUpdateOk were one combined
// flag until Zoho returned a generic {"code":1000,"message":"Internal
// Error"} for the bundled call -- split so a failure in just one of the two
// still reports specifically which one.
// REWRITTEN (2026-08-03, per Ravindra: "After the processing is done a new
// flashing screen is overwriting the actual results and it is very
// confusing even when error occurs or it's a success. just show the
// results as a single screen without flashing and if success dont make the
// progress bars of the files disappear. keep them and have a button to
// close. Incase of error same thing but show the complete error currently
// it is inside a scroller which is not properly seen"). This used to hide
// #progress-panel entirely and swap in a completely separate #done-panel
// (with its own animated pop-in checkmark) -- exactly the "flashing screen
// overwriting the actual results" complaint: the per-account rows (each
// already showing its own full, untruncated result) disappeared right when
// they mattered most, replaced by a duplicate one-line-per-account summary
// squeezed into a small fixed-height scrolling box that clipped longer
// error text. Now this just reveals a small result banner IN PLACE below
// the existing rows -- nothing above it is hidden, removed, or re-rendered,
// and there is no separate "screen" to flash in.
function showDonePanel(allSucceeded, notes, statusUpdateOk, entityOrgUpdateOk, bankAccountsUpdateOk) {
  var banner = document.getElementById("result-banner");
  banner.hidden = false;
  banner.className = "result-banner" + (allSucceeded ? "" : " has-errors");

  document.getElementById("result-icon").textContent = allSucceeded ? "✓" : "!";
  document.getElementById("result-title").textContent = allSucceeded
    ? "All done!"
    : "Finished with some errors";

  var subtitle;
  if (allSucceeded) {
    subtitle = statusUpdateOk
      ? "Every account was categorized and this record's status is now “" + STATUS_VALUE_ON_SUCCESS + "”."
      : "Every account was categorized, but the record's status field couldn't be updated -- see the note below.";
  } else {
    subtitle = statusUpdateOk
      ? "Some accounts couldn't be processed -- see each row above for the full error. Status set to “" +
        STATUS_VALUE_ON_ERROR + "”."
      : "Some accounts couldn't be processed, and the record's status field couldn't be updated either -- " +
        "see each row above, and the notes below.";
  }
  if (!entityOrgUpdateOk && !bankAccountsUpdateOk) {
    subtitle += " (Organization and bank-accounts fields also weren't updated -- see the notes below.)";
  } else if (!entityOrgUpdateOk) {
    subtitle += " (The organization field also wasn't updated -- see the note below.)";
  } else if (!bankAccountsUpdateOk) {
    subtitle += " (The bank-accounts field also wasn't updated -- see the note below.)";
  }
  document.getElementById("result-subtitle").textContent = subtitle;

  var notesEl = document.getElementById("result-notes");
  notesEl.innerHTML = "";
  notes.forEach(function (line) {
    var div = document.createElement("div");
    div.className = "result-note-line";
    div.textContent = line;
    notesEl.appendChild(div);
  });
}
