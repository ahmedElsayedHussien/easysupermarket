from decimal import Decimal
import datetime
from django.db.models import Sum, Q, Count
from django.urls import reverse
from django.utils import timezone
from apps.tenant.partners.models import Partner, Payment
from apps.tenant.invoicing.models import Invoice
from apps.tenant.accounting.models import Voucher, JournalItem

try:
    from apps.tenant.accounting.models import Cheque
except ImportError:
    Cheque = None


def get_partner_full_statement(partner, start_date=None, end_date=None):
    """
    محرك كشف الحساب الموحد الشامل للشريك:
    يجمع كافة حركات الشريك (مبيعات، مشتريات، مرتجعات، سندات قبض وصرف، رصيد أول مدة، وقيود اليومية)
    مع حساب الرصيد التراكمي المستمر ورصيد ما قبل الفترة بدقة محاسبية متناهية.
    """
    raw_txns = []

    def get_invoice_url(inv_id):
        try:
            return reverse('invoicing:invoice_detail', kwargs={'invoice_id': inv_id})
        except Exception:
            return f"/invoicing/invoice/{inv_id}/"

    def get_voucher_url(v_id):
        try:
            return reverse('accounting:voucher_detail', kwargs={'pk': v_id})
        except Exception:
            return f"/accounting/vouchers/{v_id}/"

    def get_journal_url(j_id):
        try:
            return reverse('accounting:journal_detail', kwargs={'pk': j_id})
        except Exception:
            return f"/accounting/journals/{j_id}/"

    # 1. Opening Balance (رصيد أول مدة)
    if partner.opening_balance and partner.opening_balance > Decimal('0'):
        op_date = partner.opening_balance_date or (partner.created_at.date() if partner.created_at else datetime.date(2000, 1, 1))
        op_dt = datetime.datetime.combine(op_date, datetime.time.min)
        op_dr = partner.opening_balance if partner.opening_balance_type == Partner.OPENING_DEBIT else Decimal('0')
        op_cr = partner.opening_balance if partner.opening_balance_type == Partner.OPENING_CREDIT else Decimal('0')
        raw_txns.append({
            'date': op_date,
            'created_at': op_dt,
            'doc_type': 'رصيد أول مدة (افتتاحي)',
            'badge_type': 'opening',
            'reference': f'OPN-{partner.id}',
            'description': f'رصيد أول مدة افتتاحي للشريك: {partner.name}',
            'url': None,
            'debit': op_dr,
            'credit': op_cr,
        })

    # 2. Invoices (فواتير المبيعات والمشتريات والمرتجعات)
    invoices = Invoice.objects.filter(
        partner=partner,
        status=Invoice.POSTED
    ).select_related('partner').prefetch_related('lines__product', 'lines__uom__uom').order_by('date', 'created_at')

    for inv in invoices:
        inv_dt = inv.created_at or datetime.datetime.combine(inv.date, datetime.time.min)
        inv_url = get_invoice_url(inv.id)

        lines_data = []
        for line in inv.lines.all():
            uom_name = ''
            if line.uom and getattr(line.uom, 'uom', None):
                uom_name = line.uom.uom.name
            elif hasattr(line.product, 'unit') and line.product.unit:
                uom_name = str(line.product.unit)
            lines_data.append({
                'product_name': line.product.name,
                'quantity': line.quantity,
                'uom_name': uom_name,
                'unit_price': line.unit_price,
                'discount_amount': line.discount_amount or Decimal('0'),
                'discount_pct': line.discount_pct or Decimal('0'),
                'tax_amount': line.tax_amount or Decimal('0'),
                'total_amount': line.total_amount,
            })
        has_lines = bool(lines_data)

        if inv.invoice_type == Invoice.SALE:
            # فاتورة مبيعات: مدين للعميل بكامل القيمة
            raw_txns.append({
                'date': inv.date,
                'created_at': inv_dt,
                'doc_type': 'فاتورة مبيعات',
                'badge_type': 'sale',
                'reference': inv.invoice_number,
                'description': f'فاتورة مبيعات - {inv.get_payment_type_display()}',
                'url': inv_url,
                'debit': inv.total_amount,
                'credit': Decimal('0'),
                'lines': lines_data,
                'has_lines': has_lines,
            })

            # في حال وجود سداد فوري (نقدي، بطاقة، بنكي، أو مركب)
            if inv.payment_type == 'SPLIT':
                paid_immed = (inv.paid_cash or Decimal('0')) + (inv.paid_card or Decimal('0')) + (inv.paid_ewallet or Decimal('0')) + (inv.paid_bank or Decimal('0'))
                if paid_immed > Decimal('0'):
                    raw_txns.append({
                        'date': inv.date,
                        'created_at': inv_dt + datetime.timedelta(seconds=1),
                        'doc_type': 'سداد فوري (مقسم)',
                        'badge_type': 'payment_instant',
                        'reference': f'تسوية {inv.invoice_number}',
                        'description': f'سداد فوري للفاتورة {inv.invoice_number}',
                        'url': inv_url,
                        'debit': Decimal('0'),
                        'credit': paid_immed,
                    })
            elif inv.payment_type != 'CREDIT':
                raw_txns.append({
                    'date': inv.date,
                    'created_at': inv_dt + datetime.timedelta(seconds=1),
                    'doc_type': f'سداد فوري ({inv.get_payment_type_display()})',
                    'badge_type': 'payment_instant',
                    'reference': f'تسوية {inv.invoice_number}',
                    'description': f'سداد نقدي/بنكي فوري للفاتورة {inv.invoice_number}',
                    'url': inv_url,
                    'debit': Decimal('0'),
                    'credit': inv.total_amount,
                })

        elif inv.invoice_type == Invoice.RETURN_SALE:
            # مرتجع مبيعات: دائن للعميل (تقليل مديونيته)
            raw_txns.append({
                'date': inv.date,
                'created_at': inv_dt,
                'doc_type': 'مرتجع مبيعات',
                'badge_type': 'return_sale',
                'reference': inv.invoice_number,
                'description': f'مرتجع مبيعات - {inv.get_payment_type_display()}',
                'url': inv_url,
                'debit': Decimal('0'),
                'credit': inv.total_amount,
                'lines': lines_data,
                'has_lines': has_lines,
            })

            if inv.payment_type == 'SPLIT':
                paid_immed = (inv.paid_cash or Decimal('0')) + (inv.paid_card or Decimal('0')) + (inv.paid_ewallet or Decimal('0')) + (inv.paid_bank or Decimal('0'))
                if paid_immed > Decimal('0'):
                    raw_txns.append({
                        'date': inv.date,
                        'created_at': inv_dt + datetime.timedelta(seconds=1),
                        'doc_type': 'استرداد فوري (مقسم)',
                        'badge_type': 'payment_instant',
                        'reference': f'تسوية {inv.invoice_number}',
                        'description': f'استرداد فوري لمرتجع {inv.invoice_number}',
                        'url': inv_url,
                        'debit': paid_immed,
                        'credit': Decimal('0'),
                    })
            elif inv.payment_type != 'CREDIT':
                raw_txns.append({
                    'date': inv.date,
                    'created_at': inv_dt + datetime.timedelta(seconds=1),
                    'doc_type': f'استرداد فوري ({inv.get_payment_type_display()})',
                    'badge_type': 'payment_instant',
                    'reference': f'تسوية {inv.invoice_number}',
                    'description': f'استرداد نقدي فوري لمرتجع {inv.invoice_number}',
                    'url': inv_url,
                    'debit': inv.total_amount,
                    'credit': Decimal('0'),
                })

        elif inv.invoice_type == Invoice.PURCHASE:
            # فاتورة مشتريات: دائن للمورد بكامل القيمة
            raw_txns.append({
                'date': inv.date,
                'created_at': inv_dt,
                'doc_type': 'فاتورة مشتريات',
                'badge_type': 'purchase',
                'reference': inv.invoice_number,
                'description': f'فاتورة مشتريات - {inv.get_payment_type_display()}',
                'url': inv_url,
                'debit': Decimal('0'),
                'credit': inv.total_amount,
                'lines': lines_data,
                'has_lines': has_lines,
            })

            if inv.payment_type == 'SPLIT':
                paid_immed = (inv.paid_cash or Decimal('0')) + (inv.paid_card or Decimal('0')) + (inv.paid_ewallet or Decimal('0')) + (inv.paid_bank or Decimal('0'))
                if paid_immed > Decimal('0'):
                    raw_txns.append({
                        'date': inv.date,
                        'created_at': inv_dt + datetime.timedelta(seconds=1),
                        'doc_type': 'سداد فوري للمورد (مقسم)',
                        'badge_type': 'payment_instant',
                        'reference': f'تسوية {inv.invoice_number}',
                        'description': f'سداد فوري لمشتريات {inv.invoice_number}',
                        'url': inv_url,
                        'debit': paid_immed,
                        'credit': Decimal('0'),
                    })
            elif inv.payment_type != 'CREDIT':
                raw_txns.append({
                    'date': inv.date,
                    'created_at': inv_dt + datetime.timedelta(seconds=1),
                    'doc_type': f'سداد فوري للمورد ({inv.get_payment_type_display()})',
                    'badge_type': 'payment_instant',
                    'reference': f'تسوية {inv.invoice_number}',
                    'description': f'سداد نقدي/بنكي فوري لمشتريات {inv.invoice_number}',
                    'url': inv_url,
                    'debit': inv.total_amount,
                    'credit': Decimal('0'),
                })

        elif inv.invoice_type == Invoice.RETURN_PURCHASE:
            # مرتجع مشتريات: مدين للمورد (تقليل مستحقاته علينا)
            raw_txns.append({
                'date': inv.date,
                'created_at': inv_dt,
                'doc_type': 'مرتجع مشتريات',
                'badge_type': 'return_purchase',
                'reference': inv.invoice_number,
                'description': f'مرتجع مشتريات للمورد - {inv.get_payment_type_display()}',
                'url': inv_url,
                'debit': inv.total_amount,
                'credit': Decimal('0'),
                'lines': lines_data,
                'has_lines': has_lines,
            })

            if inv.payment_type == 'SPLIT':
                paid_immed = (inv.paid_cash or Decimal('0')) + (inv.paid_card or Decimal('0')) + (inv.paid_ewallet or Decimal('0')) + (inv.paid_bank or Decimal('0'))
                if paid_immed > Decimal('0'):
                    raw_txns.append({
                        'date': inv.date,
                        'created_at': inv_dt + datetime.timedelta(seconds=1),
                        'doc_type': 'استرداد نقدي من المورد',
                        'badge_type': 'payment_instant',
                        'reference': f'تسوية {inv.invoice_number}',
                        'description': f'استرداد فوري لمرتجع مشتريات {inv.invoice_number}',
                        'url': inv_url,
                        'debit': Decimal('0'),
                        'credit': paid_immed,
                    })
            elif inv.payment_type != 'CREDIT':
                raw_txns.append({
                    'date': inv.date,
                    'created_at': inv_dt + datetime.timedelta(seconds=1),
                    'doc_type': f'استرداد نقدي من المورد ({inv.get_payment_type_display()})',
                    'badge_type': 'payment_instant',
                    'reference': f'تسوية {inv.invoice_number}',
                    'description': f'استرداد نقدي فوري لمرتجع مشتريات {inv.invoice_number}',
                    'url': inv_url,
                    'debit': Decimal('0'),
                    'credit': inv.total_amount,
                })

    # 3. Vouchers (سندات القبض والصرف)
    vouchers = Voucher.objects.filter(
        partner=partner,
        status=Voucher.POSTED
    ).order_by('date', 'created_at')

    for v in vouchers:
        v_dt = v.created_at or datetime.datetime.combine(v.date, datetime.time.min)
        v_url = get_voucher_url(v.id)
        if v.voucher_type == Voucher.RECEIPT:
            # قبض: دائن للشريك (استلام منه يقلل مديونيته)
            raw_txns.append({
                'date': v.date,
                'created_at': v_dt,
                'doc_type': 'سند قبض',
                'badge_type': 'receipt',
                'reference': v.voucher_number,
                'description': v.description or f'سند قبض نقدية/بنك ({v.get_payment_method_display()})',
                'url': v_url,
                'debit': Decimal('0'),
                'credit': v.amount,
            })
        else:
            # صرف: مدين للشريك (دفع له يقلل مستحقاته)
            raw_txns.append({
                'date': v.date,
                'created_at': v_dt,
                'doc_type': 'سند صرف',
                'badge_type': 'payment',
                'reference': v.voucher_number,
                'description': v.description or f'سند صرف نقدية/بنك ({v.get_payment_method_display()})',
                'url': v_url,
                'debit': v.amount,
                'credit': Decimal('0'),
            })

    # 3b. E-service merchant recharges on credit (شحن ماكينات آجل من المندوب = دائن للمورد)
    from apps.tenant.accounting.models import EServiceTransaction
    for es in EServiceTransaction.objects.filter(
            agent=partner, transaction_type=EServiceTransaction.MERCHANT_RECHARGE,
            paid_on_credit=True, status=EServiceTransaction.POSTED).order_by('date', 'created_at'):
        es_url = None
        try:
            es_url = reverse('accounting:eservice_detail', kwargs={'pk': es.pk})
        except Exception:
            pass
        raw_txns.append({
            'date': es.date,
            'created_at': es.created_at or datetime.datetime.combine(es.date, datetime.time.min),
            'doc_type': 'شحن ماكينة آجل',
            'badge_type': 'purchase',
            'reference': es.transaction_number,
            'description': es.description or f'شحن رصيد {es.pos_machine or es.ewallet or ""} (آجل)',
            'url': es_url,
            'debit': Decimal('0'),
            'credit': (es.principal_amount or Decimal('0')) + (es.commission_expense or Decimal('0')),
        })

    # 4. Partner Payments (المدفوعات المسجلة عبر نموذج Payment إن وجدت ولم تسجل كسند)
    for pay in partner.payments.filter(status='POSTED'):
        pay_dt = pay.created_at or datetime.datetime.combine(pay.date, datetime.time.min)
        if pay.payment_type == 'RECEIPT':
            raw_txns.append({
                'date': pay.date,
                'created_at': pay_dt,
                'doc_type': 'سند قبض دفعات',
                'badge_type': 'receipt',
                'reference': pay.reference or f'PAY-{pay.id}',
                'description': pay.notes or 'قبض دفعة من الشريك',
                'url': None,
                'debit': Decimal('0'),
                'credit': pay.amount,
            })
        else:
            raw_txns.append({
                'date': pay.date,
                'created_at': pay_dt,
                'doc_type': 'سند صرف دفعات',
                'badge_type': 'payment',
                'reference': pay.reference or f'PAY-{pay.id}',
                'description': pay.notes or 'صرف دفعة للشريك',
                'url': None,
                'debit': pay.amount,
                'credit': Decimal('0'),
            })

    # 5. Cheques (أوراق القبض وأوراق الدفع المسجلة للشريك)
    def get_cheque_url(chq_id):
        try:
            return reverse('accounting:cheque_detail', kwargs={'pk': chq_id})
        except Exception:
            return f"/accounting/cheques/{chq_id}/"

    # أ. شيكات القبض المستلمة من العميل
    for chq in partner.cheques.filter(cheque_type='RECEIVABLE'):
        chq_dt = chq.created_at or datetime.datetime.combine(chq.received_date, datetime.time.min)
        chq_url = get_cheque_url(chq.id)

        # قيد استلام الشيك: دائن للشريك (تخفيض مديونيته)
        raw_txns.append({
            'date': chq.received_date,
            'created_at': chq_dt,
            'doc_type': 'ورقة قبض (شيك)',
            'badge_type': 'receipt',
            'reference': f'شيك #{chq.cheque_number}',
            'description': f'شيك قبض مسحوب على {chq.drawer_bank_name or "البنك"} - استحقاق {chq.due_date} ({chq.get_status_display()})',
            'url': chq_url,
            'debit': Decimal('0'),
            'credit': chq.amount,
        })

        # في حال ارتداد الشيك: حركة عكسية بالمديونية بتاريخ الارتداد
        if chq.status == 'BOUNCED':
            bnc_date = chq.updated_at.date() if chq.updated_at else chq.due_date
            bnc_dt = chq.updated_at or chq_dt
            raw_txns.append({
                'date': bnc_date,
                'created_at': bnc_dt,
                'doc_type': 'ارتداد شيك قبض',
                'badge_type': 'return_sale',
                'reference': f'مرتد #{chq.cheque_number}',
                'description': f'إعادة مديونية لارتداد الشيك: {chq.bounce_reason or "رفض البنك"}',
                'url': chq_url,
                'debit': chq.amount,
                'credit': Decimal('0'),
            })

    # ب. شيكات الدفع الصادرة للمورد
    for chq in partner.cheques.filter(cheque_type='PAYABLE'):
        chq_dt = chq.created_at or datetime.datetime.combine(chq.received_date, datetime.time.min)
        chq_url = get_cheque_url(chq.id)

        # قيد تحرير الشيك: مدين للمورد (تخفيض التزامه)
        raw_txns.append({
            'date': chq.received_date,
            'created_at': chq_dt,
            'doc_type': 'ورقة دفع (شيك)',
            'badge_type': 'payment',
            'reference': f'شيك #{chq.cheque_number}',
            'description': f'شيك دفع مسحوب على {chq.bank_account.name if chq.bank_account else "البنك"} - استحقاق {chq.due_date} ({chq.get_status_display()})',
            'url': chq_url,
            'debit': chq.amount,
            'credit': Decimal('0'),
        })

        # في حال إلغاء الشيك: حركة عكسية دائنة للمورد لإعادة استحقاقه
        if chq.status == 'CANCELLED':
            cnc_date = chq.updated_at.date() if chq.updated_at else chq.received_date
            cnc_dt = chq.updated_at or chq_dt
            raw_txns.append({
                'date': cnc_date,
                'created_at': cnc_dt,
                'doc_type': 'إلغاء شيك دفع',
                'badge_type': 'payment_instant',
                'reference': f'ملغي #{chq.cheque_number}',
                'description': f'إعادة مديونية لإلغاء شيك الدفع #{chq.cheque_number}',
                'url': chq_url,
                'debit': Decimal('0'),
                'credit': chq.amount,
            })

    # Sort strictly by date, then created_at (handling naive vs aware datetimes safely)
    def _sort_key(x):
        dt = x.get('created_at')
        if dt and hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        elif dt is None:
            dt = datetime.datetime.min
        return (x['date'], dt)

    raw_txns.sort(key=_sort_key)

    for t in raw_txns:
        if 'lines' not in t:
            t['lines'] = []
            t['has_lines'] = False

    # Filter by period & calculate continuous running balance
    opening_balance = Decimal('0')
    period_transactions = []
    period_debit = Decimal('0')
    period_credit = Decimal('0')
    running_balance = Decimal('0')

    for txn in raw_txns:
        date_str = txn['date'].strftime('%Y-%m-%d') if hasattr(txn['date'], 'strftime') else str(txn['date'])
        dr = txn['debit'] or Decimal('0')
        cr = txn['credit'] or Decimal('0')

        if start_date and date_str < start_date:
            opening_balance += (dr - cr)
            running_balance = opening_balance
        else:
            if end_date and date_str > end_date:
                continue

            if not period_transactions and opening_balance != Decimal('0'):
                running_balance = opening_balance

            running_balance += (dr - cr)
            period_debit += dr
            period_credit += cr

            txn_copy = txn.copy()
            txn_copy['running_balance'] = running_balance
            period_transactions.append(txn_copy)

    final_balance = running_balance if period_transactions else opening_balance
    # ترتيب الحركات تسلسلياً وزمنياً: الأقدم أولاً ثم الأحدث

    return {
        'transactions': period_transactions,
        'opening_balance': opening_balance,
        'period_debit': period_debit,
        'period_credit': period_credit,
        'final_balance': final_balance,
        'total_count': len(period_transactions),
    }


def batch_get_partners_balances(partner_ids=None, partner_qs=None):
    """
    حساب أرصدة وإجماليات مجموعة من الشركاء دفعة واحدة باستخدام استعلامات تجميعية SQL
    دون الحاجة لتنفيذ استعلامات منفصلة أو لوبات متداخلة لكل شريك.
    """
    if partner_qs is None:
        partner_qs = Partner.objects.filter(is_active=True)
        if partner_ids:
            partner_qs = partner_qs.filter(id__in=partner_ids)

    partners = {p.id: {
        'id': p.id,
        'partner': p,
        'period_debit': Decimal('0'),
        'period_credit': Decimal('0'),
        'final_balance': Decimal('0'),
        'transactions_count': 0,
    } for p in partner_qs}

    if not partners:
        return {}

    pids = list(partners.keys())

    # 1. Opening balance
    for pid, data in partners.items():
        p = data['partner']
        if p.opening_balance and p.opening_balance > Decimal('0'):
            op = p.opening_balance
            data['transactions_count'] += 1
            if p.opening_balance_type == Partner.OPENING_DEBIT:
                data['period_debit'] += op
            elif p.opening_balance_type == Partner.OPENING_CREDIT:
                data['period_credit'] += op

    # 2. Invoices (مبيعات ومشتريات ومرتجعات)
    inv_data = Invoice.objects.filter(
        partner_id__in=pids,
        status=Invoice.POSTED
    ).values('partner_id', 'invoice_type', 'payment_type').annotate(
        total=Sum('total_amount'),
        paid_cash=Sum('paid_cash'),
        paid_card=Sum('paid_card'),
        paid_ewallet=Sum('paid_ewallet'),
        paid_bank=Sum('paid_bank'),
        rounding=Sum('rounding_amount'),
        cnt=Count('id')
    )

    for row in inv_data:
        pid = row['partner_id']
        if pid not in partners:
            continue
        itype = row['invoice_type']
        ptype = row['payment_type']
        tot = row['total'] or Decimal('0')
        cnt = row['cnt'] or 0

        immed = (row['paid_cash'] or Decimal('0')) + (row['paid_card'] or Decimal('0')) + \
                (row['paid_ewallet'] or Decimal('0')) + (row['paid_bank'] or Decimal('0'))
        # POS cash rounding is collected on top of the invoice: not a payment against it
        immed -= (row.get('rounding') or Decimal('0'))

        partners[pid]['transactions_count'] += cnt

        if itype == Invoice.SALE:
            partners[pid]['period_debit'] += tot
            if ptype == 'SPLIT':
                if immed > Decimal('0'):
                    partners[pid]['period_credit'] += immed
                    partners[pid]['transactions_count'] += cnt
            elif ptype != 'CREDIT':
                partners[pid]['period_credit'] += tot
                partners[pid]['transactions_count'] += cnt
        elif itype == Invoice.RETURN_SALE:
            partners[pid]['period_credit'] += tot
            if ptype == 'SPLIT':
                if immed > Decimal('0'):
                    partners[pid]['period_debit'] += immed
                    partners[pid]['transactions_count'] += cnt
            elif ptype != 'CREDIT':
                partners[pid]['period_debit'] += tot
                partners[pid]['transactions_count'] += cnt
        elif itype == Invoice.PURCHASE:
            partners[pid]['period_credit'] += tot
            if ptype == 'SPLIT':
                if immed > Decimal('0'):
                    partners[pid]['period_debit'] += immed
                    partners[pid]['transactions_count'] += cnt
            elif ptype != 'CREDIT':
                partners[pid]['period_debit'] += tot
                partners[pid]['transactions_count'] += cnt
        elif itype == Invoice.RETURN_PURCHASE:
            partners[pid]['period_debit'] += tot
            if ptype == 'SPLIT':
                if immed > Decimal('0'):
                    partners[pid]['period_credit'] += immed
                    partners[pid]['transactions_count'] += cnt
            elif ptype != 'CREDIT':
                partners[pid]['period_credit'] += tot
                partners[pid]['transactions_count'] += cnt

    # 3. Vouchers (سندات القبض والصرف)
    v_data = Voucher.objects.filter(
        partner_id__in=pids,
        status=Voucher.POSTED
    ).values('partner_id', 'voucher_type').annotate(
        total=Sum('amount'),
        cnt=Count('id')
    )
    for row in v_data:
        pid = row['partner_id']
        if pid not in partners:
            continue
        vtype = row['voucher_type']
        tot = row['total'] or Decimal('0')
        cnt = row['cnt'] or 0
        partners[pid]['transactions_count'] += cnt
        if vtype == Voucher.RECEIPT:
            partners[pid]['period_credit'] += tot
        elif vtype == Voucher.PAYMENT:
            partners[pid]['period_debit'] += tot

    # 3b. E-service merchant recharges on credit: the agent (supplier) is credited
    from apps.tenant.accounting.models import EServiceTransaction
    es_data = EServiceTransaction.objects.filter(
        agent_id__in=pids, transaction_type=EServiceTransaction.MERCHANT_RECHARGE,
        paid_on_credit=True, status=EServiceTransaction.POSTED,
    ).values('agent_id').annotate(p=Sum('principal_amount'), f=Sum('commission_expense'), cnt=Count('id'))
    for row in es_data:
        pid = row['agent_id']
        if pid not in partners:
            continue
        partners[pid]['period_credit'] += (row['p'] or Decimal('0')) + (row['f'] or Decimal('0'))
        partners[pid]['transactions_count'] += row['cnt'] or 0

    # 4. Payments (الدفعات)
    pay_data = Payment.objects.filter(
        partner_id__in=pids,
        status='POSTED'
    ).values('partner_id', 'payment_type').annotate(
        total=Sum('amount'),
        cnt=Count('id')
    )
    for row in pay_data:
        pid = row['partner_id']
        if pid not in partners:
            continue
        ptype = row['payment_type']
        tot = row['total'] or Decimal('0')
        cnt = row['cnt'] or 0
        partners[pid]['transactions_count'] += cnt
        if ptype == 'RECEIPT':
            partners[pid]['period_credit'] += tot
        else:
            partners[pid]['period_debit'] += tot

    # 5. Cheques (أوراق القبض وأوراق الدفع)
    if Cheque:
        c_data = Cheque.objects.filter(
            partner_id__in=pids
        ).values('partner_id', 'cheque_type', 'status').annotate(
            total=Sum('amount'),
            cnt=Count('id')
        )
        for row in c_data:
            pid = row['partner_id']
            if pid not in partners:
                continue
            ctype = row['cheque_type']
            status = row['status']
            tot = row['total'] or Decimal('0')
            cnt = row['cnt'] or 0
            if ctype == 'RECEIVABLE':
                if status != 'BOUNCED':
                    partners[pid]['period_credit'] += tot
                    partners[pid]['transactions_count'] += cnt
                else:
                    partners[pid]['period_credit'] += tot
                    partners[pid]['period_debit'] += tot
                    partners[pid]['transactions_count'] += cnt * 2
            elif ctype == 'PAYABLE':
                if status != 'CANCELLED':
                    partners[pid]['period_debit'] += tot
                    partners[pid]['transactions_count'] += cnt
                else:
                    partners[pid]['period_debit'] += tot
                    partners[pid]['period_credit'] += tot
                    partners[pid]['transactions_count'] += cnt * 2

    # Compute final balance & cache on partner object
    for pid, data in partners.items():
        bal = data['period_debit'] - data['period_credit']
        data['final_balance'] = bal
        p = data['partner']
        # Prime instance caches to avoid downstream queries
        p._cached_customer_balance = bal
        p._cached_supplier_balance = -bal

    return partners


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
