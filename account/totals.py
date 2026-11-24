from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class DayTotals:
    row_count: int
    total_ml: int


def summarize_rows(milliliters: Iterable[int | None]) -> DayTotals:
    row_count = 0
    total_ml = 0
    for value in milliliters:
        row_count += 1
        if value is not None:
            total_ml += value
    return DayTotals(row_count, total_ml)
