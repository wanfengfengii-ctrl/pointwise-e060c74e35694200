import sqlite3
from dataclasses import dataclass
from itertools import zip_longest

from account.day import DaySnapshot
from account.totals import DayTotals, summarize_rows
from archive.index import UnknownArchiveReceipt
from archive.integrity import ArchiveIntegrityError
from archive.retrieve import claim
from intake.units import InvalidReading, to_milliliters
from publication.facts import FactRow


@dataclass(frozen=True)
class ReconciliationIssue:
    reason: str
    receipt_id: str | None = None
    line_number: int | None = None
    expected: object = None
    actual: object = None


def reconcile_day(warehouse: sqlite3.Connection, snapshot: DaySnapshot) -> tuple[ReconciliationIssue, ...]:
    issues = []
    volume = 0
    row_count = 0
    all_archives_read = True
    account = snapshot.account
    for evidence in snapshot.batches:
        receipt_id = evidence.receipt.receipt_id
        try:
            original = claim(warehouse, receipt_id)
            actual_rows = tuple(
                FactRow(
                    row.position, row.device_id, row.line_number,
                    to_milliliters(row.value, row.unit, row.missing),
                )
                for row in original.rows
            )
        except (UnknownArchiveReceipt, ArchiveIntegrityError, InvalidReading) as error:
            issues.append(ReconciliationIssue('archive_unavailable', receipt_id, actual=str(error)))
            all_archives_read = False
            continue
        expected_identity = (evidence.receipt.batch_id, account.point_id, account.business_day)
        actual_identity = (original.batch_id, original.point_id, original.business_day)
        if expected_identity != actual_identity:
            issues.append(ReconciliationIssue(
                'batch_identity', receipt_id, expected=expected_identity, actual=actual_identity
            ))
        for fact, actual in zip_longest(evidence.rows, actual_rows):
            if fact != actual:
                source = fact if fact is not None else actual
                issues.append(ReconciliationIssue('source_row', receipt_id, source.line_number, fact, actual))
        expected = summarize_rows(row.milliliters for row in evidence.rows)
        actual = summarize_rows(row.milliliters for row in actual_rows)
        if expected != actual:
            issues.append(ReconciliationIssue('batch_totals', receipt_id, expected=expected, actual=actual))
        row_count += actual.row_count
        volume += actual.total_ml
    expected_day = DayTotals(account.row_count, account.total_ml)
    actual_day = DayTotals(row_count, volume)
    if all_archives_read and expected_day != actual_day:
        issues.append(ReconciliationIssue('day_totals', expected=expected_day, actual=actual_day))
    return tuple(issues)
