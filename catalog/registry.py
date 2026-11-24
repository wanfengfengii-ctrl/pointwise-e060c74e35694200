import sqlite3
from dataclasses import dataclass
from pathlib import Path

from catalog.devices import bind_device, require_identifier


@dataclass(frozen=True)
class Point:
    point_id: str
    device_id: str


@dataclass(frozen=True)
class Registration:
    point_id: str
    device_id: str
    status: str


class PointRegistrationConflict(ValueError):
    """A sampling point with this identifier already exists."""


class UnknownPoint(LookupError):
    """The sampling point is absent from the directory."""


def open_catalog(path: str | Path) -> sqlite3.Connection:
    catalog = sqlite3.connect(path, isolation_level=None)
    catalog.execute('PRAGMA foreign_keys = ON')
    return catalog


def initialize_catalog(catalog: sqlite3.Connection) -> None:
    if catalog.in_transaction:
        raise RuntimeError('catalog initialization requires an idle connection')
    try:
        catalog.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS points (
                point_id TEXT PRIMARY KEY NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY NOT NULL,
                point_id TEXT NOT NULL UNIQUE REFERENCES points(point_id)
            );
            COMMIT;
        ''')
    except BaseException:
        catalog.rollback()
        raise


def register(catalog: sqlite3.Connection, point_id: str, device_id: str) -> Registration:
    require_identifier(point_id, 'point_id')
    require_identifier(device_id, 'device_id')
    catalog.execute('BEGIN IMMEDIATE')
    try:
        existing = catalog.execute(
            'SELECT 1 FROM points WHERE point_id = ?', (point_id,)
        ).fetchone()
        if existing is not None:
            raise PointRegistrationConflict(f'point {point_id!r} is already registered')
        catalog.execute('INSERT INTO points(point_id) VALUES (?)', (point_id,))
        bind_device(catalog, device_id, point_id)
        catalog.commit()
    except BaseException:
        catalog.rollback()
        raise
    return Registration(point_id, device_id, 'active')


def get_point(catalog: sqlite3.Connection, point_id: str) -> Point:
    require_identifier(point_id, 'point_id')
    binding = catalog.execute(
        '''SELECT p.point_id, d.device_id
           FROM points AS p JOIN devices AS d USING (point_id)
           WHERE p.point_id = ?''',
        (point_id,),
    ).fetchone()
    if binding is None:
        raise UnknownPoint(f'unknown point {point_id!r}')
    return Point(*binding)
