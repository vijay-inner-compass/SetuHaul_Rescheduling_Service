"""Order numbers as the warehouse enters them."""

from __future__ import annotations

from setuhaul.order_numbers import is_valid, normalise


def test_validity_and_normalising() -> None:
    assert normalise("ord 261127-001") == "ORD261127001"
    assert is_valid("ORD-261127-001") and is_valid("ord 261127 001")
    for bad in ("ORD-DEMO-P1", "ORD-261127-001 ok", "ORD-12345", "261127001", "ORD_261127001", ""):
        assert not is_valid(bad), bad
