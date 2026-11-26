from decimal import Decimal
import datetime
from django.db.models import Sum, Q, Count
from django.urls import reverse
from apps.tenant.partners.models import Partner
from apps.tenant.invoicing.models import Invoice
from apps.tenant.accounting.models import Voucher, JournalItem
from apps.tenant.partners.balances import _accounts

ZERO = Decimal('0')


def _url(name, **kw):
    try:
        return reverse(name, kwargs=kw)
    except Exception:
        return None


def _partner_items(account_ids):
    """POSTED journal items on the partner's own accounts. An entry and its reversal (a cancelled
    document) net to zero and are both left out — the statement shows what is in force."""
    return (JournalItem.objects.filter(account_id__in=account_ids, entry__status='POSTED',
                                       entry__reversal_of__isnull=True, entry__reversed_by__isnull=True))


# what each invoice type does to the partner: its document side, and the instant payment / refund side
_INVOICE_LABELS = {
    Invoice.SALE: ('debit', 'فاتورة مبيعات', 'sale', 'سداد فوري', 'سداد فوري للفاتورة'),
    Invoice.RETURN_SALE: ('credit', 'مرتجع مبيعات', 'return_sale', 'استرداد فوري', 'استرداد فوري لمرتجع'),
    Invoice.PURCHASE: ('credit', 'فاتورة مشتريات', 'purchase', 'سداد فوري للمورد', 'سداد فوري لمشتريات'),
    Invoice.RETURN_PURCHASE: ('debit', 'مرتجع مشتريات', 'return_purchase', 'استرداد نقدي من المورد',
                              'استرداد فوري لمرتجع مشتريات'),
}


def _invoice_lines(inv):
    out = []
    for line in inv.lines.all():
        uom_name = ''
        if line.uom and getattr(line.uom, 'uom', None):
            uom_name = line.uom.uom.name
        elif line.product:
            uom_name = getattr(line.product, 'pos_unit_name', None) or line.product.get_unit_display() or ''
        # line total from its parts (old lines may hold total_amount = 0 although the amounts are right)
        line_total = (line.subtotal or ZERO) + (line.tax_amount or ZERO) - (line.wht_amount or ZERO)
        out.append({'product_name': line.product.name, 'quantity': line.quantity, 'uom_name': uom_name,
                    'unit_price': line.unit_price, 'discount_amount': line.discount_amount or ZERO,
                    'discount_pct': line.discount_pct or ZERO, 'tax_amount': line.tax_amount or ZERO,
                    'total_amount': line_total})
    return out


def get_partner_full_statement(partner, start_date=None, end_date=None):
    """
    كشف حساب الشريك من دفتر الأستاذ: كل حركة مرحّلة على حساباته (عميل 1130-x / مورد 2110-x)، مربوطة
    بمستندها (فاتورة، سند، شيك، رصيد أول مدة، شحن آجل…) — والقيود اليدوية على حسابه بتظهر كـ«قيد يومية».
    الرصيد = مدين − دائن (موجب: عليه لنا، سالب: له علينا) ويطابق ميزان المراجعة دايماً.
    """
    from apps.tenant.accounting.models import Cheque, EServiceTransaction
    from apps.tenant.partners.models import Payment

    account_ids = [a for a in _accounts(partner) if a]
    rows = list(_partner_items(account_ids).order_by().values('entry_id').annotate(d=Sum('debit'), c=Sum('credit')))
    entry_ids = [r['entry_id'] for r in rows]
    from apps.tenant.accounting.models import JournalEntry
    entries = {e.pk: e for e in JournalEntry.objects.filter(pk__in=entry_ids).select_related('content_type')}

    by_model = {}
    for e in entries.values():
        if e.content_type_id:
            by_model.setdefault(e.content_type.model, set()).add(e.object_id)
    invoices = {i.pk: i for i in Invoice.objects.filter(pk__in=by_model.get('invoice', ())).prefetch_related(
        'lines__product__uoms__uom', 'lines__uom__uom')}
    eservices = {x.pk: x for x in EServiceTransaction.objects.filter(pk__in=by_model.get('eservicetransaction', ()))
                 .select_related('pos_machine', 'ewallet')}
    vouchers = {v.journal_entry_id: v for v in Voucher.objects.filter(journal_entry_id__in=entry_ids)}
    payments = {p.journal_entry_id: p for p in Payment.objects.filter(journal_entry_id__in=entry_ids)}
    cheque_stage = {}
    stage_fields = (('entry_receive_issue', 'issue'), ('entry_deposit', 'deposit'), ('entry_clearance', 'clear'),
                    ('entry_bounce', 'bounce'), ('entry_endorse', 'endorse'))
    q = Q()
    for f, _ in stage_fields:
        q |= Q(**{f'{f}_id__in': entry_ids})
    cheques_by_pk = {}
    for chq in Cheque.objects.filter(q | Q(partner=partner)).select_related('bank_account'):
        cheques_by_pk[chq.pk] = chq
        for f, stage in stage_fields:
            eid = getattr(chq, f'{f}_id')
            if eid in entries:
                cheque_stage[eid] = (chq, stage)

    seen_invoice_doc = set()
    raw = []
    for r in rows:
        e = entries[r['entry_id']]
        d, c = r['d'] or ZERO, r['c'] or ZERO
        net = d - c
        debit, credit = (net, ZERO) if net > 0 else (ZERO, -net)
        if not debit and not credit:
            continue
        side = 'debit' if debit else 'credit'
        t = {'date': e.date, 'created_at': e.created_at, 'debit': debit, 'credit': credit, 'lines': [],
             'has_lines': False, 'entry_id': e.pk}
        model = e.content_type.model if e.content_type_id else None
        inv = invoices.get(e.object_id) if model == 'invoice' else None
        if inv is not None and inv.invoice_type in _INVOICE_LABELS:
            doc_side, doc_label, badge, pay_label, pay_desc = _INVOICE_LABELS[inv.invoice_type]
            url = _url('invoicing:invoice_detail', invoice_id=inv.pk)
            if side == doc_side and inv.pk not in seen_invoice_doc:
                seen_invoice_doc.add(inv.pk)
                lines = _invoice_lines(inv)
                t.update(doc_type=doc_label, badge_type=badge, reference=inv.invoice_number, url=url,
                         description=f'{doc_label} - {inv.get_payment_type_display()}', lines=lines,
                         has_lines=bool(lines))
            else:
                t.update(doc_type=f'{pay_label} ({inv.get_payment_type_display()})', badge_type='payment_instant',
                         reference=f'تسوية {inv.invoice_number}', url=url,
                         description=f'{pay_desc} {inv.invoice_number}')
        elif e.pk in vouchers:
            v = vouchers[e.pk]
            receipt = v.voucher_type == Voucher.RECEIPT
            t.update(doc_type='سند قبض' if receipt else 'سند صرف', badge_type='receipt' if receipt else 'payment',
                     reference=v.voucher_number, url=_url('accounting:voucher_detail', pk=v.pk),
                     description=v.description or f'{"سند قبض" if receipt else "سند صرف"} ({v.get_payment_method_display()})')
        elif e.pk in cheque_stage or e.reference.startswith('CHQ-'):
            chq, stage = cheque_stage.get(e.pk, (None, None))
            if chq is None:
                try:
                    chq = cheques_by_pk.get(int(e.reference.split('-')[1]))
                except (IndexError, ValueError):
                    chq = None
                stage = 'cancel' if e.reference.endswith('-CNCPAY') else 'other'
            receivable = chq is not None and chq.cheque_type == 'RECEIVABLE'
            label = {'issue': 'ورقة قبض (شيك)' if receivable else 'ورقة دفع (شيك)', 'bounce': 'ارتداد شيك قبض',
                     'cancel': 'إلغاء شيك دفع'}.get(stage, 'حركة شيك')
            t.update(doc_type=label,
                     badge_type={'issue': 'receipt' if receivable else 'payment', 'bounce': 'return_sale'}.get(
                         stage, 'payment_instant'),
                     reference=f'شيك #{chq.cheque_number}' if chq else e.reference,
                     url=_url('accounting:cheque_detail', pk=chq.pk) if chq else None,
                     description=e.description)
        elif model == 'eservicetransaction' and e.object_id in eservices:
            es = eservices[e.object_id]
            t.update(doc_type='شحن ماكينة آجل', badge_type='purchase', reference=es.transaction_number,
                     url=_url('accounting:eservice_detail', pk=es.pk),
                     description=es.description or f'شحن رصيد {es.pos_machine or es.ewallet or ""} (آجل)')
        elif e.pk == partner.opening_journal_entry_id or e.reference.startswith(f'OPENING-PRT-{partner.pk}'):
            t.update(doc_type='رصيد أول مدة (افتتاحي)', badge_type='opening', reference=f'OPN-{partner.pk}',
                     url=None, description=f'رصيد أول مدة افتتاحي للشريك: {partner.name}')
        elif e.pk in payments:
            p = payments[e.pk]
            receipt = p.payment_type == 'RECEIPT'
            t.update(doc_type='سند قبض دفعات' if receipt else 'سند صرف دفعات',
                     badge_type='receipt' if receipt else 'payment', reference=p.reference, url=None,
                     description=p.notes or e.description)
        else:
            # a manual entry on the partner's account (write-off, allowed discount, offset…) or any other document
            t.update(doc_type='قيد يومية', badge_type='journal', reference=e.reference,
                     url=_url('accounting:journal_detail', pk=e.pk), description=e.description)
        raw.append(t)

    def _sort_key(x):
        dt = x.get('created_at')
        if dt is not None and getattr(dt, 'tzinfo', None) is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return (x['date'], dt or datetime.datetime.min, x['entry_id'])

    raw.sort(key=_sort_key)

    def _as_date(v):
        if v in (None, ''):
            return None
        if isinstance(v, datetime.date):
            return v
        try:
            return datetime.date.fromisoformat(str(v)[:10])
        except ValueError:
            return None
    start, end = _as_date(start_date), _as_date(end_date)

    opening_balance = ZERO
    period, period_debit, period_credit = [], ZERO, ZERO
    running = ZERO
    for t in raw:
        if start and t['date'] < start:
            opening_balance += t['debit'] - t['credit']
            running = opening_balance
            continue
        if end and t['date'] > end:
            continue
        running += t['debit'] - t['credit']
        period_debit += t['debit']
        period_credit += t['credit']
        period.append({**t, 'running_balance': running})

    return {
        'transactions': period,
        'opening_balance': opening_balance,
        'period_debit': period_debit,
        'period_credit': period_credit,
        'final_balance': opening_balance + period_debit - period_credit,
        'total_count': len(period),
    }


def batch_get_partners_balances(partner_ids=None, partner_qs=None):
    """
    أرصدة مجموعة شركاء من دفتر الأستاذ في استعلام واحد: {partner_id: {period_debit, period_credit,
    final_balance (مدين − دائن على حساباته), transactions_count, partner}}. القيود الملغاة بقيد عكسي
    (المستند الملغي) مش بتتعد في المجاميع ولا في عدد الحركات — الرصيد هو هو.
    """
    if partner_qs is None:
        partner_qs = Partner.objects.filter(is_active=True)
        if partner_ids:
            partner_qs = partner_qs.filter(id__in=partner_ids)
    partners = list(partner_qs)
    if not partners:
        return {}
    accounts = {p.pk: [a for a in _accounts(p) if a] for p in partners}
    all_ids = {a for ids in accounts.values() for a in ids}
    sums = {r['account_id']: r for r in _partner_items(all_ids).order_by().values('account_id').annotate(
        d=Sum('debit'), c=Sum('credit'), n=Count('entry_id', distinct=True))}
    out = {}
    for p in partners:
        d = sum(((sums.get(a) or {}).get('d') or ZERO for a in accounts[p.pk]), ZERO)
        c = sum(((sums.get(a) or {}).get('c') or ZERO for a in accounts[p.pk]), ZERO)
        n = sum(((sums.get(a) or {}).get('n') or 0 for a in accounts[p.pk]), 0)
        bal = d - c
        out[p.pk] = {'id': p.pk, 'partner': p, 'period_debit': d, 'period_credit': c, 'final_balance': bal,
                     'transactions_count': n}
        # list pages read these afterwards without a query (net balance on both views, as before)
        p._cached_customer_balance = bal
        p._cached_supplier_balance = -bal
    return out


def get_all_partners_summary(partner_type=None, balance_filter=None, search_query=None):
    """
    تجميع إحصائيات وأرصدة جميع الشركاء في شاشة واحدة:
    حساب الرصيد الافتتاحي، إجمالي المدين، إجمالي الدائن، والرصيد النهائي المستحق
    عبر استعلامات تجميعية سريعة بدلاً من الحلقات التكرارية البطيئة.
    """
    qs = Partner.objects.filter(is_active=True).order_by('name')

    if partner_type:
        if partner_type == 'BOTH':
            qs = qs.filter(partner_type=Partner.BOTH)
        elif partner_type in [Partner.CUSTOMER, Partner.SUPPLIER]:
            qs = qs.filter(Q(partner_type=partner_type) | Q(partner_type=Partner.BOTH))

    if search_query:
        sq = search_query.strip()
        qs = qs.filter(
            Q(name__icontains=sq) |
            Q(phone__icontains=sq) |
            Q(mobile__icontains=sq) |
            Q(tax_id__icontains=sq)
        )

    # Batch calculate all balances in 4 fast aggregated SQL queries instead of N*6 queries
    batch_results = batch_get_partners_balances(partner_qs=qs)

    partners_data = []
    total_receivables = Decimal('0')  # مدين (لنا طرفهم)
    total_payables = Decimal('0')     # دائن (لهم طرفنا)
    total_balanced_count = 0

    for p in qs:
        data = batch_results.get(p.id)
        if not data:
            continue

        net_bal = data['final_balance']

        if net_bal > Decimal('0.005'):
            bal_status = 'DEBIT'      # مدين - عليه مديونية لنا
            total_receivables += net_bal
        elif net_bal < Decimal('-0.005'):
            bal_status = 'CREDIT'     # دائن - له مستحقات علينا
            total_payables += abs(net_bal)
        else:
            bal_status = 'BALANCED'   # متزن
            total_balanced_count += 1

        # Apply balance_filter if provided
        if balance_filter == 'receivable' and bal_status != 'DEBIT':
            continue
        elif balance_filter == 'payable' and bal_status != 'CREDIT':
            continue
        elif balance_filter == 'zero' and bal_status != 'BALANCED':
            continue
        elif balance_filter == 'active' and bal_status == 'BALANCED':
            continue

        partners_data.append({
            'id': p.id,
            'name': p.name,
            'partner_type': p.partner_type,
            'partner_type_display': p.get_partner_type_display(),
            'phone': p.phone or p.mobile or '—',
            'tax_id': p.tax_id or '—',
            'opening_balance': p.opening_balance or Decimal('0'),
            'opening_balance_type': p.get_opening_balance_type_display() if p.opening_balance else '—',
            'period_debit': data['period_debit'],
            'period_credit': data['period_credit'],
            'net_balance': net_bal,
            'abs_balance': abs(net_bal),
            'balance_status': bal_status,
            'transactions_count': data['transactions_count'],
        })

    net_position = total_receivables - total_payables

    return {
        'partners': partners_data,
        'total_partners_count': len(partners_data),
        'total_receivables': total_receivables,
        'total_payables': total_payables,
        'net_position': net_position,
        'total_balanced_count': total_balanced_count,
    }
