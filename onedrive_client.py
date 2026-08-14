"""
Watches a OneDrive/SharePoint folder for new bank statement files, via the
Microsoft Graph API. Plain `requests` calls -- no msal dependency.

This implements the "trigger" discussed in the design: a scheduled POLL
(call check_for_new_files() on a timer -- e.g. every hour via cron/Task
Scheduler/Azure Function timer trigger), not a push webhook. Polling is
simpler to run and debug; if you need near-real-time pickup later, Graph
also supports change-notification webhooks, which would replace only this
module -- nothing else in the pipeline changes.

Setup (one-time):
  1. Register an app in Azure AD (portal.azure.com > App registrations).
  2. Grant it the Microsoft Graph *application* permission Files.Read.All
     (or Sites.Read.All if the folder lives in a SharePoint document
     library rather than a personal OneDrive), then have an admin grant
     consent. Application permissions (not delegated) are required for a
     script that runs unattended with no signed-in user.
     -- If anything is going to call upload_file() below (currently only
     categorize_statement.py's --upload option), grant Files.ReadWrite.All
     (or Sites.ReadWrite.All) instead -- Read.All alone will 403 on upload.
  3. Create a client secret for the app.
  4. Put TENANT_ID, CLIENT_ID, CLIENT_SECRET, and the target DRIVE_ID +
     FOLDER_PATH in your .env (see .env.example).

State: `processed_files.json` (see processed_log.py) tracks which file IDs
have already been picked up, so re-running the poll never reprocesses the
same statement twice -- this is the "no duplicate posting on re-run"
requirement, applied at the file level (Zoho-side reference-number checks
in zoho_client.py cover it at the transaction level too, as a second layer).
"""
import os
import re
from dataclasses import dataclass
from typing import List, Optional

import requests

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


def _raise_with_graph_body(resp: requests.Response) -> None:
    """Like resp.raise_for_status(), but folds Graph's own JSON error body
    (error.code / error.message -- e.g. "accessDenied", "Access is denied due
    to a sensitivity label...") into the raised exception's message. Plain
    raise_for_status() only gives you "403 Client Error: Forbidden for url:
    ..." with no hint of WHY -- and content-download 403s in particular can
    have several distinct real causes (Files.Read.All not admin-consented as
    an Application permission specifically, a sensitivity-label/IRM-protected
    file blocking app-only content reads even though listing still works, a
    conditional-access/DLP policy scoped to downloads) that are only
    distinguishable by actually reading this body."""
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise requests.exceptions.HTTPError(f"{e} | Graph response body: {resp.text[:500]}", response=resp) from e


# ---------------------------------------------------------------------------
# "Don't reprocess an already-fully-posted file" marker convention
# (2026-08-10, per Ravindra: "I dont want to process the files i already
# processed/posted. what would be the best way to do it. Is it good to
# rename the file with .COMPLETED at the end and post only those files
# which does not end with it?"). Used together by main.py: /post-
# transactions calls completed_file_name() + OneDriveClient.rename_file()
# once every postable row in a file has a real Zoho posting reference (see
# that route's own comment for the exact eligibility rule), and /list-files
# calls is_marked_complete() to filter such files out of what it returns --
# so a completed file is skipped BEFORE it's ever downloaded again, not
# merely re-processed-and-silently-skipped at the row level the way this
# pipeline already handled duplicates before this update.
#
# Module-level (not on OneDriveClient) since this is a pure filename
# convention with no Graph call of its own -- both call sites (marking one
# file complete, filtering a whole listing) need the exact same rule, so
# it's defined once here rather than risking the two drifting apart.
# ---------------------------------------------------------------------------
_COMPLETED_TOKEN = "COMPLETED"


def completed_file_name(file_name: str) -> str:
    """Inserts the completion marker into file_name, right BEFORE its final
    extension -- "Statement.xlsx" -> "Statement.COMPLETED.xlsx" -- NOT
    appended after it ("Statement.xlsx.COMPLETED"). Appending after the real
    extension would stop Windows/OneDrive from recognizing the file as an
    Excel file at all (no more double-click-to-open-in-Excel, no preview);
    inserting before it keeps the file fully openable while still being a
    trivial, unambiguous substring for is_marked_complete() to filter on.

    A file_name with no "." at all (no extension -- not a real case for
    this pipeline, every bank statement is .xlsx/.xls, but handled rather
    than crashing) just gets the marker appended with a leading dot."""
    if "." not in file_name:
        return f"{file_name}.{_COMPLETED_TOKEN}"
    stem, ext = file_name.rsplit(".", 1)
    return f"{stem}.{_COMPLETED_TOKEN}.{ext}"


def is_marked_complete(file_name: str) -> bool:
    """True if file_name already carries the marker completed_file_name()
    inserts. Checked as a case-insensitive ".completed." substring (dot on
    BOTH sides, matching the "insert before the extension" convention above)
    -- OR a trailing ".completed" with nothing after it, covering the rare
    no-extension case completed_file_name() also handles, so that edge case
    can't get re-marked/double-suffixed on a later run
    ("Statement.COMPLETED.COMPLETED...") for lack of ever being recognized
    as already complete."""
    lower = file_name.lower()
    marker = f".{_COMPLETED_TOKEN.lower()}"
    return f"{marker}." in lower or lower.endswith(marker)


@dataclass
class OneDriveConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    drive_id: str
    # Legacy single-folder path -- still used as the default by
    # list_files_in_folder()/check_for_new_files() when no folder_path is
    # passed explicitly, so old single-bank callers keep working unchanged.
    # Multi-bank callers (main.py, once an automation config workbook is in
    # play) pass each bank's own folder_path per call instead and can leave
    # this at "" / MS_FOLDER_PATH unset. Also doubles as where the config
    # workbook itself lives, via MS_CONFIG_FOLDER_PATH below.
    folder_path: str = ""
    config_folder_path: str = ""   # OneDrive folder the automation config workbook lives in
    config_file_name: str = "Automation Config.xlsx"
    # OneDrive folder + file name for the Category/Keywords lookup workbook
    # used by categorize_statement.py -- a separate, simpler workbook from
    # the Automation Config above (see that script's docstring). Optional:
    # only categorize_statement.py --source onedrive reads these.
    categories_folder_path: str = ""
    categories_file_name: str = "Categories.xlsx"

    @classmethod
    def from_env(cls) -> "OneDriveConfig":
        missing = [
            k for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_DRIVE_ID")
            if not os.environ.get(k)
        ]
        if missing:
            raise EnvironmentError(f"Missing Microsoft Graph credentials in environment: {missing}. See .env.example.")
        return cls(
            tenant_id=os.environ["MS_TENANT_ID"],
            client_id=os.environ["MS_CLIENT_ID"],
            client_secret=os.environ["MS_CLIENT_SECRET"],
            drive_id=os.environ["MS_DRIVE_ID"],
            folder_path=os.environ.get("MS_FOLDER_PATH", ""),
            config_folder_path=os.environ.get("MS_CONFIG_FOLDER_PATH", ""),
            config_file_name=os.environ.get("MS_CONFIG_FILE_NAME", "Automation Config.xlsx"),
            categories_folder_path=os.environ.get("MS_CATEGORIES_FOLDER_PATH", ""),
            categories_file_name=os.environ.get("MS_CATEGORIES_FILE_NAME", "Categories.xlsx"),
        )


class OneDriveClient:
    def __init__(self, config: OneDriveConfig):
        self.config = config
        self._access_token = None

    def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        resp = requests.post(
            f"https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",  # app-only auth, no interactive login
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_access_token()}"}

    def list_files_in_folder(self, folder_path: Optional[str] = None) -> List[dict]:
        """Returns Graph driveItem objects for every file directly in the given
        folder (or self.config.folder_path if omitted -- the original
        single-bank/single-folder behavior). Pass a specific bank's
        onedrive_folder_path (see config_loader.BankProfile) for a
        multi-bank run.

        Follows Graph's `@odata.nextLink` until exhausted (2026-08-02, per a
        live report from Ravindra: two just-uploaded files in
        BankStatements/Dubai weren't being found by the widget even though
        the folder listing clearly succeeded and returned OTHER files fine).
        The single-page version of this method (no nextLink handling at all)
        would silently drop any file that fell on a second or later page --
        Graph's `:children` endpoint pages results once a folder has enough
        items (default page size varies, commonly around 200), and there was
        previously no code here to ask for the next page at all. Whether or
        not that's what actually happened in Ravindra's case (a Graph/
        SharePoint indexing-consistency lag right after upload is at least
        as likely, given the files in question were only ~3 minutes old --
        see the reply this was delivered with), a folder that grows past one
        page would hit this bug for real, every time, regardless of timing --
        so it's fixed either way rather than left as a latent scaling gap."""
        target = folder_path if folder_path is not None else self.config.folder_path
        if not target:
            raise ValueError("No folder_path given and OneDriveConfig.folder_path is empty -- pass folder_path "
                              "explicitly (e.g. from a BankProfile) or set MS_FOLDER_PATH for single-folder use.")
        path = target.strip("/")
        url = f"{GRAPH_BASE_URL}/drives/{self.config.drive_id}/root:/{path}:/children"
        items = []
        while url:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            _raise_with_graph_body(resp)
            data = resp.json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return [item for item in items if "file" in item]  # exclude subfolders

    def list_all_items(self, folder_path: str) -> List[dict]:
        """Like list_files_in_folder, but does NOT filter out subfolders --
        returns everything Graph sees directly inside folder_path, files and
        folders both, each tagged with an added '_is_folder' bool. Meant for
        diagnosing a folder path that isn't resolving (see
        categorize_statement.py --list-folder) -- list_files_in_folder alone
        would silently hide a subfolder you're trying to path into next,
        since the pipeline only ever wants statement files from it.

        Also follows @odata.nextLink now (2026-08-02), same reason and same
        fix as list_files_in_folder() above -- a diagnostic tool that itself
        silently truncated a large folder would be actively misleading."""
        path = folder_path.strip("/")
        url = f"{GRAPH_BASE_URL}/drives/{self.config.drive_id}/root:/{path}:/children" if path \
            else f"{GRAPH_BASE_URL}/drives/{self.config.drive_id}/root/children"
        items = []
        while url:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        for item in items:
            item["_is_folder"] = "folder" in item
        return items

    def find_file_by_name(self, folder_path: str, file_name: str) -> dict:
        """Returns the Graph driveItem for file_name inside folder_path (matched
        case-insensitively, so a trivial capitalization difference doesn't break
        a run). Raises FileNotFoundError, listing what WAS found, if it's missing --
        this is the common "download one specific named file" case (the config
        workbook, the categories workbook, one named statement), as opposed to
        check_for_new_files()'s "process whatever's new" polling case.

        file_name is normalized with os.path.basename() first -- if you
        accidentally pass the full path (folder included) instead of just
        the file name, this still resolves correctly rather than silently
        failing to match."""
        if "/" in file_name or "\\" in file_name:
            original = file_name
            file_name = os.path.basename(file_name.rstrip("/\\"))
            print(f"Note: --file_name looked like a full path ({original!r}) -- using just "
                  f"the file name {file_name!r} to match against {folder_path!r}.")
        items = self.list_files_in_folder(folder_path)
        target = file_name.strip().lower()
        match = next((item for item in items if item.get("name", "").strip().lower() == target), None)
        if not match:
            found = [item.get("name") for item in items]
            raise FileNotFoundError(f"{file_name!r} not found in OneDrive folder {folder_path!r}. Found: {found}.")
        return match

    def find_files_containing(self, folder_path: str, substring: str) -> List[dict]:
        """Returns every FILE (never a subfolder -- see list_files_in_folder(),
        which this reuses) directly inside folder_path whose name CONTAINS
        `substring`, matched case-insensitively AND with every non-
        alphanumeric character stripped from both sides first (so "INV-
        20260184.pdf" matches a search substring of "INV20260184", "INV
        20260184", or "inv-20260184" -- real invoice numbers get typed with
        or without hyphens/spaces inconsistently between a bank statement's
        own narration/reference text and however the actual invoice PDF got
        named/saved). Returns an empty list (never raises) if nothing
        matches or `substring` is blank -- "no candidate file found" is a
        normal, expected outcome for this search, not an error condition.

        Added 2026-08-12, later same day, for the Supporting Document
        Attachment feature (main.py's /attach-supporting-docs, renamed from
        /verify-supporting-docs when Ravindra scoped the feature down to
        attachment-only): the "find this row's invoice" search key is its
        reference_number, from categorize_from_module.py's
        extract_attachment_check_rows() -- SOURCE CHANGED 2026-08-13, now
        the statement's own raw reference/transaction-reference column
        rather than the resolved PostingReference column (see that
        function's own docstring) -- which is a free-text value this
        codebase doesn't otherwise constrain to look like a filename --
        this alnum-only, contains-based match is deliberately loose (a
        stricter exact-filename match would miss real invoices constantly)
        while still cheap and file-content-free (no need to open/OCR every
        file in the folder just to find the right one -- see main.py's
        own docstring for what happens if this returns more than one
        match: never guessed, always surfaced as ambiguous)."""
        target = re.sub(r"[^a-z0-9]", "", (substring or "").strip().lower())
        if not target:
            return []
        items = self.list_files_in_folder(folder_path)
        matches = []
        for item in items:
            name = item.get("name") or ""
            normalized_name = re.sub(r"[^a-z0-9]", "", name.lower())
            if target in normalized_name:
                matches.append(item)
        return matches

    def download_file(self, item: dict, dest_path: str) -> str:
        download_url = item.get("@microsoft.graph.downloadUrl")
        if not download_url:
            # driveItem from a list response sometimes omits this; fetch the item directly
            item_url = f"{GRAPH_BASE_URL}/drives/{self.config.drive_id}/items/{item['id']}"
            resp = requests.get(item_url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            download_url = resp.json()["@microsoft.graph.downloadUrl"]

        with requests.get(download_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return dest_path

    def download_file_by_path(self, folder_path: str, file_name: str, dest_path: str) -> str:
        """Downloads folder_path/file_name directly via Graph's path addressing --
        unlike find_file_by_name() + download_file(), this does NOT list the
        folder first. Use this whenever the caller already knows the exact
        file name (e.g. from one earlier list_files_in_folder() call, or a
        client-side account-number match against that list) and just wants
        the bytes, without paying for a repeated listing round-trip per file.
        Raises FileNotFoundError (not a generic HTTPError) on a 404, so
        callers can tell "no such file" apart from other request failures.

        Confirmed live on 2026-07-23: a tenant can return a generic 403
        accessDenied on this direct :/content call while :/children (folder
        listing) and the @microsoft.graph.downloadUrl obtained FROM a listing
        response both still work fine -- this matches how some SharePoint/
        OneDrive "unmanaged devices" access-control policies are configured
        (browse allowed, direct API download blocked, but the signed
        downloadUrl redirect issued via a listing call is treated
        differently). So on a 403 here, this now falls back to listing the
        folder and downloading via that signed URL instead of failing
        outright -- if that fallback ALSO fails, the ORIGINAL direct-path
        error (with Graph's response body) is what gets raised, since it's
        the more informative one to act on."""
        path = f"{folder_path.strip('/')}/{file_name}" if folder_path.strip("/") else file_name
        url = f"{GRAPH_BASE_URL}/drives/{self.config.drive_id}/root:/{path}:/content"
        print(f"[onedrive_client] download_file_by_path: trying direct path GET {url}", flush=True)
        resp = requests.get(url, headers=self._headers(), stream=True, timeout=120)
        print(f"[onedrive_client] direct path GET returned status {resp.status_code}", flush=True)
        if resp.status_code == 404:
            raise FileNotFoundError(f"{file_name!r} not found at {folder_path!r} (direct path lookup).")
        if resp.status_code == 403:
            print("[onedrive_client] direct path got 403 -- ENTERING FALLBACK "
                  "(find_file_by_name + download_file via signed downloadUrl)", flush=True)
            try:
                item = self.find_file_by_name(folder_path, file_name)
                print(f"[onedrive_client] fallback: find_file_by_name() succeeded, item id={item.get('id')} -- "
                      "now calling download_file()", flush=True)
                result = self.download_file(item, dest_path)
                print(f"[onedrive_client] fallback: download_file() SUCCEEDED -> {result}", flush=True)
                return result
            except Exception as fallback_err:
                # Surface BOTH failures now -- previously this silently
                # discarded the fallback's own error and re-raised only the
                # original direct-path one, which made it impossible to tell
                # "fallback never ran" apart from "fallback ran and also
                # failed" just by reading the error message. Now the message
                # itself proves the fallback was attempted and shows why it
                # didn't help either.
                print(f"[onedrive_client] fallback FAILED: {fallback_err!r}", flush=True)
                try:
                    _raise_with_graph_body(resp)
                except requests.exceptions.HTTPError as direct_err:
                    raise requests.exceptions.HTTPError(
                        f"Direct path download failed ({direct_err}); fallback via find_file_by_name()+"
                        f"download_file() ALSO failed: {fallback_err}",
                        response=resp,
                    ) from fallback_err
        print(f"[onedrive_client] direct path GET succeeded outright (status {resp.status_code}), "
              "no fallback needed", flush=True)
        _raise_with_graph_body(resp)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest_path

    def upload_file(self, local_path: str, folder_path: str, file_name: str) -> dict:
        """Uploads local_path to folder_path/file_name in OneDrive, creating or
        overwriting that file (Graph's "simple upload" -- fine for files under
        ~4MB, which covers every workbook this pipeline handles). Requires the
        app registration to have Files.ReadWrite.All (or Sites.ReadWrite.All)
        -- see this module's docstring; Files.Read.All alone will 403 here.
        Returns the resulting driveItem."""
        path = f"{folder_path.strip('/')}/{file_name}" if folder_path.strip("/") else file_name
        url = f"{GRAPH_BASE_URL}/drives/{self.config.drive_id}/root:/{path}:/content"
        with open(local_path, "rb") as f:
            content = f.read()
        headers = self._headers()
        headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        resp = requests.put(url, headers=headers, data=content, timeout=120)
        _raise_with_graph_body(resp)
        return resp.json()

    def rename_file(self, item_id: str, new_name: str) -> dict:
        """Renames a file IN PLACE via Graph's driveItem PATCH (PATCH
        /drives/{drive_id}/items/{item_id}, body {"name": new_name}) --
        the correct Graph operation for a rename: the same item id (and
        therefore version history, sharing links, etc.) is preserved,
        unlike downloading-then-reuploading-under-a-new-name, which would
        create a brand new item and briefly make the file vanish from the
        folder under its old name.

        2026-08-10, per Ravindra: "I dont want to process the files i
        already processed/posted ... Is it good to rename the file with
        .COMPLETED at the end and post only those files which does not end
        with it?" -- used by main.py's /post-transactions to append this
        pipeline's completion marker (see completed_file_name() below) once
        every postable row in a file has a real Zoho posting reference, so
        a future /list-files call can filter it out entirely (see
        is_marked_complete()) -- the file is never even downloaded again,
        not merely re-processed-and-skipped-again at the row level."""
        url = f"{GRAPH_BASE_URL}/drives/{self.config.drive_id}/items/{item_id}"
        resp = requests.patch(url, headers=self._headers(), json={"name": new_name}, timeout=30)
        _raise_with_graph_body(resp)
        return resp.json()

    def check_for_new_files(self, already_processed_ids: set, folder_path: Optional[str] = None) -> List[dict]:
        """The polling entry point: call this on a schedule. Returns driveItems not seen before.
        folder_path: same meaning as on list_files_in_folder() -- pass a specific bank's folder
        for a multi-bank run, omit for the original single-folder behavior."""
        all_files = self.list_files_in_folder(folder_path)
        return [f for f in all_files if f["id"] not in already_processed_ids]