"""RFC 8785 JSON Canonicalization Scheme (JCS) and payload fingerprints (DESIGN §2.1).

Only the JSON data model is accepted: dict (str keys), list, str, int, float, bool, None.
Integers outside the IEEE-754 safe range are rejected rather than silently rounded,
because payment amounts must never be fingerprinted after a lossy conversion.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

_MAX_SAFE_INT = 2**53 - 1


class CanonicalizationError(ValueError):
    pass


def _serialize_number(value: float) -> str:
    """ECMAScript Number.prototype.toString, as required by RFC 8785 §3.2.2.3."""
    if math.isnan(value) or math.isinf(value):
        raise CanonicalizationError("NaN/Infinity are not valid JSON numbers")
    if value == 0:
        return "0"  # covers -0
    # repr() yields the shortest round-tripping digits; re-format them per ECMAScript.
    sign = "-" if value < 0 else ""
    digits, exponent = _shortest_digits(abs(value))
    k = len(digits)
    n = exponent + k  # position of the decimal point relative to the digit string
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    exp = f"e+{e}" if e >= 0 else f"e-{-e}"
    if k == 1:
        return sign + digits + exp
    return sign + digits[0] + "." + digits[1:] + exp


def _shortest_digits(value: float) -> tuple[str, int]:
    """Return (digits, exponent) with value == int(digits) * 10**exponent, digits minimal."""
    mantissa, _, exp_part = repr(value).partition("e")
    exponent = int(exp_part) if exp_part else 0
    int_part, _, frac_part = mantissa.partition(".")
    if frac_part == "0":
        frac_part = ""
    digits = (int_part + frac_part).lstrip("0")
    exponent -= len(frac_part)
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    return stripped, exponent


def _serialize_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:  # lone surrogates
        raise CanonicalizationError("string is not valid Unicode") from exc
    # json.dumps(ensure_ascii=False) escapes exactly what JSON.stringify escapes:
    # '"', '\\', and U+0000..U+001F (short forms \b\t\n\f\r, else \u00xx lowercase).
    return json.dumps(value, ensure_ascii=False)


def _utf16_key(key: str) -> list[int]:
    data = key.encode("utf-16-be")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise CanonicalizationError(f"integer {value} exceeds the IEEE-754 safe range")
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_serialize_number(value))
    elif isinstance(value, str):
        out.append(_serialize_string(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalizationError("object keys must be strings")
        out.append("{")
        for i, key in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(_serialize_string(key))
            out.append(":")
            _serialize(value[key], out)
        out.append("}")
    else:
        raise CanonicalizationError(f"unsupported type {type(value).__name__}")


def canonicalize(value: Any) -> bytes:
    out: list[str] = []
    _serialize(value, out)
    return "".join(out).encode("utf-8")


def fingerprint(value: Any) -> str:
    """Hex SHA-256 of the JCS form: f_i = SHA-256(JCS(p_i))."""
    return hashlib.sha256(canonicalize(value)).hexdigest()
