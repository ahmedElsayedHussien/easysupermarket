"""
One precision rule for amounts (P3-4).

* Money — what a customer pays, what a document totals, what a balance shows: 2 places (piasters), rounded
  half away from zero (0.125 -> 0.13, -0.125 -> -0.13), the way a cashier and the tax authority round.
* Unit values — a unit price or unit cost, and quantities: 4 places, same rounding.

Fields: MoneyField (18, 2), UnitField (18, 4), QtyField (18, 4). Ledger amounts (JournalItem) and inventory
costs (InventoryBatch, StockMovement) keep their own precision on purpose.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import models

CENT = Decimal('0.01')
UNIT = Decimal('0.0001')
ZERO = Decimal('0')


def _dec(x):
    if x is None or x == '':
        return ZERO
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'not a number: {x!r}')


def money(x):
    """Amount to 2 places, half away from zero."""
    return _dec(x).quantize(CENT, rounding=ROUND_HALF_UP)


def unit(x):
    """Unit price / unit cost / quantity to 4 places, half away from zero."""
    return _dec(x).quantize(UNIT, rounding=ROUND_HALF_UP)


class MoneyField(models.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_digits', 18)
        kwargs.setdefault('decimal_places', 2)
        super().__init__(*args, **kwargs)


class UnitField(models.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_digits', 18)
        kwargs.setdefault('decimal_places', 4)
        super().__init__(*args, **kwargs)


class QtyField(UnitField):
    pass
