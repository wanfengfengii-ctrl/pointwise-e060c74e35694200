import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial

from catalog.devices import require_identifier, resolve_device
from catalog.registry import get_point
from intake.identity import StageParameters, check_business_day
from intake.requests import StagedRequest, bind_staged_request, find_staged_request
from intake.rows import SourceRow, ValidatedRow, validate_rows
from intake.units import decode_integer, encode_integer
from publication.storage import warehouse_write


@dataclass(frozen=True)
class StagedBatch:
    batch_id: int
    request_id: str
    point_id: str
    business_day: str
    row_count: int
    total_ml: int
    rows: tuple[ValidatedRow, ...]


class UnknownStagedBatch(LookupError):
    """The identifier does not name a complete staged batch."""


class StagedBatchIntegrityError(RuntimeError):
    """The staged data does not match its fixed request or header."""


def _total_ml(rows: Iterable[ValidatedRow]) -> int:
    return sum(row.milliliters for row in rows if row.milliliters is not None)


def stage(
    catalog: sqlite3.Connection,
    warehouse: sqlite3.Connection,
    request_id: str,
    point_id: str,
    business_day: str,
    rows: Iterable[SourceRow],
) -> StagedBatch:
    require_identifier(request_id, 'request_id')
    check_business_day(business_day)
    point = get_point(catalog, point_id)
    validated = validate_rows(rows, point.point_id, partial(resolve_device, catalog))
    parameters = StageParameters(point_id, business_day, tuple(row.source for row in validated))
    with warehouse_write(warehouse):
        existing = find_staged_request(warehouse, request_id)
        if existing is not None:
            existing.require_match(parameters)
            return load_staged_batch(warehouse, existing.batch_id)
        return _store_batch(warehouse, request_id, parameters, validated)


def _store_batch(
    warehouse: sqlite3.Connection,
    request_id: str,
    parameters: StageParameters,
    rows: tuple[ValidatedRow, ...],
) -> StagedBatch:
    total_ml = _total_ml(rows)
    header = warehouse.execute(
        '''INSERT INTO staged_batches(point_id, business_day, row_count, total_ml)
           VALUES (?, ?, ?, ?)''',
        (parameters.point_id, parameters.business_day, len(rows), hex(total_ml)),
    )
    batch_id = header.lastrowid
    warehouse.executemany(
        '''INSERT INTO staged_rows(batch_id, ordinal, device_id, line_number, raw_value,
                                   unit, missing, milliliters)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            (batch_id, row.position, row.source.device_id, hex(row.source.line_number),
             encode_integer(row.source.value), row.source.unit, int(row.source.missing),
             encode_integer(row.milliliters))
            for row in rows
        ),
    )
    bind_staged_request(warehouse, request_id, batch_id, parameters)
    return StagedBatch(
        batch_id, request_id, parameters.point_id, parameters.business_day, len(rows), total_ml, rows
    )


def load_staged_batch(warehouse: sqlite3.Connection, batch_id: int) -> StagedBatch:
    """Load a complete batch inside a warehouse write or read transaction."""
    header = warehouse.execute(
        '''SELECT r.request_id, r.parameters, b.point_id, b.business_day, b.row_count, b.total_ml
           FROM staged_batches AS b JOIN staged_requests AS r USING (batch_id)
           WHERE b.batch_id = ?''',
        (batch_id,),
    ).fetchone()
    if header is None:
        raise UnknownStagedBatch(f'unknown staged batch {batch_id!r}')
    request_id, encoded_parameters, point_id, business_day, row_count, stored_total = header
    entries = warehouse.execute(
        '''SELECT ordinal, device_id, line_number, raw_value, unit, missing, milliliters
           FROM staged_rows WHERE batch_id = ? ORDER BY ordinal''',
        (batch_id,),
    )
    rows = tuple(
        ValidatedRow(
            ordinal,
            SourceRow(device_id, int(line_number, 16), decode_integer(raw_value), unit, bool(missing)),
            decode_integer(milliliters),
        )
        for ordinal, device_id, line_number, raw_value, unit, missing, milliliters in entries
    )
    total_ml = int(stored_total, 16)
    if (len(rows) != row_count or any(row.position != index for index, row in enumerate(rows))
            or _total_ml(rows) != total_ml):
        raise StagedBatchIntegrityError(f'staged batch {batch_id} has inconsistent rows or totals')
    request = StagedRequest(request_id, batch_id, encoded_parameters)
    try:
        request.require_match(StageParameters(point_id, business_day, tuple(row.source for row in rows)))
    except ValueError as error:
        raise StagedBatchIntegrityError(f'staged batch {batch_id} differs from its request') from error
    return StagedBatch(batch_id, request_id, point_id, business_day, row_count, total_ml, rows)
