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


def line_amounts(quantity, unit_price, discount_pct=None, discount_fixed=None, tax_rate=None, wht_rate=None):
    """The money figures of one document line — the ONE computation shared by InvoiceLine, QuotationLine and
    the van cart, so a screen never shows a figure the saved invoice does not have.

    gross is exact (quantity and unit price keep 4 places); every amount is money() once, from exact inputs:
        discount = money(fixed, capped at gross)  or  money(gross × pct / 100)
        subtotal = money(gross − discount)
        tax      = money(subtotal × tax_rate / 100)
        wht      = money(subtotal × wht_rate / 100)
        total    = subtotal + tax − wht           (already 2 places: no further rounding)
    """
    hundred = Decimal('100')
    gross = _dec(quantity) * _dec(unit_price)
    if discount_fixed is not None:
        discount = money(min(max(_dec(discount_fixed), ZERO), gross))
    else:
        discount = money(gross * _dec(discount_pct) / hundred)
    subtotal = money(gross - discount)
    if subtotal < ZERO:
        subtotal = money(ZERO)
    tax = money(subtotal * _dec(tax_rate) / hundred)
    wht = money(subtotal * _dec(wht_rate) / hundred)
    return {'gross': gross, 'discount': discount, 'subtotal': subtotal, 'tax': tax, 'wht': wht,
            'total': subtotal + tax - wht}
