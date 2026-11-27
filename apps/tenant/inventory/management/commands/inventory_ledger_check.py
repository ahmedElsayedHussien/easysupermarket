"""
Compare total inventory value from FIFO batches with Account 1140 in the general ledger. Read-only.

    python manage.py inventory_ledger_check
    python manage.py tenant_command inventory_ledger_check --schema=<schema>
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import DecimalField, F, Sum

from apps.tenant.accounting.models import Account
from apps.tenant.inventory.models import InventoryBatch, Warehouse


class Command(BaseCommand):
    help = 'Compare inventory batches value Sum(quantity_remaining * unit_cost) with Account 1140 ledger balance (read-only).'

    def handle(self, *args, **options):
        # 1. Total ledger balance for Account 1140 & its descendants
        inv_acc = Account.objects.filter(code='1140').first()
        ledger_balance = inv_acc.current_balance if inv_acc else Decimal('0.0000')

        # 2. Total batches value across all warehouses
        batches_qs = InventoryBatch.objects.exclude(quantity_remaining=Decimal('0'))
        total_batches_val = batches_qs.aggregate(
            v=Sum(F('quantity_remaining') * F('unit_cost'), output_field=DecimalField(max_digits=20, decimal_places=4))
        )['v'] or Decimal('0.0000')

        total_diff = total_batches_val - ledger_balance

        self.stdout.write("=" * 80)
        self.stdout.write("فحص ومطابقة تقييم المخزون (الدفعات) مع حساب الأستاذ 1140")
        self.stdout.write("=" * 80)

        # 3. Breakdown per warehouse
        warehouses = Warehouse.objects.all().order_by('branch', 'name')
        self.stdout.write(f"{'كود المستودع':<12} {'اسم المستودع':<30} {'قيمة الدفعات (ج.م)':>20}")
        self.stdout.write("-" * 65)

        for wh in warehouses:
            wh_val = batches_qs.filter(warehouse=wh).aggregate(
                v=Sum(F('quantity_remaining') * F('unit_cost'), output_field=DecimalField(max_digits=20, decimal_places=4))
            )['v'] or Decimal('0.0000')
            self.stdout.write(f"{wh.code:<12} {wh.name[:28]:<30} {wh_val:>20.2f}")

        self.stdout.write("-" * 65)
        self.stdout.write(f"{'إجمالي قيمة الدفعات:':<43} {total_batches_val:>20.2f}")
        self.stdout.write(f"{'رصيد حساب 1140 في الأستاذ:':<43} {ledger_balance:>20.2f}")
        self.stdout.write(f"{'الفارق الإجمالي:':<43} {total_diff:>20.2f}")
        self.stdout.write("=" * 80)

        if abs(total_diff) >= Decimal('0.01'):
            self.stdout.write(self.style.WARNING(
                f"❗ تنبيه: يوجد فارق غير مطابق قدره {total_diff:.2f} ج.م بين الدفعات وحساب 1140!"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "✅ مطابقة تامة: رصيد المخزون في الدفعات متطابق 100% مع حساب الأستاذ 1140."
            ))
