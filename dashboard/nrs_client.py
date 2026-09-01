"""Client for Cryptware's NRS e-invoicing API, based on the
"Cryptware NRS-Einvoicing API.postman_collection.json" export dropped into
the project root. Auth is a static `x-api-key` header (not the bearerAuth
JWT option also advertised in the public Swagger shell).
"""

import os
import ssl
from pathlib import Path

import httpx
import truststore
from dotenv import load_dotenv

DASHBOARD_DIR = Path(__file__).resolve().parent
load_dotenv(DASHBOARD_DIR / ".env")

NRS_BASE_URL = os.getenv("NRS_BASE_URL", "https://preprod-api.cryptwaresystemsltd.com").rstrip("/")
NRS_API_KEY = os.getenv("NRS_API_KEY", "")

SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_http = httpx.AsyncClient(timeout=30.0, verify=SSL_CONTEXT)

# invoice_type_code per Cryptware's payload spec.
DOC_TYPE_CODE = {"invoice": "381", "creditnote": "380", "debitnote": "384"}


class NRSAPIError(RuntimeError):
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"NRS API error {status_code}: {body}")


async def aclose():
    await _http.aclose()


async def fetch_image_data_uri(url: str) -> str:
    """Download an image (e.g. the NRS QR code PNG) and return it as a
    base64 data URI, so it embeds directly in the PDF/HTML without the
    renderer needing to make its own network/SSL call."""
    import base64
    resp = await _http.get(url)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "image/png")
    b64 = base64.b64encode(resp.content).decode()
    return f"data:{content_type};base64,{b64}"


def _headers():
    return {"x-api-key": NRS_API_KEY}


async def _request(method: str, path: str, **kwargs) -> dict:
    resp = await _http.request(method, f"{NRS_BASE_URL}{path}", headers=_headers(), **kwargs)
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        raise NRSAPIError(resp.status_code, body)
    return resp.json()


async def generate_invoice(payload: dict) -> dict:
    """POST /invoice/generate - used for invoices, credit notes and debit
    notes alike; only invoice_type_code (and cancel_references for
    credit/debit notes) differ."""
    return await _request("POST", "/invoice/generate", json=payload)


async def get_invoice(irn: str) -> dict:
    return await _request("GET", f"/invoice/{irn}")


async def get_invoice_status(irn: str) -> dict:
    return await _request("GET", f"/invoice/{irn}/status")


async def cancel_invoice(irn: str) -> dict:
    return await _request("PATCH", f"/invoice/{irn}/cancel")


async def update_payment_status(irn: str, payment_status: str, reference: str = None, amount: float = None) -> dict:
    body = {"payment_status": payment_status}
    if reference:
        body["reference"] = reference
    if amount is not None:
        body["amount"] = amount
    return await _request("PATCH", f"/invoice/{irn}", json=body)


async def get_reference_data(kind: str) -> dict:
    """kind: countries | tax-categories | payment-means | invoice-types |
    service-codes | hs-codes | currencies | uom"""
    return await _request("GET", f"/reference-data/{kind}")
