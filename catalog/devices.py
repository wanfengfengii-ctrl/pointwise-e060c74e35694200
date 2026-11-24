import sqlite3


class DeviceBindingConflict(ValueError):
    """The device already belongs to a sampling point."""


class UnknownDevice(LookupError):
    """The device has no registered sampling point."""


def require_identifier(value: str, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f'{label} must be a non-empty string')
    return value


def bind_device(catalog: sqlite3.Connection, device_id: str, point_id: str) -> None:
    """Insert a binding inside the caller's registration transaction."""
    try:
        owner = resolve_device(catalog, device_id)
    except UnknownDevice:
        catalog.execute(
            'INSERT INTO devices(device_id, point_id) VALUES (?, ?)',
            (device_id, point_id),
        )
    else:
        raise DeviceBindingConflict(f'device {device_id!r} already belongs to {owner!r}')


def resolve_device(catalog: sqlite3.Connection, device_id: str) -> str:
    require_identifier(device_id, 'device_id')
    binding = catalog.execute(
        'SELECT point_id FROM devices WHERE device_id = ?', (device_id,)
    ).fetchone()
    if binding is None:
        raise UnknownDevice(f'unknown device {device_id!r}')
    return binding[0]
