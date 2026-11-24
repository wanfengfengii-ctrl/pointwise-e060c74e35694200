import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from account.day import read_day_snapshot
from account.reconcile import reconcile_day
from archive.cold_storage import ColdSlot, cold_claim, decode_reply, initialize_cold
from archive.index import UnknownArchiveReceipt
from archive.integrity import ArchiveIntegrityError
from archive.retrieve import claim
from catalog.registry import initialize_catalog, open_catalog
from cold_fixture import DAY, DEVICE, POINT, Mailbox, ReplyLost, reject_next_commit, seed
from intake.rows import SourceRow
from intake.staging import stage
from publication.admission import seal
from publication.cold_handoff import TransferDesk, TransferResult, initialize_source
from publication.storage import initialize_warehouse, open_warehouse


def reply_status(reply):
    return json.loads(reply.decode('utf-8'))['status']


class ColdHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='cold-handoff-test-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.catalog_path = root / 'catalog.sqlite3'
        self.warehouse_path = root / 'warehouse.sqlite3'
        cold_dir = root / 'cold-store'
        cold_dir.mkdir()
        self.cold_path = cold_dir / 'cold.sqlite3'
        self.catalog = None
        self.warehouse = None
        self.cold = None
        self.mailbox = None
        self.desk = None
        self.addCleanup(self.close_connections)
        self.reopen()
        initialize_catalog(self.catalog)
        initialize_warehouse(self.warehouse)
        initialize_source(self.warehouse)
        initialize_cold(self.cold)
        self.receipts = seed(self.catalog, self.warehouse)

    def close_connections(self):
        for connection in (self.catalog, self.warehouse, self.cold):
            if connection is not None:
                connection.close()
        self.catalog = self.warehouse = self.cold = None

    def reopen(self):
        self.close_connections()
        self.catalog = open_catalog(self.catalog_path)
        self.warehouse = open_warehouse(self.warehouse_path)
        self.cold = open_warehouse(self.cold_path)
        handler = ColdSlot(self.cold).receive
        if self.mailbox is None:
            self.mailbox = Mailbox(handler)
        else:
            self.mailbox.handler = handler
        self.desk = TransferDesk(self.warehouse, self.mailbox)

    def reopen_cold_only(self):
        if self.cold is not None:
            self.cold.close()
        self.cold = open_warehouse(self.cold_path)
        self.mailbox.handler = ColdSlot(self.cold).receive

    def assert_source_intact(self):
        snapshot = read_day_snapshot(self.catalog, self.warehouse, POINT, DAY)
        self.assertEqual((snapshot.account.row_count, snapshot.account.total_ml), (2, 18))
        self.assertEqual(reconcile_day(self.warehouse, snapshot), ())
        rows = [
            (row.line_number, row.value, row.unit, row.missing)
            for receipt_id in (self.receipts['A'], self.receipts['B'])
            for row in claim(self.warehouse, receipt_id).rows
        ]
        self.assertEqual(rows, [(17, 7, 'mL', False), (4, 11, 'mL', False)])

    def test_cancel_wins_and_slot_is_reused(self):
        self.desk.open_transfer('A', self.receipts['A'])
        self.mailbox.lose_next_reply('write')
        lost = self.desk.advance('A')
        self.assertEqual((lost.state, lost.receipt), ('pending', None))
        self.mailbox.hold_next('confirm')
        resumed = self.desk.advance('A')
        self.assertEqual((resumed.state, resumed.receipt), ('pending', None))
        cancelled = self.desk.cancel('A')
        self.assertEqual((cancelled.state, cancelled.receipt), ('cancelled', None))

        self.desk.open_transfer('B', self.receipts['B'])
        confirmed = self.desk.advance('B')
        self.assertEqual(confirmed.state, 'confirmed')
        self.assertIsInstance(confirmed.receipt, bytes)
        self.assertTrue(confirmed.receipt)

        # Late or redelivered A messages cannot touch B's occupation.
        old_write = self.mailbox.lost_replies[0][0]
        self.assertEqual(old_write[0], 'write')
        self.assertEqual(reply_status(self.mailbox.deliver(old_write)), 'cancelled')
        held_confirm = self.mailbox.held[0]
        self.assertEqual(held_confirm[0], 'confirm')
        self.assertEqual(reply_status(self.mailbox.deliver(held_confirm)), 'cancelled')
        old_cancel = next(packet for packet in self.mailbox.sent if packet[0] == 'cancel')
        self.assertEqual(reply_status(self.mailbox.deliver(old_cancel)), 'cancelled')

        # A stays cancelled and resuming it cannot even emit a confirm.
        sent_before = len(self.mailbox.sent)
        self.assertEqual(self.desk.advance('A').state, 'cancelled')
        self.assertEqual(self.desk.cancel('A').state, 'cancelled')
        self.assertEqual(self.desk.status('A').state, 'cancelled')
        self.assertEqual(self.desk.open_transfer('A', self.receipts['A']).state, 'cancelled')
        with self.assertRaises(ValueError):
            self.desk.open_transfer('A', self.receipts['B'])
        self.assertEqual(len(self.mailbox.sent), sent_before)
        self.assertEqual(self.desk.status('B').state, 'confirmed')
        self.assertEqual(len(self.mailbox.sent), sent_before)

        # Close source and catalog; reopen only the cold store and claim B.
        first_receipt = confirmed.receipt
        self.catalog.close()
        self.catalog = None
        self.warehouse.close()
        self.warehouse = None
        self.reopen_cold_only()
        claimed = cold_claim(self.cold, first_receipt)
        self.assertEqual(claimed.receipt_id, self.receipts['B'])
        self.assertEqual((claimed.point_id, claimed.business_day), (POINT, DAY))
        self.assertEqual(
            [(row.position, row.device_id, row.line_number, row.value, row.unit, row.missing)
             for row in claimed.rows],
            [(0, DEVICE, 4, 11, 'mL', False)],
        )

        # Reopen everything: the ledger remembers and V1 still totals 18 mL.
        self.reopen()
        self.assertEqual(self.desk.status('A').state, 'cancelled')
        self.assertEqual(self.desk.status('B').state, 'confirmed')
        self.assert_source_intact()

    def test_confirm_wins_and_is_recognized_after_reopen(self):
        self.desk.open_transfer('A', self.receipts['A'])
        self.mailbox.lose_next_reply('confirm')
        lost = self.desk.advance('A')
        self.assertEqual((lost.state, lost.receipt), ('pending', None))
        self.reopen()

        # The confirm already won, so cancel must hand back the first receipt.
        recognized = self.desk.cancel('A')
        self.assertEqual(recognized.state, 'confirmed')
        first_receipt = recognized.receipt
        self.assertIsInstance(first_receipt, bytes)
        self.assertTrue(first_receipt)
        # Status and resume recognize the very same first receipt bytes.
        self.assertEqual(self.desk.status('A').state, 'confirmed')
        self.assertEqual(self.desk.status('A').receipt, first_receipt)
        self.assertEqual(self.desk.advance('A').receipt, first_receipt)

        # B still reports the slot full.
        self.desk.open_transfer('B', self.receipts['B'])
        self.assertEqual(self.desk.advance('B').state, 'full')
        self.assertEqual(self.desk.status('B').state, 'full')

        # Rebinding and wrong receipts are refused.
        with self.assertRaises(ValueError):
            self.desk.open_transfer('A', self.receipts['B'])
        with self.assertRaises(ValueError):
            cold_claim(self.cold, b'not-a-receipt')
        with self.assertRaises(ValueError):
            cold_claim(self.cold, first_receipt + b'!')
        forged = json.dumps({
            'kind': 'cold-first-receipt', 'key': 'A', 'receipt_id': self.receipts['A'],
            'batch_id': 1, 'digest': '0' * 64,
        }).encode('utf-8')
        with self.assertRaises(ValueError):
            cold_claim(self.cold, forged)

        # The confirmed batch stays claimable; a redelivered cancel cannot free the slot.
        claimed = cold_claim(self.cold, first_receipt)
        self.assertEqual(claimed.receipt_id, self.receipts['A'])
        self.assertEqual(
            [(row.line_number, row.value, row.unit, row.missing) for row in claimed.rows],
            [(17, 7, 'mL', False)],
        )
        old_cancel = next(packet for packet in self.mailbox.sent if packet[0] == 'cancel')
        status, receipt = decode_reply(self.mailbox.deliver(old_cancel))
        self.assertEqual((status, receipt), ('confirmed', first_receipt))
        self.assertEqual(self.desk.advance('B').state, 'full')
        self.assertEqual(cold_claim(self.cold, first_receipt).rows[0].value, 7)
        self.assert_source_intact()

    def test_confirm_commit_failure_defers_first_receipt(self):
        self.desk.open_transfer('A', self.receipts['A'])
        self.mailbox.hold_next('confirm')
        self.assertEqual(self.desk.advance('A').state, 'pending')
        held_confirm = self.mailbox.held[0]

        # The cold confirm ruling fails to commit: no new first receipt.
        with reject_next_commit(self.cold) as tripped:
            with self.assertRaises(sqlite3.Error):
                self.mailbox.deliver(held_confirm)
        self.assertTrue(tripped)
        self.assertEqual(self.desk.status('A').state, 'pending')

        # Cold database errors reach the caller instead of being hidden.
        with reject_next_commit(self.cold):
            with self.assertRaises(sqlite3.Error):
                self.desk.advance('A')
        self.assertEqual(self.desk.status('A').state, 'pending')

        # Once the fault clears, the original transfer resumes and completes.
        confirmed = self.desk.advance('A')
        self.assertEqual(confirmed.state, 'confirmed')
        self.assertEqual(cold_claim(self.cold, confirmed.receipt).rows[0].value, 7)
        status, receipt = decode_reply(self.mailbox.deliver(held_confirm))
        self.assertEqual((status, receipt), ('confirmed', confirmed.receipt))
        self.assert_source_intact()

    def test_full_slot_can_wait(self):
        self.desk.open_transfer('A', self.receipts['A'])
        self.mailbox.hold_next('confirm')
        self.assertEqual(self.desk.advance('A').state, 'pending')
        self.desk.open_transfer('B', self.receipts['B'])
        self.assertEqual(self.desk.advance('B').state, 'full')
        self.assertEqual(self.desk.status('B').state, 'full')
        self.assertEqual(self.desk.cancel('A').state, 'cancelled')
        confirmed = self.desk.advance('B')
        self.assertEqual(confirmed.state, 'confirmed')
        claimed = cold_claim(self.cold, confirmed.receipt)
        self.assertEqual([(row.line_number, row.value) for row in claimed.rows], [(4, 11)])
        self.assert_source_intact()

    def test_mixed_batch_claim_order_and_body_corruption(self):
        rows = (
            SourceRow(DEVICE, 30, 1, 'L'),
            SourceRow(DEVICE, 10, 5, 'mL'),
            SourceRow(DEVICE, 20, None, 'mL', missing=True),
        )
        staged = stage(self.catalog, self.warehouse, 'mixed', POINT, DAY, rows)
        mixed_receipt = seal(self.catalog, self.warehouse, 'mixed', staged.batch_id).receipt_id
        self.desk.open_transfer('M', mixed_receipt)
        confirmed = self.desk.advance('M')
        self.assertEqual(confirmed.state, 'confirmed')
        claimed = cold_claim(self.cold, confirmed.receipt)
        self.assertEqual(claimed.receipt_id, mixed_receipt)
        self.assertEqual((claimed.point_id, claimed.business_day), (POINT, DAY))
        self.assertEqual([row.position for row in claimed.rows], [0, 1, 2])
        self.assertEqual(
            [(row.device_id, row.line_number, row.value, row.unit, row.missing)
             for row in claimed.rows],
            [(DEVICE, 30, 1, 'L', False), (DEVICE, 10, 5, 'mL', False),
             (DEVICE, 20, None, 'mL', True)],
        )

        # A damaged cold body is refused, never partially returned.
        self.cold.execute(
            "UPDATE cold_rows SET raw_value = '0x2' WHERE occupant_key = 'M' AND ordinal = 0"
        )
        with self.assertRaises(ValueError):
            cold_claim(self.cold, confirmed.receipt)
        self.cold.execute(
            "UPDATE cold_rows SET raw_value = '0x1' WHERE occupant_key = 'M' AND ordinal = 0"
        )
        self.assertEqual(cold_claim(self.cold, confirmed.receipt).rows[0].value, 1)
        self.cold.execute("DELETE FROM cold_rows WHERE occupant_key = 'M' AND ordinal = 2")
        with self.assertRaises(ValueError):
            cold_claim(self.cold, confirmed.receipt)

        snapshot = read_day_snapshot(self.catalog, self.warehouse, POINT, DAY)
        self.assertEqual((snapshot.account.row_count, snapshot.account.total_ml), (5, 1023))
        self.assertEqual(reconcile_day(self.warehouse, snapshot), ())

    def test_desk_api_edges(self):
        self.assertTrue(issubclass(ReplyLost, TimeoutError))
        for operation in (self.desk.advance, self.desk.cancel, self.desk.status):
            with self.assertRaises(KeyError):
                operation('unknown')
        with self.assertRaises(ValueError):
            self.desk.open_transfer('', self.receipts['A'])
        with self.assertRaises(ValueError):
            self.desk.open_transfer('A', '')
        with self.assertRaises(UnknownArchiveReceipt):
            self.desk.open_transfer('A', 'no-such-receipt')
        with self.assertRaises(ValueError):
            TransferResult('confirmed', None)
        with self.assertRaises(ValueError):
            TransferResult('pending', b'stray')

        opened = self.desk.open_transfer('A', self.receipts['A'])
        self.assertEqual((opened.state, opened.receipt), ('pending', None))
        again = self.desk.open_transfer('A', self.receipts['A'])
        self.assertEqual((again.state, again.receipt), ('pending', None))
        with self.assertRaises(ValueError):
            self.desk.open_transfer('A', self.receipts['B'])

        # A lost write reply settles nothing; status must not self-complete.
        self.mailbox.lose_next_reply('write')
        self.assertEqual(self.desk.advance('A').state, 'pending')
        self.assertEqual(self.desk.status('A').state, 'pending')
        # A lost cancel reply is not a cancel, but the ruling can be recognized later.
        self.mailbox.lose_next_reply('cancel')
        self.assertEqual(self.desk.cancel('A').state, 'pending')
        self.assertEqual(self.desk.status('A').state, 'cancelled')
        self.assertEqual(self.desk.cancel('A').state, 'cancelled')

        # An unprovable original cannot be opened.
        self.warehouse.execute(
            'DELETE FROM archive_rows WHERE batch_id = '
            '(SELECT batch_id FROM archive_receipts WHERE receipt_id = ?)',
            (self.receipts['B'],),
        )
        with self.assertRaises(ArchiveIntegrityError):
            self.desk.open_transfer('B', self.receipts['B'])

        # Initialization is rerunnable and preserves the ledger.
        initialize_source(self.warehouse)
        initialize_cold(self.cold)
        self.assertEqual(self.desk.status('A').state, 'cancelled')
