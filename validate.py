"""Input validation for container numbers and carrier codes."""

from __future__ import annotations

import re

from config import SUPPORTED_CARRIERS

_CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")
# ISO 6346 letter values skip 11, 22, and 33.
_ISO_LETTERS = {
    "A": 10,
    "B": 12,
    "C": 13,
    "D": 14,
    "E": 15,
    "F": 16,
    "G": 17,
    "H": 18,
    "I": 19,
    "J": 20,
    "K": 21,
    "L": 23,
    "M": 24,
    "N": 25,
    "O": 26,
    "P": 27,
    "Q": 28,
    "R": 29,
    "S": 30,
    "T": 31,
    "U": 32,
    "V": 34,
    "W": 35,
    "X": 36,
    "Y": 37,
    "Z": 38,
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
            value = _ISO_LETTERS[char]
        total += value * (2**index)
    check = total % 11 % 10
    return check == int(container[10])


def carrier_supported(carrier: str) -> bool:
    return carrier in SUPPORTED_CARRIERS
