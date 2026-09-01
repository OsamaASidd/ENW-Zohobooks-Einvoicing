"""Best-effort mapping from a Zoho document to an NRS submission form.

Only fills in fields we can derive with confidence (dates, amounts,
currency, document numbers, PO/reference numbers, tax rate already on the
Zoho line item). Compliance-critical fields Zoho doesn't currently carry
for this organization - customer TIN, business description, product/service
tax classification (HSN/ISIC codes) - are left blank and marked required in
the review form rather than guessed.
"""

import re

from nrs_client import DOC_TYPE_CODE

_INVALID_IDENTIFIER_CHARS = re.compile(r"[^A-Za-z0-9-]+")

# Fallback when the customer's Zoho contact has no "Remarks" (nature of
# business) set. Per client instruction, defaults to ENW's own business
# description rather than leaving every invoice stuck on a missing-field
# error - override per-customer in Zoho's Remarks field when known.
_DEFAULT_BUSINESS_DESCRIPTION = "Construction of roads and railways"


def _sanitize_identifier(value: str) -> str:
    """NRS document_identifier only allows letters, numbers and hyphens -
    Zoho invoice/credit-note numbers often contain slashes (e.g.
    LFZ/16769/07/2026)."""
    return _INVALID_IDENTIFIER_CHARS.sub("-", value or "").strip("-")


def _normalize_hsn(code: str) -> str:
    """NRS requires HSN codes as 0000.00 (4 digits, dot, exactly 2 decimals).
    Zoho stores them loosely (e.g. 6403.4 or 6403). Pad the decimal part to
    two digits when the value looks like an HSN; otherwise leave it untouched
    so NRS validates and reports anything genuinely malformed."""
    code = (code or "").strip()
    m = re.match(r"^(\d{4})\.?(\d{0,2})$", code)
    if m:
        return f"{m.group(1)}.{m.group(2).ljust(2, '0')}"
    return code


def _sanitize_phone(value: str) -> str:
    """NRS requires E.164 telephone format: a leading '+' followed by digits
    only. Zoho stores numbers with dashes/spaces (e.g. +234-803 530 0995)."""
    value = (value or "").strip()
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    return "+" + digits if digits else ""

_UOM_GUESSES = {
    "each": "EA", "ea": "EA", "unit": "EA", "units": "EA", "": "EA",
    "pcs": "PC", "pc": "PC", "piece": "PC", "pieces": "PC",
    "kg": "KG", "kgs": "KG", "kilogram": "KG", "kilograms": "KG",
}

_NUMBER_FIELD = {"invoice": "invoice_number", "creditnote": "creditnote_number", "debitnote": "invoice_number"}


def _guess_uom(unit: str) -> str:
    return _UOM_GUESSES.get((unit or "").strip().lower(), "EA")


def _guess_tax_category(tax_percentage) -> str:
    try:
        return "ZERO_VAT" if not float(tax_percentage or 0) else "STANDARD_VAT"
    except (TypeError, ValueError):
        return "STANDARD_VAT"


def suggest_lines(doc: dict) -> list:
    lines = []
    for li in doc.get("line_items", []):
        tax_pct = li.get("tax_percentage") or 0
        name = li.get("name") or ""
        desc = (li.get("description") or "").strip()
        # ENW's Zoho items carry the NRS tax classification code in the
        # native HSN/SAC field (li["hsn_or_sac"], e.g. "4210") rather than
        # the free-text Description field. Goes to isic_code for services and
        # hsn_code for goods (NRS accepts one or the other per line); the
        # item name fills the required companion category field.
        code = (li.get("hsn_or_sac") or "").strip()
        is_service = li.get("product_type") == "service" or li.get("item_type") == "service"
        line = {
            "description": name or desc,
            "invoiced_quantity": li.get("quantity", ""),
            "price_amount": li.get("rate", ""),
            "price_unit": _guess_uom(li.get("unit")),
            "tax_rate": tax_pct,
            "tax_category_id": _guess_tax_category(tax_pct),
            "discount_rate": 0,
            "hsn_code": "", "product_category": "",
            "isic_code": "", "service_category": "",
        }
        if is_service:
            line["isic_code"] = code
            line["service_category"] = name
        else:
            line["hsn_code"] = _normalize_hsn(code)
            line["product_category"] = name
        lines.append(line)
    return lines


def suggest_payload(doc_type: str, doc: dict, customer: dict) -> dict:
    billing = customer.get("billing_address") or {}
    return {
        "document_identifier": _sanitize_identifier(doc.get(_NUMBER_FIELD[doc_type], "")),
        "invoice_type": "STANDARD",
        "issue_date": doc.get("date", ""),
        "due_date": doc.get("due_date", ""),
        "invoice_type_code": DOC_TYPE_CODE[doc_type],
        "payment_status": "PAID" if doc.get("status") == "paid" else "PENDING",
        "document_currency_code": doc.get("currency_code", "NGN"),
        "tax_currency_code": doc.get("currency_code", "NGN"),
        "transaction_category": "B2B",
        "buyer_reference": "",
        "order_reference": doc.get("reference_number", ""),
        "note": "",
        "customer": {
            "party_name": doc.get("customer_name", ""),
            "email": customer.get("email", ""),
            "telephone": _sanitize_phone(customer.get("phone", "")),
            # ENW's Zoho contacts carry the customer's NRS TIN in the native
            # TRN field (contact.tax_reg_no). Fall back to Company ID / tax_id
            # if TRN isn't set on a given contact.
            "tin": customer.get("tax_reg_no") or customer.get("company_id") or customer.get("tax_id", ""),
            # Finance keeps the customer's nature-of-business in Zoho's contact
            # "Remarks" field (returned as `notes`).
            "business_description": customer.get("notes") or _DEFAULT_BUSINESS_DESCRIPTION,
            "street_name": billing.get("address", ""),
            "city_name": billing.get("city", ""),
            "postal_zone": billing.get("zip", ""),
            "country": billing.get("country_code") or "NG",
        },
        "lines": suggest_lines(doc),
    }


_LINE_FIELDS = [
    "description", "quantity", "price_amount", "price_unit", "tax_rate",
    "tax_category_id", "discount_rate", "hsn_code", "product_category",
    "isic_code", "service_category",
]


def parse_lines_from_form(form) -> list:
    """form is a Starlette FormData - repeated `ln_<field>` inputs (one per
    table row, in DOM order) are zipped back together into line dicts."""
    lists = {f: form.getlist(f"ln_{f}") for f in _LINE_FIELDS}
    count = len(lists["description"])
    lines = []
    for i in range(count):
        row = {f: (lists[f][i] if i < len(lists[f]) else "") for f in _LINE_FIELDS}
        if not row["description"].strip():
            continue
        lines.append({
            "description": row["description"],
            "invoiced_quantity": row["quantity"],
            "price_amount": row["price_amount"],
            "price_unit": row["price_unit"],
            "tax_rate": row["tax_rate"],
            "tax_category_id": row["tax_category_id"],
            "discount_rate": row["discount_rate"],
            "hsn_code": row["hsn_code"],
            "product_category": row["product_category"],
            "isic_code": row["isic_code"],
            "service_category": row["service_category"],
        })
    return lines


def form_to_review_payload(form, lines: list) -> dict:
    """Reshapes a submitted (and possibly rejected) form back into the
    nested structure nrs_review.html expects, so re-rendering after an NRS
    error shows the user exactly what they submitted instead of losing it."""
    return {
        "document_identifier": form.get("document_identifier", ""),
        "invoice_type": form.get("invoice_type", "STANDARD"),
        "issue_date": form.get("issue_date", ""),
        "due_date": form.get("due_date", ""),
        "payment_status": form.get("payment_status", "PENDING"),
        "document_currency_code": form.get("document_currency_code", "NGN"),
        "tax_currency_code": form.get("tax_currency_code", "NGN"),
        "transaction_category": form.get("transaction_category", "B2B"),
        "buyer_reference": form.get("buyer_reference", ""),
        "order_reference": form.get("order_reference", ""),
        "note": form.get("note", ""),
        "original_irn": form.get("original_irn", ""),
        "original_issue_date": form.get("original_issue_date", ""),
        "customer": {
            "party_name": form.get("party_name", ""),
            "email": form.get("email", ""),
            "telephone": form.get("telephone", ""),
            "tin": form.get("tin", ""),
            "business_description": form.get("business_description", ""),
            "street_name": form.get("street_name", ""),
            "city_name": form.get("city_name", ""),
            "postal_zone": form.get("postal_zone", ""),
            "country": form.get("country", ""),
        },
        "lines": lines,
    }


def _build_nrs_lines(lines: list) -> list:
    result = []
    for ln in lines:
        item = {
            "description": ln["description"],
            "invoiced_quantity": float(ln["invoiced_quantity"]),
            "price_amount": float(ln["price_amount"]),
            "price_unit": ln["price_unit"],
            "tax_rate": float(ln["tax_rate"]),
            "tax_category_id": ln["tax_category_id"],
        }
        if ln.get("discount_rate"):
            item["discount_rate"] = float(ln["discount_rate"])
        if ln.get("hsn_code"):
            item["hsn_code"] = ln["hsn_code"]
            if ln.get("product_category"):
                item["product_category"] = ln["product_category"]
        if ln.get("isic_code"):
            item["isic_code"] = ln["isic_code"]
            if ln.get("service_category"):
                item["service_category"] = ln["service_category"]
        result.append(item)
    return result


def missing_required_fields(doc_type: str, suggestion: dict) -> list:
    """Fields NRS requires that the auto-suggested payload couldn't fill in
    from Zoho. Used to skip (rather than blindly submit) bulk-posted
    documents that haven't been reviewed/completed."""
    missing = []
    customer = suggestion.get("customer", {})
    if not customer.get("tin"):
        missing.append("customer TIN")
    if not customer.get("business_description"):
        missing.append("business description")
    if not customer.get("street_name"):
        missing.append("customer street address")
    if not customer.get("city_name"):
        missing.append("customer city")
    if not customer.get("email"):
        missing.append("customer email")
    lines = suggestion.get("lines", [])
    if not lines:
        missing.append("line items")
    for i, ln in enumerate(lines, 1):
        if not ln.get("hsn_code") and not ln.get("isic_code"):
            missing.append(f"line {i}: HSN or ISIC code")
    if doc_type != "invoice" and not suggestion.get("cancel_references") and not suggestion.get("original_irn"):
        note = suggestion.get("_cancel_ref_note") or "original invoice IRN (cancel_references)"
        missing.append(note)
    return missing


def payload_from_suggestion(doc_type: str, suggestion: dict) -> dict:
    """Same shape as build_final_payload, but built straight from a
    suggest_payload() dict instead of a submitted form - used by bulk
    posting, which has no per-item review step."""
    customer = suggestion["customer"]
    payload = {
        "document_identifier": _sanitize_identifier(suggestion["document_identifier"]),
        "invoice_type": suggestion["invoice_type"],
        "issue_date": suggestion["issue_date"],
        "invoice_type_code": DOC_TYPE_CODE[doc_type],
        "payment_status": suggestion["payment_status"],
        "document_currency_code": suggestion["document_currency_code"],
        "tax_currency_code": suggestion["tax_currency_code"],
        "transaction_category": suggestion["transaction_category"],
        "accounting_customer_party": {
            "party_name": customer["party_name"],
            "email": customer["email"],
            "tin": customer["tin"],
            "business_description": customer["business_description"],
            "postal_address": {
                "street_name": customer["street_name"],
                "city_name": customer["city_name"],
                "country": customer["country"],
            },
        },
        "invoice_lines": _build_nrs_lines(suggestion["lines"]),
    }
    if customer.get("telephone"):
        payload["accounting_customer_party"]["telephone"] = customer["telephone"]
    if customer.get("postal_zone"):
        payload["accounting_customer_party"]["postal_address"]["postal_zone"] = customer["postal_zone"]
    if suggestion.get("due_date"):
        payload["due_date"] = suggestion["due_date"]
    if suggestion.get("buyer_reference"):
        payload["buyer_reference"] = suggestion["buyer_reference"]
    if suggestion.get("order_reference"):
        payload["order_reference"] = suggestion["order_reference"]
    if suggestion.get("note"):
        payload["note"] = suggestion["note"]
    if suggestion.get("cancel_references"):
        payload["cancel_references"] = suggestion["cancel_references"]
    elif suggestion.get("original_irn"):
        payload["cancel_references"] = [{
            "original_irn": suggestion["original_irn"],
            "original_issue_date": suggestion.get("original_issue_date", ""),
        }]
    return payload


def build_final_payload(doc_type: str, form: dict, lines: list) -> dict:
    """Builds the actual NRS API payload straight from the submitted review
    form (the form, not Zoho, is the source of truth at submit time - it's
    what the user actually reviewed)."""
    payload = {
        "document_identifier": _sanitize_identifier(form["document_identifier"]),
        "invoice_type": form["invoice_type"],
        "issue_date": form["issue_date"],
        "invoice_type_code": DOC_TYPE_CODE[doc_type],
        "payment_status": form["payment_status"],
        "document_currency_code": form["document_currency_code"],
        "tax_currency_code": form["tax_currency_code"],
        "transaction_category": form["transaction_category"],
        "accounting_customer_party": {
            "party_name": form["party_name"],
            "email": form["email"],
            "tin": form["tin"],
            "business_description": form["business_description"],
            "postal_address": {
                "street_name": form["street_name"],
                "city_name": form["city_name"],
                "country": form["country"],
            },
        },
        "invoice_lines": _build_nrs_lines(lines),
    }
    if form.get("telephone"):
        payload["accounting_customer_party"]["telephone"] = form["telephone"]
    if form.get("postal_zone"):
        payload["accounting_customer_party"]["postal_address"]["postal_zone"] = form["postal_zone"]
    if form.get("due_date"):
        payload["due_date"] = form["due_date"]
    if form.get("buyer_reference"):
        payload["buyer_reference"] = form["buyer_reference"]
    if form.get("order_reference"):
        payload["order_reference"] = form["order_reference"]
    if form.get("note"):
        payload["note"] = form["note"]
    if form.get("original_irn"):
        payload["cancel_references"] = [{
            "original_irn": form["original_irn"],
            "original_issue_date": form.get("original_issue_date", ""),
        }]
    return payload
