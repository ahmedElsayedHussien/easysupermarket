"""
التسوية الضريبية: إقفال ضريبة القيمة المضافة لفترة، وسداد المستحق، وسداد ضريبة الخصم والإضافة.
Balances are read from the ledger up to the chosen date (database aggregates).
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce

from .models import JournalEntry, JournalItem, TaxSettlement
from apps.tenant.core.money import money

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=20, decimal_places=2)
OUTPUT_VAT, INPUT_VAT, VAT_PAYABLE, WHT_PAYABLE, WHT_RECEIVABLE = '2130', '1190', '2135', '2120', '1155'
ROUNDING = '5115'          # فروق التقريب — resolved through account_mapping like every system account


def _balance(code, as_of, credit_side=True):
    """Own balance of the account ``code`` (posted, up to ``as_of``): credit-debit for a
    liability, debit-credit for an asset."""
    from apps.tenant.services.journal_service import _get_system_account
    acc = _get_system_account(code)
    expr = F('credit') - F('debit') if credit_side else F('debit') - F('credit')
    return JournalItem.objects.filter(account=acc, entry__status=JournalEntry.POSTED, entry__date__lte=as_of).aggregate(
        s=Coalesce(Sum(expr), Value(ZERO), output_field=MONEY))['s'], acc


def position(as_of=None):
    as_of = as_of or datetime.date.today()
    out_vat, _ = _balance(OUTPUT_VAT, as_of)
    in_vat, _ = _balance(INPUT_VAT, as_of, credit_side=False)
    payable, _ = _balance(VAT_PAYABLE, as_of)
    wht, _ = _balance(WHT_PAYABLE, as_of)
    wht_rec, _ = _balance(WHT_RECEIVABLE, as_of, credit_side=False)
    return {'as_of': as_of, 'output_vat': out_vat, 'input_vat': in_vat, 'net_vat': out_vat - in_vat,
            'vat_payable': payable, 'wht_payable': wht, 'wht_receivable': wht_rec}


def _post(obj, label, lines, user):
    """Posted entry dated on the settlement date (the closing belongs to its period)."""
    from apps.tenant.accounting.posting import post_entry
    return post_entry(date=obj.date, reference=f'TAX-{obj.kind}-{obj.pk}', description=label, lines=lines,
                      user=user, source=obj)


def eta_documents_vat(period_end):
    """The accepted ETA documents (VALID, or a cancel / reject request that was not granted) not settled by any
    VAT close yet, of invoices dated up to ``period_end`` — each document is settled ONCE, by the first close
    after it is accepted, whatever its invoice date. A credit note counts negative. Locks the rows (call
    inside the closing transaction). Returns (log ids, VAT per the documents, sum of rounding_diff), signed."""
    from apps.tenant.einvoicing.models import EInvoiceLog
    from apps.tenant.invoicing.models import Invoice
    rows = list(EInvoiceLog.objects.select_for_update(of=('self',)).filter(
        status__in=EInvoiceLog.ACCEPTED_STATES, settlement__isnull=True, invoice__status=Invoice.POSTED,
        invoice__date__lte=period_end, eta_tax_total__isnull=False,
    ).order_by('pk').values_list('pk', 'invoice__invoice_type', 'eta_tax_total', 'rounding_diff'))
    vat = diff = ZERO
    for _pk, invoice_type, tax, rounding in rows:
        sign = -1 if invoice_type == Invoice.RETURN_SALE else 1
        vat += sign * tax
        diff += sign * (rounding or ZERO)
    return [r[0] for r in rows], vat, diff


def close_vat(period_end, user=None):
    """Move the output / input VAT balances up to ``period_end`` into 2135 (payable).

    Output VAT is declared as the accepted ETA documents say (official 5-place formulas), not as the invoices'
    own 2-place VAT: declared = money(books − Σ rounding_diff of the accepted documents not settled yet) —
    exactly money(Σ document VAT) when every sale went to the ETA; a sale with no accepted document keeps its
    booked VAT, and a document accepted after its month was closed is settled by the next close (each document
    once — EInvoiceLog.settlement). The difference (books − declared) goes to the rounding account in the same entry:
        Dr 2130 (the whole balance)  Cr 1190 / 2135 (declared)  Cr|Dr rounding (the difference)"""
    from apps.tenant.accounting.models import ensure_open_period
    from apps.tenant.core.money import money
    ensure_open_period(period_end)
    with transaction.atomic():
        out_vat, out_acc = _balance(OUTPUT_VAT, period_end)
        in_vat, in_acc = _balance(INPUT_VAT, period_end, credit_side=False)
        if out_vat <= 0:
            raise ValidationError('مفيش ضريبة مبيعات تتقفل لحد التاريخ ده (ضريبة المشتريات بتفضل رصيد مرحّل).')
        log_ids, eta_vat, eta_diff = eta_documents_vat(period_end)
        declared = money(out_vat - eta_diff)
        adjustment = out_vat - declared
        settled_input = max(min(in_vat, declared), ZERO)
        payable = max(declared - in_vat, ZERO)
        obj = TaxSettlement.objects.create(kind=TaxSettlement.VAT_CLOSE, date=period_end, period_end=period_end,
                                           output_vat=declared, input_vat=in_vat, amount=payable,
                                           eta_output_vat=eta_vat, rounding_adjustment=adjustment, created_by=user)
        label = f'إقفال ضريبة القيمة المضافة حتى {period_end}'
        from apps.tenant.services.journal_service import _get_system_account
        lines = []
        if out_vat > 0:
            lines.append((out_acc, out_vat, ZERO, f'{label} - ضريبة المبيعات'))
        if settled_input > 0:
            lines.append((in_acc, ZERO, settled_input, f'{label} - ضريبة المشتريات المخصومة'))
        if payable > 0:
            lines.append((_get_system_account(VAT_PAYABLE), ZERO, payable, f'{label} - المستحق للمصلحة'))
        if adjustment:
            rounding_label = f'{label} - فروق تقريب بين الفواتير ومستندات الضرائب'
            lines.append((_get_system_account(ROUNDING), ZERO if adjustment > 0 else -adjustment,
                          adjustment if adjustment > 0 else ZERO, rounding_label))
        obj.journal_entry = _post(obj, label, lines, user)
        obj.save(update_fields=['journal_entry'])
        if log_ids:                                  # these documents' differences are settled — never again
            from apps.tenant.einvoicing.models import EInvoiceLog
            EInvoiceLog.objects.filter(pk__in=log_ids).update(settlement=obj)
    return obj


def pay(kind, amount, treasury, bank, user=None, date=None):
    """Pay the VAT payable (2135) or the withholding tax collected from suppliers (2120)."""
    from apps.tenant.hr.payroll_payments import _source_account
    from apps.tenant.services.journal_service import _get_system_account
    date = date or datetime.date.today()
    code = VAT_PAYABLE if kind == TaxSettlement.VAT_PAY else WHT_PAYABLE
    amount = money(Decimal(amount or 0))
    with transaction.atomic():
        due, acc = _balance(code, date)
        if amount <= 0 or amount > due:
            raise ValidationError(f'المبلغ لازم يكون أكبر من صفر ومش أكبر من المستحق ({due}).')
        src = _source_account(treasury, bank, amount)
        obj = TaxSettlement.objects.create(kind=kind, date=date, period_end=date, amount=amount, treasury=treasury,
                                           bank_account=bank, created_by=user)
        label = f'{obj.get_kind_display()} — {date}'
        obj.journal_entry = _post(obj, label, [(acc, amount, ZERO, label), (src, ZERO, amount, label)], user)
        obj.save(update_fields=['journal_entry'])
    return obj
