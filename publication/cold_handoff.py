"""Source-warehouse desk for handing sealed original batches to cold storage.

``initialize_source`` adds the transfer ledger to a V1-initialized warehouse.
``TransferDesk`` drives the cold-storage protocol over a caller-provided
``exchange(phase, message)`` channel: open fixes a transfer key to one source
receipt, advance writes the complete batch and then confirms, cancel asks the
cold store to rule, and status recognizes outcomes the cold store already
settled.  The two databases commit independently; only the cold store's
ruling is authoritative, and recognized terminal states never regress.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from archive.cold_storage import (
    decode_reply,
    encode_key_message,
    encode_write_message,
)
from archive.integrity import calculate_archive_digest
from archive.retrieve import claim
from catalog.devices import require_identifier
from publication.storage import warehouse_snapshot, warehouse_write

_SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS cold_transfers (
    transfer_key TEXT PRIMARY KEY NOT NULL,
    receipt_id TEXT NOT NULL,
    batch_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'confirmed', 'cancelled')),
    first_receipt BLOB
);
COMMIT;
'''

_STATES = ('pending', 'full', 'cancelled', 'confirmed')


def initialize_source(warehouse: sqlite3.Connection) -> None:
    """Add the cold-handoff ledger to a V1 warehouse.  Safe to run repeatedly."""
    if warehouse.in_transaction:
        raise RuntimeError('source initialization requires an idle connection')
    try:
        warehouse.executescript(_SCHEMA)
    except BaseException:
        warehouse.rollback()
        raise


@dataclass(frozen=True)
class TransferResult:
    """The recognizable outcome of one transfer operation."""

    state: str
    receipt: bytes | None

    def __post_init__(self):
        if self.state not in _STATES:
            raise ValueError(f'unknown transfer state {self.state!r}')
        if self.state == 'confirmed':
            if type(self.receipt) is not bytes or not self.receipt:
                raise ValueError('a confirmed transfer requires a non-empty first receipt')
        elif self.receipt is not None:
            raise ValueError(f'a {self.state} transfer cannot carry a receipt')


@dataclass(frozen=True)
class _Transfer:
    key: str
    receipt_id: str
    batch_id: int
    state: str
    first_receipt: bytes | None


class TransferDesk:
    """Drives open/advance/cancel/status for cold handoffs of one warehouse."""

    def __init__(self, warehouse: sqlite3.Connection, exchange):
        self._warehouse = warehouse
        self._exchange = exchange

    # -- public operations ----------------------------------------------------

    def open_transfer(self, key: str, receipt_id: str) -> TransferResult:
        """Fix a transfer key to one sealed source receipt.

        Reopening with the same parameters returns the recognized state;
        rebinding the key to another receipt raises ValueError.  The first
        open claims the batch from the V1 archive, so unknown or unprovable
        originals cannot be opened.
        """
        require_identifier(key, 'transfer key')
        require_identifier(receipt_id, 'receipt_id')
        with warehouse_snapshot(self._warehouse):
            existing = self._find(key)
        if existing is not None:
            if existing.receipt_id != receipt_id:
                raise ValueError(
                    f'transfer {key!r} is already bound to receipt {existing.receipt_id!r}'
                )
            return self._recognized(existing)
        claimed = claim(self._warehouse, receipt_id)
        with warehouse_write(self._warehouse):
            existing = self._find(key)
            if existing is not None:
                if existing.receipt_id != receipt_id:
                    raise ValueError(
                        f'transfer {key!r} is already bound to receipt {existing.receipt_id!r}'
                    )
                return self._recognized(existing)
            self._warehouse.execute(
                '''INSERT INTO cold_transfers(transfer_key, receipt_id, batch_id, state, first_receipt)
                   VALUES (?, ?, ?, 'open', NULL)''',
                (key, receipt_id, claimed.batch_id),
            )
        return TransferResult('pending', None)

    def advance(self, key: str) -> TransferResult:
        """Proceed or resume: write the complete batch, then confirm it."""
        transfer = self._require(key)
        if transfer.state != 'open':
            return self._recognized(transfer)
        claimed = claim(self._warehouse, transfer.receipt_id)
        digest = calculate_archive_digest(
            claimed.batch_id, claimed.point_id, claimed.business_day, claimed.rows
        )
        message = encode_write_message(key, claimed, digest)
        status, receipt = self._send('write', message)
        if status is None:
            return TransferResult('pending', None)
        if status == 'full':
            return TransferResult('full', None)
        if status in ('cancelled', 'confirmed'):
            return self._settle(key, status, receipt)
        status, receipt = self._send('confirm', encode_key_message('confirm', key))
        if status is None:
            return TransferResult('pending', None)
        if status in ('cancelled', 'confirmed'):
            return self._settle(key, status, receipt)
        return TransferResult('pending', None)

    def cancel(self, key: str) -> TransferResult:
        """Ask the cold store to cancel; report the ruling it actually made."""
        transfer = self._require(key)
        if transfer.state != 'open':
            return self._recognized(transfer)
        status, receipt = self._send('cancel', encode_key_message('cancel', key))
        if status is None:
            return TransferResult('pending', None)
        if status in ('cancelled', 'confirmed'):
            return self._settle(key, status, receipt)
        return TransferResult('pending', None)

    def status(self, key: str) -> TransferResult:
        """Query the cold store and recognize any outcome it already settled."""
        transfer = self._require(key)
        if transfer.state != 'open':
            return self._recognized(transfer)
        status, receipt = self._send('query', encode_key_message('query', key))
        if status is None:
            return TransferResult('pending', None)
        if status in ('cancelled', 'confirmed'):
            return self._settle(key, status, receipt)
        if status == 'full':
            return TransferResult('full', None)
        return TransferResult('pending', None)

    # -- ledger access ----------------------------------------------------------

    def _find(self, key: str) -> _Transfer | None:
        row = self._warehouse.execute(
            '''SELECT transfer_key, receipt_id, batch_id, state, first_receipt
               FROM cold_transfers WHERE transfer_key = ?''',
            (key,),
        ).fetchone()
        return _Transfer(*row) if row is not None else None

    def _require(self, key: str) -> _Transfer:
        require_identifier(key, 'transfer key')
        with warehouse_snapshot(self._warehouse):
            transfer = self._find(key)
        if transfer is None:
            raise KeyError(f'unknown transfer {key!r}')
        return transfer

    @staticmethod
    def _recognized(transfer: _Transfer) -> TransferResult:
        if transfer.state == 'confirmed':
            return TransferResult('confirmed', transfer.first_receipt)
        if transfer.state == 'cancelled':
            return TransferResult('cancelled', None)
        return TransferResult('pending', None)

    # -- protocol driving ---------------------------------------------------------

    def _send(self, phase: str, message: bytes) -> tuple[str | None, bytes | None]:
        """Exchange one message; a lost reply leaves the outcome unrecognizable."""
        try:
            reply = self._exchange(phase, message)
        except TimeoutError:
            return None, None
        return decode_reply(reply)

    def _settle(self, key: str, state: str, receipt: bytes | None) -> TransferResult:
        """Durably recognize a cold-store ruling, then return it.

        Terminal states never regress: only a still-open transfer adopts the
        ruling, and the already-recognized state is what gets returned.
        """
        with warehouse_write(self._warehouse):
            self._warehouse.execute(
                '''UPDATE cold_transfers SET state = ?, first_receipt = ?
                   WHERE transfer_key = ? AND state = 'open' ''',
                (state, receipt, key),
            )
            settled = self._find(key)
        if settled is None:
            raise RuntimeError(f'transfer {key!r} vanished from the ledger')
        return self._recognized(settled)
