"""使用 SQLite 保存协议域数据与幂等请求。"""
import sqlite3
from pathlib import Path

from .domain import Record

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_key TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS protocols (
    protocol_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    state TEXT NOT NULL,
    medication_version INTEGER NOT NULL DEFAULT 0,
    quota_total INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
    consent_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE TABLE IF NOT EXISTS practitioners (
    practitioner_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    qualification TEXT NOT NULL,
    store_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS medication_lists (
    protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    items_json TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (protocol_id, version)
);
CREATE TABLE IF NOT EXISTS goals (
    goal_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS followups (
    followup_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL,
    business_key TEXT NOT NULL UNIQUE,
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL,
    medication_version INTEGER NOT NULL,
    signed_by TEXT,
    signed_at TEXT,
    occurred_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS escalations (
    escalation_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_by TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS quota_ledger (
    entry_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    ref_id TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL,
    from_store TEXT NOT NULL,
    to_store TEXT NOT NULL,
    transferred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL,
    business_key TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    business_key TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def add(self, record: Record) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id,owner_id,state,revision,created_at) VALUES(?,?,?,?,?)",
                (record.record_id, record.owner_id, record.state, record.revision, record.created_at),
            )

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id,owner_id,state,revision,created_at FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None
