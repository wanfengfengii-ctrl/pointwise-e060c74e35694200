import sqlite3

from account.day import read_day_snapshot
from account.reconcile import reconcile_day
from account.totals import DayTotals
from archive.batches import load_archive_manifest
from archive.index import UnknownArchiveReceipt
from archive.integrity import ArchiveIntegrityError
from archive.retrieve import claim
from catalog.devices import DeviceBindingConflict, UnknownDevice, resolve_device
from catalog.lifecycle import PointStatus, read_point_status
from catalog.registry import PointRegistrationConflict, UnknownPoint, get_point, initialize_catalog, register
from catalog.retirement import PointAlreadyRetired, RetirementRequestConflict, retire
from catalog.retirement_log import find_retirement_request
from intake.identity import InvalidBusinessDay
from intake.requests import StageRequestConflict, find_staged_request
from intake.rows import InvalidSourceRow, SourceRow
from intake.staging import UnknownStagedBatch
from intake.units import InvalidReading
from publication.admission import seal
from publication.retry import AdmissionBindingConflict
from publication.storage import initialize_warehouse
from tests.fixtures import DAY, WarehouseCase, fail_commit, mixed_rows


class FrozenScenarioTests(WarehouseCase):
    def test_register_conflict(self):
        with self.assertRaises(PointRegistrationConflict):
            register(self.catalog, 'P', 'other-device')
        with self.assertRaises(DeviceBindingConflict):
            register(self.catalog, 'Q', 'device-p')
        with self.assertRaises(UnknownPoint):
            get_point(self.catalog, 'Q')
        with self.assertRaises(UnknownDevice):
            resolve_device(self.catalog, 'other-device')
        self.assertEqual(resolve_device(self.catalog, 'device-p'), 'P')
        registration = register(self.catalog, 'Q', 'device-q')
        self.assertEqual((registration.point_id, registration.device_id, registration.status), ('Q', 'device-q', 'active'))

    def test_initialization(self):
        register(self.catalog, 'CUSTOM', 'user-device')
        receipt = self.original_receipt()
        decision = retire(self.catalog, self.warehouse, 'stop', 'P')
        self.reopen()
        for _ in range(2):
            initialize_catalog(self.catalog)
            initialize_warehouse(self.warehouse)
        self.assertEqual(get_point(self.catalog, 'CUSTOM').device_id, 'user-device')
        self.assertEqual(retire(self.catalog, self.warehouse, 'stop', 'P'), decision)
        self.assertEqual(read_point_status(self.catalog, self.warehouse, 'P'), PointStatus.RETIRED)
        self.assert_day(1, 5, (receipt,))

    def test_published_counts(self):
        batch = self.stage_rows('mixed', mixed_rows())
        receipt = self.seal_batch(batch)
        self.assert_day(3, 1005, (receipt,))
        self.assertEqual(len(claim(self.warehouse, receipt.receipt_id).rows), 3)

    def test_missing_count(self):
        first = self.seal_batch(self.stage_rows('mixed', mixed_rows()))
        second = self.seal_batch(self.stage_value('extra', 7))
        self.assert_day(4, 1012, (first, second))
        self.take_catalog_offline()
        rows = claim(self.warehouse, first.receipt_id).rows + claim(self.warehouse, second.receipt_id).rows
        self.assertEqual(len(rows), 4)
        self.assertEqual([row.line_number for row in rows if row.missing], [20])
        self.assertIsNone(rows[2].value)

    def test_exact_integer_storage(self):
        big = 10**40
        rows = (SourceRow('device-p', big, big + 1, 'L'), SourceRow('device-p', 2, -5, 'mL'))
        staged = self.stage_rows('large', rows)
        self.assertEqual(staged.total_ml, 10**43 + 995)
        self.reopen()
        receipt = self.seal_batch(staged)
        self.assert_day(2, 10**43 + 995, (receipt,))
        first = claim(self.warehouse, receipt.receipt_id).rows[0]
        self.assertEqual((first.line_number, first.value), (big, big + 1))

    def test_invalid_batch_rollback(self):
        original = self.original_receipt()
        valid_prefix = SourceRow('device-p', 8, 5, 'mL')
        with self.assertRaises(InvalidReading):
            self.stage_rows('bad', (valid_prefix, SourceRow('device-p', 9, 7, 'bad')))
        self.reopen()
        self.assertIsNone(find_staged_request(self.warehouse, 'bad'))
        with self.assertRaises(UnknownStagedBatch):
            seal(self.catalog, self.warehouse, 'bad', original.batch_id + 1)
        self.assert_day(1, 5, (original,))
        corrected = self.stage_rows('bad', (valid_prefix,))
        self.assertEqual((corrected.row_count, corrected.total_ml), (1, 5))

    def test_bad_source_and_reading_are_distinct(self):
        register(self.catalog, 'Q', 'device-q')
        with self.assertRaises(UnknownDevice):
            self.stage_rows('unknown', (SourceRow('absent', 17, 7, 'mL'),))
        with self.assertRaises(InvalidSourceRow):
            self.stage_rows('wrong', (SourceRow('device-q', 17, 7, 'mL'),))
        with self.assertRaises(InvalidReading):
            self.stage_rows('boolean', (SourceRow('device-p', 17, True, 'mL'),))
        with self.assertRaises(InvalidReading):
            self.stage_rows('implicit', (SourceRow('device-p', 17, None, 'mL'),))
        for request in ('unknown', 'wrong', 'boolean', 'implicit'):
            self.assertIsNone(find_staged_request(self.warehouse, request))
        self.assert_day(0, 0, ())

    def test_retirement_replay(self):
        register(self.catalog, 'Q', 'device-q')
        with self.assertRaises(UnknownPoint):
            retire(self.catalog, self.warehouse, 'unknown', 'absent')
        self.assertIsNone(find_retirement_request(self.warehouse, 'unknown'))
        decision = retire(self.catalog, self.warehouse, 'stop', 'P')
        self.reopen()
        self.assertEqual(retire(self.catalog, self.warehouse, 'stop', 'P'), decision)
        with self.assertRaises(RetirementRequestConflict):
            retire(self.catalog, self.warehouse, 'stop', 'Q')
        with self.assertRaises(PointAlreadyRetired):
            retire(self.catalog, self.warehouse, 'another-stop', 'P')
        self.assertEqual(read_point_status(self.catalog, self.warehouse, 'Q'), PointStatus.ACTIVE)

    def test_retirement_authority(self):
        original = self.original_receipt()
        followup = self.stage_value('F', 7)
        self.assert_day(1, 5, (original,))
        decision = retire(self.catalog, self.warehouse, 'stop', 'P')
        self.reopen()
        self.assert_retired_batch_denied(followup, original)
        self.assertEqual(find_retirement_request(self.warehouse, 'stop'), decision)
        self.assertEqual(read_point_status(self.catalog, self.warehouse, 'P'), PointStatus.RETIRED)
        self.assertGreater(decision.position, original.position)

    def test_stale_eligibility(self):
        original = self.original_receipt()
        followup = self.stage_value('F', 7)
        completed = []

        def retire_after_read(status):
            self.assertEqual(status, PointStatus.ACTIVE)
            with self.other_connections() as (catalog, warehouse):
                completed.append(retire(catalog, warehouse, 'stop', 'P'))

        self.assert_retired_batch_denied(followup, original, before_decision=retire_after_read)
        decision, = completed
        self.assertEqual(find_retirement_request(self.warehouse, 'stop'), decision)

    def test_retirement_commit_failure(self):
        original = self.original_receipt()
        followup = self.stage_value('F', 7)
        with fail_commit(self.warehouse, 'retirement') as statements:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY constraint failed'):
                retire(self.catalog, self.warehouse, 'stop', 'P')
            self.assertIn('COMMIT', statements)
            self.assertFalse(self.warehouse.in_transaction)
        self.reopen()
        self.assertIsNone(find_retirement_request(self.warehouse, 'stop'))
        self.assertEqual(read_point_status(self.catalog, self.warehouse, 'P'), PointStatus.ACTIVE)
        receipt = self.seal_batch(followup)
        retirement = retire(self.catalog, self.warehouse, 'stop', 'P')
        self.assertLess(receipt.position, retirement.position)
        self.assert_day(2, 12, (original, receipt))

    def test_commit_rollback_then_retry(self):
        original = self.original_receipt()
        followup = self.stage_value('F', 7)
        with fail_commit(self.warehouse, 'admission'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.seal_batch(followup)
        self.reopen()
        self.assert_day(1, 5, (original,))
        self.assertIsNone(load_archive_manifest(self.warehouse, followup.batch_id))
        with self.assertRaises(UnknownArchiveReceipt):
            claim(self.warehouse, 'admission-2')
        self.assertEqual(claim(self.warehouse, original.receipt_id).rows[0].value, 5)
        receipt = self.seal_batch(followup)
        self.assert_day(2, 12, (original, receipt))
        self.assertEqual(claim(self.warehouse, receipt.receipt_id).rows[0].value, 7)

    def test_exact_stage_replay(self):
        rows = mixed_rows()
        batch = self.stage_rows('mixed', rows)
        self.reopen()
        self.assertEqual(self.stage_rows('mixed', iter(rows)), batch)
        with self.assertRaises(StageRequestConflict):
            self.stage_rows('mixed', reversed(rows))
        with self.assertRaises(StageRequestConflict):
            self.stage_rows('mixed', rows, business_day='2026-11-19')
        wrong = self.stage_value('other', 7)
        with self.assertRaises(AdmissionBindingConflict):
            seal(self.catalog, self.warehouse, 'mixed', wrong.batch_id)
        self.assert_day(0, 0, ())

    def test_same_total_different_payload(self):
        batch = self.stage_rows('named', (SourceRow('device-p', 17, 1, 'L'),))
        receipt = self.seal_batch(batch)
        with self.assertRaises(StageRequestConflict):
            self.stage_rows('named', (SourceRow('device-p', 17, 1000, 'mL'),))
        self.assertEqual(self.seal_batch(batch), receipt)
        self.assert_day(1, 1000, (receipt,))
        row, = claim(self.warehouse, receipt.receipt_id).rows
        self.assertEqual((row.value, row.unit), (1, 'L'))

    def test_day_partition_order(self):
        register(self.catalog, 'Q', 'device-q')
        early = self.stage_value('staged-first', 7)
        elsewhere = self.stage_rows('other-point', (SourceRow('device-q', 5, 25, 'mL'),), point='Q')
        other_day = self.stage_rows('other-day', (SourceRow('device-p', 8, 12, 'mL'),), business_day='2026-11-19')
        first = self.original_receipt()
        second_point = self.seal_batch(elsewhere)
        next_day = self.seal_batch(other_day)
        last = self.seal_batch(early)
        self.assert_day(2, 12, (first, last))
        self.assertGreater(first.batch_id, last.batch_id)
        self.assertLess(first.position, last.position)
        point_result = self.account(point='Q')
        self.assertEqual((point_result.row_count, point_result.total_ml, point_result.receipts), (1, 25, (second_point,)))
        day_result = self.account(business_day='2026-11-19')
        self.assertEqual((day_result.row_count, day_result.total_ml, day_result.receipts), (1, 12, (next_day,)))

    def test_empty_or_invalid_day(self):
        self.assert_day(0, 0, ())
        for invalid in ('2026-02-29', '20261118', '2026-11-8'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InvalidBusinessDay):
                    self.account(business_day=invalid)
        with self.assertRaises(UnknownPoint):
            self.account(point='absent')

    def test_raw_order(self):
        receipt = self.seal_batch(self.stage_rows('mixed', mixed_rows()))
        self.take_catalog_offline()
        original = claim(self.warehouse, receipt.receipt_id)
        self.assertEqual(claim(self.warehouse, receipt.receipt_id), original)
        self.assertEqual([row.position for row in original.rows], [0, 1, 2])
        self.assertEqual([row.line_number for row in original.rows], [30, 10, 20])
        self.assertEqual([row.device_id for row in original.rows], ['device-p'] * 3)
        self.assertEqual([(row.value, row.unit) for row in original.rows], [(1, 'L'), (5, 'mL'), (None, 'mL')])

    def test_archive_integrity(self):
        damaged = self.seal_batch(self.stage_rows('mixed', mixed_rows()))
        intact = self.seal_batch(self.stage_value('extra', 7))
        self.warehouse.execute('DELETE FROM archive_rows WHERE batch_id = ? AND ordinal = 1', (damaged.batch_id,))
        self.take_catalog_offline()
        with self.assertRaises(ArchiveIntegrityError):
            claim(self.warehouse, damaged.receipt_id)
        self.assertEqual(claim(self.warehouse, intact.receipt_id).rows[0].value, 7)
        with self.assertRaises(UnknownArchiveReceipt):
            claim(self.warehouse, 'unsent')
        self.warehouse.execute('UPDATE archive_rows SET raw_value = ? WHERE batch_id = ?', ('0x8', intact.batch_id))
        with self.assertRaises(ArchiveIntegrityError):
            claim(self.warehouse, intact.receipt_id)

    def test_reconcile_evidence(self):
        original = self.original_receipt()
        second = self.seal_batch(self.stage_value('followup', 7))
        old_snapshot = read_day_snapshot(self.catalog, self.warehouse, 'P', DAY)
        self.warehouse.execute(
            'UPDATE fact_rows SET milliliters = ? WHERE fact_position = ? AND ordinal = 0',
            ('0x8', second.position),
        )
        new_snapshot = read_day_snapshot(self.catalog, self.warehouse, 'P', DAY)
        self.take_catalog_offline()
        self.assertEqual(reconcile_day(self.warehouse, old_snapshot), ())
        row, batch, day = reconcile_day(self.warehouse, new_snapshot)
        self.assertEqual((row.reason, row.receipt_id, row.line_number), ('source_row', second.receipt_id, 17))
        self.assertEqual((row.expected.milliliters, row.actual.milliliters), (8, 7))
        self.assertEqual((batch.receipt_id, batch.expected, batch.actual),
                         (second.receipt_id, DayTotals(1, 8), DayTotals(1, 7)))
        self.assertEqual((day.reason, day.expected, day.actual), ('day_totals', DayTotals(2, 13), DayTotals(2, 12)))
        self.warehouse.execute('DELETE FROM archive_rows WHERE batch_id = ?', (original.batch_id,))
        issue, = reconcile_day(self.warehouse, old_snapshot)
        self.assertEqual((issue.reason, issue.receipt_id), ('archive_unavailable', original.receipt_id))
