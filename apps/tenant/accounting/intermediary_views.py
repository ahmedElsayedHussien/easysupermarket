"""
شركات التحصيل الوسيطة (فوري، أمان، بساطة…).

The rep pays the FULL amount at the intermediary (RepCashHandover → Dr 1157-x / Cr rep cash box). Later the
intermediary pays the bank minus its commission and the accountant records it here:
    Dr bank (net) + Dr 5306 عمولة التحصيل (fee) + Dr 1190 ض.ق.م مدخلات (14% on the fee, when we are
    VAT-registered — otherwise it is part of the expense) / Cr 1157-x (gross)
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, F, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import redirect, render

from apps.tenant.core.decorators import custom_permission_required

from .models import BankAccount, IntermediaryCompany, IntermediarySettlement, JournalEntry, JournalItem

ZERO = Decimal('0')
CENT = Decimal('0.01')
FEES_ACCOUNT = '5306'
INPUT_VAT_ACCOUNT = '1190'
VAT_RATE = Decimal('14')   # on the commission: payment companies are not banks, so it is not VAT-exempt


def balances(companies):
    """{company_id: what it still holds for us} — one query (debit-normal leaf accounts)."""
    by_acc = {c.account_id: c.id for c in companies if c.account_id}
    rows = dict(JournalItem.objects.filter(account_id__in=list(by_acc), entry__status=JournalEntry.POSTED).order_by()
                .values('account_id').annotate(b=Coalesce(Sum(F('debit') - F('credit'),
                                                              output_field=DecimalField(max_digits=18, decimal_places=2)), ZERO))
                .values_list('account_id', 'b'))
    return {cid: (rows.get(acc) or ZERO).quantize(CENT) for acc, cid in by_acc.items()}


def _dec(v, label):
    try:
        d = Decimal(str(v or '0').strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise ValidationError(f'{label}: رقم غير صالح.')
    if not d.is_finite() or d < 0:
        raise ValidationError(f'{label}: لازم رقم موجب.')
    return d.quantize(CENT)


def settle(company, gross, fee, bank_account, user=None, reference='', vat=ZERO, vat_recoverable=True):
    from apps.tenant.invoicing.shift_services import _post
    from apps.tenant.services.journal_service import _get_system_account
    gross, fee, vat = _dec(gross, 'المبلغ'), _dec(fee, 'العمولة'), _dec(vat, 'ضريبة العمولة')
    if gross <= 0:
        raise ValidationError('اكتب المبلغ اللي اتسوّى من مستحقات الشركة.')
    if fee + vat >= gross:
        raise ValidationError('العمولة وضريبتها لازم يكونوا أقل من المبلغ.')
    if bank_account is None:
        raise ValidationError('اختار البنك اللي استلم التحويل.')
    with transaction.atomic():
        IntermediaryCompany.objects.select_for_update().filter(pk=company.pk).first()
        acc = company.ensure_account()
        held = balances([company]).get(company.id, ZERO)
        if gross > held:
            raise ValidationError(f'{company.name} معاها {held:.2f} بس — مينفعش تسوّي {gross:.2f}.')
        bank_acc = bank_account.ensure_account()
        s = IntermediarySettlement.objects.create(company=company, gross=gross, fee=fee, vat=vat,
                                                  vat_recoverable=bool(vat_recoverable), bank_account=bank_account,
                                                  reference=(reference or '').strip()[:100], created_by=user)
        label = f'تسوية {company.name} — تحويل للبنك {bank_account.name}' + (f' ({s.reference})' if s.reference else '')
        lines = [(bank_acc, gross - fee - vat, ZERO, label)]
        expense = fee + (ZERO if vat_recoverable else vat)          # not registered: the VAT is a cost too
        if expense > 0:
            lines.append((_get_system_account(FEES_ACCOUNT), expense, ZERO,
                          f'عمولة تحصيل {company.name}' + ('' if vat_recoverable or not vat else ' شاملة الضريبة')))
        if vat > 0 and vat_recoverable:
            lines.append((_get_system_account(INPUT_VAT_ACCOUNT), vat, ZERO, f'ض.ق.م مدخلات على عمولة {company.name}'))
        lines.append((acc, ZERO, gross, label))
        s.journal_entry = _post(f'INTM-{s.pk}', label, lines, user, s)
        s.save(update_fields=['journal_entry'])
    return s


@login_required
@custom_permission_required('accounting.view_intermediarycompany', redirect_url='core:main_screen')
def intermediaries(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add' and request.user.has_perm('accounting.add_intermediarycompany'):
                name = (request.POST.get('name') or '').strip()
                if not name:
                    raise ValidationError('اكتب اسم الشركة.')
                if IntermediaryCompany.objects.filter(name=name).exists():
                    raise ValidationError('فيه شركة بنفس الاسم.')
                c = IntermediaryCompany.objects.create(name=name[:100], notes=(request.POST.get('notes') or '')[:255])
                c.ensure_account()
                messages.success(request, f'اتضافت {c.name} — حسابها {c.account.code}.')
            elif action == 'toggle' and request.user.has_perm('accounting.add_intermediarycompany'):
                c = IntermediaryCompany.objects.get(pk=request.POST.get('company'))
                c.is_active = not c.is_active
                c.save(update_fields=['is_active'])
            elif action == 'settle' and request.user.has_perm('accounting.add_intermediarysettlement'):
                c = IntermediaryCompany.objects.get(pk=request.POST.get('company'))
                bank = BankAccount.objects.filter(pk=request.POST.get('bank') or 0).first()
                s = settle(c, request.POST.get('gross'), request.POST.get('fee'), bank, request.user,
                           request.POST.get('reference', ''), vat=request.POST.get('vat'),
                           vat_recoverable=bool(request.POST.get('vat_recoverable')))
                messages.success(request, f'اتسجلت تسوية {c.name}: {s.gross:.2f} — عمولة {s.fee:.2f} + ضريبة {s.vat:.2f} '
                                          f'— وصل البنك {s.net:.2f}.')
            else:
                messages.error(request, 'عفواً، لا تملك الصلاحية.')
        except (IntermediaryCompany.DoesNotExist, ValueError):
            messages.error(request, 'بيانات غير صالحة.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        return redirect('accounting:intermediaries')
    companies = list(IntermediaryCompany.objects.select_related('account').order_by('-is_active', 'name'))
    held = balances(companies)
    from apps.tenant.invoicing.models import RepCashHandover
    pending = dict(RepCashHandover.objects.filter(intermediary__in=companies).order_by().values('intermediary')
                   .annotate(n=Sum('amount')).values_list('intermediary', 'n'))
    for c in companies:
        c.held = held.get(c.id, ZERO)
        c.paid_in = pending.get(c.id) or ZERO
    return render(request, 'accounting/intermediaries.html', {
        'title': 'شركات التحصيل الوسيطة', 'companies': companies,
        'total_held': sum((c.held for c in companies), ZERO),
        'settlements': IntermediarySettlement.objects.select_related('company', 'bank_account', 'created_by')[:30],
        'handovers': RepCashHandover.objects.filter(intermediary__isnull=False)
        .select_related('intermediary', 'rep__employee__user').order_by('-id')[:30],
        'banks': BankAccount.objects.all(),
        'vat_registered': bool((getattr(__import__('apps.tenant.core.models', fromlist=['SystemSetting'])
                                        .SystemSetting.get_settings(), 'tax_number', '') or '').strip()),
        'vat_rate': VAT_RATE,
        'can_add': request.user.has_perm('accounting.add_intermediarycompany'),
        'can_settle': request.user.has_perm('accounting.add_intermediarysettlement'),
    })


@login_required
@custom_permission_required('accounting.change_accountmapping', redirect_url='core:main_screen')
def account_mapping_view(request):
    """ربط الحسابات: point an operation to another posting account (admins)."""
    from . import account_mapping as amap
    from .models import Account
    if request.method == 'POST':
        code = request.POST.get('code') or ''
        try:
            if request.POST.get('action') == 'reset':
                amap.set_mapping(code, None, request.user)
                messages.success(request, f'رجعت العملية {code} لحسابها الافتراضي.')
            else:
                acc = Account.objects.filter(pk=request.POST.get('account') or 0).first()
                if acc is None:
                    raise ValidationError('اختار الحساب.')
                amap.set_mapping(code, acc, request.user)
                messages.success(request, f'العملية {code} بقت بتترحّل على {acc.code} {acc.name} من دلوقتي.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        return redirect('accounting:account_mapping')
    rows = amap.roles()
    choices = {}
    for r in rows:
        if r['type'] and r['type'] not in choices:
            choices[r['type']] = list(amap.posting_accounts(r['type']))
        r['choices'] = choices.get(r['type'], [])
    groups = [{'key': k, 'title': t, 'rows': [r for r in rows if r['group'] == k]} for k, t in amap.GROUPS]
    return render(request, 'accounting/account_mapping.html', {'title': 'ربط الحسابات', 'groups': groups,
                                                               'changed': sum(1 for r in rows if r['mapped'])})
