import json
from dataclasses import dataclass
from datetime import date

from intake.rows import SourceRow
from intake.units import encode_integer


@dataclass(frozen=True)
class StageParameters:
    point_id: str
    business_day: str
    rows: tuple[SourceRow, ...]


class InvalidBusinessDay(ValueError):
    """The business day is not a calendar date in YYYY-MM-DD form."""


def check_business_day(business_day: str) -> str:
    if type(business_day) is not str:
        raise InvalidBusinessDay('business_day must be YYYY-MM-DD')
    try:
        parsed = date.fromisoformat(business_day)
    except ValueError as error:
        raise InvalidBusinessDay(f'invalid business day {business_day!r}') from error
    if parsed.isoformat() != business_day:
        raise InvalidBusinessDay(f'business day must be YYYY-MM-DD, got {business_day!r}')
    return business_day


def encode_stage_parameters(parameters: StageParameters) -> str:
    source_rows = [
        (row.device_id, hex(row.line_number), encode_integer(row.value), row.unit, row.missing)
        for row in parameters.rows
    ]
    return json.dumps((parameters.point_id, parameters.business_day, source_rows), separators=(',', ':'))
