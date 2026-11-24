import sqlite3
from dataclasses import dataclass

from archive.integrity import calculate_archive_digest
from archive.rows import ArchivedRow, write_archive_rows
from intake.staging import StagedBatch


@dataclass(frozen=True)
class ArchiveBatch:
    batch_id: int
    point_id: str
    business_day: str
    row_count: int
    digest: str


def freeze_archive(warehouse: sqlite3.Connection, staged: StagedBatch) -> ArchiveBatch:
    rows = tuple(
        ArchivedRow(row.position, row.source.device_id, row.source.line_number, row.source.value,
                    row.source.unit, row.source.missing)
        for row in staged.rows
    )
    digest = calculate_archive_digest(staged.batch_id, staged.point_id, staged.business_day, rows)
    manifest = ArchiveBatch(staged.batch_id, staged.point_id, staged.business_day, len(rows), digest)
    warehouse.execute(
        '''INSERT INTO archive_batches(batch_id, point_id, business_day, row_count, digest)
           VALUES (?, ?, ?, ?, ?)''',
        (manifest.batch_id, manifest.point_id, manifest.business_day, manifest.row_count, manifest.digest),
    )
    write_archive_rows(warehouse, manifest.batch_id, rows)
    return manifest


def load_archive_manifest(warehouse: sqlite3.Connection, batch_id: int) -> ArchiveBatch | None:
    row = warehouse.execute(
        'SELECT batch_id, point_id, business_day, row_count, digest FROM archive_batches WHERE batch_id = ?',
        (batch_id,),
    ).fetchone()
    return ArchiveBatch(*row) if row is not None else None
