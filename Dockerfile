FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# --timeout raised 120 -> 1800 (2026-08-13, root cause of Ravindra's live
# report: "stream ended without a 'done' event ... not posting back the
# zoho posting reference for any transactions"). gunicorn's sync worker
# --timeout is effectively a MAX SINGLE-REQUEST duration, not a socket-idle
# timeout -- the worker only checks in with the arbiter between requests,
# so a /post-transactions run (one long streamed response covering every
# row's own Zoho API call, plus, since this session's Egypt itemized-
# posting feature, extra per-group duplicate-check + create-expense calls)
# that takes longer than --timeout gets SIGKILLed by the arbiter mid-
# request. SIGKILL can't be caught, so main.py's own try/except around the
# whole generator (which would otherwise yield a specific {"type": "error"}
# event) never runs -- the TCP connection just dies, which is exactly why
# the widget saw the generic "stream ended without a 'done' event" fallback
# instead of a real error message, and why write_posting_results()/
# wb.save()/upload_file() (all at the very end of the generator) never ran
# at all, even for rows that had already posted successfully to Zoho.
# 1800s (30 min) gives real headroom for a large statement; see also this
# repo's DEPLOY.md -- the `gcloud run deploy` command's own --timeout flag
# must be at least this high too (Cloud Run's own request timeout is a
# SEPARATE ceiling from gunicorn's -- whichever is lower wins).
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "1800", "main:app"]