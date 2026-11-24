import sqlite3

from publication.facts import find_fact_by_request
from publication.receipts import AdmissionReceipt, find_admission_receipt


class AdmissionBindingConflict(ValueError):
    """The named request cannot be signed with this staged batch."""


def resolve_admission_replay(
    warehouse: sqlite3.Connection, request_id: str, batch_id: int
) -> AdmissionReceipt | None:
    fact = find_fact_by_request(warehouse, request_id)
    if fact is None:
        return None
    if fact.batch_id != batch_id:
        raise AdmissionBindingConflict(f'admission {request_id!r} belongs to batch {fact.batch_id}')
    receipt = find_admission_receipt(warehouse, fact.position)
    if receipt is None:
        raise RuntimeError(f'admitted batch {fact.batch_id} has no stored receipt')
    return receipt
