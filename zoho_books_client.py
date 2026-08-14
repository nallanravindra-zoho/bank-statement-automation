"""
zoho_books_client.py -- minimal Zoho Books client for this service.

Scoped to exactly two things the categorization widget needs and can no
longer get via ZFAPPS.request() (that requires an "API Configuration" set up
inside Zoho Sigma, which doesn't exist for a widget that was never
registered there -- see the conversation this was built from):
  1. list_custom_module_records() -- read every record in the "Categorization"
     module (category/account/posting rules).
  2. update_custom_module_record() -- set cf_status on the "Bank Transaction
     Testing" record once a run finishes.

Auth pattern (OAuth refresh-token / Self Client) copied from the original
pipeline's zoho_client.py, since it's already correct and already handles a
real incident (see _refresh_access_token()'s docstring): send refresh
params in the POST body not the URL (so a failure never leaks credentials
into a request-URL-bearing error message), and cache the access token at
module level so a burst of calls in one run doesn't each mint a fresh token
and trip Zoho's refresh-rate limit. Deliberately NOT importing zoho_client.py
directly -- that file also carries journal-entry/bank-transfer posting logic
and a models.py dependency this service has no use for.
"""
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

_token_cache = {"access_token": None, "expires_at": 0.0}

ACCOUNTS_BASE_URL = os.environ.get("ZOHO_ACCOUNTS_BASE_URL", "https://accounts.zoho.com")
BOOKS_BASE_URL = os.environ.get("ZOHO_BOOKS_BASE_URL", "https://www.zohoapis.com/books/v3")


@dataclass
class ZohoConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    organization_id: str

    @classmethod
    def from_env(cls) -> "ZohoConfig":
        missing = [
            k for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_ORGANIZATION_ID")
            if not os.environ.get(k)
        ]
        if missing:
            raise EnvironmentError(f"Missing Zoho credentials in environment: {missing}.")
        return cls(
            client_id=os.environ["ZOHO_CLIENT_ID"],
            client_secret=os.environ["ZOHO_CLIENT_SECRET"],
            refresh_token=os.environ["ZOHO_REFRESH_TOKEN"],
            organization_id=os.environ["ZOHO_ORGANIZATION_ID"],
        )


class ZohoBooksClient:
    def __init__(self, config: ZohoConfig):
        self.config = config
        self._access_token: Optional[str] = None

    def _refresh_access_token(self) -> str:
        now = time.time()
        if _token_cache["access_token"] and now < _token_cache["expires_at"]:
            self._access_token = _token_cache["access_token"]
            return self._access_token

        resp = requests.post(
            f"{ACCOUNTS_BASE_URL}/oauth/v2/token",
            data={  # POST body, not URL query params -- see module docstring
                "refresh_token": self.config.refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            raise RuntimeError(f"Zoho token refresh failed ({resp.status_code}): {resp.text[:300]}") from None

        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Zoho token refresh failed: {data}")

        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        _token_cache["access_token"] = self._access_token
        _token_cache["expires_at"] = now + max(expires_in - 300, 60)  # refresh 5 min early
        return self._access_token

    def _headers(self) -> dict:
        if not self._access_token:
            self._refresh_access_token()
        return {"Authorization": f"Zoho-oauthtoken {self._access_token}"}

    def _request(self, method: str, path: str, organization_id=None, **kwargs) -> dict:
        """organization_id lets a caller override which org this ONE request
        hits, instead of always self.config.organization_id (2026-08-01, per
        Ravindra -- see list_organizations()/list_bank_accounts() below: a
        single Self Client login can see more than one Zoho Books
        organization, confirmed live against the real tenant, so a request
        needs to be able to target a DIFFERENT org than whichever one this
        service's own credentials default to). Pass organization_id=False
        (not None) to omit the param entirely -- used by list_organizations(),
        since that call lists every org and isn't scoped to one at all;
        Zoho's own docs don't document what it does if you send one anyway,
        so it's left out rather than guessed at."""
        url = f"{BOOKS_BASE_URL}{path}"
        params = kwargs.pop("params", {}) or {}
        if organization_id is not False:
            params["organization_id"] = organization_id or self.config.organization_id

        resp = requests.request(method, url, headers=self._headers(), params=params, **kwargs)
        if resp.status_code == 401:  # access token expired mid-run -- refresh once and retry
            self._refresh_access_token()
            resp = requests.request(method, url, headers=self._headers(), params=params, **kwargs)
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise requests.exceptions.HTTPError(f"{e} | Zoho response body: {resp.text}", response=resp) from e
        return resp.json()

    def list_custom_module_records(self, module_api_name: str, per_page: int = 200) -> list:
        """Returns every record in a custom module. Confirmed live on
        2026-07-23 against the real cm_categorisation module: the list comes
        back under "module_records" (tried first below). The other keys are
        kept as a fallback in case a different custom module ever wraps it
        differently -- if that ever happens, this raises with the RAW body,
        which is exactly what to paste back to add the right key here."""
        data = self._request("GET", f"/{module_api_name}", params={"per_page": per_page})
        for key in ("module_records", "custommodule_records", "data", "records", module_api_name):
            value = data.get(key)
            if isinstance(value, list):
                return value
        raise ValueError(
            f"Unexpected response shape from GET /{module_api_name} -- no list found under "
            f"module_records/custommodule_records/data/records/{module_api_name!r}. Raw response: {data}"
        )

    def update_custom_module_record(self, module_api_name: str, record_id: str, fields: dict) -> dict:
        """Updates one or more fields on a single custom module record (e.g.
        {"cf_status": "Categorization Complete"})."""
        return self._request("PUT", f"/{module_api_name}/{record_id}", json=fields)

    def get_custom_module_record(self, module_api_name: str, record_id: str, organization_id: str = None) -> dict:
        """Fetches ONE custom-module record's FULL detail (GET
        /{module_api_name}/{record_id}). Added 2026-08-11, same reasoning as
        get_journal()/get_expense()/get_bank_transaction()/get_vendor_payment()
        above: Ravindra reported cm_referenceaiprompt's cf_ai_prompt field
        (a long "Text Box (Multi-line)" custom field) kept showing as
        blank/0-chars via list_custom_module_records() (GET
        /{module_api_name}, the LIST endpoint) even after confirming, via a
        Zoho UI screenshot, that the record held real prompt text -- the
        leading theory is the same one already confirmed for Journal/Expense/
        etc.'s notes/description fields: Zoho's LIST response for a module
        omits or truncates a long free-text field, only including it on a
        single-record detail GET. _fetch_ai_prompt_template() (main.py) uses
        this as a fallback specifically when the list response's field comes
        back blank.

        DOUBLY UNVERIFIED LIVE (flagged more heavily than usual): unlike
        get_journal()/get_expense() (where /journals/{id} and /expenses/{id}
        are Zoho's own well-documented standard endpoints), a CUSTOM
        module's single-record GET path and response shape aren't documented
        anywhere this project has access to -- both the URL shape
        (/{module_api_name}/{record_id}, by analogy with
        update_custom_module_record()'s PUT to the same path) and the
        response's wrapper key are best-effort guesses. This tries the same
        candidate wrapper keys list_custom_module_records() already uses
        (plus the module's own api name), and falls back to returning the
        raw top-level response if none match -- some Zoho single-record GETs
        return the record's fields unwrapped at the top level. If every
        guess here is wrong, the caller's field lookup on the returned dict
        just comes back blank (same safe failure mode as before -- this
        never raises for a shape mismatch, only for a genuine HTTP/network
        failure), and _fetch_ai_prompt_template()'s own diagnostic logging
        will still print enough (raw keys, response shape) to build an exact
        fix next, without needing another blind guess."""
        data = self._request("GET", f"/{module_api_name}/{record_id}", organization_id=organization_id)
        for key in (module_api_name, "module_record", "custommodule_record", "data", "record"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return data

    def list_contacts(self, contact_type: str = None, per_page: int = 200, organization_id: str = None) -> list:
        """Returns every Contact in the org (GET /contacts), paginated the
        same way as list_bank_accounts()/list_custom_module_records(). Zoho
        Books' Contacts module covers BOTH vendors and customers -- each
        record's own contact_type field ("vendor" or "customer") is what
        distinguishes them, confirmed via Zoho's own API docs. Filtering by
        contact_type is done CLIENT-SIDE here (after fetching the full page),
        not via a query param -- Zoho's docs don't clearly document a
        query-param name for this filter, and getting that wrong would
        silently return the wrong (or an empty) list rather than erroring, a
        much worse failure mode than one extra full-list fetch. Used by
        posting_service.py's find_contact_id() to fuzzy-match a Vendor
        Payment/Customer payment row's narration against real Zoho contact
        names -- called at most once per posting run (per contact_type), not
        once per row, same "list once, match client-side" shape as
        list_bank_accounts()/resolveAccountIds() in widget.js.

        organization_id (2026-08-09, per Ravindra's "post to Zoho Books"
        feature -- the Post widget gets its OWN organization picker, run
        independently of whichever org a prior categorize run used): pass a
        DIFFERENT org's ID to list THAT org's contacts instead of this
        client's own default, same override mechanism as
        list_bank_accounts()/create_journal_entry() etc. below. Omit (None)
        to keep using self.config.organization_id, unchanged from before
        this parameter existed."""
        contacts = []
        page = 1
        while True:
            data = self._request(
                "GET", "/contacts", organization_id=organization_id,
                params={"page": page, "per_page": per_page},
            )
            page_contacts = data.get("contacts")
            if page_contacts is None:
                raise ValueError(
                    f"Unexpected response shape from GET /contacts -- no list found under "
                    f"'contacts'. Raw response: {data}"
                )
            contacts.extend(page_contacts)
            page_context = data.get("page_context") or {}
            if not page_context.get("has_more_page"):
                break
            page += 1
        if contact_type:
            contacts = [c for c in contacts if str(c.get("contact_type", "")).strip().lower() == contact_type.strip().lower()]
        return contacts

    def create_journal_entry(self, journal_date: str, line_items: list, reference_number: str = None,
                              notes: str = None, organization_id: str = None) -> dict:
        """Creates a Journal Entry (POST /journals) -- a plain double-entry
        posting, e.g. [{"account_id": "...", "debit_or_credit": "debit",
        "amount": 100.0}, {"account_id": "...", "debit_or_credit": "credit",
        "amount": 100.0}]. Used for the "Journal" posting type -- the generic
        fallback for an entry that isn't a bank-to-bank Transfer, an Expense,
        or a Vendor Payment.

        CORRECTED 2026-08-09, per Ravindra (who caught this from an earlier,
        separate phase of this project): "Transfer to another account"/
        "Transfer from another account" are NOT posted through this method
        anymore -- see create_bank_transfer() below. The original assumption
        here (recorded 2026-07-26) that "Zoho Books' v3 API has no dedicated
        fund-transfer endpoint" was wrong -- POST /banktransactions with
        transaction_type=transfer_fund is exactly that endpoint, confirmed
        via a real, previously-working implementation of this same feature
        from an earlier build. create_journal_entry() is a fully generic
        double-entry post with no account-type restriction at all (works for
        GL/expense/liability/equity/asset accounts in any combination) -- use
        it for "Journal", not as a transfer substitute.

        notes (2026-08-11, per Ravindra: "For all the posting types i want
        the description/notes filed to be filled with the narration text
        from the statement"): Zoho's own field name for a Journal Entry's
        free-text field is "notes" (Expense/Vendor Payment use
        "description" instead -- see create_expense()/create_vendor_payment()
        below). posting_service.py's _post_journal_like() passes the row's
        own narration here.

        organization_id (2026-08-09, per Ravindra's "post to Zoho Books"
        feature -- the Post widget picks its own organization, same as
        /bank-accounts already supports): posts into a DIFFERENT org than
        this client's own default -- see list_bank_accounts()'s docstring for
        the confirmed-live "one Self Client login, many orgs" mechanics this
        relies on. Omit (None) to keep using self.config.organization_id."""
        body = {"journal_date": journal_date, "line_items": line_items}
        if reference_number:
            body["reference_number"] = reference_number
        if notes:
            body["notes"] = notes
        return self._request("POST", "/journals", organization_id=organization_id, json=body)

    def get_journal(self, journal_id: str, organization_id: str = None) -> dict:
        """Fetches ONE Journal Entry's full detail (GET /journals/{journal_id})
        and returns just the inner "journal" object (same unwrap convention
        as create_journal_entry()'s own response handling in
        posting_service.py). Added 2026-08-11, per Ravindra's live report:
        a repost of the same Journal narration wasn't caught as a duplicate
        even though the FIRST post's entry genuinely had notes text in Zoho
        (visible when he opened it directly in the Zoho UI). The leading
        theory: Zoho's list/search endpoints (GET /journals with query
        params, including find_journals_by_date() below) return an
        abbreviated record that likely omits free-text fields like "notes"
        entirely, so a duplicate check that only looked at a LIST response's
        own "notes" key would always see it blank and never find a real
        match -- this full single-record GET is the fix (see
        posting_service.py's _check_duplicate_description() for how it's
        used). NOTE: this theory hasn't itself been confirmed against a raw
        Zoho response -- it's inferred from the reported symptom, same
        confidence level as every other "UNVERIFIED LIVE" item in this
        project -- worth Ravindra pasting a raw GET /journals (list) response
        alongside a raw GET /journals/{id} (detail) response to confirm
        directly whether "notes" really is list-only-omitted. Returns {} if
        the response is missing the expected "journal" key (defensive, same
        as every other get_*/find_* method here -- the caller treats an
        empty dict's missing "notes" key as simply not matching, not as an
        error)."""
        data = self._request("GET", f"/journals/{journal_id}", organization_id=organization_id)
        return data.get("journal") or {}

    def create_expense(self, account_id: str, paid_through_account_id: str, date: str, amount: float,
                        reference_number: str = None, description: str = None, tax_treatment: str = None,
                        tax_id: str = None, organization_id: str = None) -> dict:
        """Creates an Expense (POST /expenses). account_id is the expense/GL
        account being charged; paid_through_account_id is the bank/cash
        account it was paid from -- both required for a real expense entry
        (Zoho's own public docs page for this endpoint didn't list
        paid_through_account_id explicitly when checked 2026-07-26, which
        looked like a documentation gap rather than the field not existing --
        worth confirming with a real curl call per DEPLOY.md before relying
        on this in production, same as every other "unverified live" item in
        this project).

        tax_treatment / tax_id (2026-08-11 REWORK, per a live 400 rejection
        of the first attempt -- see posting_service.py's module docstring
        for the full Bank Charges/VAT consolidation feature): only ever set
        on the two synthetic consolidated Expense rows main.py's
        /post-bank-charges route builds. Two IMPORTANT corrections vs the
        2026-08-10 first version of this parameter:

        1. tax_treatment and the tax RATE ("Standard Rate [5%]" vs "Exempt")
           are TWO SEPARATE Zoho fields, confirmed live by Ravindra sending
           screenshots of his org's actual Expense form -- a "Tax Treatment"
           dropdown (VAT Registered / Non VAT Registered / GCC VAT
           Registered / GCC Non VAT Registered / Non GCC / Out Of Scope /
           VAT Registered - Designated Zone / Non VAT Registered -
           Designated Zone) and, separately, a "Tax" dropdown (Exempt /
           Standard Rate [5%] / Zero Rate [0%] / ...). The first version of
           this feature wrongly conflated the two into one string
           ("VAT Registered Standard 5%"), which Zoho rejected outright
           (`{"code":2,"message":"Invalid value passed for tax_treatment"}`)
           since that text matches neither field's real values.

        2. tax_treatment itself is a closed enum of short snake_case codes,
           not the human-readable UI label. Zoho's own public API docs (for
           Invoices, which share the same GCC VAT tax_treatment concept)
           list: vat_registered, vat_not_registered, gcc_vat_registered,
           gcc_vat_not_registered, non_gcc, dz_vat_registered,
           dz_vat_not_registered. Ravindra's screenshot additionally shows
           "Out Of Scope" as a selectable option on the EXPENSE form
           specifically (not listed in the generic Invoices doc page, which
           makes sense -- Invoices/Bills' tax_treatment describes a
           contact's VAT-registration status, which can't be "out of
           scope", while an Expense's tax_treatment describes the
           TRANSACTION itself, which can). Sent here: "vat_registered" for
           the VAT-matched "Bank Charges - VAT" entry, "out_of_scope" for
           the no-VAT-match "Bank Charges Non-VAT" entry -- both are
           main.py's `_TAX_TREATMENT_VAT_MATCHED`/`_TAX_TREATMENT_NON_VAT`
           constants. STILL UNVERIFIED LIVE: the exact snake_case spelling
           of "out_of_scope" is inferred (by the same lowercase+underscore
           pattern every other confirmed value follows, e.g. "Non GCC" ->
           non_gcc) from the UI label, not confirmed against a raw curl
           call -- if Zoho rejects it again with the same error, that's the
           next thing to check.

        tax_id: the specific tax RATE to apply (Zoho Books' own concept of
        a configured Settings -> Taxes rate, e.g. "Standard Rate [5%]"),
        sent ONLY on the VAT-matched entry -- resolved dynamically at
        posting time via find_tax_id() below rather than hardcoded, since a
        tax_id is an opaque per-organization GUID this codebase has no way
        to know in advance. Left unset (None) on the Non-VAT/out_of_scope
        entry on the theory that "a transaction on which VAT charges are
        not applicable" (Zoho's own description of Out Of Scope) doesn't
        need a rate -- UNVERIFIED, worth confirming live; if Zoho's API
        actually requires some tax_id even for an Out Of Scope expense, the
        next fix is resolving an "Exempt" rate the same way for that case.

        organization_id: see create_journal_entry()'s docstring -- same
        override mechanism, same "Post widget picks its own org" reason."""
        body = {"account_id": account_id, "paid_through_account_id": paid_through_account_id,
                 "date": date, "amount": amount}
        if reference_number:
            body["reference_number"] = reference_number
        if description:
            body["description"] = description
        if tax_treatment:
            body["tax_treatment"] = tax_treatment
        if tax_id:
            body["tax_id"] = tax_id
        return self._request("POST", "/expenses", organization_id=organization_id, json=body)

    def create_itemized_expense(self, paid_through_account_id: str, date: str, line_items: list,
                                 reference_number: str = None, organization_id: str = None) -> dict:
        """Creates a SINGLE Expense with multiple line items via Zoho's
        "itemized" mode (POST /expenses, is_itemized_expense=true) -- 2026-
        08-13, for the Egypt entity paired salary+bank-charge feature (see
        categorize_from_module.py's ITEMIZED_WITH_COLUMN_NAME and
        posting_service.py's post_itemized_expense_group()). Ravindra's
        screenshots of Zoho's own "Itemize" toggle on the Expense entry form
        confirmed this UI exists and posts one Expense record containing
        several {account, amount, description} rows instead of one flat
        account_id/amount pair -- but the exact JSON shape this maps to on
        the API side is NOT confirmed against Zoho's own API docs (that page
        is JS-rendered and wasn't fetchable in this environment) or a real
        curl call, so this is UNVERIFIED LIVE, same as create_expense()'s own
        paid_through_account_id gap and the tax_treatment values below it --
        expect to correct the body shape against the first real 400/200
        response.

        line_items: list of {"account_id": str, "amount": float,
        "description": str (optional, e.g. this line's own narration)} --
        one per group member (the primary's own regular category line, plus
        one "Bank charges - <SUFFIX>" line per charge member in the group --
        almost always exactly 2 for this feature, but not assumed to be
        capped at 2 here). paid_through_account_id / date / reference_number
        apply to the WHOLE Expense record (Zoho itemized expenses share one
        paid-through account, one date, one reference across all lines --
        there's no per-line equivalent of those three fields in the UI
        screenshots), matching post_itemized_expense_group()'s "resolve
        once per group" design. Best-guess body shape sent here:
        is_itemized_expense=true PLUS a line_items array (each entry using
        account_id/amount/description, the same field names create_expense()
        uses at the top level) -- top-level account_id/amount are
        deliberately OMITTED (Zoho's non-itemized create_expense() requires
        them; the itemized mode, per the UI, replaces that single
        account/amount pair with the line_items array instead, so sending
        both would likely be redundant or conflicting, not required). If
        Zoho instead expects a different array key (e.g. "line_items" vs
        "expense_line_items") or still wants top-level account_id/amount
        duplicated from the first/total line, that's the first thing to
        check against the live error."""
        if not line_items:
            raise ValueError("create_itemized_expense() needs at least one line item.")
        body = {
            "paid_through_account_id": paid_through_account_id,
            "date": date,
            "is_itemized_expense": True,
            "line_items": [
                {k: v for k, v in {
                    "account_id": li.get("account_id"),
                    "amount": li.get("amount"),
                    "description": li.get("description"),
                }.items() if v is not None}
                for li in line_items
            ],
        }
        if reference_number:
            body["reference_number"] = reference_number
        return self._request("POST", "/expenses", organization_id=organization_id, json=body)

    def get_expense(self, expense_id: str, organization_id: str = None) -> dict:
        """Fetches ONE Expense's full detail (GET /expenses/{expense_id}),
        unwrapped to just the inner "expense" object. See get_journal()'s
        docstring for why this exists -- same 2026-08-11 fix, same theory
        (list/search endpoints likely omit "description", only a real
        detail GET reliably returns it), same "not directly confirmed, only
        inferred from Ravindra's reported symptom" caveat. Used by
        posting_service.py's _check_duplicate_description()."""
        data = self._request("GET", f"/expenses/{expense_id}", organization_id=organization_id)
        return data.get("expense") or {}

    def attach_receipt_to_expense(self, expense_id: str, file_path: str, file_name: str = None,
                                   content_type: str = "application/pdf", organization_id: str = None) -> dict:
        """Attaches a local file as the Receipt on an already-posted Expense
        (POST /expenses/{expense_id}/receipt, multipart/form-data) -- added
        2026-08-12, later same day, for the Supporting Document Attachment
        feature ("Attach the document from onedrive and attach to the
        post"). Reads `file_path` off local disk (main.py's caller
        downloads the OneDrive invoice to a temp file first, same pattern
        as every other OneDrive-file-then-Zoho-call flow in this project)
        and uploads it as multipart form field "receipt".

        NOTE (2026-08-12, still later): this feature originally also
        included an update_expense() method (PUT /expenses/{id}) to correct
        an Expense's amount/tax fields once a matched invoice showed VAT
        added on top -- that whole validation/correction path (Gemini PDF
        extraction, amount matching, VAT strip-and-update) was explicitly
        discarded by Ravindra the same day ("i want to park the document
        extraction and validation aside and want only to attach the
        required document to the expense entry ... discard all the
        previous changes made for validation and gemini pdf doc reading"),
        so update_expense() was removed -- this attach method is the only
        piece of that original design still in use.

        UNVERIFIED LIVE (flagged like every other endpoint in this project
        that couldn't be confirmed against Zoho's real interactive API
        docs from this environment): the endpoint path (/expenses/
        {expense_id}/receipt) and the multipart field name ("receipt") are
        Zoho Books' long-standing, publicly documented "Attach Receipt to
        an expense" endpoint as best recalled/researched -- NOT confirmed
        against a live curl call or a fully-rendered docs page the way
        create_expense()'s tax fields eventually were (that page's detailed
        endpoint content wasn't reachable from this environment, only its
        navigation menu). If Zoho rejects this call, the two most likely
        fixes, in order: (1) the field name isn't "receipt" -- try
        "attachment" instead (Zoho Books uses "attachment" as the generic
        multipart field name on some other modules' file-upload endpoints);
        (2) the path uses a different verb/segment than "/receipt". Confirm
        with one real live call and update this docstring once known,
        same "verify, don't just trust the guess" discipline as every
        other UNVERIFIED LIVE item in this project.

        No `json=`/no explicit Content-Type header is set here -- passing
        `files=` to `requests.request()` (via _request()'s **kwargs
        passthrough) makes the `requests` library set the correct
        multipart/form-data Content-Type with boundary automatically;
        _headers() only ever sets Authorization, so there's no conflicting
        header to override."""
        name = file_name or os.path.basename(file_path)
        with open(file_path, "rb") as f:
            return self._request(
                "POST", f"/expenses/{expense_id}/receipt", organization_id=organization_id,
                files={"receipt": (name, f, content_type)},
            )

    def list_tax_rates(self, organization_id: str = None) -> list:
        """Lists this org's configured tax rates (GET /settings/taxes) --
        e.g. the "Exempt" / "Standard Rate [5%]" / "Zero Rate [0%]" entries
        Ravindra's "Tax" dropdown screenshot showed, each with its own
        opaque tax_id. Added 2026-08-11 to resolve a real tax_id for the
        Bank Charges/VAT consolidation feature's VAT-matched Expense entry
        dynamically at posting time, instead of hardcoding a guessed GUID
        that would only be valid for one specific organization_id (and
        would silently break if Ravindra ever renames/re-configures a rate).

        UNVERIFIED LIVE: the exact response shape
        (`{"taxes": [{"tax_id": ..., "tax_name": ..., "tax_percentage": ...},
        ...]}`) is Zoho Books' well-established, long-stable public Settings
        API shape, not something guessed for this project -- but it hasn't
        been confirmed against a real curl call for this specific org.
        Returns the raw list of tax dicts as Zoho returns them (defensively
        `[]` if the "taxes" key is missing, same empty-safe pattern as every
        other list_*/find_* method here)."""
        data = self._request("GET", "/settings/taxes", organization_id=organization_id)
        return data.get("taxes") or []

    def find_tax_id(self, name_contains: str, percentage: float = None, organization_id: str = None) -> str:
        """Resolves a tax_id by matching list_tax_rates() entries whose
        tax_name contains `name_contains` (case-insensitive), optionally
        also requiring tax_percentage == `percentage` (when given) to
        disambiguate e.g. a "Standard Rate" that might exist at more than
        one percentage in an org's history. Added 2026-08-11 for the Bank
        Charges/VAT consolidation feature's VAT-matched entry -- see
        create_expense()'s tax_id docstring.

        Raises ValueError (never returns a guessed/partial match) if zero or
        more than one rate matches -- an ambiguous or missing match here
        would silently post the WRONG tax rate on a real Zoho Expense if it
        just picked one, which is worse than failing loudly and letting the
        caller surface a clear "NOT POSTED" reason instead."""
        rates = self.list_tax_rates(organization_id=organization_id)
        needle = name_contains.strip().lower()
        matches = [
            r for r in rates
            if needle in str(r.get("tax_name") or "").strip().lower()
            and (percentage is None or float(r.get("tax_percentage") or 0) == float(percentage))
        ]
        if not matches:
            raise ValueError(
                f"No tax rate found matching name_contains={name_contains!r}"
                f"{'' if percentage is None else f', percentage={percentage!r}'} -- "
                f"this org's configured rates are: "
                f"{[(r.get('tax_name'), r.get('tax_percentage')) for r in rates]!r}"
            )
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous tax rate match for name_contains={name_contains!r}"
                f"{'' if percentage is None else f', percentage={percentage!r}'} -- "
                f"{len(matches)} rates matched: "
                f"{[(r.get('tax_name'), r.get('tax_percentage'), r.get('tax_id')) for r in matches]!r}"
            )
        return matches[0]["tax_id"]

    def create_bank_transfer(self, from_account_id: str, to_account_id: str, amount: float, date: str,
                              reference_number: str = None, description: str = None,
                              organization_id: str = None) -> dict:
        """Creates a Bank Transfer (POST /banktransactions, transaction_type=
        "transfer_fund") -- this is what Zoho's own "Transfer to Another
        Account" UI screen calls under the hood, and is a STRUCTURALLY
        DIFFERENT Zoho Books object from both a Journal Entry (POST /journals)
        and an Expense (POST /expenses): it's a pure balance movement between
        two of the org's own accounts, shows up under Banking > Transfers
        (not Expenses, not the Journal list), and per Zoho's own docs is only
        valid when BOTH from_account_id and to_account_id are Bank/Card-type
        accounts.

        Added 2026-08-09, per Ravindra -- who caught that this project's
        rebuilt posting feature (this same session) was routing "Transfer to
        another account"/"Transfer from another account" through
        create_journal_entry() instead, based on a now-disproven 2026-07-26
        assumption that no dedicated transfer endpoint existed. This same
        method (same endpoint/payload shape) previously worked correctly for
        Expense reimbursement in an earlier, separate phase of this project
        -- confirmed by Ravindra directly ("this worked well for me") -- for
        the same underlying reason: whichever category posts through this
        method must have its OTHER-side account set up as Bank/Card type in
        Zoho, not as a P&L expense account (that earlier build's
        EMPLOYEE_REIMBURSEMENT_ACCOUNT was itself a Bank/Card-type clearing
        account, which is why bank-transfer -- not create_expense() -- was
        the correct call for it, despite the category being named "Expense
        reimbursement").

        Money moves FROM from_account_id TO to_account_id. posting_service.py
        derives which is which from the row's already-resolved Debit
        Account/Credit Account: the CREDIT side (the account being decreased)
        is from_account_id, the DEBIT side (being increased) is
        to_account_id -- this holds regardless of whether the category is
        labeled "Transfer to another account" or "Transfer from another
        account", since both are just this account statement's own
        debit/credit direction relative to the transfer.

        organization_id: see create_journal_entry()'s docstring -- same
        override mechanism, same "Post widget picks its own org" reason.

        Response shape (banktransaction_id under a "banktransaction" key) is
        assumed from Zoho's usual "singular resource name wraps the created
        object" convention (matching "journal"/"expense"/"payment" on the
        other create_* methods) -- NOT yet confirmed via a real curl call
        against this org, flagged the same way as create_expense()'s
        paid_through_account_id gap above. If Zoho actually wraps this under
        a different key, posting_service.py's _post_bank_transfer() will
        raise a clear "no banktransaction_id found -- raw response: ..."
        error rather than silently posting successfully with no usable ID
        for delete_bank_transaction() to reference later -- that raw
        response is exactly what to paste back to fix the key name here.

        description (2026-08-11, per Ravindra: "For all the posting types i
        want the description/notes filed to be filled with the narration
        text from the statement"): UNVERIFIED LIVE, more so than any other
        field on this method -- a Bank Transfer is documented as a pure
        balance movement (see this docstring's own "structurally different"
        note above), so it's genuinely unclear whether Zoho's
        /banktransactions endpoint even has a free-text field to store this
        in at all, let alone one named "description". Sent as a plain
        optional body key regardless (same "doesn't fail the call if
        rejected/ignored" reasoning as create_expense()'s tax_treatment) --
        if Zoho drops it silently, the transfer still posts correctly, just
        without the narration attached; worth Ravindra confirming live
        whether it actually shows up anywhere in the Zoho UI for a transfer."""
        body = {
            "from_account_id": from_account_id,
            "to_account_id": to_account_id,
            "transaction_type": "transfer_fund",
            "amount": amount,
            "date": date,
        }
        if reference_number:
            body["reference_number"] = reference_number
        if description:
            body["description"] = description
        return self._request("POST", "/banktransactions", organization_id=organization_id, json=body)

    def get_bank_transaction(self, banktransaction_id: str, organization_id: str = None) -> dict:
        """Fetches ONE Bank Transfer's full detail (GET
        /banktransactions/{banktransaction_id}), unwrapped to just the inner
        "banktransaction" object (that wrapper key IS confirmed live, unlike
        this field's own "description" -- see create_bank_transfer()'s
        docstring). See get_journal()'s docstring for the general "why a
        full detail GET" reasoning. Used by posting_service.py's
        _check_duplicate_description()."""
        data = self._request("GET", f"/banktransactions/{banktransaction_id}", organization_id=organization_id)
        return data.get("banktransaction") or {}

    def create_vendor_payment(self, vendor_id: str, amount: float, paid_through_account_id: str,
                               date: str = None, reference_number: str = None, description: str = None,
                               organization_id: str = None) -> dict:
        """Creates a Vendor Payment (POST /vendorpayments) WITHOUT a `bills`
        array -- an on-account/unapplied payment, confirmed via Zoho's own
        docs to be valid (a bill_id is not mandatory). Deliberately not
        matching this to a specific open Bill -- this pipeline has no
        bill-matching logic (which open bill this payment settles), same gap
        the original Python pipeline explicitly deferred for vendor/customer
        payments generally ("we will see that later"). The payment still
        posts and reduces the vendor's outstanding balance in aggregate;
        applying it to a specific bill can be done later, by hand, in the
        Zoho Books UI.

        description (2026-08-11, per Ravindra's "description/notes ...
        narration text" request): Zoho's documented field name for a Vendor
        Payment's own free-text field.

        organization_id: see create_journal_entry()'s docstring -- same
        override mechanism, same "Post widget picks its own org" reason."""
        body = {"vendor_id": vendor_id, "amount": amount, "paid_through_account_id": paid_through_account_id}
        if date:
            body["date"] = date
        if reference_number:
            body["reference_number"] = reference_number
        if description:
            body["description"] = description
        return self._request("POST", "/vendorpayments", organization_id=organization_id, json=body)

    def get_vendor_payment(self, payment_id: str, organization_id: str = None) -> dict:
        """Fetches ONE Vendor Payment's full detail (GET
        /vendorpayments/{payment_id}). Unwrap key is uncertain -- mirrors
        _post_vendor_payment()'s own existing "payment" then "vendorpayment"
        fallback (that same ambiguity was never resolved for the CREATE
        response either; see post_row()'s handling), tried in the same
        order here. See get_journal()'s docstring for the general "why a
        full detail GET" reasoning. Used by posting_service.py's
        _check_duplicate_description()."""
        data = self._request("GET", f"/vendorpayments/{payment_id}", organization_id=organization_id)
        return data.get("payment") or data.get("vendorpayment") or {}

    def _search_by_reference_number(self, path: str, list_key: str, reference_number: str, organization_id=None) -> list:
        """Generic pre-post duplicate-check helper. Added 2026-08-09, same
        day as the Transfer-type/transaction_id fixes above -- a real live
        duplicate got posted to Zoho because the ONLY thing that had ever
        stopped a re-post was this pipeline's own local "Zoho Posting
        Reference" Excel cell, which had just been proven capable of being
        wrong/stale (the exact transaction_id-key bug earlier the same day:
        a row's cell said "ERROR: ..." even though Zoho already genuinely
        had the entry, so the very next posting run created it again).
        This queries Zoho itself, live, on every single post attempt,
        instead of trusting a local cell that can drift out of sync with
        reality -- see posting_service.py's _check_for_duplicate() for how
        the result is used (the amount is cross-checked too, client-side,
        not just this filter -- see that function's docstring for why).

        Returns [] if reference_number is blank (nothing to search for).
        Never raises for a "no matches" result -- only for a real API/
        network failure, which posting_service.py's caller catches the
        same way it already catches a vendor-contacts-fetch failure (log
        and proceed without the check, rather than blocking every post on
        one flaky call)."""
        if not reference_number:
            return []
        data = self._request("GET", path, organization_id=organization_id,
                              params={"reference_number": reference_number})
        return data.get(list_key) or []

    def find_journals_by_reference(self, reference_number, organization_id=None) -> list:
        """Pre-post duplicate check for Journal Entries (GET /journals?reference_number=...).
        See _search_by_reference_number()'s docstring."""
        return self._search_by_reference_number("/journals", "journals", reference_number, organization_id)

    def find_expenses_by_reference(self, reference_number, organization_id=None) -> list:
        """Pre-post duplicate check for Expenses (GET /expenses?reference_number=...).
        See _search_by_reference_number()'s docstring."""
        return self._search_by_reference_number("/expenses", "expenses", reference_number, organization_id)

    def find_vendor_payments_by_reference(self, reference_number, organization_id=None) -> list:
        """Pre-post duplicate check for Vendor Payments (GET /vendorpayments?reference_number=...).
        See _search_by_reference_number()'s docstring."""
        return self._search_by_reference_number("/vendorpayments", "vendorpayments", reference_number, organization_id)

    def find_bank_transactions_by_reference(self, reference_number, organization_id=None) -> list:
        """Pre-post duplicate check for Bank Transfers (GET /banktransactions?reference_number=...).
        UNVERIFIED LIVE, same flag as create_bank_transfer(): assumed this
        endpoint accepts the same reference_number query filter every other
        list endpoint here does. If it doesn't, Zoho most likely just
        ignores the unrecognized param and returns its normal unfiltered
        list -- posting_service.py's client-side amount+reference_number
        cross-check (_find_duplicate_record()) still protects against a
        false-positive duplicate flag in that case, it just does more of
        the filtering work locally instead of the server doing it. Worth
        confirming with a direct curl call per DEPLOY.md; if wrong, this is
        a one-line fix (a different query param name)."""
        return self._search_by_reference_number("/banktransactions", "banktransactions", reference_number, organization_id)

    def _search_by_date(self, path: str, list_key: str, date_str: str, organization_id=None) -> list:
        """Content-based (notes/description) duplicate-check helper -- 2026-08-11,
        per Ravindra: "i want the content in description/notes field to be
        checked for duplicate posting." reference_number alone (the checks
        above) isn't always usable for this -- many rows share a blank or
        non-unique reference_number, which is exactly why a second,
        independent check keyed on the row's own narration text is needed.
        Since Zoho has no "search by free-text field" query param, this
        narrows candidates to same-day records first (date_start/date_end,
        the same param shape Zoho documents for its own Reports filters),
        and posting_service.py's _check_duplicate_description() does the
        actual text comparison client-side, on each candidate's FULL record
        (fetched via the matching get_*() method above) -- NOT on whatever
        this list call itself returns, since that's the whole point of
        those get_*() methods existing (see get_journal()'s docstring).

        UNVERIFIED LIVE: assumes date_start/date_end filters these list
        endpoints the same way as every other Zoho Books list/report
        endpoint that documents them. If Zoho ignores these params instead,
        the result is simply a broader "every record" list handed to
        _check_duplicate_description() -- correctness doesn't depend on this
        filter actually narrowing anything (the full-record text comparison
        catches a real duplicate either way), it would just mean more
        candidate GET calls per post than strictly necessary on a busy day.

        Returns [] if date_str is blank. Never raises for a "no matches"
        result -- only for a real API/network failure, caught the same
        tolerant way as _search_by_reference_number()."""
        if not date_str:
            return []
        data = self._request("GET", path, organization_id=organization_id,
                              params={"date_start": date_str, "date_end": date_str})
        return data.get(list_key) or []

    def find_journals_by_date(self, date_str, organization_id=None) -> list:
        """Content-based duplicate check candidates for Journal Entries
        (GET /journals?date_start=...&date_end=...). See _search_by_date()'s
        docstring -- each candidate's real "notes" text is fetched separately
        via get_journal(), not read off this list response."""
        return self._search_by_date("/journals", "journals", date_str, organization_id)

    def find_expenses_by_date(self, date_str, organization_id=None) -> list:
        """Content-based duplicate check candidates for Expenses (GET
        /expenses?date_start=...&date_end=...). See _search_by_date()'s
        docstring -- each candidate's real "description" text is fetched
        separately via get_expense()."""
        return self._search_by_date("/expenses", "expenses", date_str, organization_id)

    def find_vendor_payments_by_date(self, date_str, organization_id=None) -> list:
        """Content-based duplicate check candidates for Vendor Payments (GET
        /vendorpayments?date_start=...&date_end=...). See _search_by_date()'s
        docstring -- each candidate's real "description" text is fetched
        separately via get_vendor_payment()."""
        return self._search_by_date("/vendorpayments", "vendorpayments", date_str, organization_id)

    def find_bank_transactions_by_date(self, date_str, organization_id=None) -> list:
        """Content-based duplicate check candidates for Bank Transfers (GET
        /banktransactions?date_start=...&date_end=...). See
        _search_by_date()'s docstring -- each candidate's real "description"
        text (itself unverified to even exist -- see create_bank_transfer())
        is fetched separately via get_bank_transaction()."""
        return self._search_by_date("/banktransactions", "banktransactions", date_str, organization_id)

    def delete_journal_entry(self, journal_id: str, organization_id: str = None) -> dict:
        """Deletes a Journal Entry (DELETE /journals/{journal_id}) -- the
        Zoho Books v3 REST convention (delete-by-id at the same path a create
        POSTs to) confirmed against Zoho's own API docs for this module.
        Added 2026-08-09, per Ravindra's "post to Zoho Books" feature's
        Delete button ("Delete in Zoho + clear reference" -- the widget's own
        Delete button must ACTUALLY remove the entry from Zoho Books, not
        just clear the statement's own reference cell -- see main.py's
        /delete-postings and posting_service.py's delete_posting())."""
        return self._request("DELETE", f"/journals/{journal_id}", organization_id=organization_id)

    def delete_expense(self, expense_id: str, organization_id: str = None) -> dict:
        """Deletes an Expense (DELETE /expenses/{expense_id}) -- see
        delete_journal_entry()'s docstring for why/when this is called."""
        return self._request("DELETE", f"/expenses/{expense_id}", organization_id=organization_id)

    def delete_vendor_payment(self, payment_id: str, organization_id: str = None) -> dict:
        """Deletes a Vendor Payment (DELETE /vendorpayments/{payment_id}) --
        see delete_journal_entry()'s docstring for why/when this is called."""
        return self._request("DELETE", f"/vendorpayments/{payment_id}", organization_id=organization_id)

    def delete_bank_transaction(self, banktransaction_id: str, organization_id: str = None) -> dict:
        """Deletes a Bank Transfer (DELETE /banktransactions/{banktransaction_id})
        -- added 2026-08-09 alongside create_bank_transfer() above, same
        delete-at-the-create-path REST convention as the other three delete_*
        methods, and same "Delete in Zoho + clear reference" reason."""
        return self._request("DELETE", f"/banktransactions/{banktransaction_id}", organization_id=organization_id)

    def list_bank_accounts(self, per_page: int = 200, organization_id: str = None) -> list:
        """Returns every bank/credit-card account in the org, via Zoho's
        native Banking module (GET /bankaccounts) -- confirmed from Zoho's own
        API docs to include both account_id (the real chart-of-account unique
        ID) and account_number (the account's own number) per entry, which is
        exactly the {account_number -> account_id} mapping the widget needs
        to resolve a statement's own account to a real Zoho account_id (2026-
        07-25, per Ravindra -- see categorize_from_module.py's docstring for
        why account_number itself can't be written into Debit Account/Credit
        Account). Done dynamically here rather than via a hand-maintained
        list, since an org can have hundreds of accounts and Ravindra wants
        this to keep working without editing a lookup table every time an
        account is added.

        organization_id (2026-08-01, per Ravindra): pass a DIFFERENT org's ID
        to list THAT org's bank accounts instead of this client's own default
        -- confirmed live that a single Self Client login can see more than
        one Zoho Books organization (GET /organizations), which is what lets
        the widget's org-picker popup show real accounts for whichever org
        was selected, not just the org this deployment's credentials default
        to. Omit (None) to keep using self.config.organization_id, unchanged
        from before this parameter existed.

        Paginated via 'page_context.has_more_page' (same shape
        list_bank_transactions() in the original pipeline's zoho_client.py
        already pages through) since the whole point of doing this
        dynamically is to not silently stop at Zoho's default page size once
        an org has enough accounts to need more than one page. Not cached --
        called once per widget run (not once per row), so the extra request
        cost is negligible next to the OneDrive download/categorize/upload
        work already happening per account."""
        accounts = []
        page = 1
        while True:
            data = self._request(
                "GET", "/bankaccounts", organization_id=organization_id,
                params={"page": page, "per_page": per_page},
            )
            page_accounts = data.get("bankaccounts")
            if page_accounts is None:
                raise ValueError(
                    f"Unexpected response shape from GET /bankaccounts -- no list found under "
                    f"'bankaccounts'. Raw response: {data}"
                )
            accounts.extend(page_accounts)
            page_context = data.get("page_context") or {}
            if not page_context.get("has_more_page"):
                break
            page += 1
        return accounts

    def get_bank_account_balance(self, account_id: str, organization_id: str = None) -> dict:
        """Fetches ONE bank account's detail (GET /bankaccounts/{account_id})
        and returns the unwrapped "bankaccount" object -- added 2026-08-13
        for the month-end Reconciliation feature's "does the statement's
        closing balance match the books" check.

        UNVERIFIED LIVE (flagged like every other endpoint in this project
        whose exact response shape couldn't be confirmed against Zoho's real
        interactive API docs from this environment -- that page's detailed
        endpoint content wasn't reachable, only its navigation menu, which
        DID confirm a "Get account details"/"Get a bank account balance"
        endpoint exists under this path, just not the exact field name the
        balance comes back under). Tries, in order, "balance",
        "current_balance", "closing_balance" (all plausible given Zoho's own
        UI language -- the sample statement this feature was built against
        literally has a "Current Balance:" field) -- whichever key is
        present and not None wins. If Zoho uses a different key entirely,
        this raises a clear error with the RAW response body, same "paste
        this back to fix it" discipline as every other unverified live call
        in this codebase, rather than silently returning a wrong/blank
        balance for a financial reconciliation check.

        IMPORTANT CAVEAT this method can't fix by itself: this is Zoho's
        CURRENT/real-time balance, not a point-in-time historical one --
        Zoho Books' REST API has no documented "balance as of date X"
        parameter here. Reconciliation is only meaningful against this value
        for the most recently completed month, with nothing posted to this
        account in Zoho after the statement's own end date -- the caller
        (reconciliation_service.py) surfaces this caveat directly in the
        result, it's not hidden in a code comment only Ravindra would see."""
        data = self._request("GET", f"/bankaccounts/{account_id}", organization_id=organization_id)
        account = data.get("bankaccount") or {}
        for key in ("balance", "current_balance", "closing_balance"):
            if account.get(key) is not None:
                return {"balance": account[key], "balance_field_used": key, "raw": account}
        raise ValueError(
            f"Unexpected response shape from GET /bankaccounts/{account_id} -- no balance-like field found "
            f"under 'balance'/'current_balance'/'closing_balance' on the returned bankaccount object. "
            f"Raw bankaccount object: {account}"
        )

    def find_bank_account_transactions(self, account_id: str, date_start: str, date_end: str,
                                        organization_id: str = None, per_page: int = 200) -> list:
        """Lists every Zoho "Bank Transaction" feed entry for ONE account
        within a date range (GET /banktransactions?account_id=...&date_
        start=...&date_end=...), paginated the same way list_bank_accounts()
        already is -- added 2026-08-13 for the Reconciliation feature's
        "aggregate credits/debits match" check.

        NO LONGER used for "every statement row posted" (2026-08-13, later
        same day, per Ravindra's narration-first redesign -- see
        reconciliation_service.py's match_statement_rows_to_zoho()): that
        check now searches Zoho directly via posting_service.py's existing
        by-date/by-type duplicate-check infrastructure instead of this
        account-scoped feed, so a real narration-matching entry is found
        even if this method's own account_id/response-shape guess turns out
        to be wrong.

        UNVERIFIED LIVE, and more so than most other flags in this codebase
        -- this reuses the SAME REST path (/banktransactions) already
        confirmed live for create_bank_transfer()/find_bank_transactions_by_
        date() (wrapped under a "banktransactions" list key, each record's
        real detail fetched via get_bank_transaction() elsewhere in this
        file), but those existing call sites only ever created/searched ONE
        specific Zoho transaction TYPE (a fund transfer between two of this
        org's own accounts). Zoho Books' Banking module UI shows a much
        broader feed for a given account -- every Expense paid from it,
        every Invoice payment/Customer receipt deposited to it, every
        Vendor Payment, every transfer, and any bank-feed-imported line --
        and it's NOT confirmed from this environment whether GET
        /banktransactions with an account_id filter actually returns that
        SAME broad feed, or only the narrower "Bank Transfer" object type
        those other call sites use. Zoho's own docs page did confirm a "Get
        matching transactions" operation exists under this same Bank
        Transactions API section, which is consistent with this being the
        broader reconciliation-feed concept -- but that's inference, not a
        confirmed response body.

        account_id is sent as a query param (not embedded in the path,
        unlike get_bank_account_balance() above) -- the best-guess filter
        name for "just this account's transactions", consistent with how
        every other list endpoint in this file takes its scoping value as a
        query param. date_start/date_end reuse the exact param names
        _search_by_date() already sends successfully against this same
        /banktransactions path elsewhere in this file.

        Returns the RAW list of transaction dicts, unprocessed -- see
        reconciliation_service.py's classify_bank_transaction_amount() for
        where the per-record amount/direction field-name guessing happens
        (kept out of this client so a shape correction only needs editing in
        one place, not here too)."""
        transactions = []
        page = 1
        while True:
            data = self._request(
                "GET", "/banktransactions", organization_id=organization_id,
                params={"account_id": account_id, "date_start": date_start, "date_end": date_end,
                        "page": page, "per_page": per_page},
            )
            page_transactions = data.get("banktransactions")
            if page_transactions is None:
                raise ValueError(
                    f"Unexpected response shape from GET /banktransactions?account_id={account_id} -- no list "
                    f"found under 'banktransactions'. Raw response: {data}"
                )
            transactions.extend(page_transactions)
            page_context = data.get("page_context") or {}
            if not page_context.get("has_more_page"):
                break
            page += 1
        return transactions

    def list_account_transactions(self, account_id: str, date_start: str = None, date_end: str = None,
                                    organization_id: str = None, per_page: int = 200) -> list:
        """Lists every GENERAL LEDGER entry (debit/credit line) posted to ONE
        Chart of Accounts account within a date range -- added 2026-08-14,
        per Ravindra: "can you actually pull the transactions for the
        specified time period using Journal records in Zoho where you get
        all the debit and credit entries for each of the transactions for
        the selected account." This is a fundamentally more reliable data
        source for reconciliation's aggregate check (2) than find_bank_
        account_transactions() above: that method returns each high-level
        Zoho OBJECT (an Expense, a Bank Transfer, a Vendor Payment, ...) with
        a single ambiguous "amount" this codebase then has to guess the
        direction of; THIS endpoint is Zoho's own General Ledger view of the
        account, where every entry -- regardless of which object type
        created it -- has already been resolved by Zoho itself into a debit
        or a credit against THIS SPECIFIC account. It's the same underlying
        data the "Deposits"/"Withdrawals" columns in Zoho's own Banking
        module UI are built from.

        UNVERIFIED LIVE, same discipline as every other guessed endpoint in
        this codebase -- but this specific endpoint's PATH (not its response
        shape) is more solidly grounded than most: Zoho's own public API
        reference (zoho.com/books/api/v3/chart-of-accounts/) lists a "List
        of transactions for an account" operation under Chart of Accounts,
        confirming this feature exists; that page is JS-rendered, so the
        exact query-param names and JSON response field names couldn't be
        scraped from this environment (only the operation's existence and
        rough path). Tries GET /chartofaccounts/{account_id}/transactions
        first (account_id in the path, matching the REST-sub-resource
        pattern Zoho's own docs page structure implies); on a 404
        specifically, falls back once to GET /chartofaccounts/transactions
        with account_id as a query param instead (the alternate shape a
        third-party API index summarized it as) -- covers both plausible
        interpretations of the same doc listing without guessing blind on
        just one. date_start/date_end reuse the same param names already
        confirmed live against /banktransactions elsewhere in this file
        (Zoho's own convention across list endpoints).

        Returns the RAW list of transaction/ledger-entry dicts, unprocessed
        -- see reconciliation_service.py's classify_account_transaction_
        amount() for where the per-record field-name guessing happens (same
        "keep the guessing in one place" reasoning as find_bank_account_
        transactions() above). Raises a clear error with the raw response
        body if NEITHER path returns a recognizable list -- exactly what to
        paste back to fix the path/key name here once a real org is
        available to test against."""
        def _fetch(path, params):
            transactions = []
            page = 1
            while True:
                page_params = dict(params)
                page_params["page"] = page
                page_params["per_page"] = per_page
                data = self._request("GET", path, organization_id=organization_id, params=page_params)
                page_list = None
                for key in ("chartofaccounts_transactions", "transactions", "chartofaccounts"):
                    value = data.get(key)
                    if isinstance(value, list):
                        page_list = value
                        break
                if page_list is None:
                    raise ValueError(
                        f"Unexpected response shape from GET {path} (account_id={account_id}) -- no list "
                        f"found under 'chartofaccounts_transactions'/'transactions'/'chartofaccounts'. "
                        f"Raw response: {data}"
                    )
                transactions.extend(page_list)
                page_context = data.get("page_context") or {}
                if not page_context.get("has_more_page"):
                    break
                page += 1
            return transactions

        try:
            return _fetch(f"/chartofaccounts/{account_id}/transactions",
                           {"date_start": date_start, "date_end": date_end})
        except requests.exceptions.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
            return _fetch("/chartofaccounts/transactions",
                           {"account_id": account_id, "date_start": date_start, "date_end": date_end})

    def list_organizations(self) -> list:
        """Returns every Zoho Books organization the authenticated LOGIN (not
        just this client's own configured organization_id) has access to, via
        GET /organizations -- confirmed live 2026-08-01 (Ravindra ran this
        directly) that a single Self Client refresh token generated under one
        org can see every org that same Zoho user is a member of: for Cyber
        Knight this returned BOTH "Cyber Knight Gulf LLC" (Qatar) and "Cyber
        Knight Technologies FZ-LLC" (UAE/CBK) from the one token already
        configured for this service. This is what backs the widget's live
        "which organization" picker -- no separate credential set needed per
        entity, at least for however many orgs this login is actually a
        member of (an org this login was never added to as a user simply
        won't appear here, no error -- that's a Zoho-side access grant to fix,
        not something this call can work around).

        No organization_id is sent on this request at all (organization_id=
        False) -- this call lists organizations, so scoping it to one up
        front wouldn't make sense, and Zoho's docs don't say what happens if
        you send one anyway."""
        data = self._request("GET", "/organizations", organization_id=False)
        orgs = data.get("organizations")
        if orgs is None:
            raise ValueError(
                f"Unexpected response shape from GET /organizations -- no list found under "
                f"'organizations'. Raw response: {data}"
            )
        return orgs