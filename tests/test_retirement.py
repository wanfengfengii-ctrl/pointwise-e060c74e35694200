from archive.retrieve import claim
from catalog.retirement import retire
from intake.requests import StageRequestConflict
from publication.retry import AdmissionBindingConflict
from tests.fixtures import ReplyLost, WarehouseCase, drop_reply


class RetirementTests(WarehouseCase):
    def test_existing_fact_remains(self):
        receipt = self.original_receipt()
        decision = retire(self.catalog, self.warehouse, 'stop', 'P')
        self.assertLess(receipt.position, decision.position)
        self.assert_day(1, 5, (receipt,))
        row, = claim(self.warehouse, receipt.receipt_id).rows
        self.assertEqual((row.line_number, row.value, row.unit), (4, 5, 'mL'))

    def test_staged_fact_loses_admission(self):
        original = self.original_receipt()
        followup = self.stage_value('F', 7)
        retire(self.catalog, self.warehouse, 'stop', 'P')
        self.assert_retired_batch_denied(followup, original)

    def test_admission_survives_lost_reply(self):
        original = self.original_receipt()
        followup = self.stage_value('F', 7)
        with self.assertRaises(ReplyLost):
            self.seal_batch(followup, after_commit=drop_reply)
        decision = retire(self.catalog, self.warehouse, 'stop', 'P')
        self.reopen()
        account = self.assert_day(2, 12)
        self.assertEqual(account.receipts[0], original)
        receipt = self.seal_batch(followup)
        self.assertEqual(receipt, account.receipts[1])
        self.assertLess(receipt.position, decision.position)
        with self.assertRaises(StageRequestConflict):
            self.stage_value('F', 8)
        from publication.admission import seal
        with self.assertRaises(AdmissionBindingConflict):
            seal(self.catalog, self.warehouse, 'F', original.batch_id)
        self.assert_day(2, 12, (original, receipt))
        self.take_catalog_offline()
        row, = claim(self.warehouse, receipt.receipt_id).rows
        self.assertEqual((row.device_id, row.line_number, row.value, row.unit), ('device-p', 17, 7, 'mL'))
