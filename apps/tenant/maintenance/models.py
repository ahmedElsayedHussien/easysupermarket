"""
Maintenance models:
  - MaintenanceTicket: Flexible repair ticket for any device type.
  - TicketPart: Spare part used in a ticket — stock leaves on add, comes back when the part is removed
    or the ticket is cancelled; frozen once the ticket is delivered.
  - CommissionRule: Milestone-based commission per Category.
  - CommissionRecord: Tracks calculated commissions per user per period.

Design decisions:
  - device_model / device_serial are free-text → works for any device (mobile, laptop, appliance).
  - serial_item (FK to SerialItem) is optional → link to our inventory if the device was sold by us.
  - TicketPart.actual_cost is auto-calculated from FIFO batches on save.
  - Journal entry is created atomically when status changes to 'DELIVERED'.
"""
from decimal import Decimal
from django.db import models, transaction
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.utils import timezone


User = settings.AUTH_USER_MODEL


# ---------------------------------------------------------------------------
# MaintenanceTicket
# ---------------------------------------------------------------------------
class MaintenanceTicket(models.Model):
    STATUS_PENDING        = 'PENDING'
    STATUS_IN_PROGRESS    = 'IN_PROGRESS'
    STATUS_WAITING_PARTS  = 'WAITING_PARTS'
    STATUS_DONE           = 'DONE'
    STATUS_DELIVERED      = 'DELIVERED'
    STATUS_CANCELLED      = 'CANCELLED'
    CLOSED_STATUSES = (STATUS_DELIVERED, STATUS_CANCELLED)

    STATUS_CHOICES = [
        (STATUS_PENDING,       _('قيد الانتظار')),
        (STATUS_IN_PROGRESS,   _('جاري العمل')),
        (STATUS_WAITING_PARTS, _('في انتظار قطع الغيار')),
        (STATUS_DONE,          _('جاهز للتسليم')),
        (STATUS_DELIVERED,     _('تم التسليم')),
        (STATUS_CANCELLED,     _('ملغاة (اترجع للعميل من غير إصلاح)')),
    ]

    branch = models.ForeignKey(
        'core.Branch', on_delete=models.PROTECT,
        related_name='maintenance_tickets', verbose_name=_('الفرع')
    )
    customer = models.ForeignKey(
        'partners.Partner', on_delete=models.PROTECT,
        related_name='maintenance_tickets', verbose_name=_('العميل')
    )
    technician = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_tickets', verbose_name=_('الفني المختص')
    )

    # ---- Device info (free-text for flexibility) ----
    device_model  = models.CharField(max_length=200, verbose_name=_('موديل الجهاز'))
    device_serial = models.CharField(max_length=100, blank=True, null=True, verbose_name=_('السيريال/IMEI (اختياري)'))
    device_condition_on_receipt = models.TextField(blank=True, verbose_name=_('حالة الجهاز عند الاستلام'))

    # Optional link to our SerialItem if the device was sold by us
    serial_item = models.ForeignKey(
        'inventory.SerialItem', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_tickets',
        verbose_name=_('جهاز من مخزوننا (اختياري)')
    )

    # ---- Ticket details ----
    issue_description = models.TextField(verbose_name=_('وصف العطل'))
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING,
        verbose_name=_('حالة التذكرة')
    )
    estimated_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name=_('التكلفة التقديرية')
    )
    labor_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0'),
        verbose_name=_('المصنعية (أجر العمل)')
    )
    warranty_days = models.IntegerField(default=0, verbose_name=_('أيام الضمان بعد الصيانة'))

    # ---- Warranty chain ----
    parent_ticket = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='followup_tickets', verbose_name=_('تذكرة الضمان الأصلية')
    )

    # ---- Accounting link (created on delivery) ----
    PAYMENT_CASH          = 'CASH'
    PAYMENT_BANK_TRANSFER = 'BANK_TRANSFER'
    PAYMENT_EWALLET       = 'EWALLET'
    PAYMENT_CREDIT        = 'CREDIT'
    PAYMENT_SPLIT         = 'SPLIT'

    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_CASH,          _('نقدي (كاش)')),
        (PAYMENT_BANK_TRANSFER, _('تحويل بنكي / انستا باي')),
        (PAYMENT_EWALLET,       _('محفظة إلكترونية')),
        (PAYMENT_CREDIT,        _('آجل (على حساب العميل)')),
        (PAYMENT_SPLIT,         _('سداد جزئي (مباشر + آجل)')),
    ]

    journal_entry = models.ForeignKey(
        'accounting.JournalEntry', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_tickets',
        verbose_name=_('القيد المحاسبي')
    )

    # ---- Delivery info ----
    delivered_at = models.DateTimeField(null=True, blank=True, verbose_name=_('تاريخ ووقت التسليم'))
    delivered_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='delivered_tickets', verbose_name=_('سلّمه')
    )
    payment_method = models.CharField(
        max_length=20, choices=PAYMENT_METHOD_CHOICES, default=PAYMENT_CASH,
        verbose_name=_('طريقة الدفع عند التسليم')
    )
    treasury = models.ForeignKey(
        'accounting.Treasury', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_receipts',
        verbose_name=_('الخزينة (لتحصيل المبلغ)')
    )
    bank_account = models.ForeignKey(
        'accounting.BankAccount', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_receipts',
        verbose_name=_('الحساب البنكي (لتحصيل المبلغ)')
    )
    ewallet = models.ForeignKey(
        'accounting.EWallet', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_receipts',
        verbose_name=_('المحفظة الإلكترونية (لتحصيل المبلغ)')
    )
    paid_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name=_('المبلغ المدفوع فوراً')
    )
    remaining_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name=_('المبلغ الآجل المتبقي')
    )

    # cash collected at delivery counts in the cashier's drawer count (invoicing.shift_services)
    pos_session = models.ForeignKey(
        'invoicing.POSSession', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='maintenance_tickets', verbose_name=_('وردية الكاشير')
    )
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name=_('وقت الإلغاء'))
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                     verbose_name=_('ألغاها'))
    cancel_reason = models.CharField(max_length=255, blank=True, default='', verbose_name=_('سبب الإلغاء'))

    notes = models.TextField(blank=True, verbose_name=_('ملاحظات'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('تذكرة صيانة')
        verbose_name_plural = _('تذاكر الصيانة')
        ordering = ['-created_at']

    def __str__(self):
        return f"صيانة #{self.id} — {self.device_model} ({self.get_status_display()})"

    # ---- Computed properties ----
    @property
    def parts_selling_total(self):
        """إجمالي سعر بيع قطع الغيار."""
        return sum((p.selling_price * p.quantity for p in self.parts.all()), Decimal('0'))

    @property
    def parts_cost_total(self):
        """إجمالي التكلفة الفعلية لقطع الغيار (من FIFO)."""
        return sum((p.actual_cost for p in self.parts.all()), Decimal('0'))

    @property
    def total_revenue(self):
        """إجمالي الإيراد = المصنعية + مبيعات قطع الغيار."""
        return self.labor_cost + self.parts_selling_total

    @property
    def ticket_profit(self):
        """صافي الربح = المصنعية + (سعر بيع القطع - تكلفة القطع)."""
        return self.labor_cost + (self.parts_selling_total - self.parts_cost_total)

    @property
    def is_closed(self):
        return self.status in self.CLOSED_STATUSES

    def cancel(self, user, reason=''):
        """The customer takes the device back unrepaired: every part goes back to stock (same batch cost /
        the same serial), no entry is booked. Not after delivery."""
        reason = (reason or '').strip()
        if not reason:
            raise ValueError('اكتب سبب الإلغاء.')
        with transaction.atomic():
            locked = type(self).objects.select_for_update(of=('self',)).get(pk=self.pk)
            if locked.status == self.STATUS_DELIVERED:
                raise ValueError('التذكرة اتسلّمت واترحّل قيدها — مينفعش تتلغي.')
            if locked.status == self.STATUS_CANCELLED:
                raise ValueError('التذكرة ملغاة بالفعل.')
            for part in TicketPart.objects.select_for_update(of=('self',)).filter(ticket=locked).order_by('product_id', 'id'):
                part.delete()                                   # returns its stock
            type(self).objects.filter(pk=self.pk).update(
                status=self.STATUS_CANCELLED, cancelled_at=timezone.now(), cancel_reason=reason[:255],
                cancelled_by=user if getattr(user, 'pk', None) else None, updated_at=timezone.now())
            self.refresh_from_db()

    # ---- Delivery & accounting ----
    def deliver(self, user, payment_method=PAYMENT_CASH, treasury=None, bank_account=None, ewallet=None,
                paid_amount=None, pos_session=None):
        """
        Mark ticket as DELIVERED, set payment details, deduct inventory for parts,
        and post the accounting journal entry — all in one atomic block.
        """
        from apps.tenant.maintenance.services import post_maintenance_journal
        with transaction.atomic():
            # Lock the row and re-check: a double submit must not post twice.
            locked = type(self).objects.select_for_update(of=('self',)).get(pk=self.pk)
            if locked.status == self.STATUS_DELIVERED or locked.journal_entry_id:
                raise ValueError('هذه التذكرة مسلّمة بالفعل.')
            if locked.status == self.STATUS_CANCELLED:
                raise ValueError('التذكرة ملغاة — مينفعش تتسلّم.')
            # Work on fresh values (labor_cost / parts may have changed since the instance was loaded)
            self.refresh_from_db()
            self.status = self.STATUS_DELIVERED
            self.delivered_at = timezone.now()
            self.delivered_by = user
            self.payment_method = payment_method
            self.treasury = treasury
            self.bank_account = bank_account
            self.ewallet = ewallet
            self.pos_session = pos_session

            total = self.total_revenue
            if payment_method == self.PAYMENT_SPLIT:
                p_amt = Decimal(str(paid_amount or '0'))
                self.paid_amount = max(Decimal('0'), min(p_amt, total))
                self.remaining_amount = max(Decimal('0'), total - self.paid_amount)
            elif payment_method == self.PAYMENT_CREDIT:
                self.paid_amount = Decimal('0')
                self.remaining_amount = total
            else:
                self.paid_amount = total
                self.remaining_amount = Decimal('0')

            je = post_maintenance_journal(self, user)
            self.journal_entry = je
            self.save()


# ---------------------------------------------------------------------------
# TicketPart
# ---------------------------------------------------------------------------
class TicketPart(models.Model):
    """
    A spare part used in a MaintenanceTicket.
    Stock is deducted immediately on save() using the FIFO engine.
    """
    ticket = models.ForeignKey(
        MaintenanceTicket, on_delete=models.CASCADE,
        related_name='parts', verbose_name=_('التذكرة')
    )
    product = models.ForeignKey(
        'inventory.Product', on_delete=models.PROTECT,
        related_name='ticket_parts', verbose_name=_('قطعة الغيار')
    )
    warehouse = models.ForeignKey(
        'inventory.Warehouse', on_delete=models.PROTECT,
        related_name='ticket_parts', verbose_name=_('مخزن السحب')
    )
    # For SERIALIZED products (e.g. a specific screen with IMEI-like serial)
    serial_item = models.ForeignKey(
        'inventory.SerialItem', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ticket_parts',
        verbose_name=_('السيريال (للقطع المسروِلة)')
    )

    quantity = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal('1'),
        verbose_name=_('الكمية')
    )
    selling_price = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_('سعر البيع للعميل')
    )
    # Filled automatically from FIFO on save
    actual_cost = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal('0'),
        verbose_name=_('التكلفة الفعلية (من المخزون)')
    )
    # Flag to ensure stock is only deducted once
    stock_deducted = models.BooleanField(default=False, verbose_name=_('تم خصم المخزون'))

    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('قطعة غيار مستهلكة')
        verbose_name_plural = _('قطع الغيار المستهلكة في الصيانة')

    def __str__(self):
        return f"{self.product.name} × {self.quantity} ← تذكرة #{self.ticket_id}"

    CLOSED_MSG = 'التذكرة مقفولة (اتسلّمت أو اتلغت) — قطعها متتعدّلش.'

    def _ticket_status(self):
        return MaintenanceTicket.objects.filter(pk=self.ticket_id).values_list('status', flat=True).first()

    def save(self, *args, **kwargs):
        """Deduct inventory and calculate actual COGS on first save (atomic: no stock out without the part)."""
        with transaction.atomic():
            if self._ticket_status() in MaintenanceTicket.CLOSED_STATUSES:
                raise ValueError(self.CLOSED_MSG)
            if not self.pk and not self.stock_deducted:
                self._deduct_stock()
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Removing a part of an open ticket puts its stock back (same cost / the same serial)."""
        with transaction.atomic():
            if self._ticket_status() in MaintenanceTicket.CLOSED_STATUSES:
                raise ValueError(self.CLOSED_MSG)
            if self.stock_deducted:
                self._return_stock()
            return super().delete(*args, **kwargs)

    def _return_stock(self):
        from apps.tenant.inventory.models import SerialItem
        from apps.tenant.services.fifo_engine import add_inventory_batch
        product = self.product
        if product.product_type == 'SERVICE':
            return
        if product.product_type == 'SERIALIZED':
            if self.serial_item_id:
                serial = SerialItem.objects.select_for_update(of=('self',)).get(pk=self.serial_item_id)
                serial.is_sold = False
                serial.warehouse = self.warehouse
                serial.save()
            return
        if self.quantity and self.quantity > 0:
            unit_cost = (Decimal(self.actual_cost or 0) / self.quantity).quantize(Decimal('0.0001'))
            add_inventory_batch(product=product, warehouse=self.warehouse, quantity=self.quantity,
                                unit_cost=unit_cost, reference=f'صيانة-#{self.ticket_id}', movement_type='RETURN_IN',
                                update_last_purchase_price=False,
                                notes=f'رجوع قطعة غيار من تذكرة صيانة #{self.ticket_id}')

    def _deduct_stock(self):
        """
        Deduct from inventory atomically.
        - SERIALIZED: marks the specific SerialItem as sold.
        - PRODUCT: consumes FIFO batches.
        - SERVICE: no stock action.
        """
        from apps.tenant.services.fifo_engine import consume_fifo_batches

        product = self.product
        if product.product_type == 'SERVICE':
            self.actual_cost = Decimal('0')
            self.stock_deducted = True
            return

        if product.product_type == 'SERIALIZED':
            from apps.tenant.inventory.models import SerialItem
            if not self.serial_item_id:
                raise ValueError(f'يجب تحديد السيريال للقطعة "{product.name}"')
            serial = SerialItem.objects.select_for_update(of=('self',)).get(pk=self.serial_item_id)
            if serial.is_sold:
                raise ValueError(f'هذا السيريال "{serial.serial_number}" تم بيعه بالفعل.')
            serial.is_sold = True            # its warehouse is kept: is_sold already takes it out of stock
            serial.save()
            self.serial_item = serial
            self.actual_cost = serial.actual_cost
            self.stock_deducted = True
            return

        # PRODUCT (TANGIBLE) — FIFO deduction
        consumptions = consume_fifo_batches(
            product=product,
            warehouse=self.warehouse,
            quantity_needed=self.quantity,
            reference=f'صيانة-#{self.ticket_id}',
            notes=f'استهلاك قطعة غيار في تذكرة صيانة #{self.ticket_id}'
        )
        self.actual_cost = Decimal(str(sum(c['total_cost'] for c in consumptions))).quantize(Decimal('0.0001'))
        self.stock_deducted = True


# ---------------------------------------------------------------------------
# Commission models — imported here so Django migrations discover them
# ---------------------------------------------------------------------------
from apps.tenant.maintenance.commission_models import CommissionRule, CommissionRecord  # noqa: E402, F401
