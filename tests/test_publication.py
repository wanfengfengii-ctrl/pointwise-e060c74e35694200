import sqlite3

from archive.retrieve import claim
from catalog.retirement import retire
from intake.requests import StageRequestConflict
from publication.facts import find_fact_by_request
from tests.fixtures import WarehouseCase, fail_commit


class PublicationTests(WarehouseCase):
    def test_failed_commit_has_no_receipt(self):
        original = self.original_receipt()
        followup = self.stage_value('F', 7)
        with fail_commit(self.warehouse, 'admission') as statements:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY constraint failed'):
                self.seal_batch(followup)
            self.assertIn('COMMIT', statements)
            self.assertFalse(self.warehouse.in_transaction)
        self.reopen()
        self.assert_day(1, 5, (original,))
        self.assertIsNone(find_fact_by_request(self.warehouse, 'F'))
        retire(self.catalog, self.warehouse, 'stop', 'P')
        self.assert_retired_batch_denied(followup, original)

    def test_reused_request_rejects_new_data(self):
        original_batch = self.stage_value('F', 7)
        receipt = self.seal_batch(original_batch)
        with self.assertRaises(StageRequestConflict):
            self.stage_value('F', 8)
        self.assertEqual(self.stage_value('F', 7), original_batch)
        self.assertEqual(self.seal_batch(original_batch), receipt)
        self.assert_day(1, 7, (receipt,))
        row, = claim(self.warehouse, receipt.receipt_id).rows
        self.assertEqual((row.line_number, row.value, row.unit), (17, 7, 'mL'))
