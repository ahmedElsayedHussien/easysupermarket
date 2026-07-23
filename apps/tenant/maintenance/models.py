"""
Maintenance models:
  - MaintenanceTicket: Flexible repair ticket for any device type.
  - TicketPart: Spare part used in a ticket — triggers immediate stock deduction.
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

    STATUS_CHOICES = [
        (STATUS_PENDING,       _('قيد الانتظار')),
        (STATUS_IN_PROGRESS,   _('جاري العمل')),
        (STATUS_WAITING_PARTS, _('في انتظار قطع الغيار')),
        (STATUS_DONE,          _('جاهز للتسليم')),
        (STATUS_DELIVERED,     _('تم التسليم')),
    ]

    branch = models.ForeignKey(
        'core.Branch', on_delete=models.CASCADE,
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
    treasury = models.ForeignKey(
        'accounting.Treasury', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_receipts',
        verbose_name=_('الخزينة (لتحصيل المبلغ)')
    )

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
        return sum(p.selling_price * p.quantity for p in self.parts.all())

    @property
    def parts_cost_total(self):
        """إجمالي التكلفة الفعلية لقطع الغيار (من FIFO)."""
        return sum(p.actual_cost for p in self.parts.all())

    @property
    def total_revenue(self):
        """إجمالي الإيراد = المصنعية + مبيعات قطع الغيار."""
        return self.labor_cost + self.parts_selling_total

    @property
    def ticket_profit(self):
        """صافي الربح = المصنعية + (سعر بيع القطع - تكلفة القطع)."""
        return self.labor_cost + (self.parts_selling_total - self.parts_cost_total)

    # ---- Delivery & accounting ----
    def deliver(self, user, treasury):
        """
        Mark ticket as DELIVERED, deduct inventory for parts,
        and post the accounting journal entry — all in one atomic block.
        """
        from apps.tenant.maintenance.services import post_maintenance_journal
        with transaction.atomic():
            self.status = self.STATUS_DELIVERED
            self.delivered_at = timezone.now()
            self.delivered_by = user
            self.treasury = treasury
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

    def save(self, *args, **kwargs):
        """Deduct inventory and calculate actual COGS on first save."""
        if not self.pk and not self.stock_deducted:
            self._deduct_stock()
        super().save(*args, **kwargs)

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
            if not self.serial_item:
                raise ValueError(f'يجب تحديد السيريال للقطعة "{product.name}"')
            if self.serial_item.is_sold:
                raise ValueError(f'هذا السيريال "{self.serial_item.serial_number}" تم بيعه بالفعل.')
            self.serial_item.is_sold = True
            self.serial_item.warehouse = None
            self.serial_item.save()
            self.actual_cost = self.serial_item.actual_cost
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
