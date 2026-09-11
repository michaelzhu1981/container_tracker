"""Input validation for container numbers and carrier codes."""

from __future__ import annotations

import re

from config import SUPPORTED_CARRIERS

_CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")
_ISO_ALPHABET = {
    **{chr(i): i - 55 for i in range(65, 91)},
}


def normalize_container(value: str) -> str:
    return re.sub(r"[\s-]+", "", str(value or "")).upper()


def normalize_carrier(value: str) -> str:
    return str(value or "").strip().upper()


def container_shape_ok(container: str) -> bool:
    return bool(_CONTAINER_RE.fullmatch(container))


def iso6346_check_digit_ok(container: str) -> bool:
    if not container_shape_ok(container):
        return False
    total = 0
    for index, char in enumerate(container[:10]):
        if char.isdigit():
            value = int(char)
        else:
            value = _ISO_ALPHABET[char]
            value = value + value // 11
        total += value * (2**index)
    check = total % 11 % 10
    return check == int(container[10])


def carrier_supported(carrier: str) -> bool:
    return carrier in SUPPORTED_CARRIERS
