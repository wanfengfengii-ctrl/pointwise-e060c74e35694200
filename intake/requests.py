import sqlite3
from dataclasses import dataclass

from intake.identity import StageParameters, encode_stage_parameters


class StageRequestConflict(ValueError):
    """A staging request already has different original parameters."""


@dataclass(frozen=True)
class StagedRequest:
    request_id: str
    batch_id: int
    encoded_parameters: str

    def require_match(self, parameters: StageParameters) -> None:
        if self.encoded_parameters != encode_stage_parameters(parameters):
            raise StageRequestConflict(f'staging request {self.request_id!r} has different parameters')


def find_staged_request(warehouse: sqlite3.Connection, request_id: str) -> StagedRequest | None:
    row = warehouse.execute(
        'SELECT request_id, batch_id, parameters FROM staged_requests WHERE request_id = ?',
        (request_id,),
    ).fetchone()
    return StagedRequest(*row) if row is not None else None


def bind_staged_request(
    warehouse: sqlite3.Connection, request_id: str, batch_id: int, parameters: StageParameters
) -> None:
    warehouse.execute(
        'INSERT INTO staged_requests(request_id, batch_id, parameters) VALUES (?, ?, ?)',
        (request_id, batch_id, encode_stage_parameters(parameters)),
    )
