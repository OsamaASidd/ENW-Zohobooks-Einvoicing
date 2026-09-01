# Zoho Books API Explorer

A live Swagger UI generated from every OpenAPI file in [`../API`](../API), backed
by a FastAPI proxy. GET operations are wired to real calls against your Zoho
Books organization; other HTTP methods are shown for reference only (this
explorer is read-only by design) and return `501` if invoked.

## Setup

```bash
cd app
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/docs.

## Credentials

Already configured in `.env` for this run (client id/secret, organization id,
and a refresh token minted from your grant token). `organization_id` is
auto-filled on every call from `ZOHO_ORGANIZATION_ID` if you leave it blank in
Swagger UI.

If auth ever breaks (401s, or `/auth/status` shows `has_refresh_token: false`):

1. Generate a fresh grant token in the
   [Zoho API Console](https://api-console.zoho.com) → your Self Client → Generate Code,
   using scope `ZohoBooks.fullaccess.all` (or narrower, per-module scopes).
2. Either:
   - Paste it into `ZOHO_GRANT_TOKEN` in `.env` and restart the server, or
   - `POST /auth/exchange` with body `{"grant_token": "..."}` from Swagger UI itself.

Grant tokens are single-use and expire within minutes, so do this right before
using it. The resulting refresh token is long-lived and gets saved back into
`.env` automatically.

## How it works

- `spec_builder.py` parses all 41 `../API/*.yml` files and merges them into
  one OpenAPI document, namespacing each file's `components` (schemas,
  parameters, etc.) so identically-named definitions across resources don't
  collide.
- `zoho_auth.py` handles the OAuth2 dance: one-time grant-token exchange,
  then transparent access-token refresh using the stored refresh token.
- `main.py` overrides FastAPI's OpenAPI schema with the merged document (so
  `/docs` shows every endpoint from the Zoho Books spec, including
  descriptions/parameters/examples), and registers one real route per GET
  path that proxies the request straight through to
  `https://www.zohoapis.com/books/v3`, injecting the `Zoho-oauthtoken` auth
  header and a default `organization_id`.
