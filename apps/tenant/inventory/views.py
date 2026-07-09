from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from .models import Product, InventoryBatch, StockMovement, Warehouse

@login_required
def stock_list(request):
    batches = InventoryBatch.objects.filter(quantity_remaining__gt=0).select_related('product', 'warehouse')
    warehouses = Warehouse.objects.filter(is_active=True)
    context = {'batches': batches, 'warehouses': warehouses, 'title': 'تتبع المخزون (FIFO)'}
    return render(request, 'inventory/stock.html', context)

@login_required
def product_list(request):
    products = Product.objects.all()
    current_branch = getattr(request, 'branch', None)
    
    products_list = list(products)
    for p in products_list:
        p.sale_price = p.get_price_for_branch(current_branch)
        
    context = {'products': products_list, 'title': 'المنتجات'}
    return render(request, 'inventory/products.html', context)

@login_required
def transfer_stock(request):
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        from_wh_id = request.POST.get('from_warehouse')
        to_wh_id = request.POST.get('to_warehouse')
        quantity = request.POST.get('quantity')
        
        from apps.tenant.services.fifo_engine import transfer_stock as ts
        from decimal import Decimal
        from django.contrib import messages
        
        try:
            qty = Decimal(quantity)
            product = Product.objects.get(id=product_id)
            from_wh = Warehouse.objects.get(id=from_wh_id)
            to_wh = Warehouse.objects.get(id=to_wh_id)
            
            ts(product, from_wh, to_wh, qty)
            messages.success(request, 'تم تحويل المخزون بنجاح')
            return redirect('inventory:stock_list')
        except Exception as e:
            messages.error(request, str(e))
    products = Product.objects.filter(is_active=True)
    warehouses = Warehouse.objects.filter(is_active=True)
    context = {'products': products, 'warehouses': warehouses, 'title': 'تحويل مخزني'}
    return render(request, 'inventory/transfer.html', context)

@login_required
def api_stock_level(request):
    product_id = request.GET.get('product_id')
    warehouse_id = request.GET.get('warehouse_id')
    
    try:
        product = Product.objects.get(id=product_id)
        wh = Warehouse.objects.get(id=warehouse_id) if warehouse_id else None
        return JsonResponse({'stock': str(product.get_stock(wh))})
    except Product.DoesNotExist:
        return JsonResponse({'error': 'Product not found'}, status=404)

@login_required
def valuation_report(request):
    batches = InventoryBatch.objects.filter(quantity_remaining__gt=0).select_related('product', 'warehouse')
    total_value = sum(b.total_value for b in batches)
    context = {'batches': batches, 'total_value': total_value, 'title': 'تقرير تقييم المخزون'}
    return render(request, 'inventory/valuation_report.html', context)

@login_required
def expiry_report(request):
    import datetime
    today = datetime.date.today()
    warning_date = today + datetime.timedelta(days=30)
    batches = InventoryBatch.objects.filter(quantity_remaining__gt=0, expiry_date__lte=warning_date).select_related('product', 'warehouse').order_by('expiry_date')
    context = {'batches': batches, 'title': 'تقرير الصلاحية والتوالف'}
    return render(request, 'inventory/expiry_report.html', context)


from django.views.generic import ListView, CreateView, UpdateView
from django.urls import reverse_lazy
from django.contrib.auth.mixins import LoginRequiredMixin
from .models import Warehouse, Category, Product, UnitOfMeasure
from .forms import UnitOfMeasureForm

# --- Warehouse Generic Views ---
class WarehouseListView(LoginRequiredMixin, ListView):
    model = Warehouse
    template_name = 'core/generic_list.html'
    context_object_name = 'objects'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إدارة المستودعات'
        context['create_url'] = reverse_lazy('inventory:warehouse_create')
        context['update_url_name'] = 'inventory:warehouse_update'
        return context

class WarehouseCreateView(LoginRequiredMixin, CreateView):
    model = Warehouse
    fields = ['branch', 'name', 'code', 'is_cold_storage', 'is_active', 'description']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('inventory:warehouse_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة مستودع جديد'
        context['cancel_url'] = reverse_lazy('inventory:warehouse_list')
        return context

class WarehouseUpdateView(LoginRequiredMixin, UpdateView):
    model = Warehouse
    fields = ['branch', 'name', 'code', 'is_cold_storage', 'is_active', 'description']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('inventory:warehouse_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل مستودع'
        context['cancel_url'] = reverse_lazy('inventory:warehouse_list')
        return context


# --- Category Generic Views ---
class CategoryListView(LoginRequiredMixin, ListView):
    model = Category
    template_name = 'core/generic_list.html'
    context_object_name = 'objects'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إدارة فئات المنتجات'
        context['create_url'] = reverse_lazy('inventory:category_create')
        context['update_url_name'] = 'inventory:category_update'
        return context

class CategoryCreateView(LoginRequiredMixin, CreateView):
    model = Category
    fields = ['name', 'parent', 'is_active', 'description']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('inventory:category_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة فئة جديدة'
        context['cancel_url'] = reverse_lazy('inventory:category_list')
        return context

class CategoryUpdateView(LoginRequiredMixin, UpdateView):
    model = Category
    fields = ['name', 'parent', 'is_active', 'description']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('inventory:category_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل فئة'
        context['cancel_url'] = reverse_lazy('inventory:category_list')
        return context


from .forms import ProductForm, ProductUoMFormSet

# --- Product Generic Views (List view already exists) ---
class ProductCreateView(LoginRequiredMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = 'inventory/product_form.html'
    success_url = reverse_lazy('inventory:product_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة منتج جديد'
        context['cancel_url'] = reverse_lazy('inventory:product_list')
        if self.request.POST:
            context['uom_formset'] = ProductUoMFormSet(self.request.POST)
        else:
            context['uom_formset'] = ProductUoMFormSet()
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        uom_formset = context['uom_formset']
        if uom_formset.is_valid():
            self.object = form.save()
            uom_formset.instance = self.object
            uom_formset.save()
            return super().form_valid(form)
        else:
            return self.render_to_response(self.get_context_data(form=form))

class ProductUpdateView(LoginRequiredMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = 'inventory/product_form.html'
    success_url = reverse_lazy('inventory:product_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل منتج'
        context['cancel_url'] = reverse_lazy('inventory:product_list')
        if self.request.POST:
            context['uom_formset'] = ProductUoMFormSet(self.request.POST, instance=self.object)
        else:
            context['uom_formset'] = ProductUoMFormSet(instance=self.object)
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        uom_formset = context['uom_formset']
        if uom_formset.is_valid():
            self.object = form.save()
            uom_formset.instance = self.object
            uom_formset.save()
            return super().form_valid(form)
        else:
            return self.render_to_response(self.get_context_data(form=form))

from django.shortcuts import get_object_or_404
from django.db import transaction
from django.contrib import messages
from apps.tenant.services.fifo_engine import consume_fifo_batches
from apps.tenant.accounting.models import Account, JournalEntry, JournalItem
from django.contrib.contenttypes.models import ContentType
from .models import StockAdjustment, StockAdjustmentLine
from decimal import Decimal
from django.core.exceptions import ValidationError

@login_required
def adjustment_list(request):
    adjustments = StockAdjustment.objects.all()
    return render(request, 'inventory/adjustment_list.html', {'adjustments': adjustments, 'title': 'تسويات المخزون'})

@login_required
def adjustment_create(request):
    if request.method == 'POST':
        warehouse_id = request.POST.get('warehouse')
        reason = request.POST.get('reason')
        date_str = request.POST.get('date')
        adjustment_type = request.POST.get('adjustment_type')
        
        warehouse = Warehouse.objects.get(id=warehouse_id)
        
        adj = StockAdjustment.objects.create(
            warehouse=warehouse,
            adjustment_type=adjustment_type,
            reason=reason,
            date=date_str,
            created_by=request.user,
            status='DRAFT'
        )
        
        product_ids = request.POST.getlist('product_id[]')
        quantities = request.POST.getlist('quantity[]')
        unit_costs = request.POST.getlist('unit_cost[]')
        
        for pid, qty, cost in zip(product_ids, quantities, unit_costs):
            if pid and qty:
                product = Product.objects.get(id=pid)
                final_cost = Decimal(cost) if cost else product.get_average_cost(warehouse)
                StockAdjustmentLine.objects.create(
                    adjustment=adj,
                    product=product,
                    quantity=Decimal(qty),
                    unit_cost=final_cost
                )
                
        messages.success(request, 'تم إنشاء التسوية بنجاح كمسودة.')
        return redirect('inventory:adjustment_list')
        
    warehouses = Warehouse.objects.filter(is_active=True)
    products = Product.objects.filter(is_active=True)
    return render(request, 'inventory/adjustment_form.html', {
        'warehouses': warehouses,
        'products': products,
        'title': 'إضافة تسوية مخزون جديدة'
    })

@login_required
@transaction.atomic
def adjustment_confirm(request, pk):
    adj = get_object_or_404(StockAdjustment, pk=pk, status='DRAFT')
    
    if request.method == 'POST':
        total_cost = Decimal('0')
        
        # Process stock changes
        for line in adj.lines.all():
            if adj.adjustment_type == 'OUT':
                try:
                    consumptions = consume_fifo_batches(
                        product=line.product,
                        warehouse=adj.warehouse,
                        quantity_needed=line.quantity,
                        reference=f'تسوية {adj.id}',
                        notes=adj.reason
                    )
                    line_total = sum(c['total_cost'] for c in consumptions)
                    total_cost += line_total
                except ValueError as e:
                    messages.error(request, str(e))
                    return redirect('inventory:adjustment_list')
            else:
                # IN or OPENING: Create new batch
                from .models import InventoryBatch
                batch = InventoryBatch.objects.create(
                    product=line.product,
                    warehouse=adj.warehouse,
                    quantity_original=line.quantity,
                    quantity_remaining=line.quantity,
                    unit_cost=line.unit_cost,
                    batch_number=f'تسوية {adj.id}'
                )
                m_type = 'ADJUSTMENT_IN' if adj.adjustment_type == 'IN' else 'OPENING_BALANCE'
                StockMovement.objects.create(
                    product=line.product,
                    warehouse=adj.warehouse,
                    batch=batch,
                    movement_type=m_type,
                    quantity=line.quantity,
                    unit_cost=line.unit_cost,
                    reference=f'تسوية {adj.id}',
                    notes=adj.reason
                )
                total_cost += (line.quantity * line.unit_cost)

        # Update movements to ADJUSTMENT_OUT if OUT
        if adj.adjustment_type == 'OUT':
            StockMovement.objects.filter(
                reference=f'تسوية {adj.id}',
                movement_type='OUT'
            ).update(movement_type='ADJUSTMENT_OUT')
        
        # Journal Entry
        if total_cost > 0:
            inventory_acc, _ = Account.objects.get_or_create(
                code='1140', defaults={'name': 'المخزون', 'account_type': Account.ASSET, 'is_active': True}
            )
            
            if adj.adjustment_type == 'OUT':
                offset_acc, _ = Account.objects.get_or_create(
                    code='5999', defaults={'name': 'مصروفات التالف والعجز (تسويات)', 'account_type': Account.EXPENSE, 'is_active': True}
                )
                debit_acc = offset_acc
                credit_acc = inventory_acc
                desc = f'عجز بضاعة تسوية {adj.id}'
            elif adj.adjustment_type == 'IN':
                offset_acc, _ = Account.objects.get_or_create(
                    code='4999', defaults={'name': 'إيرادات تسويات بالزيادة', 'account_type': Account.REVENUE, 'is_active': True}
                )
                debit_acc = inventory_acc
                credit_acc = offset_acc
                desc = f'زيادة بضاعة تسوية {adj.id}'
            else:
                offset_acc, _ = Account.objects.get_or_create(
                    code='3999', defaults={'name': 'أرصدة افتتاحية', 'account_type': Account.EQUITY, 'is_active': True}
                )
                debit_acc = inventory_acc
                credit_acc = offset_acc
                desc = f'رصيد افتتاحي بضاعة تسوية {adj.id}'

            je = JournalEntry.objects.create(
                date=adj.date,
                reference=f'ADJ-{adj.id}',
                description=f'تسوية مخزون رقم {adj.id}: {adj.reason}',
                status=JournalEntry.DRAFT,
                created_by=request.user,
                content_type=ContentType.objects.get_for_model(StockAdjustment),
                object_id=adj.id
            )
            
            JournalItem.objects.create(entry=je, account=debit_acc, debit=total_cost, credit=Decimal('0'), description=desc)
            JournalItem.objects.create(entry=je, account=credit_acc, debit=Decimal('0'), credit=total_cost, description=desc)
            
            try:
                je.post()
            except ValidationError as e:
                messages.error(request, f'حدث خطأ محاسبي: {e.message if hasattr(e, "message") else str(e)}')
                return redirect('inventory:adjustment_list')
            
        adj.status = 'POSTED'
        adj.save()
        messages.success(request, 'تم ترحيل التسوية بنجاح وتحديث المخزون والحسابات.')
        
    return redirect('inventory:adjustment_list')

# --- Unit of Measure Generic Views ---
class UnitOfMeasureListView(LoginRequiredMixin, ListView):
    model = UnitOfMeasure
    template_name = 'inventory/uom_list.html'
    context_object_name = 'uoms'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'وحدات القياس'
        return context

class UnitOfMeasureCreateView(LoginRequiredMixin, CreateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'inventory/uom_form.html'
    success_url = reverse_lazy('inventory:uom_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة وحدة قياس'
        context['cancel_url'] = self.success_url
        return context

class UnitOfMeasureUpdateView(LoginRequiredMixin, UpdateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'inventory/uom_form.html'
    success_url = reverse_lazy('inventory:uom_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل وحدة قياس'
        context['cancel_url'] = self.success_url
        return context
