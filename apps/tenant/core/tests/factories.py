"""
Shared test fixtures for the EasySupermarket test suite.

Everything here builds plain model rows (no django_tenants): the test settings
(config.settings.test) run on an in-memory SQLite database.

Typical use::

    class MyTest(TestCase):
        @classmethod
        def setUpTestData(cls):
            cls.w = build_world()

``build_world()`` returns a ``World`` namespace with a seeded chart of
accounts, a branch + warehouse + treasury, the SystemSetting row, an admin and a
cashier (each with an Employee profile), two stocked-ready products and a
customer / supplier.
"""
import datetime
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth.models import Permission, User

D = Decimal

ADMIN_PASSWORD = 'admin-pass-123'
CASHIER_PASSWORD = 'cashier-pass-123'

_counter = {'n': 0}


def _uid(prefix):
    _counter['n'] += 1
    return f'{prefix}{_counter["n"]}'


# ---------------------------------------------------------------------------
# Chart of accounts / settings
# ---------------------------------------------------------------------------

def seed_chart_of_accounts():
    from apps.tenant.core.management.commands.setup_chart_of_accounts import setup_chart_of_accounts_data
    setup_chart_of_accounts_data()


def account(code):
    from apps.tenant.accounting.models import Account
    return Account.objects.get(code=code)


def system_settings(**overrides):
    from apps.tenant.core.models import SystemSetting
    s = SystemSetting.get_settings()
    for k, v in overrides.items():
        setattr(s, k, v)
    if overrides:
        s.save()
    return s


# ---------------------------------------------------------------------------
# Organisation
# ---------------------------------------------------------------------------

def make_branch(name=None, **kw):
    from apps.tenant.core.models import Branch
    code = kw.pop('code', None) or _uid('BR')
    return Branch.objects.create(name=name or f'فرع {code}', code=code, **kw)


def make_warehouse(branch, name=None, **kw):
    from apps.tenant.inventory.models import Warehouse
    code = kw.pop('code', None) or _uid('WH')
    return Warehouse.objects.create(branch=branch, name=name or f'مخزن {code}', code=code, **kw)


def make_treasury(branch, name=None, opening_balance=D('0'), **kw):
    from apps.tenant.accounting.models import Treasury
    t = Treasury.objects.create(
        branch=branch, name=name or _uid('خزينة '), opening_balance=opening_balance, **kw
    )
    t.refresh_from_db()
    return t


def make_bank_account(name=None, opening_balance=D('0')):
    from apps.tenant.accounting.models import BankAccount
    b = BankAccount.objects.create(name=name or _uid('Bank '), opening_balance=opening_balance)
    b.refresh_from_db()
    return b


def make_ewallet(name=None, opening_balance=D('0')):
    from apps.tenant.accounting.models import EWallet
    e = EWallet.objects.create(name=name or _uid('Wallet '), opening_balance=opening_balance)
    e.refresh_from_db()
    return e


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def make_user(username=None, password='pass-123', role='CASHIER', branch=None, perms=(), employee=True, **user_kw):
    from apps.tenant.core.models import Employee
    user = User.objects.create_user(username=username or _uid('user'), password=password, **user_kw)
    if employee:
        Employee.objects.create(user=user, role=role, branch=branch)
    if perms:
        add_perms(user, *perms)
    return User.objects.get(pk=user.pk)  # fresh instance (no stale perm/profile cache)


def add_perms(user, *perms):
    for p in perms:
        app_label, codename = p.split('.')
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )


def login(client, user):
    """Log ``user`` in on the test client without going through django-axes."""
    client.force_login(user, backend='django.contrib.auth.backends.ModelBackend')
    return client


# ---------------------------------------------------------------------------
# Catalogue / partners
# ---------------------------------------------------------------------------

def make_category(name='بقالة'):
    from apps.tenant.inventory.models import Category
    return Category.objects.create(name=name)


def make_product(category=None, name=None, sale_price=D('10'), tax_rate=D('14'), uom_name='قطعة', **kw):
    from apps.tenant.inventory.models import Product, ProductUoM, UnitOfMeasure
    category = category or make_category()
    p = Product.objects.create(
        name=name or _uid('منتج '), category=category, sale_price=sale_price, tax_rate=tax_rate, **kw
    )
    uom, _ = UnitOfMeasure.objects.get_or_create(name=uom_name)
    ProductUoM.objects.create(product=p, uom=uom, is_base=True, conversion_factor=D('1'))
    return p


def make_partner(name=None, partner_type='CUSTOMER', **kw):
    from apps.tenant.partners.models import Partner
    p = Partner.objects.create(name=name or _uid('شريك '), partner_type=partner_type, **kw)
    p.refresh_from_db()
    return p


def stock_in(product, warehouse, qty, cost):
    from apps.tenant.services.fifo_engine import add_inventory_batch
    return add_inventory_batch(product, warehouse, D(str(qty)), D(str(cost)))


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def make_invoice(w, invoice_type='SALE', lines=(), payment_type='CASH', partner=None, treasury='default',
                 confirm=True, **kw):
    """
    Create an invoice with ``lines`` = [(product, qty, unit_price[, tax_rate]), ...].
    CASH invoices get the world's treasury unless treasury=None is passed.
    Returns the (refreshed) invoice; confirmed (POSTED) unless confirm=False.
    """
    from apps.tenant.invoicing.models import Invoice, InvoiceLine
    from apps.tenant.services.invoice_service import confirm_invoice

    if partner is None:
        partner = w.supplier if invoice_type in ('PURCHASE', 'RETURN_PURCHASE') else w.customer
    if treasury == 'default':
        treasury = w.treasury if payment_type == 'CASH' else None
    inv = Invoice.objects.create(
        invoice_type=invoice_type, partner=partner, branch=kw.pop('branch', w.branch),
        warehouse=kw.pop('warehouse', w.warehouse), date=kw.pop('date', datetime.date.today()),
        payment_type=payment_type, treasury=treasury, status=Invoice.DRAFT,
        cashier=kw.pop('cashier', w.admin), **kw
    )
    for line in lines:
        product, qty, price = line[0], line[1], line[2]
        tax = line[3] if len(line) > 3 else D('0')
        InvoiceLine.objects.create(
            invoice=inv, product=product, quantity=D(str(qty)), unit_price=D(str(price)), tax_rate=D(str(tax)),
        )
    inv.recalculate_totals()
    total = inv.total_amount
    field = {'CASH': 'paid_cash', 'CARD': 'paid_card', 'EWALLET': 'paid_ewallet',
             'BANK_TRANSFER': 'paid_bank', 'CREDIT': 'paid_credit'}.get(payment_type)
    if field:
        setattr(inv, field, total)
        inv.save(update_fields=[field])
    if confirm:
        confirm_invoice(inv.id)
    return Invoice.objects.get(pk=inv.pk)


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def entry_totals(entry):
    return entry.get_total_debit(), entry.get_total_credit()


def account_net(entry, code):
    """Debit - credit posted on account ``code`` by ``entry`` (0 when absent)."""
    from django.db.models import Sum
    agg = entry.items.filter(account__code=code).aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or D('0')) - (agg['c'] or D('0'))


def ledger_totals():
    """(sum debit, sum credit) over every POSTED journal item."""
    from django.db.models import Sum
    from apps.tenant.accounting.models import JournalItem
    agg = JournalItem.objects.filter(entry__status='POSTED').aggregate(d=Sum('debit'), c=Sum('credit'))
    return agg['d'] or D('0'), agg['c'] or D('0')


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


def assertInventoryMatchesLedger(test_case=None, warehouse=None, max_diff=Decimal('0.01')):
    """
    P1-1 Helper: Asserts that the sum value of active inventory batches (quantity_remaining * unit_cost)
    matches the posted ledger balance of account 1140 (including sub-accounts).
    """
    from apps.tenant.accounting.models import Account
    from apps.tenant.inventory.models import InventoryBatch
    from django.db.models import DecimalField, F, Sum

    batch_qs = InventoryBatch.objects.exclude(quantity_remaining=Decimal('0'))
    if warehouse is not None:
        batch_qs = batch_qs.filter(warehouse=warehouse)

    batch_val = batch_qs.aggregate(
        v=Sum(F('quantity_remaining') * F('unit_cost'), output_field=DecimalField(max_digits=20, decimal_places=4))
    )['v'] or Decimal('0.0000')

    inv_acc = Account.objects.filter(code='1140').first()
    ledger_val = inv_acc.current_balance if inv_acc else Decimal('0.0000')

    diff = batch_val - ledger_val
    if abs(diff) > max_diff:
        msg = (
            f"فارق بين تقييم المخزون وحساب 1140: "
            f"قيمة الدفعات = {batch_val:.2f} ج.م، "
            f"رصيد الأستاذ (حساب 1140) = {ledger_val:.2f} ج.م، "
            f"الفارق = {diff:.2f} ج.م"
        )
        if test_case is not None:
            test_case.fail(msg)
        else:
            raise AssertionError(msg)


def build_world():
    seed_chart_of_accounts()
    settings_obj = system_settings()
    branch = make_branch('الفرع الرئيسي', code='MAIN')
    warehouse = make_warehouse(branch, 'المخزن الرئيسي', code='WH-MAIN')
    treasury = make_treasury(branch, 'خزينة الفرع الرئيسي')
    admin = make_user('tenant_admin', ADMIN_PASSWORD, role='ADMIN', branch=branch)
    cashier = make_user('cashier1', CASHIER_PASSWORD, role='CASHIER', branch=branch,
                        perms=('invoicing.add_invoice', 'invoicing.view_invoice'))
    category = make_category()
    product = make_product(category, 'أرز 1 كجم', sale_price=D('10'), tax_rate=D('14'))
    product2 = make_product(category, 'زيت 1 لتر', sale_price=D('50'), tax_rate=D('14'))
    customer = make_partner('شركة السلام', 'CUSTOMER')
    supplier = make_partner('مورد الأرز', 'SUPPLIER')
    return SimpleNamespace(
        settings=settings_obj, branch=branch, warehouse=warehouse, treasury=treasury,
        admin=admin, cashier=cashier, category=category, product=product, product2=product2,
        customer=customer, supplier=supplier,
    )
