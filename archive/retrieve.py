import sqlite3
from dataclasses import dataclass

from archive.batches import load_archive_manifest
from archive.index import find_archive_binding
from archive.integrity import ArchiveIntegrityError, verify_archive
from archive.rows import ArchivedRow, load_archive_rows
from publication.storage import warehouse_snapshot


@dataclass(frozen=True)
class ClaimedBatch:
    receipt_id: str
    batch_id: int
    point_id: str
    business_day: str
    rows: tuple[ArchivedRow, ...]


def claim(warehouse: sqlite3.Connection, receipt_id: str) -> ClaimedBatch:
    if type(receipt_id) is not str or not receipt_id.strip():
        raise ValueError('receipt_id must be a non-empty string')
    with warehouse_snapshot(warehouse):
        binding = find_archive_binding(warehouse, receipt_id)
        if binding.archive_batch_id is None:
            raise ArchiveIntegrityError(f'receipt {receipt_id!r} is missing its archive binding')
        manifest = load_archive_manifest(warehouse, binding.archive_batch_id)
        if manifest is None:
            raise ArchiveIntegrityError(f'receipt {receipt_id!r} is missing its archive manifest')
        try:
            rows = load_archive_rows(warehouse, manifest.batch_id)
        except (TypeError, ValueError) as error:
            raise ArchiveIntegrityError(f'archive batch {manifest.batch_id} has invalid row data') from error
        verify_archive(binding, manifest, rows)
        return ClaimedBatch(receipt_id, manifest.batch_id, manifest.point_id, manifest.business_day, rows)
