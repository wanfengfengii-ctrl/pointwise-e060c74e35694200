"""Independent cold storage for sealed original batches.

The cold store keeps at most one batch body for handoff or claiming.  It is
a separate SQLite database, opened with ``publication.storage.open_warehouse``
and initialized with ``initialize_cold``.  ``ColdSlot.receive`` applies the
write/confirm/cancel/query messages that a source desk sends over the trusted
exchange; ``cold_claim`` redeems a confirmed first receipt for the original
rows in their original order.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass

from archive.integrity import ArchiveIntegrityError, calculate_archive_digest
from archive.retrieve import ClaimedBatch
from archive.rows import ArchivedRow
from intake.units import decode_integer, encode_integer
from publication.storage import warehouse_snapshot, warehouse_write

_SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS cold_slot (
    slot_id INTEGER PRIMARY KEY CHECK (slot_id = 1),
    occupant_key TEXT,
    source_receipt_id TEXT,
    batch_id INTEGER,
    point_id TEXT,
    business_day TEXT,
    row_count INTEGER,
    digest TEXT,
    phase TEXT CHECK (phase IN ('written', 'confirmed') OR phase IS NULL),
    first_receipt BLOB
);
INSERT OR IGNORE INTO cold_slot(slot_id) VALUES (1);
CREATE TABLE IF NOT EXISTS cold_rows (
    occupant_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    device_id TEXT NOT NULL,
    line_number TEXT NOT NULL,
    raw_value TEXT,
    unit TEXT NOT NULL,
    missing INTEGER NOT NULL CHECK (missing IN (0, 1)),
    PRIMARY KEY (occupant_key, ordinal)
);
CREATE TABLE IF NOT EXISTS cold_tombstones (
    transfer_key TEXT PRIMARY KEY NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('cancelled'))
);
COMMIT;
'''

_RECEIPT_KIND = 'cold-first-receipt'
_MESSAGE_KINDS = {'write', 'confirm', 'cancel', 'query'}
_STATUSES = {'written', 'full', 'cancelled', 'confirmed', 'empty'}


def initialize_cold(cold: sqlite3.Connection) -> None:
    """Create the cold-store schema.  Safe to run repeatedly."""
    if cold.in_transaction:
        raise RuntimeError('cold storage initialization requires an idle connection')
    try:
        cold.executescript(_SCHEMA)
    except BaseException:
        cold.rollback()
        raise


# --- wire codec shared with the source desk ---------------------------------


def encode_write_message(key: str, claimed: ClaimedBatch, digest: str) -> bytes:
    """Serialize the complete candidate batch for the write phase."""
    body = {
        'kind': 'write',
        'key': key,
        'receipt_id': claimed.receipt_id,
        'batch_id': claimed.batch_id,
        'point_id': claimed.point_id,
        'business_day': claimed.business_day,
        'digest': digest,
        'rows': [
            [row.position, row.device_id, hex(row.line_number), encode_integer(row.value),
             row.unit, row.missing]
            for row in claimed.rows
        ],
    }
    return _dump(body)


def encode_key_message(kind: str, key: str) -> bytes:
    """Serialize a confirm, cancel or query request for one transfer key."""
    if kind not in ('confirm', 'cancel', 'query'):
        raise ValueError(f'unsupported message kind {kind!r}')
    return _dump({'kind': kind, 'key': key})


def encode_reply(status: str, receipt: bytes | None = None) -> bytes:
    body = {'status': status}
    if receipt is not None:
        body['receipt'] = base64.b64encode(receipt).decode('ascii')
    return _dump(body)


def decode_reply(reply: bytes) -> tuple[str, bytes | None]:
    """Parse a cold-store reply into (status, first receipt or None)."""
    body = _parse_bytes(reply, 'cold reply')
    status = body.get('status')
    if status not in _STATUSES:
        raise ValueError(f'cold reply has unknown status {status!r}')
    encoded = body.get('receipt')
    if status == 'confirmed':
        if type(encoded) is not str or not encoded:
            raise ValueError('confirmed cold reply is missing its first receipt')
        try:
            receipt = base64.b64decode(encoded.encode('ascii'), validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError('confirmed cold reply carries an invalid first receipt') from error
        if not receipt:
            raise ValueError('confirmed cold reply carries an empty first receipt')
        return status, receipt
    if encoded is not None:
        raise ValueError(f'cold reply with status {status!r} must not carry a receipt')
    return status, None


def _dump(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _parse_bytes(message: bytes, label: str) -> dict:
    if type(message) is not bytes or not message:
        raise ValueError(f'{label} must be non-empty bytes')
    try:
        body = json.loads(message.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f'{label} is not valid JSON') from error
    if type(body) is not dict:
        raise ValueError(f'{label} must be a JSON object')
    return body


def _require_text(body: dict, field: str) -> str:
    value = body.get(field)
    if type(value) is not str or not value.strip():
        raise ValueError(f'cold request requires a non-empty text {field}')
    return value


def _decode_rows(body: dict) -> tuple[ArchivedRow, ...]:
    entries = body.get('rows')
    if type(entries) is not list or not entries:
        raise ValueError('a write request requires a non-empty rows list')
    rows = []
    for position, entry in enumerate(entries):
        try:
            ordinal, device_id, line_number, raw_value, unit, missing = entry
            row = ArchivedRow(
                ordinal, device_id, int(line_number, 16), decode_integer(raw_value), unit, bool(missing)
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f'write request row {position} is malformed') from error
        if type(row.position) is not int or row.position != position:
            raise ValueError(f'write request row {position} is out of order')
        if type(row.device_id) is not str or not row.device_id.strip():
            raise ValueError(f'write request row {position} requires a device identifier')
        if row.line_number < 1:
            raise ValueError(f'write request row {position} requires a positive line number')
        if row.unit not in ('L', 'mL') or type(missing) is not bool:
            raise ValueError(f'write request row {position} has an invalid reading')
        if row.missing != (row.value is None):
            raise ValueError(f'write request row {position} has an inconsistent missing marker')
        rows.append(row)
    return tuple(rows)


# --- slot state ---------------------------------------------------------------


@dataclass(frozen=True)
class _Slot:
    occupant_key: str | None
    source_receipt_id: str | None
    batch_id: int | None
    point_id: str | None
    business_day: str | None
    row_count: int | None
    digest: str | None
    phase: str | None
    first_receipt: bytes | None

    @property
    def occupied(self) -> bool:
        return self.occupant_key is not None


def _load_slot(cold: sqlite3.Connection) -> _Slot:
    row = cold.execute(
        '''SELECT occupant_key, source_receipt_id, batch_id, point_id, business_day,
                  row_count, digest, phase, first_receipt
           FROM cold_slot WHERE slot_id = 1''',
    ).fetchone()
    if row is None:
        raise RuntimeError('cold slot row is missing; run initialize_cold')
    return _Slot(*row)


def _tombstoned(cold: sqlite3.Connection, key: str) -> bool:
    return cold.execute(
        'SELECT 1 FROM cold_tombstones WHERE transfer_key = ?', (key,)
    ).fetchone() is not None


def _clear_slot(cold: sqlite3.Connection, key: str) -> None:
    cold.execute('DELETE FROM cold_rows WHERE occupant_key = ?', (key,))
    cold.execute(
        '''UPDATE cold_slot SET occupant_key = NULL, source_receipt_id = NULL, batch_id = NULL,
               point_id = NULL, business_day = NULL, row_count = NULL, digest = NULL,
               phase = NULL, first_receipt = NULL
           WHERE slot_id = 1''',
    )


def _first_receipt(slot: _Slot) -> bytes:
    body = {
        'kind': _RECEIPT_KIND,
        'key': slot.occupant_key,
        'receipt_id': slot.source_receipt_id,
        'batch_id': slot.batch_id,
        'digest': slot.digest,
    }
    return _dump(body)


# --- message handling ---------------------------------------------------------


class ColdSlot:
    """Applies desk messages to the single cold slot, one commit per message."""

    def __init__(self, cold: sqlite3.Connection):
        self._cold = cold

    def receive(self, phase: str, message: bytes) -> bytes:
        if phase not in _MESSAGE_KINDS:
            raise ValueError(f'unknown cold storage phase {phase!r}')
        body = _parse_bytes(message, 'cold request')
        if body.get('kind') != phase:
            raise ValueError(f'cold request kind does not match phase {phase!r}')
        key = _require_text(body, 'key')
        if phase == 'query':
            return self._query(key)
        handler = {'write': self._write, 'confirm': self._confirm, 'cancel': self._cancel}[phase]
        with warehouse_write(self._cold):
            return handler(key, body)

    def _query(self, key: str) -> bytes:
        with warehouse_snapshot(self._cold):
            return self._recognize(key, _load_slot(self._cold))

    def _recognize(self, key: str, slot: _Slot) -> bytes:
        if _tombstoned(self._cold, key):
            return encode_reply('cancelled')
        if not slot.occupied:
            return encode_reply('empty')
        if slot.occupant_key != key:
            return encode_reply('full')
        if slot.phase == 'confirmed':
            return encode_reply('confirmed', slot.first_receipt)
        return encode_reply('written')

    def _write(self, key: str, body: dict) -> bytes:
        if _tombstoned(self._cold, key):
            return encode_reply('cancelled')
        slot = _load_slot(self._cold)
        if slot.occupied and slot.occupant_key != key:
            return encode_reply('full')
        receipt_id = _require_text(body, 'receipt_id')
        batch_id = body.get('batch_id')
        if type(batch_id) is not int or batch_id < 1:
            raise ValueError('a write request requires a positive integer batch_id')
        point_id = _require_text(body, 'point_id')
        business_day = _require_text(body, 'business_day')
        digest = _require_text(body, 'digest')
        rows = _decode_rows(body)
        if calculate_archive_digest(batch_id, point_id, business_day, rows) != digest:
            raise ValueError('write request rows do not match the promised digest')
        if slot.occupied:
            promised = (slot.source_receipt_id, slot.batch_id, slot.digest, slot.row_count)
            if promised != (receipt_id, batch_id, digest, len(rows)):
                raise ValueError(f'transfer {key!r} was written with a different batch')
            if slot.phase == 'confirmed':
                return encode_reply('confirmed', slot.first_receipt)
            return encode_reply('written')
        self._cold.execute(
            '''UPDATE cold_slot SET occupant_key = ?, source_receipt_id = ?, batch_id = ?,
                   point_id = ?, business_day = ?, row_count = ?, digest = ?, phase = 'written',
                   first_receipt = NULL
               WHERE slot_id = 1''',
            (key, receipt_id, batch_id, point_id, business_day, len(rows), digest),
        )
        self._cold.executemany(
            '''INSERT INTO cold_rows(occupant_key, ordinal, device_id, line_number, raw_value, unit, missing)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (
                (key, row.position, row.device_id, hex(row.line_number), encode_integer(row.value),
                 row.unit, int(row.missing))
                for row in rows
            ),
        )
        return encode_reply('written')

    def _confirm(self, key: str, body: dict) -> bytes:
        if _tombstoned(self._cold, key):
            return encode_reply('cancelled')
        slot = _load_slot(self._cold)
        if not slot.occupied:
            return encode_reply('empty')
        if slot.occupant_key != key:
            return encode_reply('full')
        if slot.phase == 'confirmed':
            return encode_reply('confirmed', slot.first_receipt)
        receipt = _first_receipt(slot)
        self._cold.execute(
            '''UPDATE cold_slot SET phase = 'confirmed', first_receipt = ?
               WHERE slot_id = 1 AND occupant_key = ?''',
            (receipt, key),
        )
        return encode_reply('confirmed', receipt)

    def _cancel(self, key: str, body: dict) -> bytes:
        if _tombstoned(self._cold, key):
            return encode_reply('cancelled')
        slot = _load_slot(self._cold)
        if slot.occupied and slot.occupant_key == key and slot.phase == 'confirmed':
            return encode_reply('confirmed', slot.first_receipt)
        if slot.occupied and slot.occupant_key == key:
            _clear_slot(self._cold, key)
        self._cold.execute(
            "INSERT INTO cold_tombstones(transfer_key, outcome) VALUES (?, 'cancelled')", (key,)
        )
        return encode_reply('cancelled')


# --- claiming -----------------------------------------------------------------


def _parse_receipt(receipt: bytes) -> dict:
    body = _parse_bytes(receipt, 'cold first receipt')
    if body.get('kind') != _RECEIPT_KIND:
        raise ValueError('receipt is not a cold first receipt')
    for field in ('key', 'receipt_id', 'digest'):
        _require_text(body, field)
    if type(body.get('batch_id')) is not int or body['batch_id'] < 1:
        raise ValueError('cold first receipt carries an invalid batch identity')
    return body


def cold_claim(cold: sqlite3.Connection, receipt: bytes) -> ClaimedBatch:
    """Redeem a confirmed first receipt for the original batch rows.

    The receipt must match, byte for byte, the first receipt the cold store
    issued for the confirmed transfer; the stored body must still prove the
    promised transfer key, source receipt, batch identity and digest.  Wrong
    receipts raise ValueError and damaged bodies raise ArchiveIntegrityError;
    no partial rows are ever returned.
    """
    _parse_receipt(receipt)
    with warehouse_snapshot(cold):
        slot = _load_slot(cold)
        if slot.phase != 'confirmed' or slot.first_receipt is None:
            raise ValueError('no confirmed transfer matches this receipt')
        if slot.first_receipt != receipt:
            raise ValueError('receipt does not match the confirmed cold transfer')
        entries = cold.execute(
            '''SELECT ordinal, device_id, line_number, raw_value, unit, missing
               FROM cold_rows WHERE occupant_key = ? ORDER BY ordinal''',
            (slot.occupant_key,),
        )
        try:
            rows = tuple(
                ArchivedRow(ordinal, device_id, int(line_number, 16), decode_integer(raw_value),
                            unit, bool(missing))
                for ordinal, device_id, line_number, raw_value, unit, missing in entries
            )
        except (TypeError, ValueError) as error:
            raise ArchiveIntegrityError(
                f'cold batch for transfer {slot.occupant_key!r} has invalid row data'
            ) from error
        if len(rows) != slot.row_count or any(row.position != index for index, row in enumerate(rows)):
            raise ArchiveIntegrityError(
                f'cold batch for transfer {slot.occupant_key!r} has missing or misplaced rows'
            )
        digest = calculate_archive_digest(slot.batch_id, slot.point_id, slot.business_day, rows)
        if digest != slot.digest:
            raise ArchiveIntegrityError(
                f'cold batch for transfer {slot.occupant_key!r} has changed content'
            )
        return ClaimedBatch(slot.source_receipt_id, slot.batch_id, slot.point_id,
                            slot.business_day, rows)
