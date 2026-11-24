import sqlite3
from dataclasses import dataclass

from intake.staging import StagedBatch
from intake.units import decode_integer, encode_integer


@dataclass(frozen=True)
class FactRow:
    position: int
    device_id: str
    line_number: int
    milliliters: int | None


@dataclass(frozen=True)
class FactBatch:
    position: int
    batch_id: int
    request_id: str
    point_id: str
    business_day: str
    rows: tuple[FactRow, ...]


def record_fact_batch(
    warehouse: sqlite3.Connection, position: int, staged: StagedBatch
) -> FactBatch:
    rows = tuple(
        FactRow(row.position, row.source.device_id, row.source.line_number, row.milliliters)
        for row in staged.rows
    )
    warehouse.execute(
        '''INSERT INTO fact_batches(position, batch_id, request_id, point_id, business_day, row_count)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (position, staged.batch_id, staged.request_id, staged.point_id, staged.business_day, len(rows)),
    )
    warehouse.executemany(
        '''INSERT INTO fact_rows(fact_position, ordinal, device_id, line_number, milliliters)
           VALUES (?, ?, ?, ?, ?)''',
        (
            (position, row.position, row.device_id, hex(row.line_number), encode_integer(row.milliliters))
            for row in rows
        ),
    )
    return FactBatch(position, staged.batch_id, staged.request_id, staged.point_id, staged.business_day, rows)


def _load_fact_batch(warehouse: sqlite3.Connection, header: tuple) -> FactBatch:
    position, batch_id, request_id, point_id, business_day, count = header
    entries = warehouse.execute(
        '''SELECT ordinal, device_id, line_number, milliliters FROM fact_rows
           WHERE fact_position = ? ORDER BY ordinal''',
        (position,),
    )
    rows = tuple(
        FactRow(ordinal, device_id, int(line_number, 16), decode_integer(milliliters))
        for ordinal, device_id, line_number, milliliters in entries
    )
    if len(rows) != count or any(row.position != index for index, row in enumerate(rows)):
        raise RuntimeError(f'published batch {batch_id} has incomplete facts')
    return FactBatch(position, batch_id, request_id, point_id, business_day, rows)


def find_fact_by_request(warehouse: sqlite3.Connection, request_id: str) -> FactBatch | None:
    header = warehouse.execute(
        '''SELECT position, batch_id, request_id, point_id, business_day, row_count
           FROM fact_batches WHERE request_id = ?''',
        (request_id,),
    ).fetchone()
    return _load_fact_batch(warehouse, header) if header is not None else None


def read_day_facts(
    warehouse: sqlite3.Connection, point_id: str, business_day: str
) -> tuple[FactBatch, ...]:
    headers = warehouse.execute(
        '''SELECT position, batch_id, request_id, point_id, business_day, row_count
           FROM fact_batches WHERE point_id = ? AND business_day = ? ORDER BY position''',
        (point_id, business_day),
    )
    return tuple(_load_fact_batch(warehouse, header) for header in headers)
