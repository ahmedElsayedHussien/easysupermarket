import re
import sys
try:
    with open('e:/easysupermarket/apps/tenant/inventory/views.py', 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    start_idx = -1
    for i, line in enumerate(lines):
        if 'def api_products(request):' in line:
            start_idx = i - 1 # @login_required
            break
            
    if start_idx == -1:
        print("Not found")
        sys.exit(1)
        
    api_func = """@login_required
def api_products(request):
    warehouse_id = request.GET.get('warehouse_id')
    wh = None
    if warehouse_id:
        try:
            wh = Warehouse.objects.get(id=warehouse_id)
        except Warehouse.DoesNotExist:
            pass
            
    products = Product.objects.filter(is_active=True).select_related('category').prefetch_related('uoms__uom')
    current_branch = None
    branch_id = request.GET.get('branch_id')
    if branch_id:
        from apps.tenant.core.models import Branch
        current_branch = Branch.objects.filter(id=branch_id, is_active=True).first()
    
    if not current_branch:
        current_branch = getattr(request, 'branch', None)
    
    products_data = []
    for p in products:
        products_data.append({
            'id': p.id,
            'name': p.name,
            'barcode': p.barcode or '',
            'sale_price': float(p.get_price_for_branch(current_branch)),
            'available_stock': float(p.get_stock(warehouse=wh)),
            'tax_rate': float(p.tax_rate) if hasattr(p, 'tax_rate') and p.tax_rate else (float(p.category.tax_rate) if p.category and hasattr(p.category, 'tax_rate') and p.category.tax_rate else 14),
            'image': p.image.url if p.image else None,
            'uoms': [{'id': 'base', 'name': p.pos_unit_name, 'factor': 1.0}] + [{'id': pu.id, 'name': pu.uom.name, 'factor': float(pu.conversion_factor)} for pu in p.uoms.all()]
        })
        
    return JsonResponse({'products': products_data})
"""
    
    new_lines = lines[:start_idx] + [api_func]
    with open('e:/easysupermarket/apps/tenant/inventory/views.py', 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print("Fixed successfully")
except Exception as e:
    print("Error:", e)
