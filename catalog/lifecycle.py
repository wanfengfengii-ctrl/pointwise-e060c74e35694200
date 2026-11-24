import sqlite3
from enum import StrEnum

from catalog.registry import get_point
from catalog.retirement_log import find_point_retirement


class PointStatus(StrEnum):
    ACTIVE = 'active'
    RETIRED = 'retired'


def read_point_status(
    catalog: sqlite3.Connection, warehouse: sqlite3.Connection, point_id: str
) -> PointStatus:
    point = get_point(catalog, point_id)
    if is_admissible(warehouse, point.point_id):
        return PointStatus.ACTIVE
    return PointStatus.RETIRED


def is_admissible(warehouse: sqlite3.Connection, point_id: str) -> bool:
    """Check an already registered point in the caller's warehouse view."""
    return find_point_retirement(warehouse, point_id) is None
