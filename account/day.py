import sqlite3
from dataclasses import dataclass

from account.evidence import BatchEvidence, ordered_receipts, read_day_evidence
from account.totals import summarize_rows
from catalog.registry import get_point
from intake.identity import check_business_day
from publication.receipts import AdmissionReceipt
from publication.storage import warehouse_snapshot


@dataclass(frozen=True)
class DayAccount:
    point_id: str
    business_day: str
    row_count: int
    total_ml: int
    receipts: tuple[AdmissionReceipt, ...]


@dataclass(frozen=True)
class DaySnapshot:
    account: DayAccount
    batches: tuple[BatchEvidence, ...]


def read_day_snapshot(
    catalog: sqlite3.Connection, warehouse: sqlite3.Connection, point_id: str, business_day: str
) -> DaySnapshot:
    check_business_day(business_day)
    get_point(catalog, point_id)
    with warehouse_snapshot(warehouse):
        batches = read_day_evidence(warehouse, point_id, business_day)
        totals = summarize_rows(row.milliliters for batch in batches for row in batch.rows)
        account = DayAccount(point_id, business_day, totals.row_count, totals.total_ml, ordered_receipts(batches))
    return DaySnapshot(account, batches)


def read_day(
    catalog: sqlite3.Connection, warehouse: sqlite3.Connection, point_id: str, business_day: str
) -> DayAccount:
    return read_day_snapshot(catalog, warehouse, point_id, business_day).account
