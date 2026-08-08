# Deploying the categorization service to Cloud Run

This is the backend the "Select Accounts" widget calls: once to list what's
in the OneDrive statements folder, then once per bank account to actually
categorize that account's file (see `main.py`'s docstring for why it's split
that way — one folder listing per run instead of one per account). It needs
the same Microsoft Graph app-only credentials
(`onedrive_client.py` already uses these -- copy them straight out of the
Python pipeline's `.env`), plus one new shared secret the widget also needs
to know (`CATEGORIZE_API_KEY`).

## 1. One-time setup

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

## 2. Store secrets

```bash
echo -n "YOUR_MS_TENANT_ID"     | gcloud secrets create MS_TENANT_ID --data-file=-
echo -n "YOUR_MS_CLIENT_ID"     | gcloud secrets create MS_CLIENT_ID --data-file=-
echo -n "YOUR_MS_CLIENT_SECRET" | gcloud secrets create MS_CLIENT_SECRET --data-file=-
echo -n "YOUR_MS_DRIVE_ID"      | gcloud secrets create MS_DRIVE_ID --data-file=-
echo -n "pick-a-long-random-string" | gcloud secrets create CATEGORIZE_API_KEY --data-file=-
```

(If any of these secrets already exist from the other pipeline's GCP
deployment, reuse them -- `gcloud secrets versions add SECRET_NAME
--data-file=-` instead of `create`.)

## 3. Deploy

From this folder (`gcp_categorize_service/`):

```bash
gcloud run deploy categorize-service \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-secrets="MS_TENANT_ID=MS_TENANT_ID:latest,MS_CLIENT_ID=MS_CLIENT_ID:latest,MS_CLIENT_SECRET=MS_CLIENT_SECRET:latest,MS_DRIVE_ID=MS_DRIVE_ID:latest,CATEGORIZE_API_KEY=CATEGORIZE_API_KEY:latest" \
  --set-env-vars="MS_STATEMENTS_FOLDER_PATH=BankStatements/Dubai"
```

`--allow-unauthenticated` is required so the widget's browser-side `fetch()`
can reach it directly -- the `X-Api-Key` header is what protects it instead
(see `main.py`'s docstring for the tradeoff, and the IAM-based alternative if
you want it locked down harder later).

This prints a Service URL, something like:

```
https://categorize-service-xxxxxxxxxx-uc.a.run.app
```

## 4. Wire the widget up to it

Two files need that URL + the API key:

1. `zoho-books-extension/app/widget.js` -- set `GCP_CATEGORIZE_ENDPOINT` to
   `<Service URL>/categorize` and `GCP_API_KEY` to the same string you put in
   `CATEGORIZE_API_KEY` above.
2. `zoho-books-extension/plugin-manifest.json` -- add the Service URL's
   origin (e.g. `https://categorize-service-xxxxxxxxxx-uc.a.run.app`, no
   trailing path) to `cspDomains`, or the widget's `fetch()` call will be
   blocked by Zoho's extension sandboxing before it even leaves the browser.

Then repackage and reload the extension (`zet run` / `zet pack`).

## 5. Sanity-check without the widget

First, list what's actually in the folder (this is the one-time-per-run call):

```bash
curl "<Service URL>/list-files" -H "X-Api-Key: pick-a-long-random-string"
```

You should get back `{"success": true, "folder_path": "...", "files": ["012001940568_NBF AED_CBK.xlsx", ...]}`.
Copy one of those exact file names for the next step.

Then categorize that one file directly by name (no listing happens inside
this call at all):

```bash
curl -X POST "<Service URL>/categorize" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: pick-a-long-random-string" \
  -d '{
    "file_name": "012001940568_NBF AED_CBK.xlsx",
    "rules": [
      {"cf_category": "Bank Charges", "cf_key_words": "bank charge, service fee",
       "cf_from_account": "Bank Charges Expense", "cf_to_account": "NBF AED",
       "cf_type_of_transaction": "Expense"}
    ]
  }'
```

You should get back JSON with per-file stats (`total`, `others`, `by_category`),
or a clear `success: false` error (check `file_name`/`folder_path` match
exactly what `/list-files` returned).
