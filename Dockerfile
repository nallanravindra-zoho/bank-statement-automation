FROM python:3.12-slim
WORKDIR /app

# libreoffice-calc (2026-08-02, per Ravindra -- see main.py's
# _recalculate_formulas()): needed so Debit Account/Credit Account/Posting
# Type's live Excel formulas get a real CACHED value baked in right after
# generation, instead of showing blank in any viewer until someone happens to
# open the file for real in Excel first. This is a real, noticeably bigger
# dependency (LibreOffice, even Calc-only, adds a few hundred MB to the image
# and some seconds to a cold start) -- see DEPLOY.md for the Cloud Run
# memory/timeout bump this needs. If that trade-off isn't worth it, this line
# (and the _recalculate_formulas() call in main.py) can be removed -- the
# formulas themselves work correctly either way, this is purely about making
# their value visible without an extra manual step.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120", "main:app"]