from collections.abc import Callable, Iterable
from dataclasses import dataclass

from intake.units import to_milliliters


@dataclass(frozen=True)
class SourceRow:
    device_id: str
    line_number: int
    value: int | None
    unit: str
    missing: bool = False


@dataclass(frozen=True)
class ValidatedRow:
    position: int
    source: SourceRow
    milliliters: int | None


class InvalidSourceRow(ValueError):
    """An input row has invalid provenance or belongs to a different point."""


def validate_rows(
    rows: Iterable[SourceRow], point_id: str, resolve_source: Callable[[str], str]
) -> tuple[ValidatedRow, ...]:
    validated = []
    resolved = {}
    for position, row in enumerate(rows):
        if not isinstance(row, SourceRow):
            raise InvalidSourceRow(f'input position {position} requires a SourceRow')
        if type(row.line_number) is not int or row.line_number < 1:
            raise InvalidSourceRow(f'input position {position} requires a positive integer line number')
        if type(row.device_id) is not str or not row.device_id.strip():
            raise InvalidSourceRow(f'input position {position} requires a device identifier')
        if row.device_id not in resolved:
            resolved[row.device_id] = resolve_source(row.device_id)
        if resolved[row.device_id] != point_id:
            raise InvalidSourceRow(f'device {row.device_id!r} does not belong to point {point_id!r}')
        milliliters = to_milliliters(row.value, row.unit, row.missing)
        validated.append(ValidatedRow(position, row, milliliters))
    if not validated:
        raise InvalidSourceRow('a batch must contain at least one source row')
    return tuple(validated)
