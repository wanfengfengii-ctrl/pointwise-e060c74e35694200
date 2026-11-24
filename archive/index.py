import sqlite3
from dataclasses import dataclass


class UnknownArchiveReceipt(LookupError):
    """The warehouse has no successful admission with this identifier."""


@dataclass(frozen=True)
class ArchiveBinding:
    archive_batch_id: int | None
    fact_batch_id: int
    point_id: str
    business_day: str
    row_count: int


def bind_archive_receipt(warehouse: sqlite3.Connection, receipt_id: str, batch_id: int) -> None:
    warehouse.execute(
        'INSERT INTO archive_receipts(receipt_id, batch_id) VALUES (?, ?)', (receipt_id, batch_id)
    )


def find_archive_binding(warehouse: sqlite3.Connection, receipt_id: str) -> ArchiveBinding:
    row = warehouse.execute(
        '''SELECT a.batch_id, f.batch_id, f.point_id, f.business_day, f.row_count
           FROM admission_receipts AS r
           JOIN fact_batches AS f USING (position)
           LEFT JOIN archive_receipts AS a USING (receipt_id)
           WHERE r.receipt_id = ?''',
        (receipt_id,),
    ).fetchone()
    if row is None:
        raise UnknownArchiveReceipt(f'unknown admission receipt {receipt_id!r}')
    return ArchiveBinding(*row)
