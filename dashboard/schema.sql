-- Finance Dashboard local database (SQLite).
--
-- This is NOT a cache of Zoho data - invoices/credit notes/debit notes are
-- always fetched live from Zoho Books, which remains the source of truth.
-- This tracks NRS submission state per document: the current outcome
-- (posted or error), the exact request sent, and the raw response received.

-- Latest persistent state per document (upserted on doc_type + zoho_id).
-- Survives restarts and page reloads, so the list shows each document's
-- real NRS state (Posted + IRN, or Error + message) instead of resetting
-- to an un-posted button. Holds BOTH successful posts and error states.
CREATE TABLE IF NOT EXISTS nrs_submissions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type             TEXT NOT NULL CHECK (doc_type IN ('invoice', 'creditnote', 'debitnote')),
    zoho_id              TEXT NOT NULL,
    document_identifier  TEXT NOT NULL,
    customer_name        TEXT,
    issue_date           TEXT,
    irn                  TEXT,
    nrs_id               TEXT,
    status               TEXT NOT NULL,             -- 'posted' | 'error'
    error_message        TEXT,                      -- populated when status = 'error'
    request_payload      TEXT,                      -- exact JSON body sent to NRS
    response_payload     TEXT,                      -- raw NRS response (success data or rejection body)
    submitted_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (doc_type, zoho_id)
);

CREATE INDEX IF NOT EXISTS idx_nrs_submissions_customer ON nrs_submissions (customer_name);
CREATE INDEX IF NOT EXISTS idx_nrs_submissions_doc_type ON nrs_submissions (doc_type);
CREATE INDEX IF NOT EXISTS idx_nrs_submissions_status ON nrs_submissions (status);
CREATE INDEX IF NOT EXISTS idx_nrs_submissions_irn ON nrs_submissions (irn);

-- Append-only history of every failed attempt (validation rejections,
-- network errors, missing-required-field skips) - a full audit trail
-- alongside the latest-state row above.
CREATE TABLE IF NOT EXISTS nrs_errors (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type             TEXT NOT NULL CHECK (doc_type IN ('invoice', 'creditnote', 'debitnote')),
    zoho_id              TEXT NOT NULL,
    document_identifier  TEXT,
    customer_name        TEXT,
    error_message        TEXT NOT NULL,
    request_payload      TEXT,
    response_payload     TEXT,
    occurred_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nrs_errors_occurred_at ON nrs_errors (occurred_at);
CREATE INDEX IF NOT EXISTS idx_nrs_errors_doc_type ON nrs_errors (doc_type);

-- Small key/value store for dashboard sync metadata (e.g. last_synced).
CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
