from intake.requests import find_staged_request
from intake.rows import SourceRow
from intake.staging import UnknownStagedBatch
from intake.units import InvalidReading
from publication.admission import seal
from tests.fixtures import WarehouseCase, mixed_rows


class IntakeTests(WarehouseCase):
    def test_units_and_missing(self):
        source = mixed_rows()
        batch = self.stage_rows('mixed', source)
        self.assertEqual((batch.row_count, batch.total_ml), (3, 1005))
        self.assertEqual(tuple(row.milliliters for row in batch.rows), (1000, 5, None))
        self.assertEqual(tuple(row.source for row in batch.rows), source)
        self.assert_day(0, 0, ())

    def test_invalid_unit_rejects_batch(self):
        rows = (SourceRow('device-p', 4, 5, 'mL'), SourceRow('device-p', 8, 7, 'g'))
        with self.assertRaises(InvalidReading):
            self.stage_rows('invalid', rows)
        self.assertIsNone(find_staged_request(self.warehouse, 'invalid'))
        with self.assertRaises(UnknownStagedBatch):
            seal(self.catalog, self.warehouse, 'invalid', 1)
        self.assert_day(0, 0, ())
