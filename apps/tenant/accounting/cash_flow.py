"""
قائمة التدفقات النقدية (الطريقة المباشرة).

Cash = the treasuries (1110), banks (1120), e-wallets (1160), payment machines (1170) and
cash in transit (1195) sub-trees. For every posted entry of the period that moves cash,
the OTHER lines of the entry say where the money came from / went to (credit - debit of
each non-cash line = its cash effect; an entry is balanced so they add up to the cash
movement). Entries between two cash accounts (treasury -> bank) net to zero and vanish.
Sections: operating / investing / financing, grouped by account code. Totals are summed
by the database; closing = opening + net change is checked against the real balance.
"""
from decimal import Decimal

from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import Account, JournalEntry, JournalItem

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=20, decimal_places=2)
CASH_ROOTS = ['1110', '1120', '1160', '1170', '1195']

# (section, group label, code prefixes) — the first match wins, so specific codes come first
GROUPS = [
    ('operating', 'مقبوضات من العملاء والمبيعات', ['1130', '1180', '1185', '41', '43', '4120', '4130']),
    ('operating', 'مدفوعات للموردين والمشتريات والمخزون', ['2110', '2170', '1145', '1140', '5100', '5105', '5110', '5115']),
    ('operating', 'المرتبات والسلف والعهد', ['2160', '2167', '5210', '5212', '5215', '1135', '1150']),
    ('operating', 'الضرائب والتأمينات', ['2120', '2130', '2165', '2166', '1155', '1190']),
    ('operating', 'المصروفات التشغيلية والإدارية', ['5', '2140', '1152']),
    ('operating', 'إيرادات أخرى (عمولات، خصم مكتسب...)', ['42', '4999', '4240']),
    ('investing', 'شراء وبيع الأصول الثابتة', ['12']),
    ('financing', 'القروض والتسهيلات', ['2180', '2200', '2210']),
    ('financing', 'رأس المال والشركاء', ['3']),
]
SECTIONS = [('operating', 'الأنشطة التشغيلية'), ('investing', 'الأنشطة الاستثمارية'), ('financing', 'الأنشطة التمويلية')]


def cash_account_ids():
    roots = list(Account.objects.filter(code__in=CASH_ROOTS).values('tree_id', 'lft', 'rght'))
    if not roots:
        return []
    q = Q()
    for r in roots:
        q |= Q(tree_id=r['tree_id'], lft__gte=r['lft'], rght__lte=r['rght'])
    return list(Account.objects.filter(q).values_list('id', flat=True))


def _group(code):
    for section, label, prefixes in GROUPS:
        if any(code.startswith(p) for p in sorted(prefixes, key=len, reverse=True)):
            return section, label
    return 'operating', 'أخرى'


def cash_flow(date_from, date_to):
    cash_ids = cash_account_ids()
    posted = JournalItem.objects.filter(entry__status=JournalEntry.POSTED)
    cash_items = posted.filter(account_id__in=cash_ids)

    def net(qs):
        return qs.aggregate(s=Coalesce(Sum(F('debit') - F('credit')), Value(ZERO), output_field=MONEY))['s']

    opening = net(cash_items.filter(entry__date__lt=date_from))
    closing = net(cash_items.filter(entry__date__lte=date_to))
    period_entries = cash_items.filter(entry__date__gte=date_from, entry__date__lte=date_to).values('entry_id')
    rows = (posted.filter(entry_id__in=period_entries).exclude(account_id__in=cash_ids)
            .values('account__code', 'account__name')
            .annotate(effect=Coalesce(Sum(F('credit') - F('debit')), Value(ZERO), output_field=MONEY))
            .exclude(effect=0).order_by('account__code'))

    sections = {key: {'key': key, 'label': label, 'groups': {}, 'inflow': ZERO, 'outflow': ZERO, 'net': ZERO}
                for key, label in SECTIONS}
    for r in rows:
        section, label = _group(r['account__code'] or '')
        sec = sections[section]
        g = sec['groups'].setdefault(label, {'label': label, 'net': ZERO, 'accounts': []})
        g['net'] += r['effect']
        g['accounts'].append({'code': r['account__code'], 'name': r['account__name'], 'effect': r['effect']})
        sec['net'] += r['effect']
        if r['effect'] > 0:
            sec['inflow'] += r['effect']
        else:
            sec['outflow'] += r['effect']
    result = []
    for key, _ in SECTIONS:
        sec = sections[key]
        sec['groups'] = sorted(sec['groups'].values(), key=lambda g: -abs(g['net']))
        result.append(sec)
    change = sum((s['net'] for s in result), ZERO)
    return {
        'sections': result, 'opening': opening, 'change': change, 'closing': closing,
        'balanced': (opening + change - closing).copy_abs() < Decimal('0.01'),
    }
