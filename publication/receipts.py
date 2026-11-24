import sqlite3
from dataclasses import dataclass

from publication.facts import FactBatch


@dataclass(frozen=True)
class AdmissionReceipt:
    receipt_id: str
    position: int
    batch_id: int
    request_id: str


def record_admission_receipt(warehouse: sqlite3.Connection, batch: FactBatch) -> AdmissionReceipt:
    receipt_id = f'admission-{batch.position}'
    warehouse.execute(
        'INSERT INTO admission_receipts(position, receipt_id) VALUES (?, ?)',
        (batch.position, receipt_id),
    )
    return AdmissionReceipt(receipt_id, batch.position, batch.batch_id, batch.request_id)


def find_admission_receipt(warehouse: sqlite3.Connection, position: int) -> AdmissionReceipt | None:
    row = warehouse.execute(
        '''SELECT r.receipt_id, r.position, f.batch_id, f.request_id
           FROM admission_receipts AS r JOIN fact_batches AS f USING (position)
           WHERE r.position = ?''',
        (position,),
    ).fetchone()
    return AdmissionReceipt(*row) if row is not None else None
