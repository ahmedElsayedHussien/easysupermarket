"""P3-4: one rounding rule for amounts — 2 places for money, 4 for unit values, half away from zero."""
from decimal import Decimal as D

from django.test import SimpleTestCase

from apps.tenant.core.money import MoneyField, QtyField, UnitField, money, unit


class MoneyTests(SimpleTestCase):
    def test_money_rounds_half_away_from_zero(self):
        self.assertEqual(money(D('0.125')), D('0.13'))
        self.assertEqual(money(D('-0.125')), D('-0.13'))
        self.assertEqual(money(D('0.124')), D('0.12'))
        self.assertEqual(str(money(D('10'))), '10.00')

    def test_unit_four_places(self):
        self.assertEqual(unit(D('33.33335')), D('33.3334'))
        self.assertEqual(unit(D('-0.00005')), D('-0.0001'))

    def test_accepts_numbers_strings_and_none(self):
        self.assertEqual(money('1.005'), D('1.01'))           # via str, no float binary error
        self.assertEqual(money(1.005), D('1.01'))
        self.assertEqual(money(None), D('0.00'))
        with self.assertRaises(ValueError):
            money('abc')

    def test_fields(self):
        self.assertEqual((MoneyField().max_digits, MoneyField().decimal_places), (18, 2))
        self.assertEqual((UnitField().max_digits, UnitField().decimal_places), (18, 4))
        self.assertEqual((QtyField().max_digits, QtyField().decimal_places), (18, 4))
        name, path, args, kwargs = MoneyField(default=D('0')).deconstruct()
        self.assertEqual((kwargs['max_digits'], kwargs['decimal_places']), (18, 2))


class FieldDigitsTests(SimpleTestCase):
    """Phase 2: every amount is (18, 2), every quantity / unit price (18, 4) — only max_digits was widened."""

    def test_no_narrow_amount_or_unit_field_left(self):
        from django.apps import apps
        from django.db import models
        narrow = {(10, 2), (12, 2), (14, 2), (15, 2), (16, 2), (12, 4), (14, 4), (15, 4)}
        left = [f'{m._meta.label}.{f.name} ({f.max_digits},{f.decimal_places})'
                for m in apps.get_models() for f in m._meta.get_fields()
                if isinstance(f, models.DecimalField) and f.model is m and (f.max_digits, f.decimal_places) in narrow]
        self.assertEqual(left, [])
