import sqlite3
from dataclasses import dataclass

from intake.units import decode_integer, encode_integer


@dataclass(frozen=True)
class ArchivedRow:
    position: int
    device_id: str
    line_number: int
    value: int | None
    unit: str
    missing: bool


def write_archive_rows(
    warehouse: sqlite3.Connection, batch_id: int, rows: tuple[ArchivedRow, ...]
) -> None:
    warehouse.executemany(
        '''INSERT INTO archive_rows(batch_id, ordinal, device_id, line_number, raw_value, unit, missing)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (
            (batch_id, row.position, row.device_id, hex(row.line_number), encode_integer(row.value),
             row.unit, int(row.missing))
            for row in rows
        ),
    )


def load_archive_rows(warehouse: sqlite3.Connection, batch_id: int) -> tuple[ArchivedRow, ...]:
    rows = warehouse.execute(
        '''SELECT ordinal, device_id, line_number, raw_value, unit, missing
           FROM archive_rows WHERE batch_id = ? ORDER BY ordinal''',
        (batch_id,),
    )
    return tuple(
        ArchivedRow(ordinal, device_id, int(line_number, 16), decode_integer(raw_value), unit, bool(missing))
        for ordinal, device_id, line_number, raw_value, unit, missing in rows
    )
