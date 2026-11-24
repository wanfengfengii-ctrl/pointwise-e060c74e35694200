from account.day import read_day_snapshot
from account.reconcile import reconcile_day
from archive.retrieve import claim
from tests.fixtures import DAY, WarehouseCase, mixed_rows


class AuditTests(WarehouseCase):
    def test_offline_claim_matches_day(self):
        extra = self.stage_value('extra', 7)
        mixed = self.stage_rows('mixed', mixed_rows())
        first = self.seal_batch(mixed)
        second = self.seal_batch(extra)
        self.assert_day(4, 1012, (first, second))
        snapshot = read_day_snapshot(self.catalog, self.warehouse, 'P', DAY)
        self.take_catalog_offline()
        first_claim = claim(self.warehouse, first.receipt_id)
        second_claim = claim(self.warehouse, second.receipt_id)
        self.assertEqual((first_claim.point_id, first_claim.business_day), ('P', DAY))
        all_rows = first_claim.rows + second_claim.rows
        self.assertEqual(
            [(row.line_number, row.value, row.unit, row.missing) for row in all_rows],
            [(30, 1, 'L', False), (10, 5, 'mL', False), (20, None, 'mL', True), (17, 7, 'mL', False)],
        )
        manual_volume = sum(
            row.value * (1000 if row.unit == 'L' else 1) for row in all_rows if not row.missing
        )
        self.assertEqual((len(all_rows), manual_volume), (snapshot.account.row_count, snapshot.account.total_ml))
        self.assertEqual(reconcile_day(self.warehouse, snapshot), ())
