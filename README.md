# Zoho Books → NRS E-Invoicing Compliance (ENW Construction Limited)

Tools to browse Zoho Books documents and submit them to Nigeria's NRS/FIRS
e-invoicing platform via the Cryptware API, producing IRN + QR-stamped PDFs.

## Components

- **`app/`** — Zoho Books API explorer. A FastAPI app that merges every
  OpenAPI spec in `API/` into one live Swagger UI (`/docs`) and proxies GET
  requests to Zoho Books, handling the OAuth token exchange/refresh.

- **`dashboard/`** — Finance dashboard. Lists invoices, credit notes and
  debit notes from Zoho Books and posts them to NRS (single or bulk) with a
  one-click **Post** button. Persists per-document state (posted / error) and
  the full NRS request/response in SQLite, shows an error log, and generates
  downloadable **IRN + QR-code stamped PDFs** for successfully posted
  documents.

- **`API/`** — Zoho Books OpenAPI (`.yml`) specifications.

## Setup

Each app has its own `requirements.txt` and `.env.example`. Copy the example
to `.env`, fill in credentials, then:

```bash
cd app        # or dashboard
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000   # dashboard: --port 8600
```

`app/.env` needs a Zoho Self Client (Client ID/Secret) from the
[Zoho API Console](https://api-console.zoho.com), the org's Organization ID,
and a one-time grant token exchanged via `POST /auth/exchange` (see
`app/README.md`).

`dashboard/.env` needs the Cryptware NRS API key for this entity, per
environment (Test and Production are separate Cryptware systems — never mix
keys or base URLs across them).

## Field mapping (Zoho → NRS)

| NRS payload field | Zoho source |
|---|---|
| customer `tin` | contact **TRN** (`tax_reg_no`), falling back to Company ID |
| `business_description` | contact **Remarks**, falling back to ENW's own business description if unset |
| line `hsn_code` / `isic_code` | item master **HSN/SAC** field (`hsn_or_sac`) |

Phone numbers are normalized to E.164 and HSN codes to the `0000.00` format
automatically.

> **Note:** `.env` files (Zoho + NRS credentials), the local SQLite database
> and logs are git-ignored. Never commit real credentials.
