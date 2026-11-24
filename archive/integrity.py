from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from archive.rows import ArchivedRow
from intake.units import encode_integer

if TYPE_CHECKING:
    from archive.batches import ArchiveBatch
    from archive.index import ArchiveBinding


class ArchiveIntegrityError(ValueError):
    """The archive cannot be proved complete for the given receipt."""


def calculate_archive_digest(
    batch_id: int, point_id: str, business_day: str, rows: tuple[ArchivedRow, ...]
) -> str:
    original = [
        (row.position, row.device_id, hex(row.line_number), encode_integer(row.value), row.unit, row.missing)
        for row in rows
    ]
    payload = json.dumps((batch_id, point_id, business_day, original), separators=(',', ':')).encode('ascii')
    return hashlib.sha256(payload).hexdigest()


def verify_archive(
    binding: ArchiveBinding, manifest: ArchiveBatch, rows: tuple[ArchivedRow, ...]
) -> None:
    identity = (manifest.batch_id, manifest.point_id, manifest.business_day, manifest.row_count)
    expected = (binding.fact_batch_id, binding.point_id, binding.business_day, binding.row_count)
    if binding.archive_batch_id != binding.fact_batch_id or identity != expected:
        raise ArchiveIntegrityError('archive manifest does not match the receipt and published batch')
    if len(rows) != manifest.row_count or any(row.position != index for index, row in enumerate(rows)):
        raise ArchiveIntegrityError(f'archive batch {manifest.batch_id} has missing or misplaced rows')
    digest = calculate_archive_digest(manifest.batch_id, manifest.point_id, manifest.business_day, rows)
    if digest != manifest.digest:
        raise ArchiveIntegrityError(f'archive batch {manifest.batch_id} has changed content')
