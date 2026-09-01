"""SQLite-backed NRS submission state (see schema.sql).

Holds the latest persistent state per document (posted or error) plus the
exact request sent and raw response received, so the dashboard reflects real
NRS state across restarts/reloads. Also keeps an append-only error history.
"""

import json
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "dashboard.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
_lock = threading.Lock()

# Columns expected on each table, used to migrate a pre-existing dashboard.db
# non-destructively (SQLite CREATE TABLE IF NOT EXISTS won't add new columns).
_EXPECTED_COLUMNS = {
    "nrs_submissions": {
        "error_message": "TEXT",
        "response_payload": "TEXT",
    },
    "nrs_errors": {
        "response_payload": "TEXT",
    },
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection):
    for table, columns in _EXPECTED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, col_type in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
    # Legacy rows stored the NRS document status (e.g. "PENDING") in `status`;
    # normalize any successfully-posted ones to our 'posted' state value.
    conn.execute(
        """
        UPDATE nrs_submissions SET status = 'posted'
        WHERE status NOT IN ('posted', 'error') AND irn IS NOT NULL AND irn <> ''
        """
    )


def init_db():
    with _lock, _connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)


def _dump(payload) -> str | None:
    return json.dumps(payload) if payload is not None else None


def record_state(doc_type: str, zoho_id: str, *, status: str, document_identifier: str = "",
                 customer_name: str = "", issue_date: str = "", irn: str = "", nrs_id: str = "",
                 error_message: str = None, request_payload=None, response_payload=None):
    """Upsert the latest state for a document. `status` is 'posted' or
    'error'. Stores the request sent and the raw response for both."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO nrs_submissions
                (doc_type, zoho_id, document_identifier, customer_name, issue_date,
                 irn, nrs_id, status, error_message, request_payload, response_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (doc_type, zoho_id) DO UPDATE SET
                document_identifier = excluded.document_identifier,
                customer_name = excluded.customer_name,
                issue_date = excluded.issue_date,
                irn = excluded.irn,
                nrs_id = excluded.nrs_id,
                status = excluded.status,
                error_message = excluded.error_message,
                request_payload = excluded.request_payload,
                response_payload = excluded.response_payload,
                submitted_at = datetime('now')
            """,
            (
                doc_type, zoho_id, document_identifier, customer_name, issue_date,
                irn, nrs_id, status, error_message, _dump(request_payload), _dump(response_payload),
            ),
        )


def get_state(doc_type: str, zoho_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM nrs_submissions WHERE doc_type = ? AND zoho_id = ?",
            (doc_type, zoho_id),
        ).fetchone()
        return dict(row) if row else None


def get_states(doc_type: str, zoho_ids: list) -> dict:
    """Return {zoho_id: state_row} for the given ids - used to render each
    row's persisted NRS state when a list page loads."""
    if not zoho_ids:
        return {}
    placeholders = ",".join("?" for _ in zoho_ids)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM nrs_submissions WHERE doc_type = ? AND zoho_id IN ({placeholders})",
            [doc_type, *zoho_ids],
        ).fetchall()
        return {r["zoho_id"]: dict(r) for r in rows}


def find_invoices_by_customer(customer_name: str) -> list:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM nrs_submissions
            WHERE doc_type = 'invoice' AND customer_name = ? AND status = 'posted' AND irn <> ''
            ORDER BY submitted_at DESC
            """,
            (customer_name,),
        ).fetchall()
        return [dict(r) for r in rows]


def log_error(doc_type: str, zoho_id: str, error_message: str, *, document_identifier: str = "",
              customer_name: str = "", request_payload=None, response_payload=None):
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO nrs_errors
                (doc_type, zoho_id, document_identifier, customer_name, error_message,
                 request_payload, response_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_type, zoho_id, document_identifier, customer_name, error_message,
                _dump(request_payload), _dump(response_payload),
            ),
        )


def list_errors(limit: int = 200) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM nrs_errors ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_meta(key: str, value: str):
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


init_db()
