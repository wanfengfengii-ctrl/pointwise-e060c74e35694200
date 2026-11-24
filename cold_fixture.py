"""Frozen V1 sample and a deterministic transport for cold-handoff scenarios."""

import argparse
import sqlite3
from collections import defaultdict, deque
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from account.day import read_day_snapshot
from account.reconcile import reconcile_day
from archive.retrieve import claim
from catalog.registry import initialize_catalog, open_catalog, register
from intake.rows import SourceRow
from intake.staging import stage
from publication.admission import seal
from publication.storage import initialize_warehouse, open_warehouse

POINT = 'P'
DAY = '2026-11-18'
DEVICE = 'device-p'


class ReplyLost(TimeoutError):
    """The caller received no reply to this message."""


class Mailbox:
    def __init__(self, handler):
        self.handler = handler
        self.sent = []
        self.held = []
        self.lost_replies = []
        self._faults = defaultdict(deque)

    def hold_next(self, phase):
        self._faults[phase].append('hold')

    def lose_next_reply(self, phase):
        self._faults[phase].append('lose')

    def __call__(self, phase, message):
        packet = (phase, message)
        self.sent.append(packet)
        faults = self._faults[phase]
        fault = faults.popleft() if faults else None
        if fault == 'hold':
            self.held.append(packet)
            raise ReplyLost(f'{phase}: request held before delivery')
        reply = self.deliver(packet)
        if fault == 'lose':
            self.lost_replies.append((packet, reply))
            raise ReplyLost(f'{phase}: reply dropped after delivery')
        return reply

    def deliver(self, packet):
        return self.handler(*packet)


@contextmanager
def reject_next_commit(connection):
    tripped = []

    def authorize(action, detail, *_):
        if not tripped and action == sqlite3.SQLITE_TRANSACTION and detail == 'COMMIT':
            tripped.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        yield tripped
    finally:
        connection.set_authorizer(None)


def seed(catalog, warehouse):
    """Sign A and B into an empty V1 source and return their receipt IDs."""
    initialize_catalog(catalog)
    initialize_warehouse(warehouse)
    register(catalog, POINT, DEVICE)
    receipts = {}
    for key, line, value in [('A', 17, 7), ('B', 4, 11)]:
        batch = stage(catalog, warehouse, key, POINT, DAY, [SourceRow(DEVICE, line, value, 'mL')])
        receipts[key] = seal(catalog, warehouse, key, batch.batch_id).receipt_id
    return receipts


def check_source(catalog_path, warehouse_path):
    with closing(open_catalog(catalog_path)) as catalog, closing(open_warehouse(warehouse_path)) as warehouse:
        receipts = seed(catalog, warehouse)
        snapshot = read_day_snapshot(catalog, warehouse, POINT, DAY)
        catalog.close()
        batch_a = claim(warehouse, receipts['A'])
        batch_b = claim(warehouse, receipts['B'])
        rows = [(row.device_id, row.line_number, row.value, row.unit, row.missing)
                for batch in (batch_a, batch_b) for row in batch.rows]
        expected = [(DEVICE, 17, 7, 'mL', False), (DEVICE, 4, 11, 'mL', False)]
        if rows != expected or snapshot.account.total_ml != 18 or reconcile_day(warehouse, snapshot):
            raise AssertionError(f'incorrect V1 source fixture: {rows!r}, {snapshot.account!r}')
    print(f'V1 cold inputs: {rows!r}. Day total: {snapshot.account.total_ml} mL.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--memory', action='store_true', help='use in-memory SQLite for the V1 sample')
    if parser.parse_args().memory:
        check_source(':memory:', ':memory:')
        return
    with TemporaryDirectory(prefix='cold-handoff-') as directory:
        root = Path(directory)
        check_source(root / 'catalog.sqlite3', root / 'warehouse.sqlite3')


if __name__ == '__main__':
    main()
