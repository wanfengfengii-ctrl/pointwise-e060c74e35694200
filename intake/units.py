class InvalidReading(ValueError):
    """The unit, integer value or missing marker is invalid."""


def to_milliliters(value: int | None, unit: str, missing: bool = False) -> int | None:
    if type(unit) is not str or unit not in ('L', 'mL'):
        raise InvalidReading(f'unsupported volume unit {unit!r}')
    if type(missing) is not bool:
        raise InvalidReading('missing must be a boolean')
    if missing:
        if value is not None:
            raise InvalidReading('a missing reading must have a null value')
        return None
    if type(value) is not int:
        raise InvalidReading('a measured value must be an integer')
    return value * 1000 if unit == 'L' else value


def encode_integer(value: int | None) -> str | None:
    # SQLite INTEGER is bounded. Hex text preserves arbitrary Python integers.
    return None if value is None else hex(value)


def decode_integer(value: str | None) -> int | None:
    return None if value is None else int(value, 16)
