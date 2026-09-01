"""Zoho Books data access for the finance dashboard.

Reuses the OAuth handling already built and verified in ../app/zoho_auth.py
and the credentials/refresh token already stored in ../app/.env, so there's
a single source of truth for the Zoho connection instead of a second,
divergent auth setup.
"""

import os
import ssl
import sys
from pathlib import Path

import httpx
import truststore
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
from zoho_auth import ZohoAuth, ZohoAuthError  # noqa: E402

load_dotenv(APP_DIR / ".env")

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
ZOHO_ORGANIZATION_ID = os.getenv("ZOHO_ORGANIZATION_ID", "")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "") or None
ZOHO_ACCOUNTS_BASE = os.getenv("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.com")
ZOHO_API_BASE = os.getenv("ZOHO_API_BASE", "https://www.zohoapis.com/books/v3")

SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

auth = ZohoAuth(
    client_id=ZOHO_CLIENT_ID,
    client_secret=ZOHO_CLIENT_SECRET,
    accounts_base=ZOHO_ACCOUNTS_BASE,
    env_path=APP_DIR / ".env",
    refresh_token=ZOHO_REFRESH_TOKEN,
    ssl_context=SSL_CONTEXT,
)

_http = httpx.AsyncClient(timeout=30.0, verify=SSL_CONTEXT)


class ZohoDataError(RuntimeError):
    pass


async def _get(path: str, params: dict | None = None) -> dict:
    params = dict(params or {})
    params.setdefault("organization_id", ZOHO_ORGANIZATION_ID)
    token = await auth.get_access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    resp = await _http.get(f"{ZOHO_API_BASE}{path}", params=params, headers=headers)
    if resp.status_code >= 400:
        raise ZohoDataError(f"Zoho API error {resp.status_code}: {resp.text}")
    return resp.json()


async def aclose():
    await _http.aclose()
    await auth.aclose()


async def list_invoices(page=1, per_page=25, status=None, search_text=None):
    params = {"page": page, "per_page": per_page, "sort_column": "date", "sort_order": "D"}
    if status:
        params["status"] = status
    if search_text:
        params["search_text"] = search_text
    data = await _get("/invoices", params)
    return data.get("invoices", []), data.get("page_context", {})


async def get_invoice(invoice_id: str) -> dict:
    data = await _get(f"/invoices/{invoice_id}")
    return data.get("invoice", {})


async def list_credit_notes(page=1, per_page=25, status=None, search_text=None):
    params = {"page": page, "per_page": per_page, "sort_column": "date", "sort_order": "D"}
    if status:
        params["status"] = status
    if search_text:
        params["search_text"] = search_text
    data = await _get("/creditnotes", params)
    return data.get("creditnotes", []), data.get("page_context", {})


async def get_credit_note(creditnote_id: str) -> dict:
    data = await _get(f"/creditnotes/{creditnote_id}")
    return data.get("creditnote", {})


async def list_debit_notes(page=1, per_page=25, status=None, search_text=None):
    # Zoho Books models customer debit notes as invoices of Type.DebitNote -
    # there is no separate /debitnotes resource.
    params = {
        "page": page,
        "per_page": per_page,
        "filter_by": "Type.DebitNote",
        "sort_column": "date",
        "sort_order": "D",
    }
    if status:
        params["status"] = status
    if search_text:
        params["search_text"] = search_text
    data = await _get("/invoices", params)
    return data.get("invoices", []), data.get("page_context", {})


async def get_debit_note(debit_note_id: str) -> dict:
    data = await _get(f"/invoices/{debit_note_id}")
    return data.get("invoice", {})


async def get_organization() -> dict:
    data = await _get(f"/organizations/{ZOHO_ORGANIZATION_ID}")
    return data.get("organization", {})


async def get_contact(customer_id: str) -> dict:
    data = await _get(f"/contacts/{customer_id}")
    return data.get("contact", {})


async def get_item(item_id: str) -> dict:
    data = await _get(f"/items/{item_id}")
    return data.get("item", {})
