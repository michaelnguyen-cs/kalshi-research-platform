"""
Contract quantities as integer hundredths of a contract.

WHY THIS EXISTS
---------------
Kalshi's V2 API sends every quantity as a fixed-point decimal STRING with two
decimal places -- "104.00", "0.50", "-104.00" -- on the orderbook snapshot and
delta channels, on fill counts, and on position counts. The old code parsed
these with int(float(x)), which TRUNCATES TOWARD ZERO in both directions:

    int(float("0.50"))   ==  0     a real half-contract level read as empty
    int(float("-104.5")) == -104   a delta that removes 104.5 removes 104

Truncating a resting size understates depth; truncating a delta leaves phantom
size behind forever, because the book is incremental and the error never
washes out. Both directions are silent.

THE REPRESENTATION
------------------
One contract is UNITS_PER_CONTRACT (100) units, so every quantity the exchange
can express is an exact integer here and all book arithmetic is exact integer
arithmetic. Fields carrying this representation are named `*_units` without
exception -- that suffix is the only thing standing between this and a silent
unit mix-up, so do not drop it when passing one of these values along.

Convert to contracts ONLY at a boundary that genuinely needs a contract count:
dollar math (units_to_contracts), display, or an order `count` the exchange
requires as a whole number.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

UNITS_PER_CONTRACT = 100


def units_from_fixed_point(raw: Any) -> int | None:
    """
    Parse a V2 fixed-point quantity string to integer units, or None if it is
    not a usable number.

    Exact by construction: parsed as Decimal, not float, so "0.07" is 7 units
    rather than 6.999999999999999 units rounded by luck. A value with more
    precision than hundredths is quantized to the nearest unit -- the exchange
    does not send those, and rounding one is still better than truncating it.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite():
        return None
    return int((value * UNITS_PER_CONTRACT).to_integral_value(rounding="ROUND_HALF_EVEN"))


def contracts_to_units(contracts: float | int) -> int:
    """Whole or fractional contracts -> integer units."""
    return int(
        (Decimal(str(contracts)) * UNITS_PER_CONTRACT).to_integral_value(
            rounding="ROUND_HALF_EVEN"
        )
    )


def units_to_contracts(units: int) -> float:
    """
    Integer units -> contracts as a float, for dollar math and display.

    Not for feeding back into unit arithmetic: round-tripping through float is
    exactly the precision loss this module exists to avoid.
    """
    return units / UNITS_PER_CONTRACT
