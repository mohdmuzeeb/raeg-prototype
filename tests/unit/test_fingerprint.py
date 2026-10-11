"""RFC 8785 (JCS) conformance of raeg_common.fingerprint.

Vectors: RFC 8785 §3.2.2 (canonical sample), §3.2.3 (property sorting) and Appendix B (numbers).
The Appendix B outputs were cross-checked against Node.js Number.prototype.toString before being
committed here; test_numbers_match_ecmascript_differentially re-checks random doubles when node exists.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from raeg_common.fingerprint import CanonicalizationError, canonicalize, fingerprint
from raeg_common.models import PaymentPayload

APPENDIX_B = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]


@pytest.mark.parametrize(("hex_bits", "expected"), APPENDIX_B)
def test_appendix_b_numbers(hex_bits: str, expected: str) -> None:
    value = struct.unpack(">d", bytes.fromhex(hex_bits))[0]
    assert canonicalize(value).decode() == expected


def test_rfc_sample_3_2_2() -> None:
    data = json.loads(
        '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],'
        ' "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",'
        ' "literals": [null, true, false]}'
    )
    expected = (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )
    assert canonicalize(data).decode("utf-8") == expected


def test_rfc_sorting_3_2_3() -> None:
    data = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    order = [v for v in json.loads(canonicalize(data)).values()]
    assert order == [
        "Carriage Return",
        "One",
        "Control",
        "Latin Small Letter O With Diaeresis",
        "Euro Sign",
        "Emoji: Grinning Face",
        "Hebrew Letter Dalet With Dagesh",
    ]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 2**53, {1: "x"}, b"bytes", "\ud800"])
def test_rejects_non_canonicalizable(bad: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize(bad)


def test_payload_fingerprint_is_order_independent_and_sensitive() -> None:
    a = PaymentPayload(amount_minor=1000, currency="USD", payer_id="p1", payee_id="m1", memo="x")
    b = PaymentPayload.model_validate({"memo": "x", "payee_id": "m1", "payer_id": "p1", "currency": "USD", "amount_minor": 1000})
    c = PaymentPayload(amount_minor=1001, currency="USD", payer_id="p1", payee_id="m1", memo="x")
    assert a.fingerprint() == b.fingerprint() == fingerprint(a.model_dump())
    assert a.fingerprint() != c.fingerprint()
    assert len(a.fingerprint()) == 64


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed; static vectors still apply")
@settings(max_examples=30, deadline=None)
@given(st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=1, max_size=50))
def test_numbers_match_ecmascript_differentially(values: list[float]) -> None:
    hexes = [struct.pack(">d", v).hex() for v in values]
    script = (
        "const b=Buffer.alloc(8);"
        f"for (const h of {json.dumps(hexes)}) {{ b.write(h,'hex'); console.log(String(b.readDoubleBE(0))); }}"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout.split()
    for v, es in zip(values, out, strict=True):
        assert canonicalize(v).decode() == es  # note String(-0) === "0", as JCS requires
