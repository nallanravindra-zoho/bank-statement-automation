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

    def list_contacts(self, contact_type: str = None, per_page: int = 200) -> list:
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
        list_bank_accounts()/resolveAccountIds() in widget.js."""
        contacts = []
        page = 1
        while True:
            data = self._request("GET", "/contacts", params={"page": page, "per_page": per_page})
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

    def create_journal_entry(self, journal_date: str, line_items: list, reference_number: str = None) -> dict:
        """Creates a Journal Entry (POST /journals) -- a plain double-entry
        posting, e.g. [{"account_id": "...", "debit_or_credit": "debit",
        "amount": 100.0}, {"account_id": "...", "debit_or_credit": "credit",
        "amount": 100.0}]. Used directly for the "Journal" posting type, AND
        reused for BOTH "Transfer to another account"/"Transfer from another
        account" (see posting_service.py's docstring for why a transfer is
        just a 2-line journal entry between the two accounts -- Zoho Books'
        v3 API has no dedicated "create a fund transfer between two bank
        accounts" endpoint; the Banking module's own Transfer Fund feature
        and the bank-feed "categorize as transfer" action both work on an
        already-IMPORTED bank transaction, not a fresh row from a statement
        we're posting ourselves)."""
        body = {"journal_date": journal_date, "line_items": line_items}
        if reference_number:
            body["reference_number"] = reference_number
        return self._request("POST", "/journals", json=body)

    def create_expense(self, account_id: str, paid_through_account_id: str, date: str, amount: float,
                        reference_number: str = None, description: str = None) -> dict:
        """Creates an Expense (POST /expenses). account_id is the expense/GL
        account being charged; paid_through_account_id is the bank/cash
        account it was paid from -- both required for a real expense entry
        (Zoho's own public docs page for this endpoint didn't list
        paid_through_account_id explicitly when checked 2026-07-26, which
        looked like a documentation gap rather than the field not existing --
        worth confirming with a real curl call per DEPLOY.md before relying
        on this in production, same as every other "unverified live" item in
        this project)."""
        body = {"account_id": account_id, "paid_through_account_id": paid_through_account_id,
                 "date": date, "amount": amount}
        if reference_number:
            body["reference_number"] = reference_number
        if description:
            body["description"] = description
        return self._request("POST", "/expenses", json=body)

    def create_vendor_payment(self, vendor_id: str, amount: float, paid_through_account_id: str,
                               date: str = None, reference_number: str = None) -> dict:
        """Creates a Vendor Payment (POST /vendorpayments) WITHOUT a `bills`
        array -- an on-account/unapplied payment, confirmed via Zoho's own
        docs to be valid (a bill_id is not mandatory). Deliberately not
        matching this to a specific open Bill -- this pipeline has no
        bill-matching logic (which open bill this payment settles), same gap
        the original Python pipeline explicitly deferred for vendor/customer
        payments generally ("we will see that later"). The payment still
        posts and reduces the vendor's outstanding balance in aggregate;
        applying it to a specific bill can be done later, by hand, in the
        Zoho Books UI."""
        body = {"vendor_id": vendor_id, "amount": amount, "paid_through_account_id": paid_through_account_id}
        if date:
            body["date"] = date
        if reference_number:
            body["reference_number"] = reference_number
        return self._request("POST", "/vendorpayments", json=body)

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