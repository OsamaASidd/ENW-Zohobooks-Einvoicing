"""Finance dashboard: browse Zoho Books invoices/credit notes/debit notes
for ENW Construction Limited, preview them in the company's print format, and
review-then-push them to NRS via Cryptware's e-invoicing API.
"""

import io
import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from xhtml2pdf import pisa

import nrs_mapping
import nrs_store
import zoho_data
from amount_words import amount_in_words_naira
from nrs_client import NRSAPIError, fetch_image_data_uri, generate_invoice, get_reference_data

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("dashboard")
logger.setLevel(logging.INFO)
_file_handler = logging.handlers.RotatingFileHandler(LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=3)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.addHandler(_file_handler)

LAGOS_TZ = ZoneInfo("Africa/Lagos")
SYNC_INTERVAL_SECONDS = 600  # 10-minute auto-sync


def _inject_sync_meta(request: Request) -> dict:
    """Make the last-synced timestamp + auto-sync interval available to every
    template (rendered in the top bar)."""
    return {
        "last_synced": nrs_store.get_meta("last_synced", ""),
        "sync_interval_seconds": SYNC_INTERVAL_SECONDS,
    }


def _mark_synced():
    nrs_store.set_meta("last_synced", datetime.now(LAGOS_TZ).strftime("%Y-%m-%d %H:%M:%S"))


app = FastAPI(title="ENW Construction Limited - Finance Dashboard")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates", context_processors=[_inject_sync_meta])


@app.exception_handler(Exception)
async def log_unhandled_exceptions(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        {"detail": "Something went wrong. This has been logged - check dashboard/logs/app.log or /nrs/errors."},
        status_code=500,
    )

INVOICE_STATUSES = ["draft", "sent", "viewed", "overdue", "partially_paid", "paid", "void", "unpaid"]
CREDIT_NOTE_STATUSES = ["draft", "open", "closed", "void"]

DOC_GET = {
    "invoice": zoho_data.get_invoice,
    "creditnote": zoho_data.get_credit_note,
    "debitnote": zoho_data.get_debit_note,
}

# TODO: bank details and signatory name still needed (not provided in the
# Cryptware onboarding entity info - source from an existing ENW invoice/
# letterhead sample, same as was done for Richardson). vat_no also unconfirmed
# - the Cryptware form only gave a TIN, which may or may not double as it.
COMPANY_STATIC = {
    "address_lines": [
        "NO 10 WOJI ROAD",
        "Port Harcourt, 500102, Nigeria",
    ],
    "phone": "+2348184444414",
    "tax_id": "2523389587154",
    "vat_no": "TODO",
    "bank_name": "TODO",
    "bank_account_number": "TODO",
    "bank_account_name": "ENW CONSTRUCTION LIMITED",
    "signatory_name": "TODO",
}
_org_cache: dict = {}


async def get_company_context() -> dict:
    if not _org_cache:
        try:
            org = await zoho_data.get_organization()
        except zoho_data.ZohoDataError:
            org = {}
        _org_cache["name"] = org.get("name", "ENW Construction Limited")
        _org_cache["email"] = org.get("email", "elie.raffoul@raffoulng.com")
        _org_cache["website"] = org.get("primary_domain_name", "")
    return {**COMPANY_STATIC, **_org_cache}


def _render_pdf(html: str) -> bytes:
    buf = io.BytesIO()
    pisa.CreatePDF(src=html, dest=buf, encoding="utf-8")
    return buf.getvalue()


async def _build_doc_context(doc_type: str, zoho_id: str):
    """Assemble the print context for a POSTED document: the Zoho document,
    company letterhead, computed totals/amount-in-words, and the NRS stamp
    (IRN + QR code) from the stored response. Returns None if the document
    hasn't been successfully posted to NRS yet."""
    state = nrs_store.get_state(doc_type, zoho_id)
    if not state or state.get("status") != "posted":
        return None

    doc = await DOC_GET[doc_type](zoho_id)
    company = await get_company_context()
    resp = json.loads(state.get("response_payload") or "{}")
    data = resp.get("data", {})

    qr_uri = ""
    if data.get("qr_code_url"):
        try:
            qr_uri = await fetch_image_data_uri(data["qr_code_url"])
        except Exception:
            logger.warning("Could not fetch QR image for %s %s", doc_type, zoho_id)

    nrs = {
        "irn": state.get("irn", ""),
        "status": data.get("status", ""),
        "qr_uri": qr_uri,
        "posted_at": state.get("submitted_at", ""),
    }

    tax_total = float(doc.get("tax_total", 0) or 0)
    sub_total = float(doc.get("sub_total", 0) or 0)
    vat_percentage = round(tax_total / sub_total * 100) if sub_total else None

    is_credit = doc_type == "creditnote"
    number = doc.get("creditnote_number") if is_credit else doc.get("invoice_number")
    return {
        "doc": doc, "company": company, "nrs": nrs, "doc_type": doc_type, "zoho_id": zoho_id,
        "number": number, "vat_percentage": vat_percentage,
        "amount_in_words": amount_in_words_naira(doc.get("total", 0) or 0),
        "title": "CREDIT NOTE" if is_credit else ("DEBIT NOTE" if doc_type == "debitnote" else "INVOICE"),
        "items_total_qty": sum(float(i.get("quantity", 0) or 0) for i in doc.get("line_items", [])),
    }


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/invoices")


@app.get("/nrs/reference-data/{kind}")
async def nrs_reference_data(kind: str):
    try:
        return await get_reference_data(kind)
    except NRSAPIError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)


# ------------------------------------------------------------- NRS posting

def _error_result(doc_type, zoho_id, detail, *, document_identifier="", customer_name="",
                  issue_date="", request_payload=None, response_payload=None):
    """Persist an error as both the document's latest state and an audit-log
    row (each with the request/response when available), then return the dict
    the list renders inline."""
    nrs_store.record_state(
        doc_type, zoho_id, status="error", error_message=detail,
        document_identifier=document_identifier, customer_name=customer_name, issue_date=issue_date,
        request_payload=request_payload, response_payload=response_payload,
    )
    nrs_store.log_error(
        doc_type, zoho_id, detail, document_identifier=document_identifier,
        customer_name=customer_name, request_payload=request_payload, response_payload=response_payload,
    )
    return {"zoho_id": zoho_id, "document_identifier": document_identifier,
            "customer_name": customer_name, "outcome": "error", "detail": detail}


async def _enrich_line_codes(doc: dict):
    """A Zoho invoice line stores a snapshot taken when the line was created,
    so a later HSN/ISIC code added to the item master doesn't appear on the
    line. For any line whose Description is empty, backfill it from the item
    master's Description (where Finance enters the code)."""
    cache = {}
    for li in doc.get("line_items", []):
        if (li.get("description") or "").strip():
            continue
        item_id = li.get("item_id")
        if not item_id:
            continue
        if item_id not in cache:
            try:
                cache[item_id] = await zoho_data.get_item(item_id)
            except zoho_data.ZohoDataError:
                cache[item_id] = {}
        li["description"] = cache[item_id].get("description", "") or ""


async def _nrs_post_one(doc_type: str, zoho_id: str) -> dict:
    """Post a single document straight to NRS using the auto-mapping from
    Zoho - no preview, no review step. Every outcome (posted or error) is
    persisted to the DB with the exact request sent and the raw response
    received, so state survives reloads. Anything missing a compliance-
    required field is recorded as an error rather than submitted incomplete."""
    try:
        doc = await DOC_GET[doc_type](zoho_id)
    except Exception as e:
        logger.exception("Failed to load %s %s from Zoho", doc_type, zoho_id)
        return _error_result(doc_type, zoho_id, f"Could not load from Zoho: {e}")

    customer = {}
    if doc.get("customer_id"):
        try:
            customer = await zoho_data.get_contact(doc["customer_id"])
        except zoho_data.ZohoDataError:
            customer = {}
    await _enrich_line_codes(doc)
    suggestion = nrs_mapping.suggest_payload(doc_type, doc, customer)

    if doc_type != "invoice":
        # NRS cancel_references: each original invoice this note credits must
        # already be posted to NRS (so it has an IRN). Zoho lists them in
        # `invoices_credited`; match each to its stored IRN by the invoice's
        # Zoho id.
        credited = doc.get("invoices_credited") or []
        refs, unposted = [], []
        for inv in credited:
            inv_id = inv.get("invoice_id")
            st = nrs_store.get_state("invoice", inv_id) if inv_id else None
            if st and st.get("status") == "posted" and st.get("irn"):
                refs.append({"original_irn": st["irn"], "original_issue_date": st.get("issue_date", "")})
            else:
                unposted.append(inv.get("invoice_number") or inv_id or "?")
        # Reference whichever credited invoices are already posted; only
        # block if none are (nothing to reference at all).
        if refs:
            suggestion["cancel_references"] = refs
            if unposted:
                logger.info("Credit/debit %s references %d/%d credited invoices "
                            "(not yet posted: %s)", zoho_id, len(refs), len(credited), ", ".join(unposted))
        elif credited:
            suggestion["_cancel_ref_note"] = (
                "none of the credited invoice(s) are posted to NRS yet: " + ", ".join(unposted)
                + " - post at least one so its IRN can be referenced")
        else:
            suggestion["_cancel_ref_note"] = (
                "no original invoice linked in Zoho (invoices_credited is empty)")

    document_identifier = suggestion.get("document_identifier", "")
    customer_name = suggestion.get("customer", {}).get("party_name", "")
    issue_date = suggestion.get("issue_date", "")

    missing = nrs_mapping.missing_required_fields(doc_type, suggestion)
    if missing:
        reason = "Missing required field(s): " + ", ".join(missing)
        return _error_result(doc_type, zoho_id, reason, document_identifier=document_identifier,
                             customer_name=customer_name, issue_date=issue_date, request_payload=suggestion)

    payload = nrs_mapping.payload_from_suggestion(doc_type, suggestion)
    try:
        result = await generate_invoice(payload)
    except NRSAPIError as e:
        detail = e.body.get("message") if isinstance(e.body, dict) else e.body
        logger.warning("NRS rejected %s %s: %s", doc_type, zoho_id, detail)
        return _error_result(doc_type, zoho_id, f"NRS rejected: {detail}", document_identifier=document_identifier,
                             customer_name=customer_name, issue_date=issue_date,
                             request_payload=payload, response_payload=e.body)
    except Exception as e:
        logger.exception("Post failed for %s %s", doc_type, zoho_id)
        return _error_result(doc_type, zoho_id, str(e), document_identifier=document_identifier,
                             customer_name=customer_name, issue_date=issue_date, request_payload=payload)

    data = result.get("data", {})
    nrs_store.record_state(
        doc_type, zoho_id, status="posted",
        irn=data.get("irn", ""), nrs_id=data.get("id", ""),
        document_identifier=payload["document_identifier"], issue_date=payload["issue_date"],
        customer_name=customer_name, request_payload=payload, response_payload=result,
    )
    return {"zoho_id": zoho_id, "document_identifier": document_identifier, "customer_name": customer_name,
            "outcome": "posted", "detail": data.get("irn", ""), "nrs_status": data.get("status", "")}


@app.post("/nrs/post")
async def nrs_post(request: Request):
    """Direct-post one or more documents. Drives both the single-row Post
    button and the bulk 'Post selected' action; returns JSON so the list
    page can show each result (IRN or error) inline."""
    form = await request.form()
    doc_type = form.get("doc_type")
    ids = form.getlist("ids")
    if doc_type not in DOC_GET:
        return JSONResponse({"detail": "invalid doc_type"}, status_code=400)
    results = [await _nrs_post_one(doc_type, zoho_id) for zoho_id in ids]
    return JSONResponse({"results": results})


@app.get("/nrs/errors")
async def nrs_errors_page(request: Request):
    errors = nrs_store.list_errors()
    return templates.TemplateResponse(
        request, "nrs_errors.html", {"request": request, "errors": errors, "active_tab": "errors"},
    )


# ------------------------------------------ posted document PDF (IRN + QR)

_PLURAL_TO_SINGULAR = {"invoices": "invoice", "creditnotes": "creditnote", "debitnotes": "debitnote"}


def _pdf_template(doc_type: str) -> str:
    return "creditnote_pdf.html" if doc_type == "creditnote" else "invoice_pdf.html"


_NOT_POSTED_HTML = (
    "<div style='font-family:sans-serif;padding:40px;color:#333'>"
    "<h3>No e-invoice PDF yet</h3><p>This document hasn't been successfully posted "
    "to NRS, so there's no IRN/QR code to stamp. Post it first, then come back.</p></div>"
)


@app.get("/{plural}/{zoho_id}/view")
async def doc_view(request: Request, plural: str, zoho_id: str):
    doc_type = _PLURAL_TO_SINGULAR.get(plural)
    if not doc_type:
        return HTMLResponse("Unknown document type", status_code=404)
    ctx = await _build_doc_context(doc_type, zoho_id)
    if ctx is None:
        return HTMLResponse(_NOT_POSTED_HTML, status_code=404)
    html = templates.env.get_template(_pdf_template(doc_type)).render(web=True, plural=plural, **ctx)
    return HTMLResponse(html)


@app.get("/{plural}/{zoho_id}/pdf")
async def doc_pdf(request: Request, plural: str, zoho_id: str):
    doc_type = _PLURAL_TO_SINGULAR.get(plural)
    if not doc_type:
        return JSONResponse({"detail": "Unknown document type"}, status_code=404)
    ctx = await _build_doc_context(doc_type, zoho_id)
    if ctx is None:
        return JSONResponse({"detail": "Document not posted to NRS yet."}, status_code=404)
    html = templates.env.get_template(_pdf_template(doc_type)).render(web=False, plural=plural, **ctx)
    pdf_bytes = _render_pdf(html)
    filename = f"{(ctx['number'] or zoho_id)}.pdf".replace("/", "-").replace("\\", "-")
    return Response(
        pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_JSON_VIEW = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
 body{font-family:Menlo,Consolas,monospace;background:#0f1720;color:#d6e2f0;margin:0;padding:20px;}
 h3{font-family:Arial,sans-serif;color:#fff;margin:0 0 4px;}
 .sub{font-family:Arial,sans-serif;color:#7f95ad;font-size:12px;margin-bottom:14px;}
 button{font-family:Arial,sans-serif;padding:6px 12px;margin-bottom:12px;cursor:pointer;border:1px solid #33455c;
   background:#1b2735;color:#d6e2f0;border-radius:4px;}
 pre{white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.5;background:#141d29;
   padding:16px;border-radius:6px;border:1px solid #22303f;}
</style></head><body>
 <h3>__TITLE__</h3><div class="sub">__SUB__</div>
 <button onclick="navigator.clipboard.writeText(document.getElementById('j').innerText)">Copy JSON</button>
 <pre id="j">__BODY__</pre>
</body></html>"""


@app.get("/{plural}/{zoho_id}/payload/{kind}")
async def doc_payload(plural: str, zoho_id: str, kind: str):
    """Show the exact JSON we sent to NRS (request) or got back (response)
    for a document, straight from what's persisted in the DB."""
    import html as _html

    doc_type = _PLURAL_TO_SINGULAR.get(plural)
    if not doc_type or kind not in ("request", "response"):
        return HTMLResponse("Not found", status_code=404)
    st = nrs_store.get_state(doc_type, zoho_id)
    if not st:
        return HTMLResponse("<p style='font-family:sans-serif;padding:30px'>No NRS attempt recorded for this document yet.</p>", status_code=404)

    raw = st.get("request_payload" if kind == "request" else "response_payload")
    if not raw:
        return HTMLResponse(
            f"<p style='font-family:sans-serif;padding:30px'>No {kind} stored for this document "
            f"(state: {st.get('status')}).</p>", status_code=404)

    try:
        pretty = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        pretty = str(raw)

    title = f"NRS {kind.title()} — {st.get('document_identifier') or zoho_id}"
    sub = f"{doc_type} · status: {st.get('status')} · {st.get('submitted_at', '')}"
    page = (_JSON_VIEW.replace("__TITLE__", _html.escape(title))
            .replace("__SUB__", _html.escape(sub))
            .replace("__BODY__", _html.escape(pretty)))
    return HTMLResponse(page)


def _nrs_states(doc_type: str, rows: list, id_key: str) -> dict:
    """Latest persisted NRS state per row (posted/error), keyed by row id,
    so the list shows real state on load instead of a fresh Post button."""
    ids = [str(r.get(id_key)) for r in rows if r.get(id_key)]
    return nrs_store.get_states(doc_type, ids)


# ---------------------------------------------------------------- invoices

@app.get("/invoices")
async def invoices_list(request: Request, page: int = 1, status: str = "", q: str = ""):
    try:
        rows, page_context = await zoho_data.list_invoices(page=page, status=status or None, search_text=q or None)
        error = None
        _mark_synced()
    except zoho_data.ZohoDataError as e:
        rows, page_context, error = [], {}, str(e)

    columns = [
        {"key": "invoice_number", "label": "Invoice #"},
        {"key": "customer_name", "label": "Customer"},
        {"key": "date", "label": "Date"},
        {"key": "due_date", "label": "Due Date"},
        {"key": "total", "label": "Total", "money": True, "numeric": True},
        {"key": "balance", "label": "Balance", "money": True, "numeric": True},
        {"key": "status", "label": "Status"},
    ]
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "request": request, "active_tab": "invoices", "doc_type": "invoices", "doc_type_singular": "invoice",
            "doc_label": "Invoices", "id_key": "invoice_id", "columns": columns,
            "rows": rows, "page_context": page_context, "status_filter": status, "q": q,
            "statuses": INVOICE_STATUSES, "error": error, "nrs_states": _nrs_states("invoice", rows, "invoice_id"),
        },
    )


# ------------------------------------------------------------ credit notes

@app.get("/creditnotes")
async def creditnotes_list(request: Request, page: int = 1, status: str = "", q: str = ""):
    try:
        rows, page_context = await zoho_data.list_credit_notes(page=page, status=status or None, search_text=q or None)
        error = None
        _mark_synced()
    except zoho_data.ZohoDataError as e:
        rows, page_context, error = [], {}, str(e)

    columns = [
        {"key": "creditnote_number", "label": "Credit Note #"},
        {"key": "customer_name", "label": "Customer"},
        {"key": "date", "label": "Date"},
        {"key": "total", "label": "Total", "money": True, "numeric": True},
        {"key": "balance", "label": "Credits Remaining", "money": True, "numeric": True},
        {"key": "status", "label": "Status"},
    ]
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "request": request, "active_tab": "creditnotes", "doc_type": "creditnotes", "doc_type_singular": "creditnote",
            "doc_label": "Credit Notes", "id_key": "creditnote_id", "columns": columns,
            "rows": rows, "page_context": page_context, "status_filter": status, "q": q,
            "statuses": CREDIT_NOTE_STATUSES, "error": error, "nrs_states": _nrs_states("creditnote", rows, "creditnote_id"),
        },
    )


# ------------------------------------------------------------- debit notes

@app.get("/debitnotes")
async def debitnotes_list(request: Request, page: int = 1, status: str = "", q: str = ""):
    try:
        rows, page_context = await zoho_data.list_debit_notes(page=page, status=status or None, search_text=q or None)
        error = None
        _mark_synced()
    except zoho_data.ZohoDataError as e:
        rows, page_context, error = [], {}, str(e)

    columns = [
        {"key": "invoice_number", "label": "Debit Note #"},
        {"key": "customer_name", "label": "Customer"},
        {"key": "date", "label": "Date"},
        {"key": "total", "label": "Total", "money": True, "numeric": True},
        {"key": "balance", "label": "Balance", "money": True, "numeric": True},
        {"key": "status", "label": "Status"},
    ]
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "request": request, "active_tab": "debitnotes", "doc_type": "debitnotes", "doc_type_singular": "debitnote",
            "doc_label": "Debit Notes", "id_key": "invoice_id", "columns": columns,
            "rows": rows, "page_context": page_context, "status_filter": status, "q": q,
            "statuses": INVOICE_STATUSES, "error": error, "nrs_states": _nrs_states("debitnote", rows, "invoice_id"),
        },
    )


@app.on_event("shutdown")
async def on_shutdown():
    await zoho_data.aclose()
    from nrs_client import aclose as nrs_aclose
    await nrs_aclose()
