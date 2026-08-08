"""
categorize_statement.py

Standalone categorization pass over a bank-statement Excel file: for every
transaction row, decide which category it belongs to by looking for keyword
matches in the Narration text, using a small lookup workbook you maintain
(Category, Keywords columns) -- separate from the full automation pipeline.

This is deliberately NOT wired into config_loader.py / rules_engine.py --
it only adds a Category column and never touches account IDs, posting
methods, or Zoho at all. It's a first standalone step; a later "Postings"
workbook (category -> Zoho accounts) can map onto the Category values this
produces once that's ready.

Matching rule: PLAIN, case-insensitive substring match (no fuzzy/typo
tolerance) -- a keyword "fires" if it appears anywhere inside the narration
text. Categories are checked in the order they appear in the categories
workbook, top to bottom; the FIRST category with a matching keyword wins,
so put more specific categories above more general/overlapping ones. A row
that matches no category's keywords gets "Others" (reserved -- you can't
define "Others" as one of your own categories).

Usage (local files -- OVERWRITES --statement in place by default, adding/
updating its Category column; pass --output explicitly if you'd rather
write a separate file and leave the original untouched):
    python categorize_statement.py --source local \
        --statement "Sample BS.xlsx" \
        --categories "Categories_template.xlsx" \
        --compare-existing

Usage (OneDrive -- reads both the statement and the categories workbook from
OneDrive via Microsoft Graph, same MS_* credentials/setup as onedrive_client.py
and main.py; see that module's docstring for the one-time Azure AD app
registration steps). This writes a same-named local copy by default -- add
--upload to also overwrite the source file in OneDrive itself:
    python categorize_statement.py --source onedrive \
        --statement-folder "/Bank Statements/DTB" --statement-file "June 2026.xlsx" \
        --categories-folder "/Automation" --categories-file "Categories.xlsx" \
        --upload

--categories-folder/--categories-file default to MS_CATEGORIES_FOLDER_PATH /
MS_CATEGORIES_FILE_NAME from .env if not passed explicitly (see .env.example).
--upload requires the Graph app registration to have Files.ReadWrite.All,
not just Files.Read.All -- see onedrive_client.py's docstring.

Note: since the output is written fresh from the parsed cell values, any
non-data formatting in the original file (colors, column widths, formulas)
is not preserved -- the overwritten file has the same data, plain-formatted,
plus the Category column.

Categories workbook layout (first sheet, header row required):
    Category   -- the label to assign, e.g. "Bank Charges"
    Keywords   -- comma-separated phrase(s) to look for in the narration.
                  ANY one of them matching is enough to fire this category.

Statement workbook: first sheet, header row required, must contain a
Narration column (any header in NARRATION_HEADER_ALIASES is accepted).
Every other column is passed through unchanged.

--compare-existing: if the statement already has hand-labeled Category
values (like Sample BS.xlsx does), keep the original as a separate
"Category (original)" column next to the newly computed "Category" column,
and print an accuracy summary. Without this flag, an existing Category
column is simply overwritten with the computed value.
"""
import argparse
import os
import sys
import tempfile

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

NARRATION_HEADER_ALIASES = {"narration", "description", "details", "particulars", "narrative"}
# "kewords" covers the common typo -- seen in a real workbook in the wild.
KEYWORDS_HEADER_ALIASES = {"keywords", "keyword", "kewords", "kewords ", "key words"}
FALLBACK_CATEGORY = "Others"
# How many rows from the top to look through for the real header row, before
# giving up -- covers banks that put a "Customer Number / Client Name /
# Account Number / From / To / Branch" banner above the actual transaction
# table (row 1 isn't always the header).
HEADER_SCAN_LIMIT = 30


def find_header_row(rows):
    """Returns the index (0-based) of the first row containing a
    Narration-like column, scanned within HEADER_SCAN_LIMIT rows -- that
    row is treated as the real header, and anything above it (e.g. a
    Customer Number/Branch/date-range banner) is passed through to the
    output untouched instead of being mistaken for column headers. Returns
    None if nothing matches within the scan limit.

    TWO PASSES, exact match preferred over substring (2026-08-03, per
    Ravindra -- a real live statement, CBQ QAR 3001, hit this exactly): a
    real live row read as ['...', 'IBAN/Account Number : QA05CBQA...',
    '...', 'Account Transaction Details', '...'] is a section-title BANNER
    row, several rows above the real column-header row -- but "details" is
    one of NARRATION_HEADER_ALIASES, and "Account Transaction Details"
    contains it as a substring, so the old single-pass version (which just
    returned the FIRST row anywhere find_column() matched, exact or
    substring alike) stopped scanning right there and never reached the
    real header row below it (the one with actual Debit Amount/Credit
    Amount columns) -- producing a confusing "couldn't find Debit/Credit
    columns" error despite them existing further down the sheet.

    Pass 1 requires an EXACT cell match only (e.g. a cell whose value is
    literally "Description") -- a genuine header row almost always has
    one, and checking every row for an exact hit BEFORE considering any
    row's looser substring matches means a real header row several lines
    below a loosely-matching banner row is still found correctly. Pass 2
    (only reached if NO row in the scan range has an exact match anywhere)
    falls back to the original substring-anywhere behavior, unchanged --
    still needed for a bank whose header genuinely only matches via a
    wrapping phrase (e.g. "Txn Narrative")."""
    for i, row in enumerate(rows[:HEADER_SCAN_LIMIT]):
        lower = [str(h).strip().lower() if h is not None else "" for h in row]
        if any(h in NARRATION_HEADER_ALIASES for h in lower):
            return i
    for i, row in enumerate(rows[:HEADER_SCAN_LIMIT]):
        lower = [str(h).strip().lower() if h is not None else "" for h in row]
        if find_column(lower, NARRATION_HEADER_ALIASES) is not None:
            return i
    return None


def load_categories(path: str):
    """Returns an ordered list of (category, [keyword, ...]) tuples -- order
    is match priority, top to bottom. Keywords come back lowercased/stripped,
    ready for substring matching. A category with no keywords defined yet is
    still loaded (with an empty keyword list) -- it just can never match
    anything until keywords are added, so rows meant for it fall to
    "Others" in the meantime. A warning is printed listing which categories
    that applies to."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError(f"{path!r}: sheet is empty.")

    header = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
    kw_header = next((h for h in header if h in KEYWORDS_HEADER_ALIASES), None)
    if "category" not in header or kw_header is None:
        raise ValueError(
            f"{path!r}: expected header columns 'Category' and 'Keywords', found {list(rows[0])!r}."
        )
    cat_idx = header.index("category")
    kw_idx = header.index(kw_header)

    categories = []
    seen = set()
    no_keywords_yet = []
    for row_number, row in enumerate(rows[1:], start=2):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue  # skip blank rows

        category = str(row[cat_idx]).strip() if row[cat_idx] is not None else ""
        if not category:
            raise ValueError(f"{path!r} row {row_number}: Category is blank.")
        if category.lower() == FALLBACK_CATEGORY.lower():
            raise ValueError(
                f"{path!r} row {row_number}: {FALLBACK_CATEGORY!r} is reserved for rows that match "
                f"nothing -- don't define it as one of your own keyword-matched categories."
            )
        if category.lower() in seen:
            print(f"Warning: {path!r} row {row_number}: duplicate category {category!r} -- "
                  f"an earlier row with the same name already runs first, so this row's keywords "
                  f"will never be reached.", file=sys.stderr)
        seen.add(category.lower())

        keywords_raw = str(row[kw_idx]).strip() if row[kw_idx] is not None else ""
        keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
        if not keywords:
            no_keywords_yet.append(category)
        categories.append((category, keywords))

    if not categories:
        raise ValueError(f"{path!r}: no data rows found.")
    if no_keywords_yet:
        print(f"Warning: {len(no_keywords_yet)} categor{'y has' if len(no_keywords_yet)==1 else 'ies have'} "
              f"no keywords yet, so rows meant for {'it' if len(no_keywords_yet)==1 else 'them'} will fall "
              f"to {FALLBACK_CATEGORY!r} until filled in: {', '.join(no_keywords_yet)}.", file=sys.stderr)
    return categories


def find_column(header_lower, aliases):
    """Exact match first (e.g. header cell is literally "Narration"), then
    falls back to substring containment (e.g. "Transaction Narrative" or
    "Txn Description" -- a real header that wraps a recognized word instead
    of using it bare). Exact match is tried across every column before any
    substring fallback, so a genuine exact match elsewhere always wins."""
    for idx, h in enumerate(header_lower):
        if h in aliases:
            return idx
    for idx, h in enumerate(header_lower):
        if h and any(alias in h for alias in aliases):
            return idx
    return None


def classify(narration, categories) -> str:
    text = (str(narration) if narration is not None else "").lower()
    for category, keywords in categories:
        if any(kw in text for kw in keywords):
            return category
    return FALLBACK_CATEGORY


def add_category_dropdown(out_wb, out_ws, categories, existing_cat_idx, compare_existing,
                           new_header, num_metadata_rows, num_data_rows):
    """Adds an Excel dropdown (data validation) to every Category cell in the
    data rows, listing every category from the categories workbook plus the
    "Others" fallback -- so a cell is pre-filled with whatever classify()
    picked, but clicking it offers a list to override from instead of having
    to type a category name (and risk a typo that doesn't match anything).

    The list itself lives on a hidden helper sheet ("_CategoryList") rather
    than as an inline comma-separated formula -- Excel's inline-list data
    validation is capped at 255 characters, which a few dozen category names
    can exceed; a cell-range reference has no such limit.
    """
    if num_data_rows == 0:
        return  # nothing to attach a dropdown to

    list_ws = out_wb.create_sheet("_CategoryList")
    list_ws.sheet_state = "hidden"
    all_category_names = [c for c, _ in categories] + [FALLBACK_CATEGORY]
    for i, name in enumerate(all_category_names, start=1):
        list_ws.cell(row=i, column=1, value=name)

    dv = DataValidation(
        type="list",
        formula1=f"_CategoryList!$A$1:$A${len(all_category_names)}",
        allow_blank=True,
    )
    dv.error = "Choose a category from the dropdown list."
    dv.errorTitle = "Not a recognized category"
    out_ws.add_data_validation(dv)

    if existing_cat_idx is not None and not compare_existing:
        category_col_number = existing_cat_idx + 1  # overwritten in place -- same column as before
    else:
        category_col_number = len(new_header)  # newly appended -- last column
    category_col_letter = get_column_letter(category_col_number)

    header_row_excel = num_metadata_rows + 1
    first_data_row = header_row_excel + 1
    last_data_row = header_row_excel + num_data_rows
    dv.add(f"{category_col_letter}{first_data_row}:{category_col_letter}{last_data_row}")


def run(statement_path: str, categories_path: str, output_path: str, compare_existing: bool):
    categories = load_categories(categories_path)
    print(f"Loaded {len(categories)} categories from {categories_path!r} "
          f"(priority order: {', '.join(c for c, _ in categories)}).")

    wb = openpyxl.load_workbook(statement_path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError(f"{statement_path!r}: sheet is empty.")

    header_row_idx = find_header_row(rows)
    if header_row_idx is None:
        raise ValueError(
            f"{statement_path!r}: couldn't find a header row (one containing a Narration-like column) "
            f"in the first {HEADER_SCAN_LIMIT} rows. Expected a column named one of "
            f"{sorted(NARRATION_HEADER_ALIASES)}."
        )
    metadata_rows = rows[:header_row_idx]  # e.g. Customer Number/Branch/date-range banner rows some
                                            # bank exports put above the real table -- passed through
                                            # verbatim, untouched, at the top of the output.
    if header_row_idx > 0:
        print(f"Note: the real header row is row {header_row_idx + 1}, not row 1 -- treating rows 1-"
              f"{header_row_idx} as a metadata banner above the transaction table and copying them "
              f"through unchanged.")

    header = list(rows[header_row_idx])
    header_lower = [str(h).strip().lower() if h is not None else "" for h in header]
    nar_idx = find_column(header_lower, NARRATION_HEADER_ALIASES)
    existing_cat_idx = header_lower.index("category") if "category" in header_lower else None

    out_wb = openpyxl.Workbook()
    out_ws = out_wb.active
    out_ws.title = ws.title

    for row in metadata_rows:
        out_ws.append(list(row))

    new_header = list(header)
    if existing_cat_idx is not None and compare_existing:
        new_header[existing_cat_idx] = "Category (original)"
        new_header.append("Category")
    elif existing_cat_idx is None:
        new_header.append("Category")
    # else: existing Category column gets overwritten in place, header unchanged
    out_ws.append(new_header)

    total = 0
    compared = 0
    matches_original = 0
    for row in rows[header_row_idx + 1:]:
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        total += 1
        computed = classify(row[nar_idx], categories)

        row_out = list(row)
        if existing_cat_idx is not None and compare_existing:
            row_out.append(computed)
        elif existing_cat_idx is not None:
            row_out[existing_cat_idx] = computed
        else:
            row_out.append(computed)
        out_ws.append(row_out)

        if existing_cat_idx is not None and compare_existing:
            original = row[existing_cat_idx]
            if original is not None and str(original).strip() != "":
                compared += 1
                if str(original).strip().lower() == computed.strip().lower():
                    matches_original += 1

    add_category_dropdown(out_wb, out_ws, categories, existing_cat_idx, compare_existing,
                           new_header, len(metadata_rows), total)

    out_wb.save(output_path)
    print(f"Wrote {total} categorized rows to {output_path!r}.")
    if compare_existing and compared:
        print(f"Accuracy vs existing 'Category (original)' values: {matches_original}/{compared} "
              f"({matches_original / compared:.0%}).")


def load_dotenv_if_present(path: str = ".env"):
    # Same minimal loader main.py uses -- keeps this script runnable standalone
    # without importing all of main.py just for this one helper.
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def run_onedrive(statement_folder: str, statement_file: str, categories_folder: str, categories_file: str,
                  output_path: str, compare_existing: bool, upload: bool):
    load_dotenv_if_present()
    from onedrive_client import OneDriveClient, OneDriveConfig

    config = OneDriveConfig.from_env()
    categories_folder = categories_folder or config.categories_folder_path
    categories_file = categories_file or config.categories_file_name
    if not categories_folder:
        raise ValueError("No --categories-folder given and MS_CATEGORIES_FOLDER_PATH isn't set in .env either.")

    client = OneDriveClient(config)

    # Local copy defaults to the same file name, in the current directory --
    # i.e. by default this ends up looking like "the statement got its
    # Category column added", not "a new file appeared", matching --source
    # local's default below. Pass --output explicitly if you want a
    # differently-named local copy instead.
    if output_path is None:
        output_path = statement_file

    with tempfile.TemporaryDirectory() as tmp:
        statement_item = client.find_file_by_name(statement_folder, statement_file)
        statement_path = client.download_file(statement_item, os.path.join(tmp, statement_file))
        print(f"Downloaded statement {statement_file!r} from OneDrive folder {statement_folder!r}.")

        categories_item = client.find_file_by_name(categories_folder, categories_file)
        categories_path = client.download_file(categories_item, os.path.join(tmp, categories_file))
        print(f"Downloaded categories workbook {categories_file!r} from OneDrive folder {categories_folder!r}.")

        run(statement_path, categories_path, output_path, compare_existing)

        if upload:
            # Always overwrites the exact same file in OneDrive -- no
            # separate "- categorized" copy. This is the one step that
            # actually touches your live OneDrive file, so it stays behind
            # the explicit --upload flag; everything upstream (download,
            # categorize, local write) always happens either way.
            client.upload_file(output_path, statement_folder, statement_file)
            print(f"Uploaded categorized result to OneDrive, overwriting {statement_folder.rstrip('/')}/{statement_file!r}.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", choices=["local", "onedrive"], default="local")
    parser.add_argument("--statement", help="[--source local] Path to the bank statement .xlsx to categorize.")
    parser.add_argument("--categories", help="[--source local] Path to the Category/Keywords lookup .xlsx.")
    parser.add_argument("--statement-folder", help="[--source onedrive] OneDrive folder the statement file is in.")
    parser.add_argument("--statement-file", help="[--source onedrive] Exact file name of the statement to categorize.")
    parser.add_argument("--categories-folder", default=None,
                         help="[--source onedrive] OneDrive folder the Categories workbook is in. "
                              "Defaults to MS_CATEGORIES_FOLDER_PATH from .env if omitted.")
    parser.add_argument("--categories-file", default=None,
                         help="[--source onedrive] File name of the Categories workbook. "
                              "Defaults to MS_CATEGORIES_FILE_NAME from .env (or 'Categories.xlsx') if omitted.")
    parser.add_argument("--output", default=None,
                         help="Local path to write the categorized .xlsx to. Defaults to overwriting the "
                              "source statement itself in place (--statement for --source local, or a "
                              "same-named local copy for --source onedrive) -- pass this explicitly only if "
                              "you want a separate file instead.")
    parser.add_argument(
        "--compare-existing", action="store_true",
        help="If the statement already has a 'Category' column, keep it as 'Category (original)' "
             "next to the new computed one, and print an accuracy summary against it."
    )
    parser.add_argument("--upload", action="store_true",
                         help="[--source onedrive] Also upload the categorized result back to OneDrive, "
                              "overwriting --statement-file in --statement-folder in place. "
                              "Requires Files.ReadWrite.All on the Graph app registration.")
    parser.add_argument("--list-folder", metavar="PATH", default=None,
                         help="[--source onedrive] Diagnostic: instead of categorizing anything, just list what "
                              "Graph sees directly inside this OneDrive folder path (files AND subfolders) and "
                              "exit. Use this to confirm a folder path segment-by-segment when "
                              "--statement-folder/--categories-folder isn't resolving -- e.g. try \"/\", then "
                              "\"/Bank Statements\", then \"/Bank Statements/Dubai\", to find exactly where it "
                              "diverges from what you typed.")
    args = parser.parse_args()

    if args.list_folder is not None:
        load_dotenv_if_present()
        from onedrive_client import OneDriveClient, OneDriveConfig
        client = OneDriveClient(OneDriveConfig.from_env())
        items = client.list_all_items(args.list_folder)
        if not items:
            print(f"{args.list_folder!r} exists but is empty.")
        else:
            print(f"Contents of {args.list_folder!r}:")
            for item in items:
                kind = "folder" if item["_is_folder"] else "file"
                print(f"  [{kind}] {item.get('name')!r}")
        return

    if args.source == "local":
        if not args.statement or not args.categories:
            sys.exit("--statement and --categories are required with --source local.")
        # Default: overwrite the statement file itself in place -- pass
        # --output explicitly to write a separate file instead.
        output_path = args.output if args.output is not None else args.statement
        run(args.statement, args.categories, output_path, args.compare_existing)
    else:
        if not args.statement_folder or not args.statement_file:
            sys.exit("--statement-folder and --statement-file are required with --source onedrive.")
        run_onedrive(args.statement_folder, args.statement_file, args.categories_folder, args.categories_file,
                     args.output, args.compare_existing, args.upload)


if __name__ == "__main__":
    main()