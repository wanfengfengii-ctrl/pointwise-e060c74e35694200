import sqlite3
from dataclasses import dataclass

from publication.facts import FactRow, read_day_facts
from publication.receipts import AdmissionReceipt, find_admission_receipt


@dataclass(frozen=True)
class BatchEvidence:
    receipt: AdmissionReceipt
    rows: tuple[FactRow, ...]


def read_day_evidence(
    warehouse: sqlite3.Connection, point_id: str, business_day: str
) -> tuple[BatchEvidence, ...]:
    batches = []
    for fact in read_day_facts(warehouse, point_id, business_day):
        receipt = find_admission_receipt(warehouse, fact.position)
        if receipt is None:
            raise RuntimeError(f'admitted batch {fact.batch_id} has no stored receipt')
        batches.append(BatchEvidence(receipt, fact.rows))
    return tuple(batches)


def ordered_receipts(evidence: tuple[BatchEvidence, ...]) -> tuple[AdmissionReceipt, ...]:
    return tuple(batch.receipt for batch in evidence)
