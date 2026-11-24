import sqlite3

from catalog.devices import require_identifier
from catalog.lifecycle import is_admissible
from catalog.registry import get_point
from catalog.retirement_log import RetirementDecision, find_retirement_request, record_retirement
from publication.storage import next_decision_position, warehouse_write


class RetirementRequestConflict(ValueError):
    """A retirement request identifier is bound to a different point."""


class PointAlreadyRetired(ValueError):
    """A different request has already retired this point."""


def retire(
    catalog: sqlite3.Connection,
    warehouse: sqlite3.Connection,
    request_id: str,
    point_id: str,
) -> RetirementDecision:
    require_identifier(request_id, 'request_id')
    point = get_point(catalog, point_id)
    with warehouse_write(warehouse):
        decision = find_retirement_request(warehouse, request_id)
        if decision is not None:
            if decision.point_id != point.point_id:
                raise RetirementRequestConflict(f'retirement {request_id!r} belongs to another point')
        else:
            if not is_admissible(warehouse, point.point_id):
                raise PointAlreadyRetired(f'point {point.point_id!r} is already retired')
            position = next_decision_position(warehouse, 'retire', point.point_id)
            decision = record_retirement(warehouse, request_id, point.point_id, position)
    return decision
