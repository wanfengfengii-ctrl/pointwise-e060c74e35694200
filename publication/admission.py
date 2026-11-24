import sqlite3
from collections.abc import Callable

from archive.batches import freeze_archive
from archive.index import bind_archive_receipt
from catalog.devices import require_identifier
from catalog.lifecycle import PointStatus, is_admissible, read_point_status
from catalog.registry import get_point
from intake.requests import find_staged_request
from intake.staging import StagedBatch, load_staged_batch
from publication.facts import record_fact_batch
from publication.receipts import AdmissionReceipt, record_admission_receipt
from publication.retry import AdmissionBindingConflict, resolve_admission_replay
from publication.storage import next_decision_position, warehouse_snapshot, warehouse_write


class RetiredPoint(ValueError):
    """A retired sampling point cannot receive a first admission."""


def _bound_batch(warehouse: sqlite3.Connection, request_id: str, batch_id: int) -> StagedBatch:
    binding = find_staged_request(warehouse, request_id)
    if binding is not None and binding.batch_id != batch_id:
        raise AdmissionBindingConflict(f'staging request {request_id!r} belongs to another batch')
    staged = load_staged_batch(warehouse, batch_id)
    if staged.request_id != request_id:
        raise AdmissionBindingConflict(f'batch {batch_id} belongs to another staging request')
    return staged


def seal(
    catalog: sqlite3.Connection,
    warehouse: sqlite3.Connection,
    request_id: str,
    batch_id: int,
    *,
    before_decision: Callable[[PointStatus], None] | None = None,
    after_commit: Callable[[], None] | None = None,
) -> AdmissionReceipt:
    """Return the durable receipt, with optional observers outside the write transaction.

    The pre-decision observer receives the point status from an advisory read.
    The post-commit observer runs after success is durable and before returning the receipt.
    """
    require_identifier(request_id, 'request_id')
    if type(batch_id) is not int or batch_id < 1 or batch_id >= 2**63:
        raise ValueError('batch_id must be a positive SQLite row identifier')
    if before_decision is not None:
        with warehouse_snapshot(warehouse):
            staged = _bound_batch(warehouse, request_id, batch_id)
            status = read_point_status(catalog, warehouse, staged.point_id)
        before_decision(status)
    with warehouse_write(warehouse):
        receipt = resolve_admission_replay(warehouse, request_id, batch_id)
        if receipt is None:
            staged = _bound_batch(warehouse, request_id, batch_id)
            point = get_point(catalog, staged.point_id)
            if not is_admissible(warehouse, point.point_id):
                raise RetiredPoint(f'point {point.point_id!r} is retired')
            position = next_decision_position(warehouse, 'admit', point.point_id)
            fact = record_fact_batch(warehouse, position, staged)
            receipt = record_admission_receipt(warehouse, fact)
            archived = freeze_archive(warehouse, staged)
            bind_archive_receipt(warehouse, receipt.receipt_id, archived.batch_id)
    if after_commit is not None:
        after_commit()
    return receipt
