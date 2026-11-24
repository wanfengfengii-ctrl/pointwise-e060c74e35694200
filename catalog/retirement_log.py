import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class RetirementDecision:
    request_id: str
    point_id: str
    position: int
    receipt_id: str


def find_retirement_request(
    warehouse: sqlite3.Connection, request_id: str
) -> RetirementDecision | None:
    row = warehouse.execute(
        'SELECT request_id, point_id, position, receipt_id FROM retirements WHERE request_id = ?',
        (request_id,),
    ).fetchone()
    return RetirementDecision(*row) if row is not None else None


def find_point_retirement(
    warehouse: sqlite3.Connection, point_id: str
) -> RetirementDecision | None:
    row = warehouse.execute(
        'SELECT request_id, point_id, position, receipt_id FROM retirements WHERE point_id = ?',
        (point_id,),
    ).fetchone()
    return RetirementDecision(*row) if row is not None else None


def record_retirement(
    warehouse: sqlite3.Connection, request_id: str, point_id: str, position: int
) -> RetirementDecision:
    receipt_id = f'retirement-{position}'
    warehouse.execute(
        '''INSERT INTO retirements(request_id, point_id, position, receipt_id)
           VALUES (?, ?, ?, ?)''',
        (request_id, point_id, position, receipt_id),
    )
    return RetirementDecision(request_id, point_id, position, receipt_id)
