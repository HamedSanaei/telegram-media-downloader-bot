"""Explicit rial money/currency contract for the three rial gateways (T024/T025).

One project contract covers all three providers:

- ``currency`` is ``IRT``.
- ``amount_minor`` is an integer number of WHOLE toman (not rial, not tenths). There is no
  invisible x10 or /10 conversion anywhere in the adapters: the amount sent to every provider and
  the amount verified on every inquiry are the same integer.

Provider-enforced boundaries (fail before any HTTP request when violated):
- UniquePay: amount > 50,000 toman (exactly 50,000 is rejected by the provider).
- Tetraminator: amount >= 50,000 toman (documented minimum).
- HooshPay: 50,000 <= amount <= 1,000,000 toman (inclusive).
"""

from __future__ import annotations

CURRENCY_IRT = "IRT"

UNIQUEPAY_EXCLUSIVE_MINIMUM_TOMAN = 50_000
TETRAMINATOR_MINIMUM_TOMAN = 50_000
HOOSHPAY_MINIMUM_TOMAN = 50_000
HOOSHPAY_MAXIMUM_TOMAN = 1_000_000


def uniquepay_accepts(amount_toman: int) -> bool:
    return amount_toman > UNIQUEPAY_EXCLUSIVE_MINIMUM_TOMAN


def tetraminator_accepts(amount_toman: int) -> bool:
    return amount_toman >= TETRAMINATOR_MINIMUM_TOMAN


def hooshpay_accepts(amount_toman: int) -> bool:
    return HOOSHPAY_MINIMUM_TOMAN <= amount_toman <= HOOSHPAY_MAXIMUM_TOMAN


__all__ = [
    "CURRENCY_IRT",
    "HOOSHPAY_MAXIMUM_TOMAN",
    "HOOSHPAY_MINIMUM_TOMAN",
    "TETRAMINATOR_MINIMUM_TOMAN",
    "UNIQUEPAY_EXCLUSIVE_MINIMUM_TOMAN",
    "hooshpay_accepts",
    "tetraminator_accepts",
    "uniquepay_accepts",
]
