"""
Accounting models - Chart of Accounts, Journal Entries, Journal Items.
Implements full double-entry bookkeeping with MPTT account hierarchy.

Rules enforced:
  - Every JournalEntry must have sum(debit) == sum(credit) before posting.
  - A JournalItem may not have both debit > 0 AND credit > 0 simultaneously.
  - Accounts use MPTT for nested tree structure (Assets → Current Assets → Cash).
"""
from django.db import models, transaction
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models import Sum, Q
from mptt.models import MPTTModel, TreeForeignKey
from decimal import Decimal
import datetime
import logging

logger = logging.getLogger(__name__)


def _next_doc_number(namespace, model, field, prefix, width=5):
    """
    Race-safe "last number + 1" for document numbers.
    The counter key is namespaced per model so two models sharing a textual prefix
    (e.g. Voucher 'PAY-2026-' vs partners Payment 'PAY-2026-') never share a counter.
    Must be called inside the transaction.atomic() block that saves the document.
    """
    from apps.tenant.core.sequences import next_sequence, max_numeric_suffix
    num = next_sequence(
        f'{namespace}:{prefix}',
        seed=lambda: max_numeric_suffix(model.objects.all(), field, prefix),
    )
    return f'{prefix}{num:0{width}d}'


def _ensure_pos_session_open(pos_session, action_label='تعديل'):
    """Refuse changing posted money documents that belong to an already-closed cashier shift."""
    if pos_session is None:
        return
    from apps.tenant.invoicing.models import POSSession
    if pos_session.status == POSSession.CLOSED:
        raise ValidationError(
            f'لا يمكن {action_label} هذا المستند لأنه مرتبط بوردية كاشير مغلقة '
            f'({pos_session}). يرجى عمل سند/قيد تسوية عكسي بدلاً من ذلك.'
        )


# ---------------------------------------------------------------------------
# Accounting period lock (SystemSetting.accounting_lock_date)
# ---------------------------------------------------------------------------
# A journal entry dated on/before the lock date is frozen: it can't be created,
# have its date/status changed, be deleted, or have its lines created / changed /
# deleted. Enforced here at the model layer (save/delete/post + queryset
# update/delete/bulk_create) so every app and service follows it.
# Cancelling / editing a document of a closed period still works when it is done
# by reversal: the reversal is a NEW entry dated today (open period) and the
# original entry row is never written.

def _lock_date():
    from apps.tenant.core.settings_helpers import accounting_lock_date
    return accounting_lock_date()


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    return value if isinstance(value, datetime.date) else None


def _period_locked_error(date):
    from apps.tenant.core.settings_helpers import lock_error_message
    return ValidationError(lock_error_message(_as_date(date)))


def ensure_open_period(date):
    """Raise ValidationError (Arabic) if ``date`` falls in the closed accounting period."""
    lock = _lock_date()
    d = _as_date(date)
    if lock and d is not None and d <= lock:
        raise _period_locked_error(d)


class JournalEntryQuerySet(models.QuerySet):
    LOCKED_FIELDS = frozenset({'date', 'status'})

    def _raise_if_any_posted(self):
        if self.filter(status='POSTED', items__isnull=False).exists():
            raise ValidationError(JournalEntry.POSTED_LOCKED_MSG)

    def _raise_if_any_locked(self):
        lock = _lock_date()
        if not lock:
            return
        locked_date = self.filter(date__lte=lock).values_list('date', flat=True).first()
        if locked_date is not None:
            raise _period_locked_error(locked_date)

    def delete(self):
        self._raise_if_any_posted()
        self._raise_if_any_locked()
        return super().delete()

    def update(self, **kwargs):
        if self.LOCKED_FIELDS & set(kwargs):
            self._raise_if_any_posted()
            self._raise_if_any_locked()
            if 'date' in kwargs:
                ensure_open_period(kwargs['date'])
        return super().update(**kwargs)


class JournalItemQuerySet(models.QuerySet):
    def _raise_if_any_posted(self):
        if self.filter(entry__status='POSTED').exists():
            raise ValidationError(JournalEntry.POSTED_LOCKED_MSG)

    def _raise_if_any_locked(self):
        lock = _lock_date()
        if not lock:
            return
        locked_date = self.filter(entry__date__lte=lock).values_list('entry__date', flat=True).first()
        if locked_date is not None:
            raise _period_locked_error(locked_date)

    def delete(self):
        self._raise_if_any_posted()
        self._raise_if_any_locked()
        return super().delete()

    def update(self, **kwargs):
        self._raise_if_any_posted()
        self._raise_if_any_locked()
        if 'entry' in kwargs or 'entry_id' in kwargs:
            target = kwargs.get('entry', kwargs.get('entry_id'))
            target_id = getattr(target, 'pk', target)
            if target_id and _lock_date():
                ensure_open_period(
                    JournalEntry.objects.filter(pk=target_id).values_list('date', flat=True).first())
        return super().update(**kwargs)

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        entry_ids = {o.entry_id for o in objs if o.entry_id}
        if JournalEntry.objects.filter(pk__in=entry_ids, status='POSTED').exists():
            raise ValidationError(JournalEntry.POSTED_LOCKED_MSG)
        if _lock_date():
            entry_ids = {o.entry_id for o in objs if o.entry_id}
            for d in JournalEntry.objects.filter(pk__in=entry_ids).values_list('date', flat=True):
                ensure_open_period(d)
        return super().bulk_create(objs, *args, **kwargs)


# ---------------------------------------------------------------------------
# Account (Chart of Accounts) - MPTT tree
# ---------------------------------------------------------------------------

class Account(MPTTModel):
    """
    A node in the Chart of Accounts tree.
    Uses django-mptt for efficient tree traversal and rendering.

    Account types determine normal balance:
      ASSET / EXPENSE  → normal balance is DEBIT  (Dr increases the account)
      LIABILITY / EQUITY / REVENUE → normal balance is CREDIT
    """
    ASSET = 'ASSET'
    LIABILITY = 'LIABILITY'
    EQUITY = 'EQUITY'
    REVENUE = 'REVENUE'
    EXPENSE = 'EXPENSE'

    ACCOUNT_TYPES = [
        (ASSET, 'أصول'),
        (LIABILITY, 'خصوم'),
        (EQUITY, 'حقوق ملكية'),
        (REVENUE, 'إيرادات'),
        (EXPENSE, 'مصروفات'),
    ]

    code = models.CharField(max_length=20, unique=True, verbose_name='كود الحساب')
    name = models.CharField(max_length=200, verbose_name='اسم الحساب')
    account_type = models.CharField(
        max_length=20, choices=ACCOUNT_TYPES, verbose_name='نوع الحساب'
    )
    parent = TreeForeignKey(
        'self',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='children',
        verbose_name='الحساب الأب'
    )
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    allow_reconcile = models.BooleanField(default=False, verbose_name='قابل للتسوية')
    is_cogs = models.BooleanField(
        default=False,
        verbose_name='حساب تكلفة بضاعة (COGS)',
        help_text='خصصه لحسابات تكلفة البضاعة المباعة حتى يظهر في قائمة الدخل ضمن بند تكلفة المبيعات'
    )
    is_contra_revenue = models.BooleanField(
        default=False,
        verbose_name='إيراد معاكس (مرتجعات/خصومات)',
        help_text='خصصه لحسابات مرتجعات المبيعات والخصومات حتى تُخصم من صافي المبيعات'
    )
    is_contra = models.BooleanField(
        default=False,
        verbose_name='حساب معاكس (Contra)',
        help_text='حساب رصيده الطبيعي عكس نوعه ويُعرض في القوائم كمبلغ موجب مخصوم '
                  '(مثل مجمع الإهلاك تحت الأصول، مردودات المشتريات من التكلفة، مردودات المبيعات من الإيرادات)'
    )
    allow_direct_payment = models.BooleanField(
        default=True,
        verbose_name='يظهر في سندات الصرف المباشرة',
        help_text='حدد هذا الخيار إذا كان الحساب يمثل مصروفاً قابلاً للصرف المباشر بالسندات (مثل الإيجار، المرافق، الأجور)'
    )
    description = models.TextField(blank=True, verbose_name='وصف')
    created_at = models.DateTimeField(auto_now_add=True)

    class MPTTMeta:
        order_insertion_by = ['code']

    class Meta:
        verbose_name = 'حساب'
        verbose_name_plural = 'الحسابات'

    def __str__(self):
        return f'{self.code} - {self.name}'

    # Contra accounts of the default chart (accumulated depreciation, purchase returns,
    # sales returns / discounts): flagged automatically when created.
    DEFAULT_CONTRA_CODES = frozenset({'1220', '5110', '4120', '4130'})

    def save(self, *args, **kwargs):
        if self._state.adding and self.code in self.DEFAULT_CONTRA_CODES:
            self.is_contra = True
        # A contra-revenue account is by definition a contra account (keeps both flags consistent).
        if self.is_contra_revenue and not self.is_contra:
            self.is_contra = True
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'is_contra'})
        super().save(*args, **kwargs)

    @property
    def normal_balance(self):
        """
        Returns 'DEBIT' or 'CREDIT' based on account type.
        ASSET and EXPENSE accounts have a natural debit balance.
        LIABILITY, EQUITY, and REVENUE accounts have a natural credit balance.
        """
        if self.account_type in (self.ASSET, self.EXPENSE):
            return 'DEBIT'
        return 'CREDIT'

    @property
    def current_balance(self):
        """
        Calculates the running balance for this account from all POSTED entries.
        For parent accounts, it aggregates the balances of all its descendants as well.
        Returns a signed Decimal: positive means normal-side balance.
        Uses _prefetched_balance when available to eliminate N+1 queries.
        """
        if hasattr(self, '_prefetched_balance'):
            return self._prefetched_balance

        items = JournalItem.objects.filter(
            account__in=self.get_descendants(include_self=True),
            entry__status=JournalEntry.POSTED
        )
        sums = items.aggregate(d=Sum('debit'), c=Sum('credit'))   # one query for both sides
        total_debit = sums['d'] or Decimal('0')
        total_credit = sums['c'] or Decimal('0')
        if self.normal_balance == 'DEBIT':
            return (total_debit - total_credit).quantize(Decimal('0.0001'))
        return (total_credit - total_debit).quantize(Decimal('0.0001'))

    @property
    def has_children(self):
        return self.children.exists()

    def get_balance_display(self):
        """Return formatted balance string for display."""
        balance = self.current_balance
        return f'{balance:,.4f}'


# ---------------------------------------------------------------------------
# Journal Entry (the container for a balanced set of debits/credits)
# ---------------------------------------------------------------------------

class JournalEntry(models.Model):
    """
    A double-entry bookkeeping record containing two or more JournalItems.
    The entry is only valid when sum(debit) == sum(credit) across all items.

    Lifecycle: DRAFT → POSTED (irreversible in normal flow).
    Source document is linked via Generic FK for full traceability.
    """
    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مرحّل'),
    ]

    date = models.DateField(verbose_name='التاريخ')
    reference = models.CharField(
        max_length=50, unique=True, verbose_name='رقم القيد'
    )
    description = models.TextField(verbose_name='البيان')
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة'
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='journal_entries',
        verbose_name='أنشئ بواسطة'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Generic FK to the source document (Invoice, Payment, etc.)
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='نوع المستند المصدر'
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    source_document = GenericForeignKey('content_type', 'object_id')

    # Audit trail: a posted entry is never deleted when its document is cancelled/edited;
    # a mirror entry (debit <-> credit) is posted instead and points to the original here.
    # The original stays POSTED (history visible) and the pair nets to zero in every report.
    reversal_of = models.OneToOneField(
        'self',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='reversed_by',
        verbose_name='قيد عكسي لـ'
    )
    posted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                  verbose_name='رحّله')
    posted_at = models.DateTimeField(null=True, blank=True, verbose_name='وقت الترحيل')

    objects = JournalEntryQuerySet.as_manager()

    class Meta:
        verbose_name = 'قيد يومية'
        verbose_name_plural = 'قيود اليومية'
        ordering = ['-date', '-created_at']
        indexes = [models.Index(fields=['status', 'date'], name='je_status_date_idx')]   # reports / balances

    def _check_period_lock(self, update_fields=None):
        """
        New entry: its date must be in the open period.
        Existing entry: changing its date or status is refused when the stored or the
        new date is locked. Other fields (description, source-document link...) may
        still be updated: they don't change any balance.
        """
        lock = _lock_date()
        if not lock:
            return
        old = None
        if self.pk and not self._state.adding:
            old = JournalEntry.objects.filter(pk=self.pk).values('date', 'status').first()
        if old is None:
            ensure_open_period(self.date)
            return
        changed = {f for f in ('date', 'status') if old[f] != getattr(self, f)}
        if update_fields is not None:
            changed &= set(update_fields)
        if not changed:
            return
        ensure_open_period(old['date'])
        ensure_open_period(self.date)

    POSTED_LOCKED_MSG = 'القيد مرحّل ومينفعش يتعدّل أو يتمسح — صحّحه بقيد عكسي.'

    def save(self, *args, **kwargs):
        if not getattr(self, '_posting', False):
            if self._state.adding:
                # an entry is always created as a draft: it becomes POSTED only through post(), which
                # checks it (balanced, > 0, leaf accounts, open period)
                self.status = self.DRAFT
            else:
                old = JournalEntry.objects.filter(pk=self.pk).values('status', 'date').first()
                if old and old['status'] == self.POSTED:
                    has_items = JournalItem.objects.filter(entry_id=self.pk).exists()
                    if has_items and (self.status != self.POSTED or self.date != old['date']):
                        raise ValidationError(self.POSTED_LOCKED_MSG)
                elif old and self.status == self.POSTED:
                    # legacy "status = POSTED; save()": keep the other changes, then post properly
                    self.status = self.DRAFT
                    self._save_core(*args, **kwargs)
                    self.post()
                    return
        self._save_core(*args, **kwargs)

    def _save_core(self, *args, **kwargs):
        self._check_period_lock(kwargs.get('update_fields'))
        if not self.reference:
            year = self.date.year if self.date else datetime.date.today().year
            prefix = f"JE-MANUAL-{year}-"
            with transaction.atomic():
                self.reference = _next_doc_number('journal_entry', JournalEntry, 'reference', prefix)
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.pk and JournalEntry.objects.filter(pk=self.pk, status=self.POSTED, items__isnull=False).exists():
            raise ValidationError(self.POSTED_LOCKED_MSG)
        if self.pk and _lock_date():
            stored = JournalEntry.objects.filter(pk=self.pk).values_list('date', flat=True).first()
            ensure_open_period(stored if stored is not None else self.date)
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f'{self.reference} - {self.date} [{self.get_status_display()}]'

    @property
    def is_reversal(self):
        return self.reversal_of_id is not None

    @property
    def reversing_entry(self):
        """The entry that reversed this one, or None."""
        try:
            return self.reversed_by
        except JournalEntry.DoesNotExist:
            return None

    @property
    def is_reversed(self):
        return self.reversing_entry is not None

    def get_total_debit(self):
        """Sum of all debit amounts across journal items."""
        return self.items.aggregate(s=Sum('debit'))['s'] or Decimal('0')

    def get_total_credit(self):
        """Sum of all credit amounts across journal items."""
        return self.items.aggregate(s=Sum('credit'))['s'] or Decimal('0')

    def get_totals(self):
        """(total debit, total credit) in one query."""
        sums = self.items.aggregate(d=Sum('debit'), c=Sum('credit'))
        return sums['d'] or Decimal('0'), sums['c'] or Decimal('0')

    def is_balanced(self):
        """Returns True if total debits equal total credits (within rounding tolerance)."""
        debit, credit = self.get_totals()
        return abs(debit - credit) < Decimal('0.0001')

    def clean(self):
        """Enforce balance constraint when posting."""
        if self.status == self.POSTED:
            debit, credit = self.get_totals()
            if abs(debit - credit) >= Decimal('0.0001'):
                diff = debit - credit
                raise ValidationError(
                    f'القيد غير متوازن: مجموع المدين ({debit:.4f}) لا يساوي مجموع الدائن ({credit:.4f}).'
                    f' الفرق: {diff:.4f}'
                )

    def post(self, user=None):
        """
        The ONLY way an entry becomes POSTED. Refused when: already posted, empty (total 0), not balanced,
        a line is on a group (parent) account, or its date is in the closed period. Records who / when.
        """
        from django.utils import timezone
        with transaction.atomic():
            current = JournalEntry.objects.select_for_update(of=('self',)).filter(pk=self.pk) \
                .values_list('status', flat=True).first()
            if current == self.POSTED:
                raise ValidationError(f'القيد {self.reference} مرحّل بالفعل.')
            ensure_open_period(self.date)
            debit, credit = self.get_totals()   # one query
            if debit <= 0 or credit <= 0:
                raise ValidationError(f'القيد {self.reference} فاضي — لازم مدين ودائن أكبر من صفر.')
            if abs(debit - credit) >= Decimal('0.0001'):
                diff = debit - credit
                raise ValidationError(
                    f'القيد غير متوازن. إجمالي المدين: {debit:.4f}, إجمالي الدائن: {credit:.4f}. الفرق: {diff:.4f}'
                )
            groups = list(Account.objects.filter(journal_items__entry=self, children__isnull=False)
                          .distinct().values_list('code', 'name'))
            if groups:
                raise ValidationError('مينفعش ترحيل على حساب رئيسي (تجميعي) — اختار حساب فرعي: '
                                      + '، '.join(f'{c} {n}' for c, n in groups))
            inactive = list(Account.objects.filter(journal_items__entry=self, is_active=False)
                            .distinct().values_list('code', 'name'))
            if inactive:
                raise ValidationError('مينفعش ترحيل على حساب موقوف: ' + '، '.join(f'{c} {n}' for c, n in inactive))
            self.status = self.POSTED
            self.posted_at = timezone.now()
            if user is not None and getattr(user, 'pk', None):
                self.posted_by = user
            elif self.posted_by_id is None and self.created_by_id:
                self.posted_by_id = self.created_by_id
            self._posting = True
            try:
                self.save(update_fields=['status', 'posted_at', 'posted_by', 'updated_at'])
            finally:
                self._posting = False
        return self


# ---------------------------------------------------------------------------
# Journal Item (a single line within a JournalEntry)
# ---------------------------------------------------------------------------

class JournalItem(models.Model):
    """
    One line within a JournalEntry. Either debit OR credit must be non-zero,
    never both simultaneously. This is enforced by a DB check constraint.
    """
    entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='القيد'
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name='journal_items',
        verbose_name='الحساب'
    )
    debit = models.DecimalField(
        max_digits=15,
        decimal_places=4,
        default=Decimal('0'),
        verbose_name='مدين'
    )
    credit = models.DecimalField(
        max_digits=15,
        decimal_places=4,
        default=Decimal('0'),
        verbose_name='دائن'
    )
    description = models.CharField(
        max_length=500, blank=True, verbose_name='البيان'
    )

    objects = JournalItemQuerySet.as_manager()

    class Meta:
        verbose_name = 'بند قيد'
        verbose_name_plural = 'بنود القيد'
        constraints = [
            models.CheckConstraint(
                condition=~Q(debit__gt=0, credit__gt=0),
                name='accounting_not_both_debit_and_credit'
            ),
            models.CheckConstraint(
                condition=Q(debit__gte=0),
                name='accounting_debit_non_negative'
            ),
            models.CheckConstraint(
                condition=Q(credit__gte=0),
                name='accounting_credit_non_negative'
            ),
        ]

    def __str__(self):
        if self.debit > 0:
            return f'{self.account} | مدين: {self.debit:.4f}'
        return f'{self.account} | دائن: {self.credit:.4f}'

    def _check_period_lock(self):
        """A line can't be added to / changed in / removed from a closed-period entry."""
        if not _lock_date():
            return
        if self.entry_id:
            ensure_open_period(
                JournalEntry.objects.filter(pk=self.entry_id).values_list('date', flat=True).first())
        if self.pk and not self._state.adding:
            old_entry_date = JournalItem.objects.filter(pk=self.pk).values_list('entry__date', flat=True).first()
            ensure_open_period(old_entry_date)

    def _check_entry_open(self):
        entry_ids = {self.entry_id}
        if self.pk and not self._state.adding:
            entry_ids.add(JournalItem.objects.filter(pk=self.pk).values_list('entry_id', flat=True).first())
        if JournalEntry.objects.filter(pk__in=[e for e in entry_ids if e], status=JournalEntry.POSTED).exists():
            raise ValidationError(JournalEntry.POSTED_LOCKED_MSG)

    def save(self, *args, **kwargs):
        self._check_entry_open()
        self._check_period_lock()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._check_entry_open()
        self._check_period_lock()
        return super().delete(*args, **kwargs)

    def clean(self):
        """Ensure a journal item has either debit or credit, not both."""
        if self.debit > 0 and self.credit > 0:
            raise ValidationError('لا يمكن أن يكون للبند قيمة مدينة ودائنة في نفس الوقت.')
        if self.debit < 0 or self.credit < 0:
            raise ValidationError('لا يمكن أن تكون القيم سالبة.')


class PaymentMethod(models.Model):
    name = models.CharField(max_length=100, verbose_name='طريقة الدفع')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, verbose_name='حساب الدفع')
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    
    class Meta:
        verbose_name = 'طريقة الدفع'
        verbose_name_plural = 'طرق الدفع'
        
    def __str__(self):
        return self.name


class TreasuryQuerySet(models.QuerySet):
    def business(self):
        """Treasuries for POS, purchases, defaults…: a sales rep's cash box (invoicing.SalesRep.treasury)
        is his own and only moves through his sales, collections and hand-overs."""
        return self.filter(sales_reps__isnull=True)


def is_rep_cash_box(treasury_id):
    if not treasury_id:
        return False
    from apps.tenant.invoicing.models import SalesRep
    return SalesRep.objects.filter(treasury_id=treasury_id).exists()


class Treasury(models.Model):
    CASH = 'CASH'
    BANK = 'BANK'
    TREASURY_TYPE_CHOICES = [
        (CASH, 'خزينة نقدية'),
        (BANK, 'بنك'),
    ]

    objects = TreasuryQuerySet.as_manager()
    name = models.CharField(max_length=100, verbose_name='اسم الخزينة')
    branch = models.ForeignKey('core.Branch', on_delete=models.PROTECT, verbose_name='الفرع')
    treasury_type = models.CharField(max_length=20, choices=TREASURY_TYPE_CHOICES, default=CASH, verbose_name='نوع الخزينة')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='رصيد أول المدة')
    opening_balance_date = models.DateField(null=True, blank=True, verbose_name='تاريخ رصيد أول المدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name='حساب الخزينة')

    class Meta:
        verbose_name = 'خزينة'
        verbose_name_plural = 'الخزائن'

    def __str__(self):
        return self.name

    @property
    def current_balance(self):
        if self.account:
            return self.account.current_balance
        return Decimal('0.00')

    def ensure_account(self):
        """Ensures this Treasury is linked to a valid ledger account, auto-creating it if missing."""
        if self.account_id and self.account and getattr(self.account, 'code', None) not in ['1100', '1110', '1120']:
            return self.account
        from apps.tenant.services.journal_service import _get_system_account
        try:
            with transaction.atomic():
                parent_code = '1120' if self.treasury_type == self.BANK else '1110'
                parent_acc = _get_system_account(parent_code)
                if parent_acc:
                    acc, _ = Account.objects.get_or_create(
                        code=f"{parent_acc.code}-{self.id}",
                        defaults={
                            'name': self.name,
                            'account_type': parent_acc.account_type,
                            'parent': parent_acc,
                            'allow_reconcile': True
                        }
                    )
                    if self.account_id != acc.id:
                        self.account = acc
                        Treasury.objects.filter(id=self.id).update(account=acc)
                    return acc
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error ensuring treasury account: {e}")
        return self.account

    def save(self, *args, **kwargs):
        created = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            # explicit ledger setup (no signal): its own sub-account, then the opening-balance entry
            self.ensure_account()
            if created and (self.opening_balance or 0) > 0 and self.account_id:
                _book_opening_balance_safely(self)


class BankAccount(models.Model):
    name = models.CharField(max_length=100, verbose_name='اسم الحساب البنكي')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='رصيد أول المدة')
    opening_balance_date = models.DateField(null=True, blank=True, verbose_name='تاريخ رصيد أول المدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name='حساب الدليل')

    class Meta:
        verbose_name = 'حساب بنكي'
        verbose_name_plural = 'حسابات البنوك'

    def __str__(self):
        return self.name

    @property
    def current_balance(self):
        if self.account:
            return self.account.current_balance
        return Decimal('0.00')

    def ensure_account(self):
        """Ensures this BankAccount is linked to a valid ledger account, auto-creating it if missing."""
        if self.account_id and self.account and getattr(self.account, 'code', None) not in ['1100', '1120']:
            return self.account
        from apps.tenant.services.journal_service import _get_system_account
        try:
            with transaction.atomic():
                parent_acc = _get_system_account('1120')
                if parent_acc:
                    acc, _ = Account.objects.get_or_create(
                        code=f"{parent_acc.code}-{self.id}",
                        defaults={
                            'name': self.name,
                            'account_type': parent_acc.account_type,
                            'parent': parent_acc,
                            'allow_reconcile': True
                        }
                    )
                    if self.account_id != acc.id:
                        self.account = acc
                        BankAccount.objects.filter(id=self.id).update(account=acc)
                    return acc
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error ensuring bank account: {e}")
        return self.account

    def save(self, *args, **kwargs):
        created = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            # explicit ledger setup (no signal): its own sub-account, then the opening-balance entry
            self.ensure_account()
            if created and (self.opening_balance or 0) > 0 and self.account_id:
                _book_opening_balance_safely(self)

class EWallet(models.Model):
    name = models.CharField(max_length=100, verbose_name='اسم المحفظة')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='رصيد أول المدة')
    opening_balance_date = models.DateField(null=True, blank=True, verbose_name='تاريخ رصيد أول المدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name='حساب الدليل')

    class Meta:
        verbose_name = 'محفظة إلكترونية'
        verbose_name_plural = 'المحافظ الإلكترونية'

    def __str__(self):
        return self.name

    @property
    def current_balance(self):
        if self.account:
            return self.account.current_balance
        return Decimal('0.00')

    def ensure_account(self):
        """Ensures this EWallet is linked to a valid ledger account, auto-creating it if missing."""
        if self.account_id and self.account and getattr(self.account, 'code', None) not in ['1100', '1160']:
            return self.account
        from apps.tenant.services.journal_service import _get_system_account
        try:
            with transaction.atomic():
                parent_acc = _get_system_account('1160')
                if parent_acc:
                    acc, _ = Account.objects.get_or_create(
                        code=f"{parent_acc.code}-{self.id}",
                        defaults={
                            'name': self.name,
                            'account_type': parent_acc.account_type,
                            'parent': parent_acc,
                            'allow_reconcile': True
                        }
                    )
                    if self.account_id != acc.id:
                        self.account = acc
                        EWallet.objects.filter(id=self.id).update(account=acc)
                    return acc
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error ensuring ewallet account: {e}")
        return self.account

    def save(self, *args, **kwargs):
        created = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            # explicit ledger setup (no signal): its own sub-account, then the opening-balance entry
            self.ensure_account()
            if created and (self.opening_balance or 0) > 0 and self.account_id:
                _book_opening_balance_safely(self)


OPENING_REFERENCE_PREFIXES = {
    'Treasury': 'OPENING-TR',
    'BankAccount': 'OPENING-BK',
    'EWallet': 'OPENING-EW',
    'POSMachine': 'OPENING-PM',
}


def book_opening_balance(instance, ref_prefix=None):
    """
    Posts the opening-balance entry of a Treasury / BankAccount / EWallet / POSMachine:
        Dr  <instance ledger account>            opening_balance
        Cr  3999 أرصدة افتتاحية وتسويات رأس المال  opening_balance

    Atomic and idempotent (keyed by reference '<PREFIX>-<pk>'):
      - a POSTED entry that already has items is returned untouched;
      - an empty / DRAFT header left behind by the old broken signal is repaired in place.
    Returns the posted JournalEntry, or None when there is nothing to book.
    """
    from apps.tenant.services.journal_service import _get_system_account

    amount = Decimal(str(instance.opening_balance or 0))
    if amount <= 0 or not instance.pk:
        return None
    account = instance.account
    if account is None:
        return None
    ref_prefix = ref_prefix or OPENING_REFERENCE_PREFIXES[type(instance).__name__]
    ref = f"{ref_prefix}-{instance.pk}"

    with transaction.atomic():
        entry = JournalEntry.objects.select_for_update(of=('self',)).filter(reference=ref).first()
        if entry is not None and entry.status == JournalEntry.POSTED and entry.items.exists():
            return entry

        opening_equity_acc = _get_system_account('3999')
        entry_date = getattr(instance, 'opening_balance_date', None) or datetime.date.today()
        if entry is None:
            entry = JournalEntry.objects.create(
                date=entry_date,
                reference=ref,
                description=f"رصيد افتتاحي - {instance.name}",
                status=JournalEntry.DRAFT,
            )
        else:
            entry.items.all().delete()
            if entry.status != JournalEntry.DRAFT:
                entry.status = JournalEntry.DRAFT
                entry.save(update_fields=['status', 'updated_at'])

        entry.content_type = ContentType.objects.get_for_model(instance)
        entry.object_id = instance.pk
        entry.save(update_fields=['content_type', 'object_id'])

        JournalItem.objects.create(entry=entry, account=account, debit=amount, credit=0,
                                   description=f"رصيد افتتاحي - {instance.name}")
        JournalItem.objects.create(entry=entry, account=opening_equity_acc, debit=0, credit=amount,
                                   description=f"رصيد افتتاحي - {instance.name}")
        entry.post()
        return entry


def _book_opening_balance_safely(instance):
    """Called from save(): propagates errors so atomic save rolls back."""
    book_opening_balance(instance)


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------

class Expense(models.Model):
    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    CANCELLED = 'CANCELLED'

    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مرحل'),
        (CANCELLED, 'ملغى'),
    ]

    expense_number = models.CharField(max_length=30, unique=True, blank=True, verbose_name='رقم المصروف')
    date = models.DateField(default=datetime.date.today, verbose_name='تاريخ المصروف')
    branch = models.ForeignKey('core.Branch', on_delete=models.PROTECT, verbose_name='الفرع')
    
    expense_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='expenses_debited', limit_choices_to={'account_type': Account.EXPENSE}, verbose_name='حساب المصروف')
    payment_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='expenses_credited', limit_choices_to={'account_type': Account.ASSET}, verbose_name='حساب الدفع (الخزينة/البنك)')
    
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='المبلغ (قبل الضريبة)')
    vat_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name='نسبة القيمة المضافة (%)')
    vat_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='قيمة الضريبة المضافة')
    
    withholding_tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name='نسبة الخصم والإضافة (%)')
    withholding_tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='قيمة الخصم والإضافة')
    
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='الإجمالي الصافي')
    
    description = models.TextField(blank=True, verbose_name='البيان / ملاحظات')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة')
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='أُنشئ بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name='expense_doc', verbose_name='قيد اليومية')

    class Meta:
        verbose_name = 'مصروف'
        verbose_name_plural = 'المصروفات'
        ordering = ['-date', '-id']

    def __str__(self):
        return f"{self.expense_number} - {self.description[:30]}"

    def save(self, *args, **kwargs):
        # Calculate amounts
        self.vat_amount = (self.amount * (self.vat_percent / Decimal('100.0'))).quantize(Decimal('0.01'))
        self.withholding_tax_amount = (self.amount * (self.withholding_tax_percent / Decimal('100.0'))).quantize(Decimal('0.01'))
        self.total_amount = self.amount + self.vat_amount - self.withholding_tax_amount

        if not self.expense_number:
            year = datetime.date.today().year
            with transaction.atomic():
                self.expense_number = _next_doc_number('expense', Expense, 'expense_number', f"EXP-{year}-")
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def confirm_expense(self):
        """Generates the accounting journal entry and marks as POSTED."""
        from django.db import transaction
        
        with transaction.atomic():
            # Lock the row and re-check status so a double click / concurrent request can't post twice
            current_status = Expense.objects.select_for_update(of=('self',)).filter(pk=self.pk).values_list('status', flat=True).first()
            if current_status is not None:
                self.status = current_status
            if self.status != self.DRAFT:
                raise ValidationError("لا يمكن ترحيل إلا مسودة المصروف.")
            if (self.amount or Decimal('0')) <= 0 or (self.total_amount or Decimal('0')) <= 0:
                raise ValidationError("لا يمكن ترحيل مصروف بمبلغ صفر أو سالب.")

            from apps.tenant.services.journal_service import _get_system_account, delete_orphan_entries, unique_reference

            # Clean up any orphaned journal entry from previous failed attempts (never a reversal pair)
            delete_orphan_entries(self.expense_number)

            entry = JournalEntry.objects.create(
                date=self.date,
                reference=unique_reference(self.expense_number),
                description=f"سداد مصروف: {self.description}",
                status=JournalEntry.POSTED,
                created_by=self.created_by
            )
            
            # Dr: Expense Account (Separate line per ExpenseItem with its specific description)
            items = list(self.items.all())
            if items:
                for item in items:
                    if item.amount > 0:
                        JournalItem.objects.create(
                            entry=entry,
                            account=self.expense_account,
                            debit=item.amount,
                            credit=0,
                            description=item.description or self.description or f"سداد مصروف: {self.expense_account.name}"
                        )
            else:
                JournalItem.objects.create(
                    entry=entry,
                    account=self.expense_account,
                    debit=self.amount,
                    credit=0,
                    description=self.description or f"سداد مصروف: {self.expense_account.name}"
                )
            
            # Dr: VAT Account
            if self.vat_amount > 0:
                vat_account = _get_system_account('1190')  # 1190 = Input VAT (مدخلات) — same as purchase invoices
                if vat_account:
                    JournalItem.objects.create(
                        entry=entry,
                        account=vat_account,
                        debit=self.vat_amount,
                        credit=0,
                        description=f"ضريبة قيمة مضافة - مصروف {self.expense_number}"
                    )
                    
            # Cr: Withholding Tax Payable (2120 - same account used by purchase invoices)
            if self.withholding_tax_amount > 0:
                wh_account = _get_system_account('2120')  # raises ValueError if missing
                JournalItem.objects.create(
                    entry=entry,
                    account=wh_account,
                    debit=0,
                    credit=self.withholding_tax_amount,
                    description=f"ضريبة خصم وإضافة - مصروف {self.expense_number}"
                )
                    
            # Cr: Payment Account (Cash/Bank/Wallet)
            JournalItem.objects.create(
                entry=entry,
                account=self.payment_account,
                debit=0,
                credit=self.total_amount,
                description=f"سداد مصروف {self.expense_account.name}"
            )
            
            entry.post(self.created_by)
            self.status = self.POSTED
            self.journal_entry = entry
            self.save()

class ExpenseItem(models.Model):
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name='items', verbose_name='المصروف')
    description = models.CharField(max_length=255, verbose_name='بيان البند')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ')

    class Meta:
        verbose_name = 'بند المصروف'
        verbose_name_plural = 'بنود المصروفات'

    def __str__(self):
        return self.description

# ---------------------------------------------------------------------------
# Vouchers (Receipts & Payments)
# ---------------------------------------------------------------------------

class Voucher(models.Model):
    RECEIPT = 'RECEIPT'
    PAYMENT = 'PAYMENT'
    VOUCHER_TYPE_CHOICES = [
        (RECEIPT, 'سند قبض'),
        (PAYMENT, 'سند صرف')
    ]

    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    CANCELLED = 'CANCELLED'
    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مُرحل'),
        (CANCELLED, 'ملغي')
    ]

    CASH = 'CASH'
    CARD = 'CARD'
    EWALLET = 'EWALLET'
    BANK_TRANSFER = 'BANK_TRANSFER'
    SPLIT = 'SPLIT'
    PAYMENT_METHOD_CHOICES = [
        (CASH, 'نقدي (كاش)'),
        (CARD, 'بطاقة / فيزا'),
        (EWALLET, 'محفظة إلكترونية'),
        (BANK_TRANSFER, 'تحويل بنكي / إنستاباي'),
        (SPLIT, 'سداد مقسم / متنوع'),
    ]

    voucher_type = models.CharField(max_length=15, choices=VOUCHER_TYPE_CHOICES, verbose_name='نوع السند')
    voucher_number = models.CharField(max_length=20, unique=True, editable=False, verbose_name='رقم السند')
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ')
    
    partner = models.ForeignKey(
        'partners.Partner', on_delete=models.PROTECT, null=True, blank=True,
        related_name='vouchers', verbose_name='الشريك (عميل/مورد)'
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='vouchers',
        verbose_name='الحساب المقابل'
    )
    
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default=CASH, verbose_name='طريقة الدفع')
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, verbose_name='الخزينة')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, null=True, blank=True, verbose_name='الحساب البنكي / ماكينة الفيزا')
    ewallet = models.ForeignKey(EWallet, on_delete=models.PROTECT, null=True, blank=True, verbose_name='المحفظة الإلكترونية')
    transfer_bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, null=True, blank=True,
        related_name='transfer_vouchers', verbose_name='حساب إنستاباي / التحويل البنكي'
    )

    # حقول تفاصيل السداد المقسم / المتنوع
    paid_cash = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='المبلغ النقدي')
    paid_card = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='مبلغ البطاقة / فيزا')
    paid_ewallet = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='مبلغ المحفظة الإلكترونية')
    paid_bank = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='مبلغ التحويل البنكي / إنستاباي')

    pos_session = models.ForeignKey(
        'invoicing.POSSession', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='vouchers', verbose_name='الوردية المرتبطة'
    )
    branch = models.ForeignKey(
        'core.Branch', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='vouchers', verbose_name='الفرع'
    )

    description = models.TextField(verbose_name='البيان', blank=True, null=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة')
    
    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='voucher', verbose_name='قيد اليومية'
    )
    idempotency_key = models.UUIDField(null=True, blank=True, editable=False, verbose_name='مفتاح منع التكرار')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'سند'
        verbose_name_plural = 'السندات'
        ordering = ['-date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['idempotency_key'],
                condition=models.Q(idempotency_key__isnull=False),
                name='uniq_voucher_idempotency_key',
            ),
        ]

    def __str__(self):
        return f"{self.get_voucher_type_display()} #{self.voucher_number} - {self.amount}"

    def save(self, *args, **kwargs):
        # مزامنة تفاصيل السداد المقسم مع طريقة الدفع
        if self.payment_method == self.CASH:
            self.paid_cash = self.amount or Decimal('0')
            self.paid_card = Decimal('0')
            self.paid_ewallet = Decimal('0')
            self.paid_bank = Decimal('0')
        elif self.payment_method == self.CARD:
            self.paid_card = self.amount or Decimal('0')
            self.paid_cash = Decimal('0')
            self.paid_ewallet = Decimal('0')
            self.paid_bank = Decimal('0')
        elif self.payment_method == self.EWALLET:
            self.paid_ewallet = self.amount or Decimal('0')
            self.paid_cash = Decimal('0')
            self.paid_card = Decimal('0')
            self.paid_bank = Decimal('0')
        elif self.payment_method == self.BANK_TRANSFER:
            self.paid_bank = self.amount or Decimal('0')
            self.paid_cash = Decimal('0')
            self.paid_card = Decimal('0')
            self.paid_ewallet = Decimal('0')
            if not self.bank_account and self.transfer_bank_account:
                self.bank_account = self.transfer_bank_account
            elif not self.transfer_bank_account and self.bank_account:
                self.transfer_bank_account = self.bank_account
        elif self.payment_method == self.SPLIT:
            split_sum = (self.paid_cash or 0) + (self.paid_card or 0) + (self.paid_ewallet or 0) + (self.paid_bank or 0)
            if split_sum == 0 and self.amount:
                self.paid_cash = self.amount

        # Branch attribution (used for branch-scoped visibility of vouchers)
        if not self.branch_id:
            if self.pos_session_id and self.pos_session and self.pos_session.branch_id:
                self.branch_id = self.pos_session.branch_id
            elif self.treasury_id and self.treasury and self.treasury.branch_id:
                self.branch_id = self.treasury.branch_id

        if not self.voucher_number:
            prefix = 'REC' if self.voucher_type == self.RECEIPT else 'PAY'
            year = datetime.date.today().year
            with transaction.atomic():
                self.voucher_number = _next_doc_number('voucher', Voucher, 'voucher_number', f"{prefix}-{year}-")
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    @property
    def document_branch(self):
        """The branch this voucher belongs to (explicit, else its shift's / treasury's)."""
        if self.branch_id:
            return self.branch
        if self.pos_session_id and self.pos_session and self.pos_session.branch_id:
            return self.pos_session.branch
        if self.treasury_id and self.treasury and self.treasury.branch_id:
            return self.treasury.branch
        return None

    def _pos_defaults(self):
        """Per-branch default treasury / bank / instapay / e-wallet for this voucher."""
        from apps.tenant.core.settings_helpers import get_pos_defaults
        return get_pos_defaults(self.document_branch)

    @property
    def payment_account(self):
        if self.payment_method == self.CASH:
            if not self.treasury:
                defaults = self._pos_defaults()
                if self.pos_session and self.pos_session.treasury:
                    self.treasury = self.pos_session.treasury
                elif defaults['treasury']:
                    self.treasury = defaults['treasury']
                elif Treasury.objects.business().exists():
                    self.treasury = Treasury.objects.business().first()
                if self.treasury and self.pk:
                    Voucher.objects.filter(pk=self.pk).update(treasury=self.treasury)

            if self.treasury:
                return self.treasury.ensure_account()
            return None

        elif self.payment_method in (self.BANK_TRANSFER, self.CARD):
            if not self.bank_account and self.transfer_bank_account and self.payment_method == self.BANK_TRANSFER:
                self.bank_account = self.transfer_bank_account

            if not self.bank_account:
                defaults = self._pos_defaults()
                if self.payment_method == self.CARD and defaults['bank_account']:
                    self.bank_account = defaults['bank_account']
                elif self.payment_method == self.BANK_TRANSFER and defaults['instapay_account']:
                    self.bank_account = defaults['instapay_account']
                elif self.payment_method == self.BANK_TRANSFER and defaults['bank_account']:
                    self.bank_account = defaults['bank_account']
                elif BankAccount.objects.exists():
                    self.bank_account = BankAccount.objects.first()
                if self.bank_account and self.pk:
                    Voucher.objects.filter(pk=self.pk).update(bank_account=self.bank_account)

            if self.bank_account:
                return self.bank_account.ensure_account()
            return None

        elif self.payment_method == self.EWALLET:
            if not self.ewallet:
                defaults = self._pos_defaults()
                if defaults['ewallet']:
                    self.ewallet = defaults['ewallet']
                elif EWallet.objects.exists():
                    self.ewallet = EWallet.objects.first()
                if self.ewallet and self.pk:
                    Voucher.objects.filter(pk=self.pk).update(ewallet=self.ewallet)

            if self.ewallet:
                return self.ewallet.ensure_account()
            return None

        elif self.payment_method == self.SPLIT:
            if self.treasury:
                acc = self.treasury.ensure_account()
                if acc: return acc
            if self.bank_account:
                acc = self.bank_account.ensure_account()
                if acc: return acc
            if self.ewallet:
                acc = self.ewallet.ensure_account()
                if acc: return acc
            trans_acc = self.transfer_bank_account or self.bank_account
            if trans_acc:
                acc = trans_acc.ensure_account()
                if acc: return acc
        return None

    def confirm_voucher(self):
        """Generates the accounting journal entry and marks as POSTED."""
        from django.db import transaction
        from django.core.exceptions import ValidationError
        
        with transaction.atomic():
            # Lock the row and re-check status so a voucher can never be posted twice concurrently
            current_status = Voucher.objects.select_for_update(of=('self',)).filter(pk=self.pk).values_list('status', flat=True).first()
            if current_status is not None:
                self.status = current_status
            if self.status != self.DRAFT:
                raise ValidationError("لا يمكن ترحيل إلا مسودة السند.")
            if (self.amount or Decimal('0')) <= 0:
                raise ValidationError("مبلغ السند يجب أن يكون أكبر من صفر.")
            if any((v or Decimal('0')) < 0 for v in (self.paid_cash, self.paid_card, self.paid_ewallet, self.paid_bank)):
                raise ValidationError("مبالغ السداد لا يمكن أن تكون سالبة.")

            if self.payment_method == self.SPLIT:
                split_sum = (self.paid_cash or Decimal('0')) + (self.paid_card or Decimal('0')) + (self.paid_ewallet or Decimal('0')) + (self.paid_bank or Decimal('0'))
                if split_sum != self.amount:
                    raise ValidationError(f"إجمالي مبالغ السداد المقسم ({split_sum}) لا يساوي إجمالي مبلغ السند ({self.amount}).")

                if self.paid_cash > 0:
                    if not self.treasury:
                        self.treasury = self._pos_defaults()['treasury'] or Treasury.objects.business().first()
                        if self.treasury and self.pk:
                            Voucher.objects.filter(pk=self.pk).update(treasury=self.treasury)
                    if not self.treasury or not self.treasury.ensure_account():
                        raise ValidationError("يجب تحديد الخزينة للجزء النقدي والتأكد من ربطها بحساب محاسبي.")

                if self.paid_card > 0:
                    if not self.bank_account:
                        self.bank_account = self._pos_defaults()['bank_account'] or BankAccount.objects.first()
                        if self.bank_account and self.pk:
                            Voucher.objects.filter(pk=self.pk).update(bank_account=self.bank_account)
                    if not self.bank_account or not self.bank_account.ensure_account():
                        raise ValidationError("يجب تحديد ماكينة الفيزا / الحساب البنكي لجزء البطاقة.")

                if self.paid_ewallet > 0:
                    if not self.ewallet:
                        self.ewallet = self._pos_defaults()['ewallet'] or EWallet.objects.first()
                        if self.ewallet and self.pk:
                            Voucher.objects.filter(pk=self.pk).update(ewallet=self.ewallet)
                    if not self.ewallet or not self.ewallet.ensure_account():
                        raise ValidationError("يجب تحديد المحفظة الإلكترونية لجزء المحفظة.")

                trans_acc = self.transfer_bank_account or self.bank_account
                if self.paid_bank > 0:
                    if not trans_acc:
                        defaults = self._pos_defaults()
                        trans_acc = defaults['instapay_account'] or defaults['bank_account'] or BankAccount.objects.first()
                        if trans_acc and self.pk:
                            Voucher.objects.filter(pk=self.pk).update(transfer_bank_account=trans_acc)
                    if not trans_acc or not trans_acc.ensure_account():
                        raise ValidationError("يجب تحديد حساب إنستاباي / التحويل البنكي لجزء التحويل.")

                # Clean up any orphaned journal entry (reversed entries of a previous posting are kept)
                from apps.tenant.services.journal_service import delete_orphan_entries, unique_reference
                delete_orphan_entries(self.voucher_number)

                entry = JournalEntry.objects.create(
                    date=self.date,
                    reference=unique_reference(self.voucher_number),
                    description=f"{self.get_voucher_type_display()} (سداد مقسم) - {self.description or ''}",
                    status=JournalEntry.POSTED
                )

                if self.voucher_type == self.RECEIPT:
                    # Receipt: Dr Payment Methods | Cr Account
                    if self.paid_cash > 0:
                        JournalItem.objects.create(entry=entry, account=self.treasury.account, debit=self.paid_cash, credit=0, description=f"{self.voucher_number} - نقدي")
                    if self.paid_card > 0:
                        JournalItem.objects.create(entry=entry, account=self.bank_account.account, debit=self.paid_card, credit=0, description=f"{self.voucher_number} - بطاقة / فيزا")
                    if self.paid_ewallet > 0:
                        JournalItem.objects.create(entry=entry, account=self.ewallet.account, debit=self.paid_ewallet, credit=0, description=f"{self.voucher_number} - محفظة إلكترونية")
                    if self.paid_bank > 0:
                        JournalItem.objects.create(entry=entry, account=trans_acc.account, debit=self.paid_bank, credit=0, description=f"{self.voucher_number} - إنستاباي / تحويل بنكي")
                    JournalItem.objects.create(entry=entry, account=self.account, debit=0, credit=self.amount)
                else:
                    # Payment: Dr Account | Cr Payment Methods
                    JournalItem.objects.create(entry=entry, account=self.account, debit=self.amount, credit=0)
                    if self.paid_cash > 0:
                        JournalItem.objects.create(entry=entry, account=self.treasury.account, debit=0, credit=self.paid_cash, description=f"{self.voucher_number} - نقدي")
                    if self.paid_card > 0:
                        JournalItem.objects.create(entry=entry, account=self.bank_account.account, debit=0, credit=self.paid_card, description=f"{self.voucher_number} - بطاقة / فيزا")
                    if self.paid_ewallet > 0:
                        JournalItem.objects.create(entry=entry, account=self.ewallet.account, debit=0, credit=self.paid_ewallet, description=f"{self.voucher_number} - محفظة إلكترونية")
                    if self.paid_bank > 0:
                        JournalItem.objects.create(entry=entry, account=trans_acc.account, debit=0, credit=self.paid_bank, description=f"{self.voucher_number} - إنستاباي / تحويل بنكي")

                entry.post()
                self.status = self.POSTED
                self.journal_entry = entry
                self.save()
            else:
                if not self.payment_account:
                    raise ValidationError("يجب تحديد حساب الدفع (الخزينة/البنك/المحفظة) المرتبط بطريقة الدفع.")
                    
                # Clean up any orphaned journal entry (reversed entries of a previous posting are kept)
                from apps.tenant.services.journal_service import delete_orphan_entries, unique_reference
                delete_orphan_entries(self.voucher_number)

                entry = JournalEntry.objects.create(
                    date=self.date,
                    reference=unique_reference(self.voucher_number),
                    description=f"{self.get_voucher_type_display()} - {self.description or ''}",
                    status=JournalEntry.POSTED
                )
                
                if self.voucher_type == self.RECEIPT:
                    # Receipt: Dr Treasury | Cr Account
                    JournalItem.objects.create(entry=entry, account=self.payment_account, debit=self.amount, credit=0)
                    JournalItem.objects.create(entry=entry, account=self.account, debit=0, credit=self.amount)
                else:
                    # Payment: Dr Account | Cr Treasury
                    JournalItem.objects.create(entry=entry, account=self.account, debit=self.amount, credit=0)
                    JournalItem.objects.create(entry=entry, account=self.payment_account, debit=0, credit=self.amount)
                
                entry.post()
                
                self.status = self.POSTED
                self.journal_entry = entry
                self.save()

    def cancel_voucher(self, user=None):
        """
        Cancels the voucher. Its posted journal entry is NOT deleted: a reversal entry
        (debit <-> credit) is posted and linked to it, so the audit trail is kept and the
        pair nets to zero. ``journal_entry`` is cleared (no active entry any more).
        Runs atomically on a locked row, and is refused when the voucher belongs to an
        already-closed cashier shift (the shift's cash count was reconciled against it).
        """
        from apps.tenant.services.journal_service import reverse_entry_if_active
        with transaction.atomic():
            locked = Voucher.objects.select_for_update(of=('self',)).select_related('pos_session', 'journal_entry').get(pk=self.pk)
            if locked.status == self.CANCELLED:
                raise ValidationError("السند ملغي بالفعل.")
            if locked.status == self.POSTED:
                _ensure_pos_session_open(locked.pos_session, 'إلغاء')

            if locked.journal_entry_id:
                reverse_entry_if_active(
                    locked.journal_entry, reason=f'إلغاء السند {locked.voucher_number}', user=user
                )

            self.journal_entry = None
            self.status = self.CANCELLED
            self.save(update_fields=['status', 'journal_entry', 'updated_at'])


# ---------------------------------------------------------------------------
# POS Machine (ماكينات الدفع الإلكتروني - فوري، أمان، فيزا البنوك)
# ---------------------------------------------------------------------------

class POSMachine(models.Model):
    """
    Represents a physical payment machine (e.g., Fawry, Aman, Bank Visa terminal).
    Each machine has its own ledger account under parent code 1170.
    """
    FAWRY = 'FAWRY'
    AMAN = 'AMAN'
    BANK_VISA = 'BANK_VISA'
    INSTAPAY = 'INSTAPAY'
    OTHER = 'OTHER'

    MACHINE_TYPE_CHOICES = [
        (FAWRY, 'فوري'),
        (AMAN, 'أمان'),
        (BANK_VISA, 'ماكينة فيزا بنك'),
        (INSTAPAY, 'إنستاباي'),
        (OTHER, 'أخرى'),
    ]

    name = models.CharField(max_length=150, verbose_name='اسم الماكينة')
    machine_type = models.CharField(
        max_length=20, choices=MACHINE_TYPE_CHOICES, default=OTHER,
        verbose_name='نوع الماكينة'
    )
    branch = models.ForeignKey(
        'core.Branch', on_delete=models.PROTECT,
        related_name='pos_machines', verbose_name='الفرع'
    )
    opening_balance = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
        verbose_name='رصيد أول المدة'
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name='pos_machines', verbose_name='حساب الدليل'
    )
    is_active = models.BooleanField(default=True, verbose_name='نشطة')
    min_balance_alert = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
        verbose_name='تنبيه عند انخفاض الرصيد عن',
        help_text='0 = بدون تنبيه. يظهر تنبيه في لوحة خدمات الدفع عند نزول رصيد الماكينة عن هذا المبلغ.'
    )
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'ماكينة دفع إلكتروني'
        verbose_name_plural = 'ماكينات الدفع الإلكتروني'
        ordering = ['branch', 'name']

    def __str__(self):
        return f'{self.name} ({self.get_machine_type_display()})'

    @property
    def current_balance(self):
        if not self.account:
            return Decimal('0')
        return self.account.current_balance

    @property
    def is_low_balance(self):
        """True when a minimum-balance alert is set and the balance dropped below it."""
        return bool(self.min_balance_alert and self.min_balance_alert > 0
                    and self.current_balance < self.min_balance_alert)



    def save(self, *args, **kwargs):
        created = self._state.adding
        super().save(*args, **kwargs)
        if not getattr(self, '_ledger_setup', False):
            self._ledger_setup = True          # the setup saves the account link once more
            try:
                create_pos_machine_account(POSMachine, self, created)   # explicit — no post_save signal
            finally:
                self._ledger_setup = False

def create_pos_machine_account(sender, instance, created, **kwargs):
    """Auto-create a ledger account under 1170 for each POS machine."""
    updated = False
    has_valid_account = instance.account and getattr(instance.account, 'code', None) not in ['1100', '1170']

    if not has_valid_account:
        from apps.tenant.services.journal_service import _get_system_account
        try:
            parent_acc = _get_system_account('1170')
            if not parent_acc:
                asset_parent = Account.objects.filter(code='1100').first()
                parent_acc = Account.objects.create(
                    code='1170',
                    name='ماكينات الدفع الإلكتروني',
                    account_type=Account.ASSET,
                    parent=asset_parent
                )
            acc = Account.objects.create(
                code=f'1170-{instance.id}',
                name=f'{instance.name}',
                account_type=Account.ASSET,
                parent=parent_acc
            )
            instance.account = acc
            updated = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'Error creating POS machine account: {e}')

    if updated:
        instance.save(update_fields=['account'])

    if created and instance.opening_balance > 0 and instance.account:
        # Same pattern as treasuries/banks/wallets: atomic + idempotent (errors propagate as before)
        book_opening_balance(instance)


# ---------------------------------------------------------------------------
# E-Service Transaction (حركات مركز خدمات الدفع)
# ---------------------------------------------------------------------------

class EServiceTransaction(models.Model):
    """
    Records all e-service operations:
      - CASH_OUT: Customer withdraws cash via store machine.
      - CASH_IN:  Customer pays cash to transfer balance.
      - MERCHANT_RECHARGE: Store recharges machine from a vendor/agent.
    """
    CASH_OUT = 'CASH_OUT'
    CASH_IN = 'CASH_IN'
    MERCHANT_RECHARGE = 'MERCHANT_RECHARGE'

    TRANSACTION_TYPE_CHOICES = [
        (CASH_OUT, 'سحب كاش (عميل يسحب من محفظته)'),
        (CASH_IN, 'إيداع / تحويل (عميل يحول رصيد)'),
        (MERCHANT_RECHARGE, 'شحن الماكينة من مندوب'),
    ]

    SOURCE_MACHINE = 'MACHINE'
    SOURCE_EWALLET = 'EWALLET'
    SOURCE_TYPE_CHOICES = [
        (SOURCE_MACHINE, 'ماكينة دفع'),
        (SOURCE_EWALLET, 'محفظة إلكترونية'),
    ]

    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    CANCELLED = 'CANCELLED'
    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مرحل'),
        (CANCELLED, 'ملغي (بقيد عكسي)'),
    ]

    REVENUE_ACCOUNT_CODE = '4240'   # إيرادات عمولات خدمات الدفع

    transaction_number = models.CharField(
        max_length=30, unique=True, blank=True, verbose_name='رقم العملية'
    )
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    transaction_type = models.CharField(
        max_length=20, choices=TRANSACTION_TYPE_CHOICES, verbose_name='نوع العملية'
    )

    source_type = models.CharField(
        max_length=10, choices=SOURCE_TYPE_CHOICES, default=SOURCE_MACHINE,
        verbose_name='مصدر الحركة'
    )
    pos_machine = models.ForeignKey(
        POSMachine, on_delete=models.PROTECT, null=True, blank=True,
        related_name='transactions', verbose_name='الماكينة'
    )
    ewallet = models.ForeignKey(
        EWallet, on_delete=models.PROTECT, null=True, blank=True,
        related_name='eservice_transactions', verbose_name='المحفظة الإلكترونية'
    )
    treasury = models.ForeignKey(
        Treasury, on_delete=models.PROTECT,
        related_name='eservice_transactions', verbose_name='الخزينة / الدرج'
    )

    principal_amount = models.DecimalField(
        max_digits=15, decimal_places=2, verbose_name='المبلغ الأصلي'
    )
    commission_revenue = models.DecimalField(
        max_digits=15, decimal_places=2, default=0, verbose_name='عمولة المحل (الربح)'
    )
    commission_expense = models.DecimalField(
        max_digits=15, decimal_places=2, default=0, verbose_name='رسوم المندوب (المصروف)'
    )

    description = models.TextField(blank=True, verbose_name='البيان')
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة'
    )

    # Cash drawer reconciliation: the cashier's open shift when the operation was made
    pos_session = models.ForeignKey(
        'invoicing.POSSession', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='eservice_transactions', verbose_name='الوردية'
    )
    # Merchant recharge: the agent (supplier) and whether it was paid now or on credit
    agent = models.ForeignKey(
        'partners.Partner', on_delete=models.PROTECT, null=True, blank=True,
        related_name='eservice_recharges', verbose_name='المندوب / المورد'
    )
    paid_on_credit = models.BooleanField(
        default=False, verbose_name='شحن آجل (يُسجل على حساب المندوب بدلاً من الدفع من الخزينة)'
    )
    commission_overridden = models.BooleanField(default=False, verbose_name='تم تعديل العمولة يدوياً')
    cancelled_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='ألغاها'
    )
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name='وقت الإلغاء')
    cancel_reason = models.CharField(max_length=255, blank=True, default='', verbose_name='سبب الإلغاء')

    invoice = models.OneToOneField(
        'invoicing.Invoice', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='eservice_transaction', verbose_name='فاتورة المبيعات'
    )
    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='eservice_transaction', verbose_name='قيد اليومية'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='بواسطة'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'حركة خدمات دفع'
        verbose_name_plural = 'حركات خدمات الدفع'
        ordering = ['-date', '-created_at']
        permissions = [
            ('override_eservice_commission', 'تعديل عمولة خدمات الدفع يدوياً'),
            ('cancel_eservicetransaction', 'إلغاء حركة خدمات دفع'),
        ]

    def __str__(self):
        return f'{self.transaction_number} - {self.get_transaction_type_display()} - {self.principal_amount}'

    def save(self, *args, **kwargs):
        if not self.transaction_number:
            year = datetime.date.today().year
            with transaction.atomic():
                self.transaction_number = _next_doc_number(
                    'eservice', EServiceTransaction, 'transaction_number', f'ESVC-{year}-'
                )
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    @property
    def cash_effect(self):
        """
        Signed effect on the treasury / cash drawer once POSTED (0 otherwise):
          CASH_OUT          -> -(principal - commission)   cash handed to the customer
          CASH_IN           -> +(principal + commission)   cash received from the customer
          MERCHANT_RECHARGE -> -(principal + agent fee)    cash paid to the agent (0 if on credit)
        Single source of truth for shift reconciliation and dashboards.
        """
        if self.status != self.POSTED:
            return Decimal('0')
        principal = self.principal_amount or Decimal('0')
        if self.transaction_type == self.CASH_OUT:
            return -(principal - (self.commission_revenue or 0))
        if self.transaction_type == self.CASH_IN:
            return principal + (self.commission_revenue or 0)
        if self.transaction_type == self.MERCHANT_RECHARGE:
            return Decimal('0') if self.paid_on_credit else -(principal + (self.commission_expense or 0))
        return Decimal('0')

    def get_source_account(self):
        if self.source_type == self.SOURCE_MACHINE and self.pos_machine:
            return self.pos_machine.account
        elif self.source_type == self.SOURCE_EWALLET and self.ewallet:
            return self.ewallet.account
        return None

    @property
    def source_name(self):
        if self.source_type == self.SOURCE_MACHINE and self.pos_machine:
            return self.pos_machine.name
        if self.source_type == self.SOURCE_EWALLET and self.ewallet:
            return self.ewallet.name
        return '—'

    # -- commission -----------------------------------------------------------

    @property
    def commission_field(self):
        """The commission field that applies to this type: the store's revenue for
        CASH_IN / CASH_OUT, the agent's fee (expense) for MERCHANT_RECHARGE."""
        return 'commission_expense' if self.transaction_type == self.MERCHANT_RECHARGE else 'commission_revenue'

    @property
    def commission_amount(self):
        return getattr(self, self.commission_field) or Decimal('0')

    def apply_commission_rule(self, entered=None, can_override=False):
        """
        Sets the commission from the matching EServiceCommissionRule (most specific wins).

        - entered is None (field left blank)        -> the computed value is used.
        - no rule matches                           -> the entered value is accepted (manual).
        - entered == computed                       -> fine.
        - entered != computed and can_override      -> kept, commission_overridden=True.
        - entered != computed and not can_override  -> ValidationError.
        The field that does not apply to the type is reset to 0.
        Returns the matching rule (or None).
        """
        other = 'commission_revenue' if self.commission_field == 'commission_expense' else 'commission_expense'
        setattr(self, other, Decimal('0'))
        machine = self.pos_machine if self.source_type == self.SOURCE_MACHINE else None
        rule = EServiceCommissionRule.match(self.transaction_type, machine)
        self.commission_overridden = False
        if rule is None:
            setattr(self, self.commission_field, Decimal(str(entered or 0)).quantize(Decimal('0.01')))
            return None
        computed = rule.compute(self.principal_amount)
        if entered is None:
            setattr(self, self.commission_field, computed)
            return rule
        entered = Decimal(str(entered)).quantize(Decimal('0.01'))
        if entered != computed:
            if not can_override:
                raise ValidationError(
                    f'العمولة المدخلة ({entered:,.2f} ج.م) تختلف عن العمولة المحسوبة حسب قاعدة '
                    f'"{rule.name}" ({computed:,.2f} ج.م). تعديل العمولة يدوياً يحتاج صلاحية '
                    f'"تعديل عمولة خدمات الدفع يدوياً".'
                )
            self.commission_overridden = True
        setattr(self, self.commission_field, entered)
        return rule

    # -- accounts -------------------------------------------------------------

    @classmethod
    def revenue_account(cls):
        """4240 - إيرادات عمولات خدمات الدفع (REVENUE under 4000), created lazily."""
        acc = Account.objects.filter(code=cls.REVENUE_ACCOUNT_CODE).first()
        if acc:
            if not acc.is_active:
                acc.is_active = True
                acc.save(update_fields=['is_active'])
            return acc
        parent = Account.objects.filter(code='4000').first()
        acc, _ = Account.objects.get_or_create(
            code=cls.REVENUE_ACCOUNT_CODE,
            defaults={'name': 'إيرادات عمولات خدمات الدفع', 'account_type': Account.REVENUE, 'parent': parent},
        )
        return acc

    def agent_payable_account(self):
        """The agent's (supplier's) payable ledger account; 2110 when it has none."""
        if not self.agent_id:
            return None
        if self.agent.payable_account_id:
            return self.agent.payable_account
        from apps.tenant.services.journal_service import _get_system_account
        return _get_system_account('2110')

    # -- posting --------------------------------------------------------------

    def _validate_for_posting(self):
        principal = self.principal_amount or Decimal('0')
        if principal <= 0:
            raise ValidationError('المبلغ الأصلي يجب أن يكون أكبر من صفر.')
        if (self.commission_revenue or 0) < 0 or (self.commission_expense or 0) < 0:
            raise ValidationError('العمولات لا يمكن أن تكون سالبة.')
        if self.transaction_type == self.CASH_OUT and (self.commission_revenue or 0) > principal:
            raise ValidationError('عمولة المحل لا يمكن أن تتجاوز المبلغ الأصلي في عملية السحب.')
        if self.transaction_type not in (self.CASH_OUT, self.CASH_IN, self.MERCHANT_RECHARGE):
            raise ValidationError('نوع العملية غير صحيح.')

        if self.transaction_type == self.MERCHANT_RECHARGE:
            if self.agent_id and not self.agent.is_supplier:
                raise ValidationError('المندوب يجب أن يكون مورداً (مورد أو عميل ومورد).')
            if self.paid_on_credit and not self.agent_id:
                raise ValidationError('الشحن الآجل يتطلب تحديد المندوب / المورد الذي سيُسجل عليه المبلغ.')
        elif self.paid_on_credit:
            raise ValidationError('الشحن الآجل متاح لعمليات شحن الماكينة من مندوب فقط.')

        if not self.treasury_id or not self.treasury.account_id:
            raise ValidationError('يجب تحديد خزينة/درج صحيح.')
        session = self.pos_session
        if session is not None and session.status == 'OPEN' and session.treasury_id != self.treasury_id:
            raise ValidationError(
                f'لديك وردية مفتوحة على خزينة "{session.treasury}". '
                f'حركات النقدية يجب أن تتم على خزينة الوردية.'
            )

    def _check_balances(self, source_account, treasury_account):
        """
        Locks the ledger rows involved (pk order, no deadlocks) and verifies the money
        that leaves is actually there: concurrent operations on the same machine /
        drawer are serialized, so two cashiers can't spend the same balance.
        """
        ids = sorted({source_account.pk, treasury_account.pk})
        locked = {a.pk: a for a in Account.objects.select_for_update(of=('self',)).filter(pk__in=ids).order_by('pk')}
        principal = self.principal_amount
        if self.transaction_type == self.CASH_IN:
            available = locked[source_account.pk].current_balance
            if available < principal:
                raise ValidationError(
                    f'رصيد {self.source_name} الحالي ({available:,.2f} ج.م) غير كافٍ لتحويل '
                    f'{principal:,.2f} ج.م للعميل. اشحن الرصيد أولاً.'
                )
        elif self.transaction_type == self.CASH_OUT:
            needed = principal - (self.commission_revenue or 0)
            available = locked[treasury_account.pk].current_balance
            if available < needed:
                raise ValidationError(
                    f'رصيد الخزينة "{self.treasury.name}" الحالي ({available:,.2f} ج.م) غير كافٍ لصرف '
                    f'{needed:,.2f} ج.م كاش للعميل.'
                )
        elif self.transaction_type == self.MERCHANT_RECHARGE and not self.paid_on_credit:
            needed = principal + (self.commission_expense or 0)
            available = locked[treasury_account.pk].current_balance
            if available < needed:
                raise ValidationError(
                    f'رصيد الخزينة "{self.treasury.name}" الحالي ({available:,.2f} ج.م) غير كافٍ لدفع '
                    f'{needed:,.2f} ج.م للمندوب. يمكنك تسجيل الشحن آجلاً على حساب المندوب.'
                )

    def post_transaction(self, user=None):
        """
        Books the operation's journal entry and marks it POSTED (DRAFT only).
        No sales invoice is created: the store's commission is a payment-services
        revenue (4240), unrelated to invoices / e-invoicing.
        """
        from apps.tenant.services.journal_service import delete_orphan_entries, unique_reference

        with transaction.atomic():
            current_status = EServiceTransaction.objects.select_for_update(of=('self',)).filter(
                pk=self.pk).values_list('status', flat=True).first()
            if current_status is not None:
                self.status = current_status
            if self.status == self.POSTED:
                raise ValidationError('هذه العملية مرحلة بالفعل.')
            if self.status != self.DRAFT:
                raise ValidationError('لا يمكن ترحيل عملية ملغاة.')
            self._validate_for_posting()

            source_account = self.get_source_account()
            if not source_account:
                raise ValidationError('يجب تحديد ماكينة أو محفظة للعملية.')
            treasury_account = self.treasury.account
            self._check_balances(source_account, treasury_account)

            principal = self.principal_amount
            commission = self.commission_revenue or Decimal('0')
            fee = self.commission_expense or Decimal('0')

            delete_orphan_entries(self.transaction_number)
            entry = JournalEntry.objects.create(
                date=self.date,
                reference=unique_reference(self.transaction_number),
                description=f'{self.get_transaction_type_display()} - {self.transaction_number}'
                            + (f' - {self.description}' if self.description else ''),
                created_by=user if getattr(user, 'pk', None) else self.created_by,
                content_type=ContentType.objects.get_for_model(EServiceTransaction) if self.pk else None,
                object_id=self.pk,
            )

            def line(account, debit, credit, desc):
                JournalItem.objects.create(entry=entry, account=account, debit=debit, credit=credit,
                                           description=desc)

            if self.transaction_type == self.CASH_OUT:
                # Dr machine (customer's transfer) | Cr treasury (net cash) + Cr 4240 (commission)
                line(source_account, principal, 0, 'استلام تحويل من عميل')
                if principal - commission > 0:
                    line(treasury_account, 0, principal - commission, 'صرف كاش للعميل')
                if commission > 0:
                    line(self.revenue_account(), 0, commission, 'عمولة خدمات الدفع')

            elif self.transaction_type == self.CASH_IN:
                # Dr treasury (gross cash) | Cr machine (transfer sent) + Cr 4240 (commission)
                line(treasury_account, principal + commission, 0, 'استلام كاش من عميل')
                line(source_account, 0, principal, 'تحويل رصيد للعميل')
                if commission > 0:
                    line(self.revenue_account(), 0, commission, 'عمولة خدمات الدفع')

            else:  # MERCHANT_RECHARGE
                # Dr machine + Dr 5305 (agent fee) | Cr treasury (cash) or Cr agent payable (credit)
                line(source_account, principal, 0, 'شحن رصيد الماكينة')
                if fee > 0:
                    line(Cheque.get_bank_expense_account(), fee, 0, 'رسوم مندوب الشحن')
                if self.paid_on_credit:
                    line(self.agent_payable_account(), 0, principal + fee,
                         f'شحن آجل على حساب المندوب {self.agent.name}')
                else:
                    line(treasury_account, 0, principal + fee,
                         'دفع كاش للمندوب' + (f' {self.agent.name}' if self.agent_id else ''))

            entry.post()

            self.journal_entry = entry
            self.status = self.POSTED
            self.save()

    def cancel_transaction(self, user=None, reason=''):
        """
        POSTED -> reversal entry dated today (the original stays for the audit trail;
        the accounting period lock applies to the new entry), status CANCELLED.
        DRAFT  -> CANCELLED without any entry.
        """
        from django.utils import timezone
        from apps.tenant.services.journal_service import reverse_entry_if_active

        reason = (reason or '').strip()
        if not reason:
            raise ValidationError('برجاء إدخال سبب الإلغاء.')
        with transaction.atomic():
            locked = EServiceTransaction.objects.select_for_update(of=('self',)).select_related(
                'pos_session', 'journal_entry').get(pk=self.pk)
            if locked.status == self.CANCELLED:
                raise ValidationError('هذه العملية ملغاة بالفعل.')
            if locked.status == self.POSTED:
                _ensure_pos_session_open(locked.pos_session, 'إلغاء')
                if locked.journal_entry_id:
                    reverse_entry_if_active(
                        locked.journal_entry,
                        reason=f'إلغاء عملية خدمات الدفع {locked.transaction_number}: {reason}',
                        user=user,
                    )
            self.status = self.CANCELLED
            self.cancelled_by = user if getattr(user, 'pk', None) else None
            self.cancelled_at = timezone.now()
            self.cancel_reason = reason[:255]
            self.save(update_fields=['status', 'cancelled_by', 'cancelled_at', 'cancel_reason'])

    @property
    def reversal_entry(self):
        if not self.journal_entry_id:
            return None
        return JournalEntry.objects.filter(reversal_of_id=self.journal_entry_id).first()


class EServiceCommissionRule(models.Model):
    """
    How much the store earns (CASH_IN / CASH_OUT) or pays the agent
    (MERCHANT_RECHARGE) per operation: fixed + percent, clamped to min / max.
    Most specific match wins: machine > machine type > any.
    """
    name = models.CharField(max_length=150, verbose_name='اسم القاعدة')
    transaction_type = models.CharField(
        max_length=20, choices=EServiceTransaction.TRANSACTION_TYPE_CHOICES, verbose_name='نوع العملية')
    pos_machine = models.ForeignKey(
        POSMachine, on_delete=models.CASCADE, null=True, blank=True, related_name='commission_rules',
        verbose_name='ماكينة محددة (اختياري)')
    machine_type = models.CharField(
        max_length=20, choices=POSMachine.MACHINE_TYPE_CHOICES, blank=True, default='',
        verbose_name='نوع الماكينة (اختياري)')
    fixed_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='مبلغ ثابت')
    percent = models.DecimalField(max_digits=6, decimal_places=3, default=0, verbose_name='نسبة (%)')
    min_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='حد أدنى')
    max_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='حد أقصى (0 = بدون)')
    is_active = models.BooleanField(default=True, verbose_name='نشطة')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'قاعدة عمولة خدمات دفع'
        verbose_name_plural = 'قواعد عمولات خدمات الدفع'
        ordering = ['transaction_type', 'name']

    def __str__(self):
        return self.name

    def compute(self, amount):
        amount = Decimal(str(amount or 0))
        value = (self.fixed_amount or 0) + amount * (self.percent or 0) / Decimal('100')
        if self.min_amount and value < self.min_amount:
            value = self.min_amount
        if self.max_amount and value > self.max_amount:
            value = self.max_amount
        return Decimal(value).quantize(Decimal('0.01'))

    @classmethod
    def match(cls, transaction_type, pos_machine=None):
        """
        The active rule for ``transaction_type`` that is most specific to the machine:
        a rule for this machine > a rule for its machine type > a generic rule.
        Ties: the most recently created rule wins. None = no rule (commission is manual).
        """
        qs = cls.objects.filter(is_active=True, transaction_type=transaction_type).order_by('-created_at', '-pk')
        if pos_machine is not None:
            rule = qs.filter(pos_machine=pos_machine).first()
            if rule:
                return rule
            if pos_machine.machine_type:
                rule = qs.filter(pos_machine__isnull=True, machine_type=pos_machine.machine_type).first()
                if rule:
                    return rule
        return qs.filter(pos_machine__isnull=True, machine_type='').first()

    @property
    def scope_label(self):
        if self.pos_machine_id:
            return f'ماكينة: {self.pos_machine.name}'
        if self.machine_type:
            return f'نوع: {self.get_machine_type_display()}'
        return 'عامة (كل الماكينات والمحافظ)'


# ---------------------------------------------------------------------------
# Internal Transfer (تحويل الأموال بين الحسابات: خزن، بنوك، ومحافظ فقط)
# ---------------------------------------------------------------------------

class InternalTransfer(models.Model):
    TREASURY = 'TREASURY'
    BANK = 'BANK'
    EWALLET = 'EWALLET'

    ACCOUNT_TYPE_CHOICES = [
        (TREASURY, 'خزينة / درج'),
        (BANK, 'حساب بنكي'),
        (EWALLET, 'محفظة إلكترونية'),
    ]

    DRAFT = 'DRAFT'
    IN_TRANSIT = 'IN_TRANSIT'   # inter-branch: sent, waiting for the receiving branch
    POSTED = 'POSTED'
    REJECTED = 'REJECTED'       # inter-branch: refused by the receiving branch, money returned
    CANCELLED = 'CANCELLED'

    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (IN_TRANSIT, 'مُرسل - بالطريق'),
        (POSTED, 'مُرحل'),
        (REJECTED, 'مرفوض - أُعيد للمرسل'),
        (CANCELLED, 'ملغي'),
    ]

    TRANSIT_ACCOUNT_CODE = '1195'     # نقدية بالطريق بين الفروع
    SHORTAGE_ACCOUNT_CODE = '5400'    # عجز وزيادة الخزينة

    transfer_number = models.CharField(max_length=30, unique=True, editable=False, verbose_name='رقم سند التحويل')
    date = models.DateField(default=datetime.date.today, verbose_name='تاريخ التحويل')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ المحول')

    # Source Account
    from_account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default=TREASURY, verbose_name='نوع الجهة المحول منها')
    from_treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_from', verbose_name='الخزينة (المحول منها)')
    from_bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_from', verbose_name='الحساب البنكي (المحول منه)')
    from_ewallet = models.ForeignKey(EWallet, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_from', verbose_name='المحفظة الإلكترونية (المحول منها)')

    # Destination Account
    to_account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default=TREASURY, verbose_name='نوع الجهة المحول إليها')
    to_treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_to', verbose_name='الخزينة (المحول إليها)')
    to_bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_to', verbose_name='الحساب البنكي (المحول إليه)')
    to_ewallet = models.ForeignKey(EWallet, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_to', verbose_name='المحفظة الإلكترونية (المحول إليها)')

    description = models.TextField(blank=True, null=True, verbose_name='البيان / ملاحظات')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة')

    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='internal_transfer', verbose_name='قيد اليومية'
    )
    # Inter-branch (two-step) transfers
    send_entry = models.OneToOneField(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='internal_transfer_send', verbose_name='قيد الإرسال (نقدية بالطريق)'
    )
    sent_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='internal_transfers_sent', verbose_name='أرسله'
    )
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name='وقت الإرسال')
    received_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True, verbose_name='المبلغ المستلم فعلياً')
    received_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='internal_transfers_received', verbose_name='استلمه / رفضه'
    )
    received_at = models.DateTimeField(null=True, blank=True, verbose_name='وقت الاستلام / الرفض')
    receive_note = models.TextField(blank=True, default='', verbose_name='ملاحظات الاستلام / سبب الرفض')
    pos_session = models.ForeignKey(
        'invoicing.POSSession', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transfers', verbose_name='الوردية المرتبطة'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='internal_transfers', verbose_name='المحرر'
    )
    idempotency_key = models.UUIDField(null=True, blank=True, editable=False, verbose_name='مفتاح منع التكرار')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'تحويل مالي بين الحسابات'
        verbose_name_plural = 'تحويلات الأموال بين الحسابات'
        ordering = ['-date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['idempotency_key'],
                condition=models.Q(idempotency_key__isnull=False),
                name='uniq_internal_transfer_idempotency_key',
            ),
        ]

    def __str__(self):
        return f"تحويل #{self.transfer_number} - {self.amount} ج.م"

    def save(self, *args, **kwargs):
        if not self.transfer_number:
            year = datetime.date.today().year
            with transaction.atomic():
                self.transfer_number = _next_doc_number(
                    'internal_transfer', InternalTransfer, 'transfer_number', f"TRF-{year}-"
                )
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    @property
    def source_object(self):
        if self.from_account_type == self.TREASURY:
            return self.from_treasury
        elif self.from_account_type == self.BANK:
            return self.from_bank_account
        elif self.from_account_type == self.EWALLET:
            return self.from_ewallet
        return None

    @property
    def destination_object(self):
        if self.to_account_type == self.TREASURY:
            return self.to_treasury
        elif self.to_account_type == self.BANK:
            return self.to_bank_account
        elif self.to_account_type == self.EWALLET:
            return self.to_ewallet
        return None

    @property
    def source_name(self):
        obj = self.source_object
        return obj.name if obj else '-'

    @property
    def destination_name(self):
        obj = self.destination_object
        return obj.name if obj else '-'

    @property
    def source_gl_account(self):
        obj = self.source_object
        return obj.account if (obj and hasattr(obj, 'account')) else None

    @property
    def destination_gl_account(self):
        obj = self.destination_object
        return obj.account if (obj and hasattr(obj, 'account')) else None

    @property
    def source_branch(self):
        t = self.from_treasury if self.from_account_type == self.TREASURY else None
        return t.branch if t else None

    @property
    def destination_branch(self):
        t = self.to_treasury if self.to_account_type == self.TREASURY else None
        return t.branch if t else None

    @property
    def is_inter_branch(self):
        """Treasury -> treasury of ANOTHER branch: two-step (send, then receive)."""
        src, dst = self.source_branch, self.destination_branch
        return bool(src and dst and src.pk != dst.pk)

    @staticmethod
    def _system_account(code, name, account_type, parent_code):
        acc = Account.objects.filter(code=code).first()
        if acc:
            return acc
        parent = Account.objects.filter(code=parent_code).first()
        acc, _ = Account.objects.get_or_create(
            code=code, defaults={'name': name, 'account_type': account_type, 'parent': parent})
        return acc

    def transit_account(self):
        return self._system_account(self.TRANSIT_ACCOUNT_CODE, 'نقدية بالطريق بين الفروع', Account.ASSET, '1100')

    def shortage_account(self):
        return self._system_account(self.SHORTAGE_ACCOUNT_CODE, 'عجز وزيادة الخزينة (الورديات)', Account.EXPENSE, '5000')

    def _post_entry(self, reference, description, lines, date=None):
        """lines: [(account, debit, credit)] -> a POSTED balanced journal entry (accounting.posting)."""
        from apps.tenant.accounting.posting import post_entry
        return post_entry(date=date or datetime.date.today(), reference=reference, description=description,
                          lines=[(a, d, c, description) for a, d, c in lines], source=self)

    def receive_transfer(self, user=None, received_amount=None, note=''):
        """
        Step 2 of an inter-branch transfer (receiving branch): money leaves
        'cash in transit' and enters the destination treasury. A shortfall
        (received < sent) is booked to the cash over/short account.
        """
        from django.core.exceptions import ValidationError
        from django.utils import timezone
        with transaction.atomic():
            locked = InternalTransfer.objects.select_for_update(of=('self',)).get(pk=self.pk)
            if locked.status != self.IN_TRANSIT:
                raise ValidationError('لا يمكن استلام إلا تحويل بحالة "مُرسل - بالطريق".')
            amount = locked.amount if received_amount in (None, '') else Decimal(str(received_amount))
            if amount <= 0:
                raise ValidationError('المبلغ المستلم يجب أن يكون أكبر من صفر. لرفض التحويل استخدم زر الرفض.')
            if amount > locked.amount:
                raise ValidationError(f'المبلغ المستلم لا يمكن أن يزيد عن المبلغ المرسل ({locked.amount:,.2f} ج.م).')
            dst_acc = locked.destination_gl_account
            if not dst_acc:
                raise ValidationError('الخزينة المحول إليها غير مرتبطة بحساب في الشجرة.')
            shortage = locked.amount - amount
            lines = [(dst_acc, amount, Decimal('0'))]
            if shortage > 0:
                lines.append((locked.shortage_account(), shortage, Decimal('0')))
            lines.append((locked.transit_account(), Decimal('0'), locked.amount))
            desc = (f'استلام تحويل {locked.transfer_number} من {locked.source_name} '
                    f'({locked.source_branch}) إلى {locked.destination_name}')
            if shortage > 0:
                desc += f' | عجز {shortage:,.2f} ج.م'
            entry = locked._post_entry(f'{locked.transfer_number}-RCV', desc, lines)
            self.journal_entry = entry
            self.received_amount = amount
            self.received_by = user
            self.received_at = timezone.now()
            self.receive_note = note or ''
            self.status = self.POSTED
            self.save(update_fields=['journal_entry', 'received_amount', 'received_by', 'received_at',
                                     'receive_note', 'status', 'updated_at'])

    def reject_transfer(self, user=None, note=''):
        """Receiving branch refuses: the send entry is reversed, money goes back to the source."""
        from django.core.exceptions import ValidationError
        from django.utils import timezone
        from apps.tenant.services.journal_service import reverse_entry_if_active
        with transaction.atomic():
            locked = InternalTransfer.objects.select_for_update(of=('self',)).select_related('send_entry').get(pk=self.pk)
            if locked.status != self.IN_TRANSIT:
                raise ValidationError('لا يمكن رفض إلا تحويل بحالة "مُرسل - بالطريق".')
            if locked.send_entry_id:
                reverse_entry_if_active(locked.send_entry, reason=f'رفض استلام التحويل {locked.transfer_number}', user=user)
            self.received_by = user
            self.received_at = timezone.now()
            self.receive_note = note or ''
            self.status = self.REJECTED
            self.save(update_fields=['received_by', 'received_at', 'receive_note', 'status', 'updated_at'])

    def confirm_transfer(self, user=None):
        """Generates accounting entry and marks transfer as POSTED
        (inter-branch: only SENDS it -> IN_TRANSIT; the other branch receives it)."""
        from django.db import transaction
        from django.core.exceptions import ValidationError

        with transaction.atomic():
            # Lock the transfer row and re-check its status (no double posting)
            current_status = InternalTransfer.objects.select_for_update(of=('self',)).filter(pk=self.pk).values_list('status', flat=True).first()
            if current_status is not None:
                self.status = current_status
            if self.status != self.DRAFT:
                raise ValidationError("لا يمكن ترحيل إلا مسودة التحويل.")
            if (self.amount or Decimal('0')) <= 0:
                raise ValidationError("يجب أن يكون مبلغ التحويل أكبر من صفر.")

            src_acc = self.source_gl_account
            dst_acc = self.destination_gl_account

            if not src_acc:
                raise ValidationError("يجب تحديد حساب شجرة الحسابات المرتبط بالجهة المحول منها.")
            if not dst_acc:
                raise ValidationError("يجب تحديد حساب شجرة الحسابات المرتبط بالجهة المحول إليها.")
            if src_acc == dst_acc:
                raise ValidationError("غير مسموح بالتحويل من وإلى نفس الحساب المحاسبي.")

            # Lock the source ledger account row: concurrent transfers out of the same
            # treasury/bank/wallet are serialized, so the balance check below can't be raced.
            locked_src_acc = Account.objects.select_for_update(of=('self',)).get(pk=src_acc.pk)

            src_obj = self.source_object
            if src_obj:
                src_balance = locked_src_acc.current_balance  # fresh instance -> live ledger balance
                if src_balance < self.amount:
                    raise ValidationError(f"عفواً، رصيد {self.source_name} الحالي ({src_balance:,.2f} ج.م) غير كافٍ لإكمال تحويل بمبلغ {self.amount:,.2f} ج.م.")


            # Delete any orphaned journal entry (never a reversal pair)
            from apps.tenant.services.journal_service import delete_orphan_entries, unique_reference
            delete_orphan_entries(self.transfer_number)

            if self.is_inter_branch:
                # Step 1 (sending branch): source -> cash in transit; the receiving
                # branch confirms receipt later (receive_transfer).
                from django.utils import timezone
                entry = self._post_entry(
                    f'{self.transfer_number}-SND',
                    f'إرسال تحويل {self.transfer_number} من {self.source_name} ({self.source_branch}) '
                    f'إلى {self.destination_name} ({self.destination_branch}) - نقدية بالطريق',
                    [(self.transit_account(), self.amount, Decimal('0')),
                     (src_acc, Decimal('0'), self.amount)],
                    date=self.date,
                )
                self.send_entry = entry
                self.sent_by = user
                self.sent_at = timezone.now()
                self.status = self.IN_TRANSIT
                self.save()
                return

            entry = JournalEntry.objects.create(
                date=self.date,
                reference=unique_reference(self.transfer_number),
                description=f"تحويل مالي ({self.source_name} ⬅️ {self.destination_name}) - {self.description or ''}",
                status=JournalEntry.POSTED
            )

            # Debit: Destination Account
            JournalItem.objects.create(entry=entry, account=dst_acc, debit=self.amount, credit=0)
            # Credit: Source Account
            JournalItem.objects.create(entry=entry, account=src_acc, debit=0, credit=self.amount)

            entry.post()

            self.status = self.POSTED
            self.journal_entry = entry
            self.save()

    def cancel_transfer(self, user=None):
        """
        Cancels the transfer. Its posted journal entry is kept and a reversal entry is
        posted against it (audit trail); ``journal_entry`` is cleared.
        """
        from django.db import transaction
        from django.core.exceptions import ValidationError
        from apps.tenant.services.journal_service import reverse_entry_if_active

        with transaction.atomic():
            locked = InternalTransfer.objects.select_for_update(of=('self',)).select_related('pos_session', 'journal_entry').get(pk=self.pk)
            if locked.status in (self.CANCELLED, self.REJECTED):
                raise ValidationError("التحويل ملغي بالفعل.")
            if locked.status in (self.POSTED, self.IN_TRANSIT):
                _ensure_pos_session_open(locked.pos_session, 'إلغاء')

            if locked.journal_entry_id:
                reverse_entry_if_active(
                    locked.journal_entry, reason=f'إلغاء التحويل {locked.transfer_number}', user=user
                )
            if locked.send_entry_id:  # inter-branch: also undo the "cash in transit" step
                reverse_entry_if_active(
                    locked.send_entry, reason=f'إلغاء إرسال التحويل {locked.transfer_number}', user=user
                )

            self.journal_entry = None
            self.status = self.CANCELLED
            self.save(update_fields=['status', 'journal_entry', 'updated_at'])


# ---------------------------------------------------------------------------
# Cheque Management (أوراق القبض وأوراق الدفع)
# ---------------------------------------------------------------------------

class ChequeBook(models.Model):
    """
    دفاتر الشيكات الخاصة بحسابات بنوك الشركة (لشيكات الدفع الصادرة).
    """
    bank_account = models.ForeignKey(
        'BankAccount', on_delete=models.CASCADE, related_name='cheque_books',
        verbose_name='حساب بنك الشركة'
    )
    book_number = models.CharField(max_length=50, verbose_name='رقم / اسم الدفتر')
    series_prefix = models.CharField(max_length=15, blank=True, verbose_name='بادئة الترقيم (إن وجدت)')
    start_number = models.PositiveIntegerField(verbose_name='رقم أول شيك')
    end_number = models.PositiveIntegerField(verbose_name='رقم آخر شيك')
    current_number = models.PositiveIntegerField(verbose_name='الرقم الحالي التالي')
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'دفتر شيكات'
        verbose_name_plural = 'دفاتر الشيكات'
        ordering = ['bank_account', 'book_number']

    def __str__(self):
        return f"{self.book_number} ({self.bank_account.name}) [{self.start_number} - {self.end_number}]"

    def get_next_number(self):
        """Returns next formatted cheque number or None if exhausted."""
        if self.current_number > self.end_number:
            return None
        prefix = self.series_prefix or ''
        return f"{prefix}{self.current_number}"

    def advance_number(self):
        """Increments current number if within range."""
        if self.current_number <= self.end_number:
            self.current_number += 1
            self.save(update_fields=['current_number'])


class ChequeType(models.TextChoices):
    RECEIVABLE = 'RECEIVABLE', 'شيك قبض (ورقة قبض من عميل)'
    PAYABLE = 'PAYABLE', 'شيك صرف (ورقة دفع لمورد)'


class ChequeStatus(models.TextChoices):
    # شيكات القبض (Receivable)
    UNDER_COLLECTION = 'UNDER_COLLECTION', 'تحت التحصيل'
    COLLECTED = 'COLLECTED', 'تم التحصيل'
    BOUNCED = 'BOUNCED', 'مرتد (مرفوض)'
    IN_PORTFOLIO = 'IN_PORTFOLIO', 'تحت التحصيل'  # للتوافقية السابقة

    # شيكات الصرف (Payable)
    ISSUED = 'ISSUED', 'صادر (تحت الصرف)'
    CLEARED = 'CLEARED', 'تم الصرف من البنك'
    CANCELLED = 'CANCELLED', 'ملغي'


class Cheque(models.Model):
    """
    نموذج الشيك التجاري (ورقة قبض أو ورقة دفع).
    يدير دورة الحياة المحاسبية الكاملة للشيك وفق المعايير المحاسبية عبر حساب وسيط.
    """
    cheque_type = models.CharField(
        max_length=20, choices=ChequeType.choices, verbose_name='نوع الشيك', db_index=True
    )
    cheque_number = models.CharField(max_length=50, verbose_name='رقم الشيك', db_index=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ (ج.م)')
    
    received_date = models.DateField(default=datetime.date.today, verbose_name='تاريخ الاستلام / التحرير')
    due_date = models.DateField(verbose_name='تاريخ الاستحقاق', db_index=True)
    clearance_date = models.DateField(null=True, blank=True, verbose_name='تاريخ التحصيل / الصرف الفعلي')
    
    partner = models.ForeignKey(
        'partners.Partner', on_delete=models.PROTECT, related_name='cheques',
        verbose_name='الشريك (العميل / المورد)'
    )
    payee_name = models.CharField(max_length=200, blank=True, verbose_name='المستفيد المكتوب بالشيك')
    
    # تفاصيل شيك الصرف (Payable)
    bank_account = models.ForeignKey(
        'BankAccount', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='issued_cheques', verbose_name='حساب بنك الشركة (المسحوب منه)'
    )
    cheque_book = models.ForeignKey(
        ChequeBook, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheques', verbose_name='دفتر الشيكات'
    )
    
    # تفاصيل شيك القبض (Receivable)
    drawer_bank_name = models.CharField(max_length=150, blank=True, verbose_name='بنك العميل المسحوب عليه')
    drawer_branch = models.CharField(max_length=100, blank=True, verbose_name='فرع بنك العميل')
    treasury = models.ForeignKey(
        'Treasury', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='held_cheques', verbose_name='خزينة / حافظة الحفظ'
    )
    collecting_bank = models.ForeignKey(
        'BankAccount', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='collecting_cheques', verbose_name='بنك الإيداع والتحصيل'
    )
    bank_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name='عمولة ومصاريف التحصيل البنكية'
    )
    endorsed_to_partner = models.ForeignKey(
        'partners.Partner', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='endorsed_cheques', verbose_name='المورد المظهر له الشيك'
    )
    bounce_reason = models.TextField(blank=True, verbose_name='سبب الارتداد / الرفض')
    
    status = models.CharField(
        max_length=25, choices=ChequeStatus.choices, default=ChequeStatus.UNDER_COLLECTION,
        verbose_name='حالة الشيك', db_index=True
    )
    
    # القيود المحاسبية المرتبطة بمراحل الشيك
    entry_receive_issue = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_receive_issue_entries', verbose_name='قيد الاستلام / التحرير'
    )
    entry_deposit = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_deposit_entries', verbose_name='قيد الإيداع للتحصيل'
    )
    entry_clearance = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_clearance_entries', verbose_name='قيد التحصيل / الصرف'
    )
    entry_bounce = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_bounce_entries', verbose_name='قيد الارتداد'
    )
    entry_endorse = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_endorse_entries', verbose_name='قيد التظهير'
    )

    attachment = models.ImageField(upload_to='cheques/', null=True, blank=True, verbose_name='صورة الشيك')
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='أنشئ بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'شيك'
        verbose_name_plural = 'الشيكات وأوراق القبض والدفع'
        ordering = ['-received_date', '-created_at']

    def __str__(self):
        return f"{self.get_cheque_type_display()} #{self.cheque_number} - {self.amount} ج.م ({self.partner.name})"

    # ── مراجع القيود (فريدة لكل شيك عبر الـ pk) ─────────────────
    def _lock_and_refresh(self):
        """Lock this cheque row (SELECT ... FOR UPDATE) and refresh its status/entry links."""
        if not self.pk:
            return
        locked = Cheque.objects.select_for_update(of=('self',)).get(pk=self.pk)
        for f in ('status', 'entry_receive_issue_id', 'entry_deposit_id', 'entry_clearance_id',
                  'entry_bounce_id', 'entry_endorse_id'):
            setattr(self, f, getattr(locked, f))

    def _linked_entry_ids(self):
        return {eid for eid in (self.entry_receive_issue_id, self.entry_deposit_id, self.entry_clearance_id,
                                self.entry_bounce_id, self.entry_endorse_id) if eid}

    def _stage_reference(self, stage, legacy_ref=None, linked_entry_id=None):
        """
        Journal reference for one lifecycle stage of THIS cheque: CHQ-<pk>-<STAGE>.
        The old format (CHK-REC-<cheque_number>) was shared by different partners' cheques
        carrying the same number, and the old code deleted entries by that bare reference.
        Entries are never deleted here; a stage that is already booked for this cheque raises.
        A legacy-format entry is only considered when it is linked to this same cheque.
        """
        already = ValidationError('تم إثبات القيد المحاسبي لهذه المرحلة على هذا الشيك مسبقاً.')
        if linked_entry_id:
            raise already
        ref = f"CHQ-{self.pk}-{stage}"
        if JournalEntry.objects.filter(reference=ref).exists():
            raise already
        if legacy_ref:
            legacy = JournalEntry.objects.filter(reference=legacy_ref).values_list('id', flat=True).first()
            if legacy and legacy in self._linked_entry_ids():
                raise already
        return ref

    # ── Helpers للحسابات المحاسبية ─────────────────────────────
    @staticmethod
    def get_notes_receivable_account():
        """1180 - أوراق قبض (حافظة الشيكات)"""
        from apps.tenant.accounting.account_mapping import mapped_account
        acc = mapped_account('1180') or Account.objects.filter(code='1180').first()
        if not acc:
            curr_assets = Account.objects.filter(code='1100').first()
            acc = Account.objects.create(code='1180', name='أوراق قبض (حافظة الشيكات)', account_type=Account.ASSET, parent=curr_assets, allow_reconcile=True)
        return acc

    @staticmethod
    def get_under_collection_account():
        """1185 - شيكات برسم التحصيل (الحساب الوسيط بالبنك)"""
        from apps.tenant.accounting.account_mapping import mapped_account
        acc = mapped_account('1185') or Account.objects.filter(code='1185').first()
        if not acc:
            curr_assets = Account.objects.filter(code='1100').first()
            acc = Account.objects.create(code='1185', name='شيكات برسم التحصيل', account_type=Account.ASSET, parent=curr_assets, allow_reconcile=True)
        return acc

    @staticmethod
    def get_notes_payable_account():
        """2170 - أوراق دفع (شيكات صادرة برسم السداد)"""
        from apps.tenant.accounting.account_mapping import mapped_account
        acc = mapped_account('2170') or Account.objects.filter(code='2170').first()
        if not acc:
            curr_liab = Account.objects.filter(code='2100').first()
            acc = Account.objects.create(code='2170', name='أوراق دفع (شيكات برسم السداد)', account_type=Account.LIABILITY, parent=curr_liab, allow_reconcile=True)
        return acc

    @staticmethod
    def get_bank_expense_account():
        """5305 - عمولات ماكينات الدفع والمحافظ والبنوك (تحت 5300 - مصاريف إدارية وعمولات بنكية)"""
        from apps.tenant.services.journal_service import _get_system_account, PAYMENT_FEES_CODE
        if not Account.objects.filter(code='5300').exists():
            exp = Account.objects.filter(code='5000').first()
            Account.objects.create(code='5300', name='مصاريف إدارية وعمولات بنكية', account_type=Account.EXPENSE, parent=exp)
        return _get_system_account(PAYMENT_FEES_CODE)

    def get_partner_account(self):
        """تحديد حساب الشريك المناسب (مدين للعملاء أو دائن للموردين أو الموحد)"""
        p = self.partner
        if self.cheque_type == ChequeType.RECEIVABLE:
            if p.receivable_account:
                return p.receivable_account
            if p.account:
                return p.account
            # افتراضي ذمم مدينة
            acc = Account.objects.filter(code='1130').first()
            return acc
        else: # PAYABLE
            if p.payable_account:
                return p.payable_account
            if p.account:
                return p.account
            # افتراضي ذمم دائنة
            acc = Account.objects.filter(code='2110').first()
            return acc

    def log_action(self, action, from_status, to_status, user=None, notes=''):
        ChequeActionLog.objects.create(
            cheque=self,
            action=action,
            from_status=from_status,
            to_status=to_status,
            user=user,
            notes=notes
        )

    # ── دورة حياة شيك القبض (RECEIVABLE) ────────────────────────
    def action_receive(self, user=None):
        """
        1. استلام شيك القبض وإثباته مباشرة في الحساب الوسيط (1180 - أوراق قبض / تحت التحصيل):
        من حـ/ أوراق قبض - وسيط (1180)
        إلى حـ/ العملاء - ذمم مدينة (1130 / حساب الشريك)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.RECEIVABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات القبض فقط.")

            notes_rec_acc = self.get_notes_receivable_account()
            partner_acc = self.get_partner_account()
            if not partner_acc:
                raise ValidationError("لا يوجد حساب محاسبي مرتبط بالشريك.")

            # إنشاء قيد الاستلام بالحساب الوسيط
            ref = self._stage_reference('REC', legacy_ref=f"CHK-REC-{self.cheque_number}", linked_entry_id=self.entry_receive_issue_id)

            entry = JournalEntry.objects.create(
                date=self.received_date,
                reference=ref,
                description=f"استلام شيك قبض رقم {self.cheque_number} من العميل {self.partner.name} (حساب وسيط) - استحقاق {self.due_date}",
                status=JournalEntry.POSTED
            )

            # مدين: أوراق قبض (الحساب الوسيط)
            JournalItem.objects.create(
                entry=entry, account=notes_rec_acc, debit=self.amount, credit=0,
                description=f"ورقة قبض شيك {self.cheque_number} - {self.partner.name}"
            )
            # دائن: العميل (تخفيض المديونية)
            JournalItem.objects.create(
                entry=entry, account=partner_acc, debit=0, credit=self.amount,
                description=f"سداد بشيك رقم {self.cheque_number} - استحقاق {self.due_date}"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.UNDER_COLLECTION
            self.entry_receive_issue = entry
            self.save()
            self.log_action("استلام الشيك وإيداعه بالحساب الوسيط (تحت التحصيل)", old_status, self.status, user=user)

    def action_confirm_collection(self, destination=None, clearance_date=None, bank_fee=Decimal('0.00'), user=None):
        """
        2. التحصيل الفعلي لشيك القبض (في البنك أو الخزينة) وإقفال الحساب الوسيط:
        من حـ/ البنك أو الخزينة (بالمبلغ الصافي)
        من حـ/ عمولات ماكينات الدفع والمحافظ والبنوك (5305) بمبلغ العمولة (إن وجدت)
        إلى حـ/ أوراق قبض - وسيط (1180) بإجمالي مبلغ الشيك
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.RECEIVABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات القبض فقط.")
            if self.status not in (ChequeStatus.UNDER_COLLECTION, ChequeStatus.IN_PORTFOLIO):
                raise ValidationError("لا يمكن تحصيل الشيك إلا وهو في حالة تحت التحصيل.")

            # تحديد حساب جهة التحصيل (بنك أو خزينة)
            dest_acc = None
            dest_name = ""
            if destination:
                if hasattr(destination, 'ensure_account'):
                    dest_acc = destination.ensure_account()
                elif hasattr(destination, 'account'):
                    dest_acc = destination.account
                dest_name = getattr(destination, 'name', str(destination))
                if isinstance(destination, BankAccount):
                    self.collecting_bank = destination
                elif isinstance(destination, Treasury):
                    self.treasury = destination
            elif self.collecting_bank:
                dest_acc = self.collecting_bank.ensure_account()
                dest_name = self.collecting_bank.name
            elif self.treasury:
                dest_acc = self.treasury.ensure_account()
                dest_name = self.treasury.name

            if not dest_acc:
                raise ValidationError("يجب تحديد البنك أو الخزينة المراد تحصيل وإيداع الشيك بها.")

            clr_date = clearance_date or datetime.date.today()
            notes_rec_acc = self.get_notes_receivable_account()
            fee = Decimal(str(bank_fee or 0))
            net_amount = self.amount - fee

            if net_amount < 0:
                raise ValidationError("عمولة التحصيل لا يمكن أن تكون أكبر من قيمة الشيك.")

            ref = self._stage_reference('CLR', legacy_ref=f"CHK-CLR-{self.cheque_number}", linked_entry_id=self.entry_clearance_id)

            entry = JournalEntry.objects.create(
                date=clr_date,
                reference=ref,
                description=f"تحصيل شيك قبض رقم {self.cheque_number} وإيداعه في {dest_name}",
                status=JournalEntry.POSTED
            )

            # مدين: جهة التحصيل (البنك أو الخزينة) بالصافي
            JournalItem.objects.create(
                entry=entry, account=dest_acc, debit=net_amount, credit=0,
                description=f"إيداع صافي تحصيل شيك {self.cheque_number} في {dest_name}"
            )
            # مدين: مصاريف وعمولات بنكية (إن وجدت)
            if fee > 0:
                fee_acc = self.get_bank_expense_account()
                JournalItem.objects.create(
                    entry=entry, account=fee_acc, debit=fee, credit=0,
                    description=f"عمولة ومصاريف تحصيل شيك {self.cheque_number}"
                )
            # دائن: أوراق قبض (الحساب الوسيط 1180)
            JournalItem.objects.create(
                entry=entry, account=notes_rec_acc, debit=0, credit=self.amount,
                description=f"إقفال ورقة قبض شيك {self.cheque_number} بعد التحصيل"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.COLLECTED
            self.clearance_date = clr_date
            self.bank_fee = fee
            self.entry_clearance = entry
            self.save()
            self.log_action(f"تحصيل الشيك في {dest_name} (صافي: {net_amount:,.2f} ج.م)", old_status, self.status, user=user)

    def action_bounce(self, bounce_reason, bounce_date=None, user=None):
        """
        3. ارتداد / رفض الشيك (إعادة فتح مديونية العميل وإلغاء الحساب الوسيط):
        من حـ/ العميل (1130 / حساب الشريك)
        إلى حـ/ أوراق قبض - وسيط (1180)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.RECEIVABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات القبض فقط.")
            if self.status not in (ChequeStatus.UNDER_COLLECTION, ChequeStatus.IN_PORTFOLIO):
                raise ValidationError("لا يمكن إثبات ارتداد الشيك إلا وهو في حالة تحت التحصيل.")

            bnc_date = bounce_date or datetime.date.today()
            partner_acc = self.get_partner_account()
            notes_rec_acc = self.get_notes_receivable_account()

            ref = self._stage_reference('BNC', legacy_ref=f"CHK-BNC-{self.cheque_number}", linked_entry_id=self.entry_bounce_id)

            entry = JournalEntry.objects.create(
                date=bnc_date,
                reference=ref,
                description=f"ارتداد ورفض شيك قبض رقم {self.cheque_number} للعميل {self.partner.name} - السبب: {bounce_reason}",
                status=JournalEntry.POSTED
            )

            # مدين: العميل (إعادة مديونيته)
            JournalItem.objects.create(
                entry=entry, account=partner_acc, debit=self.amount, credit=0,
                description=f"إعادة مديونية شيك مرتد رقم {self.cheque_number} - {bounce_reason}"
            )
            # دائن: أوراق قبض (الحساب الوسيط)
            JournalItem.objects.create(
                entry=entry, account=notes_rec_acc, debit=0, credit=self.amount,
                description=f"إلغاء ورقة قبض شيك مرتد {self.cheque_number}"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.BOUNCED
            self.bounce_reason = bounce_reason
            self.entry_bounce = entry
            self.save()
            self.log_action(f"ارتداد الشيك: {bounce_reason}", old_status, self.status, user=user)

    # ── دورة حياة شيك الصرف (PAYABLE) ───────────────────────────
    def action_issue(self, user=None):
        """
        1. تحرير وتسليم شيك دفع لمورد:
        من حـ/ الموردين - ذمم دائنة (2110 / حساب الشريك)
        إلى حـ/ أوراق دفع (2170)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.PAYABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات الصرف فقط.")
            if not self.bank_account:
                raise ValidationError("يجب تحديد حساب بنك الشركة المسحوب منه الشيك.")

            supplier_acc = self.get_partner_account()
            notes_pay_acc = self.get_notes_payable_account()

            ref = self._stage_reference('PAY', legacy_ref=f"CHK-PAY-{self.cheque_number}", linked_entry_id=self.entry_receive_issue_id)

            entry = JournalEntry.objects.create(
                date=self.received_date,
                reference=ref,
                description=f"تحرير شيك دفع رقم {self.cheque_number} للمورد {self.partner.name} مسحوب على {self.bank_account.name} - استحقاق {self.due_date}",
                status=JournalEntry.POSTED
            )

            # مدين: المورد (تخفيض التزامه)
            JournalItem.objects.create(
                entry=entry, account=supplier_acc, debit=self.amount, credit=0,
                description=f"سداد بشيك دفع رقم {self.cheque_number} - مسحوب على {self.bank_account.name}"
            )
            # دائن: أوراق دفع
            JournalItem.objects.create(
                entry=entry, account=notes_pay_acc, debit=0, credit=self.amount,
                description=f"ورقة دفع شيك رقم {self.cheque_number} - استحقاق {self.due_date}"
            )
            entry.post()

            # تقدم رقم دفتر الشيكات إذا كان مسجلاً بالدفتر
            if self.cheque_book:
                self.cheque_book.advance_number()

            old_status = self.status
            self.status = ChequeStatus.ISSUED
            self.entry_receive_issue = entry
            self.save()
            self.log_action(f"تحرير وتسليم الشيك للمورد {self.partner.name}", old_status, self.status, user=user)

    def action_clear_payable(self, clearance_date=None, user=None):
        """
        2. صرف شيك الدفع وخصمه فعلياً من حساب بنك الشركة:
        من حـ/ أوراق دفع (2170)
        إلى حـ/ بنك الشركة (1120)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.PAYABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات الصرف فقط.")
            if self.status != ChequeStatus.ISSUED:
                raise ValidationError("لا يمكن صرف الشيك إلا إذا كان في حالة محرر ومُسلّم.")
            if not self.bank_account:
                raise ValidationError("لم يتم تحديد حساب بنك الشركة المسحوب منه.")

            clr_date = clearance_date or datetime.date.today()
            notes_pay_acc = self.get_notes_payable_account()
            bank_acc = self.bank_account.ensure_account()

            ref = self._stage_reference('CLRPAY', legacy_ref=f"CHK-CLR-PAY-{self.cheque_number}", linked_entry_id=self.entry_clearance_id)

            entry = JournalEntry.objects.create(
                date=clr_date,
                reference=ref,
                description=f"صرف شيك دفع رقم {self.cheque_number} وخصمه من حساب {self.bank_account.name} (المستفيد: {self.partner.name})",
                status=JournalEntry.POSTED
            )

            # مدين: أوراق دفع (إقفال الالتزام)
            JournalItem.objects.create(
                entry=entry, account=notes_pay_acc, debit=self.amount, credit=0,
                description=f"صرف ورقة دفع شيك {self.cheque_number}"
            )
            # دائن: بنك الشركة (الخصم الفعلي من الرصيد البنكي)
            JournalItem.objects.create(
                entry=entry, account=bank_acc, debit=0, credit=self.amount,
                description=f"خصم قيمة شيك {self.cheque_number} من كشف حساب {self.bank_account.name}"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.CLEARED
            self.clearance_date = clr_date
            self.entry_clearance = entry
            self.save()
            self.log_action(f"صرف الشيك من حساب {self.bank_account.name}", old_status, self.status, user=user)

    def action_cancel_payable(self, cancel_reason, user=None):
        """
        3. إلغاء شيك دفع وإعادة مديونية المورد:
        من حـ/ أوراق دفع (2170)
        إلى حـ/ الموردين (2110 / حساب الشريك)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.PAYABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات الصرف فقط.")
            if self.status != ChequeStatus.ISSUED:
                raise ValidationError("لا يمكن إلغاء الشيك إلا إذا كان في حالة محرر ومُسلّم.")

            notes_pay_acc = self.get_notes_payable_account()
            supplier_acc = self.get_partner_account()

            ref = self._stage_reference('CNCPAY', legacy_ref=f"CHK-CNC-PAY-{self.cheque_number}", linked_entry_id=None)

            entry = JournalEntry.objects.create(
                date=datetime.date.today(),
                reference=ref,
                description=f"إلغاء شيك دفع رقم {self.cheque_number} للمورد {self.partner.name} - السبب: {cancel_reason}",
                status=JournalEntry.POSTED
            )

            # مدين: أوراق دفع
            JournalItem.objects.create(
                entry=entry, account=notes_pay_acc, debit=self.amount, credit=0,
                description=f"إلغاء ورقة دفع شيك {self.cheque_number}"
            )
            # دائن: المورد (إعادة استحقاقه)
            JournalItem.objects.create(
                entry=entry, account=supplier_acc, debit=0, credit=self.amount,
                description=f"إعادة مديونية لإلغاء شيك {self.cheque_number} - {cancel_reason}"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.CANCELLED
            self.notes = f"{self.notes}\n[سبب الإلغاء]: {cancel_reason}".strip()
            self.save()
            self.log_action(f"إلغاء الشيك: {cancel_reason}", old_status, self.status, user=user)


class ChequeActionLog(models.Model):
    """سجل تدقيق ومتابعة جميع الحركات التي تمت على الشيك."""
    cheque = models.ForeignKey(
        Cheque, on_delete=models.CASCADE, related_name='action_logs', verbose_name='الشيك'
    )
    action = models.CharField(max_length=100, verbose_name='نوع الإجراء')
    from_status = models.CharField(max_length=30, blank=True, choices=ChequeStatus.choices, verbose_name='من حالة')
    to_status = models.CharField(max_length=30, choices=ChequeStatus.choices, verbose_name='إلى حالة')
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='المستخدم'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='وقت الإجراء')
    notes = models.TextField(blank=True, verbose_name='ملاحظات')

    class Meta:
        verbose_name = 'سجل حركة شيك'
        verbose_name_plural = 'سجل حركات الشيكات'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.cheque.cheque_number} - {self.action} ({self.created_at.strftime('%Y-%m-%d %H:%M')})"

    @classmethod
    def get_status_label(cls, status_val):
        if not status_val or status_val == '-':
            return '-'
        status_map = {
            'UNDER_COLLECTION': 'تحت التحصيل',
            'IN_PORTFOLIO': 'تحت التحصيل',
            'COLLECTED': 'تم التحصيل',
            'BOUNCED': 'مرتد (مرفوض)',
            'ISSUED': 'صادر (تحت الصرف)',
            'CLEARED': 'تم الصرف من البنك',
            'CANCELLED': 'ملغي',
            'ENDORSED': 'مظهر لمورد',
            'DEPOSITED': 'مودع للتحصيل',
        }
        for k, v in ChequeStatus.choices:
            if k not in status_map:
                status_map[k] = v
        return status_map.get(status_val, status_val)

    @property
    def from_status_display(self):
        return self.get_status_label(self.from_status)

    @property
    def to_status_display(self):
        return self.get_status_label(self.to_status)

    def get_from_status_display(self):
        return self.from_status_display

    def get_to_status_display(self):
        return self.to_status_display


# ---------------------------------------------------------------------------
# Fixed Assets & Depreciation (الأصول الثابتة والإهلاك)
# ---------------------------------------------------------------------------

class FixedAsset(models.Model):
    """
    Fixed Asset card representing machinery, vehicles, POS equipment, fixtures, etc.
    Maintains cost, straight-line depreciation schedule, book value, and lifecycle.
    """
    class AssetCategory(models.TextChoices):
        EQUIPMENT = 'EQUIPMENT', 'أجهزة ومعدات ومبردات'
        VEHICLE = 'VEHICLE', 'سيارات ومركبات توصيل'
        ELECTRONICS = 'ELECTRONICS', 'أجهزة إلكترونية ونقاط بيع وكاشير'
        FURNITURE = 'FURNITURE', 'أثاث وتجهيزات وديكور'
        BUILDING = 'BUILDING', 'مباني وتحسينات وتجهيزات مقرات'
        OTHER = 'OTHER', 'أصول أخرى'

    class AssetStatus(models.TextChoices):
        ACTIVE = 'ACTIVE', 'نشط ويعمل'
        FULLY_DEPRECIATED = 'FULLY_DEPRECIATED', 'مستهلك بالكامل دفترياً'
        DISPOSED = 'DISPOSED', 'مستبعد / تم بيعه أو تخريده'

    # Identification
    code = models.CharField(max_length=50, unique=True, verbose_name='كود الأصل')
    name = models.CharField(max_length=200, verbose_name='اسم الأصل')
    category = models.CharField(
        max_length=30, choices=AssetCategory.choices, default=AssetCategory.EQUIPMENT, verbose_name='تصنيف الأصل'
    )
    branch = models.ForeignKey('core.Branch', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='الفرع')
    location = models.CharField(max_length=150, blank=True, verbose_name='مكان التواجد / القسم')

    # Purchase Details
    purchase_date = models.DateField(verbose_name='تاريخ الشراء / بدء الاستخدام')
    purchase_cost = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='تكلفة الشراء الأصلية')
    salvage_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name='القيمة التخريدية (الخردة)'
    )

    # Opening Balance Handling (for assets purchased before system launch)
    is_opening_balance = models.BooleanField(
        default=False, verbose_name='أصل قائم (رصيد افتتاحي سابق)'
    )
    opening_accumulated_depreciation = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name='مجمع الإهلاك السابق (رصيد افتتاحي)'
    )

    # Depreciation Policy (Straight-Line)
    useful_life_years = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('5.00'), verbose_name='العمر الافتراضي (بالسنوات)'
    )
    depreciation_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('20.00'), verbose_name='نسبة الإهلاك السنوية %'
    )

    # Accounts
    asset_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='fixed_assets',
        limit_choices_to={'account_type': Account.ASSET},
        verbose_name='حساب الأصل'
    )
    accumulated_depreciation_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='fixed_assets_acc_dep',
        verbose_name='حساب مجمع الإهلاك'
    )
    depreciation_expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='fixed_assets_dep_exp',
        limit_choices_to={'account_type': Account.EXPENSE},
        verbose_name='حساب مصروف الإهلاك'
    )

    # Acquisition Journal Entry
    purchase_entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='fixed_asset_purchase_entries', verbose_name='قيد الشراء / الإثبات'
    )

    # Lifecycle Status & Disposal
    status = models.CharField(
        max_length=20, choices=AssetStatus.choices, default=AssetStatus.ACTIVE, verbose_name='الحالة الدفترية'
    )
    disposal_date = models.DateField(null=True, blank=True, verbose_name='تاريخ الاستبعاد / البيع')
    disposal_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name='سعر البيع / الاستبعاد'
    )
    disposal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='fixed_asset_disposal_entries', verbose_name='قيد الاستبعاد'
    )
    disposal_notes = models.TextField(blank=True, verbose_name='ملاحظات الاستبعاد')

    notes = models.TextField(blank=True, verbose_name='ملاحظات عامة')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'أصل ثابت'
        verbose_name_plural = 'الأصول الثابتة'
        ordering = ['-purchase_date', '-id']

    def __str__(self):
        return f"{self.code} - {self.name} ({self.get_category_display()})"

    def save(self, *args, **kwargs):
        if not self.code:
            # race-safe; the surrounding view (FixedAssetCreateView) runs inside transaction.atomic()
            self.code = _next_doc_number('fixed_asset', FixedAsset, 'code', "AST-", width=4)

        # Automatic sync between useful_life_years and depreciation_rate
        if self.useful_life_years and self.useful_life_years > Decimal('0'):
            self.depreciation_rate = (Decimal('100.0') / self.useful_life_years).quantize(Decimal('0.01'))
        elif self.depreciation_rate and self.depreciation_rate > Decimal('0'):
            self.useful_life_years = (Decimal('100.0') / self.depreciation_rate).quantize(Decimal('0.01'))

        # Auto-update status if fully depreciated and still active
        if self.pk and self.status == self.AssetStatus.ACTIVE:
            if self.total_accumulated_depreciation >= self.depreciable_base and self.depreciable_base > Decimal('0'):
                self.status = self.AssetStatus.FULLY_DEPRECIATED

        super().save(*args, **kwargs)

    @property
    def depreciable_base(self):
        """القيمة القابلة للإهلاك = تكلفة الأصل - قيمة الخردة"""
        cost = self.purchase_cost or Decimal('0.00')
        salvage = self.salvage_value or Decimal('0.00')
        return max(cost - salvage, Decimal('0.00'))

    @property
    def monthly_depreciation_amount(self):
        """قسط الإهلاك الشهري بطريقة القسط الثابت"""
        if self.depreciable_base <= Decimal('0.00'):
            return Decimal('0.00')
        if self.useful_life_years and self.useful_life_years > Decimal('0'):
            total_months = self.useful_life_years * Decimal('12.0')
            return (self.depreciable_base / total_months).quantize(Decimal('0.01'))
        if self.depreciation_rate and self.depreciation_rate > Decimal('0'):
            annual = self.depreciable_base * (self.depreciation_rate / Decimal('100.0'))
            return (annual / Decimal('12.0')).quantize(Decimal('0.01'))
        return Decimal('0.00')

    @staticmethod
    def with_totals(queryset):
        """Assets with their logged depreciation summed by the database in the same query
        (list pages and reports: no query per asset)."""
        from django.db.models import DecimalField, Value
        from django.db.models.functions import Coalesce
        return queryset.annotate(_logged_dep=Coalesce(Sum('depreciation_logs__amount'), Value(Decimal('0')),
                                                       output_field=DecimalField(max_digits=18, decimal_places=2)))

    @property
    def total_accumulated_depreciation(self):
        """إجمالي مجمع الإهلاك حتى اليوم (افتتاحي + أقساط مسجلة)"""
        opening = self.opening_accumulated_depreciation or Decimal('0.00')
        logged = Decimal('0.00')
        if '_logged_dep' in self.__dict__:          # annotated by FixedAsset.with_totals()
            logged = self._logged_dep or Decimal('0.00')
        elif self.pk:
            logged = self.depreciation_logs.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        total = opening + logged
        return min(total, self.depreciable_base)

    @property
    def current_book_value(self):
        """صافي القيمة الدفترية = التكلفة - مجمع الإهلاك"""
        cost = self.purchase_cost or Decimal('0.00')
        salvage = self.salvage_value or Decimal('0.00')
        return max(cost - self.total_accumulated_depreciation, salvage)

    @property
    def depreciation_progress_percent(self):
        """نسبة استهلاك الأصل حتى اليوم"""
        if self.depreciable_base <= Decimal('0.00'):
            return 100.0
        pct = (self.total_accumulated_depreciation / self.depreciable_base) * Decimal('100.0')
        return float(min(round(pct, 1), Decimal('100.0')))

    @property
    def is_depreciable(self):
        """هل الأصل مؤهل لحساب إهلاك عليه؟"""
        return (
            self.status == self.AssetStatus.ACTIVE and
            self.total_accumulated_depreciation < self.depreciable_base and
            self.current_book_value > (self.salvage_value or Decimal('0.00'))
        )


class AssetDepreciationLog(models.Model):
    """
    Periodic depreciation run entry for an asset, linking to the double-entry JournalEntry.
    """
    asset = models.ForeignKey(
        FixedAsset, on_delete=models.CASCADE, related_name='depreciation_logs', verbose_name='الأصل'
    )
    year = models.IntegerField(verbose_name='السنة')
    month = models.IntegerField(verbose_name='الشهر')
    date = models.DateField(verbose_name='تاريخ احتساب الإهلاك')
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='قسط الإهلاك')
    book_value_after = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='القيمة الدفترية بعد الإهلاك')
    entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='fixed_asset_depreciation_entries', verbose_name='قيد اليومية'
    )
    notes = models.CharField(max_length=255, blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='وقت الاحتساب')
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='المستخدم'
    )

    class Meta:
        verbose_name = 'سجل إهلاك أصل'
        verbose_name_plural = 'سجل إهلاكات الأصول'
        unique_together = ('asset', 'year', 'month')
        ordering = ['-year', '-month', '-id']

    def __str__(self):
        return f"{self.asset.name} - {self.year}/{self.month:02d} ({self.amount})"


# ---------------------------------------------------------------------------
# Loans & Financing (القروض والتسهيلات البنكية وجداول الأقساط)
# ---------------------------------------------------------------------------

class Loan(models.Model):
    """
    Bank loans, Islamic financing, overdraft facilities, or partner loans.
    Tracks principal, interest, repayment installments schedule, and ledger balances.
    """
    class LoanType(models.TextChoices):
        BANK_LOAN = 'BANK_LOAN', 'قرض بنكي تجاري'
        ISLAMIC = 'ISLAMIC', 'تمويل / مرابحة إسلامية'
        PARTNER_LOAN = 'PARTNER_LOAN', 'تمويل شخصي / قرض شريك (بدون فوائد)'
        CREDIT_FACILITY = 'CREDIT_FACILITY', 'تسهيل ائتماني / جاري مدين'

    class LoanStatus(models.TextChoices):
        ACTIVE = 'ACTIVE', 'قائم وجاري السداد'
        FULLY_PAID = 'FULLY_PAID', 'مسدد بالكامل'
        DEFAULTED = 'DEFAULTED', 'متعثر'

    # Identification
    code = models.CharField(max_length=50, unique=True, verbose_name='رقم/كود القرض')
    name = models.CharField(max_length=200, verbose_name='اسم/بيان القرض')
    loan_type = models.CharField(
        max_length=30, choices=LoanType.choices, default=LoanType.BANK_LOAN, verbose_name='نوع القرض والتمويل'
    )
    lender_name = models.CharField(max_length=150, verbose_name='جهة التمويل / اسم البنك أو المقرض')
    contract_number = models.CharField(max_length=100, blank=True, verbose_name='رقم العقد / المعاملة')
    branch = models.ForeignKey('core.Branch', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='الفرع')

    # Financial details
    principal_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='أصل مبلغ القرض المقترض')
    total_interest_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name='إجمالي الفوائد / أرباح التمويل'
    )
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name='إجمالي التزام القرض (أصل + فوائد)'
    )

    start_date = models.DateField(verbose_name='تاريخ بدء / استلام القرض')
    installments_count = models.PositiveIntegerField(default=12, verbose_name='عدد الأقساط')
    first_due_date = models.DateField(verbose_name='تاريخ استحقاق أول قسط')

    # Accounts
    loan_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='loans',
        limit_choices_to={'account_type': Account.LIABILITY},
        verbose_name='حساب التزام القرض (خصوم)'
    )
    interest_expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='loans_interest_exp',
        limit_choices_to={'account_type': Account.EXPENSE},
        verbose_name='حساب مصروف الفوائد (مصروفات)'
    )

    # Initial receipt journal entry
    disbursement_entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='loan_disbursement_entries', verbose_name='قيد استلام وصرف القرض'
    )

    status = models.CharField(
        max_length=20, choices=LoanStatus.choices, default=LoanStatus.ACTIVE, verbose_name='حالة القرض'
    )
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'قرض / تمويل'
        verbose_name_plural = 'القروض والتسهيلات'
        ordering = ['-start_date', '-id']

    def __str__(self):
        return f"{self.code} - {self.name} ({self.lender_name})"

    def save(self, *args, **kwargs):
        if not self.code:
            # race-safe; the surrounding view (LoanCreateView) runs inside transaction.atomic()
            self.code = _next_doc_number('loan', Loan, 'code', "LOAN-", width=4)

        # Automatic calculation of total commitment
        principal = self.principal_amount or Decimal('0.00')
        interest = self.total_interest_amount or Decimal('0.00')
        self.total_amount = principal + interest

        # Auto-update status if all installments are paid
        if self.pk and self.status == self.LoanStatus.ACTIVE:
            unpaid = self.installments.filter(is_paid=False).exists()
            if not unpaid and self.installments.exists():
                self.status = self.LoanStatus.FULLY_PAID

        super().save(*args, **kwargs)

    @property
    def interest_rate(self):
        """معدل الفائدة التقديري السنوي"""
        if not self.principal_amount or self.principal_amount <= Decimal('0.00'):
            return Decimal('0.00')
        years = Decimal(self.installments_count or 12) / Decimal('12.0')
        if years <= Decimal('0.00'):
            years = Decimal('1.0')
        rate = (self.total_interest_amount / (self.principal_amount * years)) * Decimal('100.0')
        return round(rate, 2)

    @staticmethod
    def with_totals(queryset):
        """Loans with paid principal / interest and overdue installments computed by the
        database in the same query (list pages and reports: no queries per loan)."""
        from django.db.models import Count, DecimalField, Q, Value
        from django.db.models.functions import Coalesce
        money = DecimalField(max_digits=18, decimal_places=2)
        paid = Q(installments__is_paid=True)
        return queryset.annotate(
            _paid_principal=Coalesce(Sum('installments__principal_amount', filter=paid), Value(Decimal('0')),
                                     output_field=money),
            _paid_interest=Coalesce(Sum('installments__interest_amount', filter=paid), Value(Decimal('0')),
                                    output_field=money),
            _overdue_count=Count('installments', filter=Q(installments__is_paid=False,
                                                          installments__due_date__lt=datetime.date.today())),
        )

    @property
    def paid_principal(self):
        """إجمالي أصل القرض المسدد حتى اليوم"""
        if '_paid_principal' in self.__dict__:
            return self._paid_principal
        if not self.pk:
            return Decimal('0.00')
        return self.installments.filter(is_paid=True).aggregate(s=Sum('principal_amount'))['s'] or Decimal('0.00')

    @property
    def paid_interest(self):
        """إجمالي الفوائد المسددة حتى اليوم"""
        if '_paid_interest' in self.__dict__:
            return self._paid_interest
        if not self.pk:
            return Decimal('0.00')
        return self.installments.filter(is_paid=True).aggregate(s=Sum('interest_amount'))['s'] or Decimal('0.00')

    @property
    def total_paid(self):
        """إجمالي المبالغ المسددة (أصل + فوائد)"""
        return self.paid_principal + self.paid_interest

    @property
    def remaining_principal(self):
        """المتبقي من أصل القرض (الالتزام الحقيقي بالميزانية)"""
        return max(self.principal_amount - self.paid_principal, Decimal('0.00'))

    @property
    def remaining_total(self):
        """المتبقي الكلي من القرض شامل الفوائد القادمة"""
        return max(self.total_amount - self.total_paid, Decimal('0.00'))

    @property
    def repayment_progress_percent(self):
        """نسبة السداد المكتملة"""
        if not self.total_amount or self.total_amount <= Decimal('0.00'):
            return 100.0
        pct = (self.total_paid / self.total_amount) * Decimal('100.0')
        return float(min(round(pct, 1), Decimal('100.0')))

    @property
    def overdue_count(self):
        """عدد الأقساط المتأخرة"""
        if '_overdue_count' in self.__dict__:
            return self._overdue_count
        if not self.pk:
            return 0
        return self.installments.filter(is_paid=False, due_date__lt=datetime.date.today()).count()

    @property
    def next_pending_installment(self):
        """القسط القادم المستحق"""
        if not self.pk:
            return None
        return self.installments.filter(is_paid=False).order_by('due_date').first()

    def generate_installments_schedule(self):
        """توليد جدول الأقساط الشهرية تلقائياً"""
        if not self.installments_count or self.installments_count <= 0:
            return

        # Delete any unpaid installments before regenerating
        self.installments.filter(is_paid=False).delete()

        n = self.installments_count
        base_p = (self.principal_amount / Decimal(n)).quantize(Decimal('0.01'))
        base_int = (self.total_interest_amount / Decimal(n)).quantize(Decimal('0.01'))

        current_date = self.first_due_date
        accumulated_p = Decimal('0.00')
        accumulated_int = Decimal('0.00')

        for i in range(1, n + 1):
            if i == n:
                # Adjust last installment for pennies rounding difference
                p_amt = self.principal_amount - accumulated_p
                int_amt = self.total_interest_amount - accumulated_int
            else:
                p_amt = base_p
                int_amt = base_int
                accumulated_p += p_amt
                accumulated_int += int_amt

            tot_amt = p_amt + int_amt

            LoanInstallment.objects.create(
                loan=self,
                installment_number=i,
                due_date=current_date,
                principal_amount=p_amt,
                interest_amount=int_amt,
                total_amount=tot_amt
            )

            # Advance by 1 month
            y = current_date.year
            m = current_date.month + 1
            if m > 12:
                y += 1
                m = 1
            d = min(current_date.day, 28)
            current_date = datetime.date(y, m, d)


class LoanInstallment(models.Model):
    """
    Individual scheduled installment for a loan.
    """
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='installments', verbose_name='القرض')
    installment_number = models.PositiveIntegerField(verbose_name='رقم القسط')
    due_date = models.DateField(verbose_name='تاريخ الاستحقاق')

    principal_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='أصل القرض في القسط')
    interest_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name='فائدة القسط')
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='إجمالي مبلغ القسط')

    # Payment details
    is_paid = models.BooleanField(default=False, verbose_name='تم السداد')
    payment_date = models.DateField(null=True, blank=True, verbose_name='تاريخ السداد الفعلي')
    payment_entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='loan_installment_entries', verbose_name='قيد سداد القسط'
    )
    paid_treasury = models.ForeignKey(
        Treasury, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='الخزينة المسدد منها'
    )
    paid_bank = models.ForeignKey(
        BankAccount, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='البنك المسدد منه'
    )
    payment_notes = models.CharField(max_length=255, blank=True, verbose_name='ملاحظات السداد')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'قسط قرض'
        verbose_name_plural = 'أقساط القروض'
        ordering = ['due_date', 'installment_number']
        unique_together = ('loan', 'installment_number')

    def __str__(self):
        return f"{self.loan.name} - قسط #{self.installment_number} ({self.total_amount})"

    @property
    def is_overdue(self):
        return not self.is_paid and self.due_date < datetime.date.today()





class Custody(models.Model):
    """عهدة نقدية: مبلغ بيتصرف لموظف يشتري بيه حاجات للمحل، وبعدين بيتسوّى بالمصروفات
    والباقي يرجع الخزينة (أو يتحول سلفة تتخصم من مرتبه).

    صرف:   مدين 1135 ذمم الموظفين والعهد / دائن الخزينة
    تسوية: مدين حسابات المصروفات + مدين الخزينة (المرتجع) / دائن 1135
           (صرف أكتر من العهدة: الفرق دائن الخزينة — بيترد للموظف)"""
    OPEN = 'OPEN'
    SETTLED = 'SETTLED'
    CANCELLED = 'CANCELLED'
    STATUS_CHOICES = ((OPEN, 'مفتوحة'), (SETTLED, 'مسوّاة'), (CANCELLED, 'ملغاة'))

    number = models.CharField(max_length=30, unique=True, editable=False, verbose_name='رقم العهدة')
    date = models.DateField(default=datetime.date.today, verbose_name='تاريخ الصرف')
    employee = models.ForeignKey('core.Employee', on_delete=models.PROTECT, related_name='custodies', verbose_name='الموظف')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ')
    purpose = models.CharField(max_length=255, verbose_name='الغرض')
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, related_name='+', verbose_name='خزينة الصرف')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN, db_index=True, verbose_name='الحالة')
    payout_entry = models.ForeignKey(JournalEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                     verbose_name='قيد الصرف')
    # settlement
    settled_at = models.DateField(null=True, blank=True, verbose_name='تاريخ التسوية')
    returned_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='المرتجع للخزينة')
    return_treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                        verbose_name='خزينة المرتجع')
    settlement_entry = models.ForeignKey(JournalEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                         verbose_name='قيد التسوية')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name='بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'عهدة نقدية'
        verbose_name_plural = 'العهد النقدية'
        ordering = ['-date', '-id']

    def __str__(self):
        return f'{self.number} — {self.amount}'

    def save(self, *args, **kwargs):
        if not self.number:
            from apps.tenant.core.sequences import max_numeric_suffix, next_sequence
            prefix = f'CUS-{(self.date or datetime.date.today()).year}-'
            with transaction.atomic():
                n = next_sequence(f'custody:{prefix}', seed=lambda: max_numeric_suffix(Custody.objects.all(), 'number', prefix))
                self.number = f'{prefix}{n:05d}'
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)


class CustodyExpense(models.Model):
    """بند مصروف في تسوية عهدة."""
    custody = models.ForeignKey(Custody, on_delete=models.CASCADE, related_name='expenses', verbose_name='العهدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='+', verbose_name='حساب المصروف')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ')
    description = models.CharField(max_length=255, blank=True, default='', verbose_name='البيان')

    class Meta:
        verbose_name = 'مصروف عهدة'
        verbose_name_plural = 'مصروفات العهد'


class VoucherAllocation(models.Model):
    """ربط سند قبض / صرف بفاتورة آجلة: الجزء ده من السند سداد للفاتورة دي.
    (اللي مش مربوط بيتوزع تلقائياً على الأقدم أولاً — accounting.allocation_services)."""
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name='allocations', verbose_name='السند')
    invoice = models.ForeignKey('invoicing.Invoice', on_delete=models.PROTECT, related_name='voucher_allocations',
                                verbose_name='الفاتورة')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ')

    class Meta:
        verbose_name = 'ربط سند بفاتورة'
        verbose_name_plural = 'ربط السندات بالفواتير'
        unique_together = ('voucher', 'invoice')


class BankReconciliation(models.Model):
    """تسوية بنكية: مطابقة كشف البنك مع حركات الحساب في الدفاتر حتى تاريخ الكشف.
    الحركات اللي اتعلّم عليها في تسوية منتهية مبتظهرش تاني (accounting.bank_reconciliation)."""
    DRAFT = 'DRAFT'
    DONE = 'DONE'
    STATUS_CHOICES = ((DRAFT, 'مسودة'), (DONE, 'منتهية'))

    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='reconciliations',
                                     verbose_name='الحساب البنكي')
    statement_date = models.DateField(verbose_name='تاريخ كشف البنك')
    statement_balance = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='الرصيد في كشف البنك')
    book_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='الرصيد في الدفاتر')
    cleared_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'),
                                          verbose_name='رصيد الحركات المطابقة')
    difference = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='الفرق')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة')
    notes = models.CharField(max_length=255, blank=True, default='', verbose_name='ملاحظات')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)
    done_at = models.DateTimeField(null=True, blank=True, verbose_name='وقت الإنهاء')

    class Meta:
        verbose_name = 'تسوية بنكية'
        verbose_name_plural = 'التسويات البنكية'
        ordering = ['-statement_date', '-id']

    def __str__(self):
        return f'تسوية {self.bank_account} — {self.statement_date}'


class BankReconciliationItem(models.Model):
    """حركة دفاتر اتعلّم عليها إنها ظهرت في كشف البنك."""
    reconciliation = models.ForeignKey(BankReconciliation, on_delete=models.CASCADE, related_name='items',
                                       verbose_name='التسوية')
    journal_item = models.ForeignKey(JournalItem, on_delete=models.PROTECT, related_name='bank_clearings',
                                     verbose_name='الحركة')

    class Meta:
        verbose_name = 'حركة مطابقة'
        verbose_name_plural = 'حركات مطابقة'
        unique_together = ('reconciliation', 'journal_item')


class TaxSettlement(models.Model):
    """التسوية الضريبية:
    VAT_CLOSE  إقفال ضريبة القيمة المضافة لفترة: مدين 2130 مخرجات / دائن 1190 مدخلات / دائن 2135 المستحق
               (لو المدخلات أكبر: الفرق بيفضل في 1190 رصيد مرحّل)
    VAT_PAY    سداد المستحق: مدين 2135 / دائن الخزينة أو البنك
    WHT_PAY    سداد ضريبة الخصم والإضافة المحصّلة من الموردين: مدين 2120 / دائن الخزينة أو البنك"""
    VAT_CLOSE = 'VAT_CLOSE'
    VAT_PAY = 'VAT_PAY'
    WHT_PAY = 'WHT_PAY'
    KIND_CHOICES = ((VAT_CLOSE, 'إقفال ضريبة القيمة المضافة'), (VAT_PAY, 'سداد ضريبة القيمة المضافة'),
                    (WHT_PAY, 'سداد ضريبة الخصم والإضافة'))

    kind = models.CharField(max_length=10, choices=KIND_CHOICES, verbose_name='النوع')
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    period_end = models.DateField(verbose_name='حتى تاريخ')
    output_vat = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='ضريبة المبيعات')
    input_vat = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='ضريبة المشتريات')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ')
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                 verbose_name='الخزينة')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                     verbose_name='الحساب البنكي')
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                      verbose_name='القيد')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'تسوية ضريبية'
        verbose_name_plural = 'التسويات الضريبية'
        ordering = ['-date', '-id']

    def __str__(self):
        return f'{self.get_kind_display()} — {self.period_end}'


class FiscalYearClose(models.Model):
    """إقفال سنة مالية: قيد يصفّر الإيرادات والمصروفات في 3200 أرباح محتجزة، وقفل الدفاتر لآخر السنة."""
    year = models.PositiveIntegerField(unique=True, verbose_name='السنة')
    revenue = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='الإيرادات')
    expenses = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='المصروفات')
    net_profit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='صافي الربح')
    closing_entry = models.ForeignKey(JournalEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                      verbose_name='قيد الإقفال')
    previous_lock_date = models.DateField(null=True, blank=True, verbose_name='تاريخ القفل قبل الإقفال')
    closed_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                  verbose_name='بواسطة')
    closed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'إقفال سنة مالية'
        verbose_name_plural = 'إقفال السنوات المالية'
        ordering = ['-year']

    def __str__(self):
        return f'إقفال {self.year}'


class IntermediaryCompany(models.Model):
    """شركة تحصيل وسيطة (فوري، أمان، بساطة…): المندوب بيورّد فيها المبلغ كامل، والفلوس بتفضل على حسابها
    (1157) لحد ما تحوّل للبنك ناقص نسبتها — والمحاسب يسوّيها (IntermediarySettlement)."""
    name = models.CharField(max_length=100, unique=True, verbose_name='اسم الشركة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                verbose_name='حساب المستحقات')
    is_active = models.BooleanField(default=True, verbose_name='نشطة')
    notes = models.CharField(max_length=255, blank=True, default='', verbose_name='ملاحظات')

    class Meta:
        verbose_name = 'شركة تحصيل وسيطة'
        verbose_name_plural = 'شركات التحصيل الوسيطة'
        ordering = ['name']

    def __str__(self):
        return self.name

    def ensure_account(self):
        if self.account_id:
            return self.account
        from apps.tenant.services.journal_service import _get_system_account
        parent = _get_system_account('1157')
        acc, _ = Account.objects.get_or_create(code=f'1157-{self.pk}', defaults={
            'name': f'مستحقات {self.name}', 'account_type': parent.account_type, 'parent': parent,
            'allow_reconcile': True})
        self.account = acc
        self.save(update_fields=['account'])
        return acc


class IntermediarySettlement(models.Model):
    """تحويل الشركة الوسيطة للبنك ناقص عمولتها وضريبة القيمة المضافة على العمولة (14% — شركات الدفع مش بنوك):
    Dr البنك (الصافي) + Dr عمولات التحصيل 5306 + Dr ض.ق.م مدخلات 1190 (لو الشركة مسجلة؛ وإلا تتحمّل على 5306)
    / Cr مستحقات الشركة (المبلغ كله)."""
    company = models.ForeignKey(IntermediaryCompany, on_delete=models.PROTECT, related_name='settlements',
                                verbose_name='الشركة')
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    gross = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ المسدد من مستحقاتها')
    fee = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='عمولة التحصيل')
    vat = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'),
                              verbose_name='ضريبة القيمة المضافة على العمولة')
    vat_recoverable = models.BooleanField(default=True, verbose_name='الضريبة مدخلات (الشركة مسجلة في القيمة المضافة)')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='+', verbose_name='البنك')
    reference = models.CharField(max_length=100, blank=True, default='', verbose_name='رقم التحويل / الكشف')
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                      verbose_name='القيد')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='سجلها')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'تسوية شركة وسيطة'
        verbose_name_plural = 'تسويات الشركات الوسيطة'
        ordering = ['-date', '-id']

    @property
    def net(self):
        return self.gross - self.fee - self.vat


class AccountMapping(models.Model):
    """ربط عملية محاسبية (معرّفة بكود حسابها الافتراضي) بحساب فرعي تاني — see accounting.account_mapping."""
    code = models.CharField(max_length=20, unique=True, verbose_name='العملية (كود الحساب الافتراضي)')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='+', verbose_name='الحساب المربوط')
    updated_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='عدّله')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'ربط حساب'
        verbose_name_plural = 'ربط الحسابات'

    def __str__(self):
        return f'{self.code} → {self.account}'


class PeriodClose(models.Model):
    """سجل إقفال / إعادة فتح الفترات (accounting.periods) — مين وإمتى وليه."""
    CLOSE, REOPEN = 'CLOSE', 'REOPEN'
    ACTION_CHOICES = [(CLOSE, 'إقفال'), (REOPEN, 'إعادة فتح')]
    action = models.CharField(max_length=10, choices=ACTION_CHOICES, verbose_name='العملية')
    lock_date = models.DateField(null=True, blank=True, verbose_name='الدفاتر مقفولة لحد')
    previous_lock_date = models.DateField(null=True, blank=True, verbose_name='كانت مقفولة لحد')
    note = models.CharField(max_length=255, blank=True, default='', verbose_name='السبب / ملاحظة')
    user = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                             verbose_name='بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'إقفال فترة'
        verbose_name_plural = 'إقفال الفترات'
        ordering = ['-created_at', '-id']
        permissions = [('reopen_periodclose', 'إعادة فتح فترة محاسبية مقفولة')]
