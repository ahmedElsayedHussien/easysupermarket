"""التسوية البنكية (accounting.bank_reconciliation)."""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from apps.tenant.core.decorators import custom_permission_required

from . import bank_reconciliation as svc
from .models import BankAccount, BankReconciliation


@login_required
@custom_permission_required('accounting.view_bankreconciliation', redirect_url='core:main_screen')
def bank_reconciliation_list(request):
    banks = list(BankAccount.objects.select_related('account').order_by('name'))
    if request.method == 'POST':
        if not request.user.has_perm('accounting.add_bankreconciliation'):
            messages.error(request, 'عفواً، لا تملك صلاحية عمل تسوية بنكية.')
            return redirect('accounting:bank_reconciliation_list')
        try:
            bank = next((b for b in banks if str(b.id) == request.POST.get('bank')), None)
            if bank is None:
                raise ValidationError('اختار الحساب البنكي.')
            date = datetime.datetime.strptime(request.POST.get('statement_date') or '', '%Y-%m-%d').date()
            rec = svc.start(bank, date, Decimal(request.POST.get('statement_balance') or '0'), user=request.user,
                            notes=request.POST.get('notes', ''))
            return redirect('accounting:bank_reconciliation_detail', rec.pk)
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (ValueError, InvalidOperation):
            messages.error(request, 'التاريخ أو الرصيد غير صالح.')
        return redirect('accounting:bank_reconciliation_list')
    recs = BankReconciliation.objects.select_related('bank_account').order_by('-statement_date', '-id')[:100]
    return render(request, 'accounting/bank_reconciliation_list.html', {
        'title': 'التسوية البنكية', 'banks': banks, 'recs': recs, 'today': datetime.date.today(),
        'can_add': request.user.has_perm('accounting.add_bankreconciliation'),
    })


@login_required
@custom_permission_required('accounting.view_bankreconciliation', redirect_url='core:main_screen')
def bank_reconciliation_detail(request, pk):
    rec = get_object_or_404(BankReconciliation.objects.select_related('bank_account'), pk=pk)
    if request.method == 'POST':
        if not request.user.has_perm('accounting.change_bankreconciliation'):
            messages.error(request, 'عفواً، لا تملك صلاحية تعديل التسويات البنكية.')
            return redirect('accounting:bank_reconciliation_detail', pk)
        action = request.POST.get('action')
        try:
            if action == 'reopen':
                svc.reopen(rec)
                messages.success(request, 'اتفتحت التسوية تاني كمسودة.')
            elif action == 'delete':
                if rec.status != BankReconciliation.DRAFT:
                    raise ValidationError('ينفع تحذف المسودة بس.')
                rec.delete()
                messages.success(request, 'اتحذفت مسودة التسوية.')
                return redirect('accounting:bank_reconciliation_list')
            else:
                rec = svc.save_ticks(rec, request.POST.getlist('items'), finish=(action == 'finish'),
                                     user=request.user)
                messages.success(request, 'اتنهت التسوية ✔' if rec.status == BankReconciliation.DONE
                                 else 'اتحفظت التسوية كمسودة.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        return redirect('accounting:bank_reconciliation_detail', pk)
    snap = svc.snapshot(rec) if rec.status == BankReconciliation.DRAFT else None
    cleared_rows = None
    if rec.status == BankReconciliation.DONE:
        cleared_rows = [{'item': i.journal_item, 'amount': i.journal_item.debit - i.journal_item.credit}
                        for i in rec.items.select_related('journal_item__entry').order_by('journal_item__entry__date')]
    return render(request, 'accounting/bank_reconciliation_detail.html', {
        'title': str(rec), 'rec': rec, 'snap': snap, 'cleared_rows': cleared_rows,
        'can_edit': request.user.has_perm('accounting.change_bankreconciliation'),
    })
