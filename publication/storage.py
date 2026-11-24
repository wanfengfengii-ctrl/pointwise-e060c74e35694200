import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


_SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS staged_batches (
    batch_id INTEGER PRIMARY KEY,
    point_id TEXT NOT NULL,
    business_day TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count > 0),
    total_ml TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staged_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    batch_id INTEGER NOT NULL UNIQUE REFERENCES staged_batches(batch_id),
    parameters TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staged_rows (
    batch_id INTEGER NOT NULL REFERENCES staged_batches(batch_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    device_id TEXT NOT NULL,
    line_number TEXT NOT NULL,
    raw_value TEXT,
    unit TEXT NOT NULL,
    missing INTEGER NOT NULL CHECK (missing IN (0, 1)),
    milliliters TEXT,
    PRIMARY KEY (batch_id, ordinal)
);
CREATE TABLE IF NOT EXISTS decision_order (
    position INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('admit', 'retire')),
    point_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retirements (
    request_id TEXT PRIMARY KEY NOT NULL,
    point_id TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL UNIQUE REFERENCES decision_order(position),
    receipt_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS fact_batches (
    position INTEGER PRIMARY KEY REFERENCES decision_order(position),
    batch_id INTEGER NOT NULL UNIQUE REFERENCES staged_batches(batch_id),
    request_id TEXT NOT NULL UNIQUE REFERENCES staged_requests(request_id),
    point_id TEXT NOT NULL,
    business_day TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count > 0)
);
CREATE INDEX IF NOT EXISTS fact_batches_by_day ON fact_batches(point_id, business_day, position);
CREATE TABLE IF NOT EXISTS fact_rows (
    fact_position INTEGER NOT NULL REFERENCES fact_batches(position),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    device_id TEXT NOT NULL,
    line_number TEXT NOT NULL,
    milliliters TEXT,
    PRIMARY KEY (fact_position, ordinal)
);
CREATE TABLE IF NOT EXISTS admission_receipts (
    position INTEGER PRIMARY KEY REFERENCES fact_batches(position),
    receipt_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS archive_batches (
    batch_id INTEGER PRIMARY KEY REFERENCES fact_batches(batch_id),
    point_id TEXT NOT NULL,
    business_day TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count > 0),
    digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_rows (
    batch_id INTEGER NOT NULL REFERENCES archive_batches(batch_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    device_id TEXT NOT NULL,
    line_number TEXT NOT NULL,
    raw_value TEXT,
    unit TEXT NOT NULL,
    missing INTEGER NOT NULL CHECK (missing IN (0, 1)),
    PRIMARY KEY (batch_id, ordinal)
);
CREATE TABLE IF NOT EXISTS archive_receipts (
    receipt_id TEXT PRIMARY KEY NOT NULL REFERENCES admission_receipts(receipt_id),
    batch_id INTEGER NOT NULL UNIQUE REFERENCES archive_batches(batch_id)
);
COMMIT;
'''


def open_warehouse(path: str | Path) -> sqlite3.Connection:
    warehouse = sqlite3.connect(path, isolation_level=None)
    warehouse.execute('PRAGMA foreign_keys = ON')
    return warehouse


def initialize_warehouse(warehouse: sqlite3.Connection) -> None:
    if warehouse.in_transaction:
        raise RuntimeError('warehouse initialization requires an idle connection')
    try:
        warehouse.executescript(_SCHEMA)
    except BaseException:
        warehouse.rollback()
        raise


@contextmanager
def _transaction(warehouse: sqlite3.Connection, begin: str) -> Iterator[None]:
    warehouse.execute(begin)
    try:
        yield
        warehouse.commit()
    except BaseException:
        warehouse.rollback()
        raise


def warehouse_write(warehouse: sqlite3.Connection):
    """Acquire the write slot before reading values used in a write decision."""
    return _transaction(warehouse, 'BEGIN IMMEDIATE')


def warehouse_snapshot(warehouse: sqlite3.Connection):
    """Read the warehouse from one SQLite transaction view."""
    return _transaction(warehouse, 'BEGIN')


def next_decision_position(warehouse: sqlite3.Connection, kind: str, point_id: str) -> int:
    if not warehouse.in_transaction:
        raise RuntimeError('a formal position requires a warehouse transaction')
    cursor = warehouse.execute('INSERT INTO decision_order(kind, point_id) VALUES (?, ?)', (kind, point_id))
    return cursor.lastrowid
