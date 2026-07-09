from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Partner, Payment

@login_required
def supplier_list(request):
    suppliers = Partner.objects.filter(partner_type__in=['SUPPLIER', 'BOTH']).select_related('account', 'receivable_account', 'payable_account')
    for supplier in suppliers:
        supplier.balance = supplier.outstanding_balance
    context = {'partners': suppliers, 'title': 'الموردين', 'type': 'supplier'}
    return render(request, 'partners/list.html', context)

@login_required
def customer_list(request):
    customers = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH']).select_related('account', 'receivable_account', 'payable_account')
    for customer in customers:
        customer.balance = customer.outstanding_balance
    context = {'partners': customers, 'title': 'العملاء', 'type': 'customer'}
    return render(request, 'partners/list.html', context)

from django.contrib import messages

@login_required
def partner_create(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        phone = request.POST.get('phone')
        tax_id = request.POST.get('tax_id')
        partner_type = request.POST.get('partner_type')
        
        Partner.objects.create(
            name=name,
            phone=phone,
            tax_id=tax_id,
            partner_type=partner_type
        )
        messages.success(request, 'تم إضافة الشريك بنجاح!')
        if partner_type == 'SUPPLIER':
            return redirect('partners:supplier_list')
        else:
            return redirect('partners:customer_list')
            
    return render(request, 'partners/form.html', {'title': 'إضافة شريك جديد'})

@login_required
def partner_edit(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    if request.method == 'POST':
        partner.name = request.POST.get('name')
        partner.phone = request.POST.get('phone')
        partner.tax_id = request.POST.get('tax_id')
        partner.partner_type = request.POST.get('partner_type')
        partner.save()
        messages.success(request, 'تم تحديث بيانات الشريك بنجاح!')
        if partner.partner_type == 'SUPPLIER':
            return redirect('partners:supplier_list')
        else:
            return redirect('partners:customer_list')
            
    return render(request, 'partners/form.html', {'partner': partner, 'title': 'تعديل الشريك'})

@login_required
def post_payment_view(request):
    if request.method == 'POST':
        # Logic to create and post payment using journal_service
        pass
    return redirect('core:main_screen')

@login_required
def partner_ledger(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    from apps.tenant.accounting.models import JournalItem
    
    # We will get all journal items for this partner's receivable/payable accounts
    items = JournalItem.objects.none()
    
    accounts = []
    if partner.receivable_account_id: accounts.append(partner.receivable_account_id)
    if partner.payable_account_id: accounts.append(partner.payable_account_id)
    
    if accounts:
        items = JournalItem.objects.filter(account_id__in=accounts, entry__status='POSTED').order_by('entry__date', 'id')
        
    context = {
        'partner': partner,
        'items': items,
        'title': f'كشف حساب: {partner.name}'
    }
    return render(request, 'partners/ledger.html', context)
