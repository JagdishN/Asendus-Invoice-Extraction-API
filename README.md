# AiZen Invoice Extractor — Backend (Phase 1)

FastAPI backend for the PDF/Image → CSV invoice extraction app
(Asendus Innovations LLP). Export format is per-invoice CSV (zipped when a
job has more than one invoice), per client confirmation — not Excel.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Docs at http://localhost:8000/docs

## ⚠️ Tesseract OCR is a required SYSTEM-level dependency

`pytesseract` (in `requirements.txt`) is only a Python wrapper — it calls
out to the `tesseract` binary, which `pip install` does **not** install.
Without it, any OCR call (scanned PDF pages, or any JPEG/PNG/WEBP upload)
raises `TesseractNotFoundError` at runtime. This must be added to whatever
deployment/provisioning steps this app has beyond this repo (Dockerfile,
server setup script, etc.) — it will not be present on a fresh server by
default:

```bash
# Debian/Ubuntu (the deployment target)
apt-get update && apt-get install -y tesseract-ocr

# Windows (local dev)
winget install --id UB-Mannheim.TesseractOCR -e
```

`app/services/ocr_extraction.py` auto-detects the default Windows install
path as a fallback if it's not on `PATH` (the winget package doesn't
update an already-open shell's `PATH`) — this is a no-op on Linux, where
`apt-get install` already puts it on `PATH`.

## Diagnosing OCR extraction (DEBUG mode)

Label-matching runs against raw OCR/PDF text you never normally see, so
when a field doesn't get found on a real invoice, the first question is
always "what did Tesseract/PyMuPDF actually read, before any regex ran?".
Set `DEBUG=true` to find out:

```bash
DEBUG=true uvicorn app.main:app --reload --port 8000
```

With it on:
- The full raw text extracted per page (native OR OCR, whichever was used)
  is logged to the console at DEBUG level — see
  `app/services/ocr_extraction.py`'s `extract_text_from_image_bytes`. At
  the default log level (`DEBUG` unset/false) only a length summary is
  logged, since raw OCR text can be long.
- `GET /api/jobs/{job_id}/debug/raw-text` returns that same per-page text
  as JSON (`app/core/debug_store.py`, an in-memory store separate from the
  Job model itself). **404s when `DEBUG` is not set** — never exposed by
  default, and this route should not ship enabled in production.

```bash
curl http://localhost:8000/api/jobs/<job_id>/debug/raw-text \
  -H "Authorization: Bearer <access_token>"
```

## ⚠️ Auth is a temporary testing stopgap — not real security

Every `/api/jobs*` route now requires a Bearer token. There is **no real
register/login system yet** — `app/core/auth.py` checks against a single
hardcoded test credential pair, bcrypt-hashed but hardcoded nonetheless.
**This is not a real security posture and must not be treated as one in
any later handoff.** It exists purely so protected routes, JWT
issuance/validation, and "how user identity flows into a Job" could be
built and tested now, ahead of real Supabase-based register/login/OTP
auth landing later.

Test credentials (override via `TEST_USERNAME` / `TEST_PASSWORD` env vars):

```
username: Admin
password: Admin@123
```

```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "Admin", "password": "Admin@123"}'
# -> {"access_token": "...", "token_type": "bearer"}

curl http://localhost:8000/api/jobs \
  -H "Authorization: Bearer <access_token>"
```

When real Supabase auth lands, only `verify_credentials()` in
`app/core/auth.py` should need to change — token issuance
(`create_access_token`), route protection (`get_current_user`), and how
the authenticated username flows into `Job.user_id` are all meant to
survive that swap unchanged. `JWT_SECRET_KEY` has a placeholder default
for local dev — **must be overridden in any real deployment.**

## Run tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers `split_parser`, `invoice_grouping`, `filename_safety`, `file_validation`,
`native_pdf_extraction`, `csv_export`, `auth`, and `ocr_extraction` at the
unit level, plus integration tests for the upload/export/history endpoints
via FastAPI's `TestClient` (synthetic PDFs/images generated on the fly with
reportlab/Pillow — see `tests/pdf_builders.py`). The in-memory job store and
the export-storage root are both reset/isolated per test
(`tests/conftest.py`), so test runs never touch the real `storage/exports/`
directory. The `client` fixture logs in with the test credentials and
carries the token automatically, so existing tests didn't need touching
when auth was added; use the `unauthenticated_client` fixture to test
rejection paths.

Tests that call real OCR are marked `@requires_tesseract`
(`tests/conftest.py`) and skip gracefully — not fail — on a machine without
the Tesseract binary installed, so the rest of the suite stays runnable
without that system dependency.

## What's implemented (this phase)

- `app/core/auth.py` + `POST /api/auth/login` — temporary testing-phase
  auth stopgap (see the warning section above). All `/api/jobs*` routes
  require a valid Bearer token via the `get_current_user` dependency;
  `Job.user_id` is now the authenticated username, not a placeholder.
- `POST /api/jobs` — upload PDF/JPEG/PNG/WEBP, with:
  - real content-signature validation (not just extension)
  - configurable max file size / max page count (`app/core/config.py`)
  - split-value parsing: single page, range, comma-separated multiple ranges
  - server-side enforcement of the "confirm before proceeding without a
    split value" rule (`confirmed_no_split` flag), so this can't be
    bypassed by calling the API directly
- `GET /api/jobs` / `GET /api/jobs/{id}` — history / job detail (in-memory
  store for now, per agreed phase-1 scope — see `app/core/job_store.py`)
- `app/services/invoice_grouping.py` — the core sequential grouping +
  non-contiguous-merge-with-flag algorithm, already covers:
  - contiguous continuation of the same invoice
  - non-contiguous repeat of an earlier invoice number → merges into the
    original group, flags `is_non_contiguous_merge=True` for the preview
    UI to surface as "confirm this merge?"
  - split-value scoping as a hard fence (no merge across range boundaries)
  - undetected/continuation pages attaching to the currently open group
- `app/services/filename_safety.py` — general filesystem-safe filename
  sanitization (handles invalid characters like `/ \ : * ? " < > |`, a
  length limit, and collision suffixing). Originally Excel-worksheet-only
  (`sheet_naming.py`); generalized when export moved to CSV since it's no
  longer naming worksheets.
- `app/services/csv_export.py` + three endpoints on
  `app/routers/history.py` — generates one `.csv` per `InvoiceGroup`
  (header-block-then-line-items-table format), zips them when a job has
  more than one, and **persists** both to disk (see `export_storage.py`
  below) so repeat requests are served, not regenerated:
  - `GET /api/jobs/{id}/export` — default download: the zip if >1 invoice,
    else the single CSV
  - `GET /api/jobs/{id}/export/zip` — explicitly the zip (404 if the job
    only has one invoice, since none is generated in that case)
  - `GET /api/jobs/{id}/export/{invoice_number}` — one invoice's CSV,
    looked up by invoice number. Uses a `:path` route converter because
    invoice numbers routinely contain `/` (e.g. `INV/2026/001`)
  - Also ships a **temporary** dummy-data generator
    (`populate_job_with_dummy_data`) and debug endpoint
    (`POST /api/jobs/{id}/_debug/populate-dummy-data`) so the upload →
    export pipeline shape can be exercised before real extraction covers
    every file type — clearly marked for deletion once it does.
- `app/services/export_storage.py` — local-filesystem persistence
  (`storage/exports/{job_id}/{filename}`, path configurable via
  `EXPORT_STORAGE_ROOT`) behind a small `save_bytes`/`read_bytes`/`exists`
  interface, so swapping in S3/MinIO later is a new class, not a rewrite
  of callers. **No cleanup/retention policy exists yet** — exported files
  accumulate indefinitely; confirm a policy before production volume.
- `app/services/native_pdf_extraction.py` — rules-based (label-matching,
  no ML/OCR) extraction for **born-digital PDFs only** (real text layer).
  Wired synchronously into `POST /api/jobs`:
  - per-page invoice-number pass feeds `invoice_grouping.py` directly
  - per-group pass fills `header_fields` + `header_field_confidences`, and
    best-effort `line_items` via PyMuPDF's `find_tables()` (only for tables
    with real ruling lines — text-only layouts intentionally return an
    empty list rather than guess)
  - every extracted field gets a `ConfidenceBand`; label/value patterns are
    first-pass guesses, not yet tuned against real client invoices (see
    `FRAGILE` comments in the module)
  - refactored to split "get text off a page" from "find fields in text"
    (`extract_header_fields_from_text`, parameterized by `ExtractionSource`)
    so `ocr_extraction.py` reuses the exact same label-matching/field-mapping
    without duplicating it — see that module and the docstring at the top of
    `native_pdf_extraction.py` for the full shape of the split
  - **Confirmed pharma/GST invoice format** (client-confirmed field layout):
    - `invoice_number` is specifically the **IRN** (64-hex-char GST
      e-invoice reference, not a vendor-assigned number) — graded `HIGH`
      only when it matches that exact shape, `REVIEW` otherwise (a plain
      vendor-style number like `INV/2026/001` still gets captured, just at
      `REVIEW` — this downgraded some previously-`HIGH` generic-format
      test fixtures, which is the correct/intended effect)
    - `party_name`/`party_gstin` are the **vendor/seller**, found via a
      `"For <company>"` signature-line regex — NOT the `"Bill To"` label,
      which now populates the new `buyer_name`/`buyer_address` fields
      instead. **`buyer_name`/`buyer_address` label matching is
      unvalidated** — we only had a cropped billing-table view, not a full
      sample, so treat as a first guess pending a real one
    - `ack_number`, `eway_bill_number`, `eway_bill_date`, `fssai_number` —
      frequently blank on real invoices; a missing value is a legitimate
      `NOT_FOUND`, not an error
    - `total_amount`, `discount_amount`, `tcs_amount`, `invoice_amount`,
      `adjustment_amount` (signed — can be negative) are the individual
      steps of the confirmed totals block, kept separate; `invoice_total`
      holds the final "Net Payable Amount"
    - `tax_bracket_summary` — `list[{rate, taxable_amount, tax_amount}]`,
      the invoice-level GST rate-wise breakup, kept separate from
      per-line-item tax fields (no consolidation)
    - Line items: `batch_number`, `expiry_date`, `mrp`, `ptr`, `rate_pts`
      (meaning unconfirmed), `quantity_sold`/`quantity_free`/
      `quantity_total`, `discount_amount`, `cgst_rate`/`sgst_rate`/
      `igst_rate` added alongside the existing `*_amount` fields. **Sold
      and Free quantities for the same product stay as separate line
      items** (confirmed), never merged into one row
    - Explicitly out of scope (client-confirmed): Terms & Conditions text,
      QR code content, "Adj. Details" — no extraction attempted
    - **Known fragility**: the confirmed column list has `"Total"` appear
      twice (the Quantity group's own sub-column, and a separate line
      amount column) — a flat keyword-substring column mapper can't
      disambiguate two columns with the literal same header text by
      content alone. Sidestepped in this module's own test PDF by labeling
      them `"Total Qty"`/`"Total Amt"`; if the real invoice's columns are
      truly both bare `"Total"`, this will misassign one to the other and
      needs positional (column-index) disambiguation once a real sample
      is available
  - CSV filename convention (per client confirmation):
    `CustomerName_DateOfInvoice_last5CharsOfIRN.csv` — `CustomerName` is
    `buyer_name`, falling back to `Customer_Unspecified` when not
    extracted; sanitized through `filename_safety.py` same as before
- `app/services/ocr_extraction.py` — OCR path for content with no native
  text layer: directly-uploaded JPEG/PNG/WEBP (always OCR'd, no text-layer
  concept for a plain image), and PDF pages `has_usable_text_layer` flags
  as scanned (rendered to an image via PyMuPDF at ~200 DPI, then OCR'd —
  pages that DO have a text layer are never OCR'd, for both speed and
  accuracy). Wired into `POST /api/jobs` via an injected callable
  (`ocr_page_text_fn`) rather than a direct import, avoiding a circular
  dependency between the two extraction modules.
  - **Reading order for tabular/form invoices**: uses
    `pytesseract.image_to_data()` (per-word bounding boxes) instead of
    plain `image_to_string()`, clustering words into rows by vertical
    position and joining each row left-to-right by horizontal position
    (`_group_words_into_rows`) — reconstructs "words on the same visual
    row" independent of Tesseract's own block/line segmentation, which
    matters on dense multi-column forms. Page segmentation mode is also
    explicitly set to `--psm 6` ("single uniform block of text", override
    via `OCR_PSM` env var) rather than Tesseract's own default (`psm 3`,
    full automatic segmentation) — **this PSM change alone was the
    difference between correctly ordered output and a genuinely broken
    one** in testing: at `psm 3`, a synthetic dense multi-column line-item
    table came back read *column-by-column* ("Description" then all 4
    descriptions, then "HSN" then all 4 HSN codes, ...) rather than
    row-by-row, which would silently scramble every label-value pairing.
    At `psm 6`, the same table read correctly in the right row order —
    and in every case tested, plain `image_to_string()` at `psm 6` already
    matched the `image_to_data()`-based reconstruction exactly. The
    row-reconstruction is still real, useful hardening (it doesn't depend
    on Tesseract's internal heuristics holding on noisier/skewed real
    scans the way `psm 6`'s built-in line grouping does), but its
    incremental benefit over plain `psm 6` output could not be
    demonstrated on the synthetic samples available in this repo — no
    real scanned/photographed invoice was available to test against.
  - **Preprocessing**: grayscale, contrast boost, and upscaling (to an
    estimated ~300 DPI-equivalent, since most uploaded
    photos/screenshots carry no DPI metadata to check) — isolated in
    `preprocess_image_for_ocr`, easy to tune further without touching the
    OCR call itself. A fixed-threshold binarization step was also tried
    (a common recommendation for scanned forms) and **measured to make
    things worse**: it fragmented cleanly-read repeated text into broken
    tokens on the test image, so it was left out rather than shipped on
    an unverified assumption. None of this preprocessing has been
    validated against a real scanned/photographed invoice — only
    synthetic test images.
  - **Confidence is always capped** for OCR-sourced fields: never `HIGH`
    (`REVIEW` at best), and the IRN specifically drops to `LOW` if it
    doesn't cleanly match the 64-hex-char shape — a malformed IRN from OCR
    is a strong signal of a misread character, not just generic
    uncertainty. Verified against real Tesseract output (not mocked): a
    64-char IRN is fragile even under good OCR conditions (a single
    stray inserted space breaks the match, correctly downgrading to
    `LOW`); everything else (dates, vendor name, amounts) read correctly
    at a normal font size and graded `REVIEW`.
  - **Known limitation: no line items or tax-bracket-summary from OCR.**
    Both come from PyMuPDF's `find_tables()` for native PDFs, which reads
    the PDF's actual vector/ruling-line object model — OCR's flat output
    text has no comparable structure. Reconstructing tables from OCR would
    need bounding-box layout analysis (`pytesseract.image_to_data`), a
    separate and meaningfully larger effort not attempted here.
    OCR-sourced invoices get header fields but always an empty
    `line_items` list, consistent with this codebase's existing
    "don't guess" philosophy for tables it can't confidently detect.

## Explicitly NOT yet implemented (next steps)

- Table/line-item reconstruction from OCR text (see the OCR known
  limitation above)
- Async job queue/worker (upload endpoint currently runs extraction
  synchronously inline; no queue/worker yet)
- Real auth (register/login/OTP via Supabase) — only a hardcoded-credential
  testing stopgap exists so far, see the warning section above
- Persistent database (in-memory store only — does not survive a restart)
- Export artifact cleanup/retention policy (see `export_storage.py` note above)
- Once OCR lands, delete the dummy-data generator/debug endpoint noted
  above under `csv_export.py`

## Design decisions worth knowing about

- **user_id on every Job from day one**, even before real auth existed, so
  ownership scoping didn't need to be retrofitted later — it now comes
  from the authenticated username (see auth section above). Default
  visibility model agreed: shared within an org, not fully public, not
  siloed per individual user — `GET /api/jobs` requires login but still
  returns all jobs, not just the caller's own; confirm before this changes.
- **Split value is a hard fence, not a merge scope.** If a split value is
  given, invoice-number-based grouping runs independently within each
  range; the same invoice number reappearing in a different range does
  NOT get merged across that boundary.
- **Merges are always flagged, never silent.** Any non-contiguous
  same-invoice-number merge sets `is_non_contiguous_merge=True` so the
  preview UI can ask the user to confirm — protects against silently
  inflating totals from an accidental duplicate scan.
