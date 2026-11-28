"""
The permissions an admin can give a user, grouped like the sidebar, each with a plain
Arabic sentence saying what it opens. This is THE list shown on the user screen:

- every permission the code checks (views, templates, dashboards) must be listed here —
  tests/test_permission_catalog.py scans the source and fails otherwise;
- permissions the code never checks are not shown (ticking them would do nothing);
- ROLE_DEFAULTS is the suggested starting set per role ("تطبيق صلاحيات الدور" button).

Admins (role ADMIN / platform superusers) always have everything.
"""

SECTIONS = [
    ('🛒', 'نقطة البيع والمبيعات', [
        ('invoicing.view_invoice', 'فتح نقطة البيع وفتح وردية، وعرض فواتير البيع والشراء'),
        ('invoicing.add_invoice', 'البيع من نقطة البيع وتقفيل الوردية'),
        ('invoicing.change_invoice', 'تأكيد فاتورة مسودة، تغيير طريقة الدفع، وخصم أكبر من الحد في نقطة البيع'),
        ('invoicing.cancel_invoice', 'إلغاء فاتورة مرحّلة'),
        ('invoicing.correct_closed_shift_payment', 'تصحيح طريقة سداد فاتورة في وردية مقفولة (قيد بدل تعديل الفاتورة)'),
        ('invoicing.add_sales_return', 'عمل مرتجع مبيعات من الفاتورة'),
        ('invoicing.add_sales_return_no_invoice', 'مرتجع مبيعات بدون فاتورة (بضاعة اتباعت قبل البرنامج)'),
    ]),
    ('🚚', 'مناديب المبيعات', [
        ('invoicing.view_salesrep', 'عرض المناديب وأدائهم ومخزون عرباتهم'),
        ('invoicing.add_salesrep', 'إضافة مندوب'),
        ('invoicing.change_salesrep', 'تعديل إعدادات المندوب (العربية، الخزنة، العمولة، التارجت)'),
        ('invoicing.add_repcommission', 'اعتماد عمولة المندوب الشهرية (بتتضاف للمسير)'),
        ('invoicing.use_rep_app', 'واجهة المندوب على الموبايل: البيع من العربية، التحصيل، طلب تحميل وتفريغ'),
        ('invoicing.view_loadingorder', 'عرض طلبات تحميل المناديب'),
        ('invoicing.approve_loadingorder', 'اعتماد طلب التحميل (وتعديل الكميات المعتمدة)'),
        ('invoicing.issue_loadingorder', 'صرف طلب التحميل من المخزن وترحيله على العربية'),
        ('invoicing.view_unloadingorder', 'عرض طلبات تفريغ المناديب'),
        ('invoicing.receive_unloadingorder', 'استلام طلب التفريغ في المخزن وترحيله'),
        ('invoicing.view_repcashhandover', 'عرض أرصدة خزن المناديب وتوريداتهم'),
        ('invoicing.add_repcashhandover', 'استلام توريد نقدية المندوب (وتحميله العجز)'),
    ]),
    ('📋', 'عروض الأسعار', [
        ('invoicing.view_quotation', 'عرض وطباعة عروض الأسعار'),
        ('invoicing.add_quotation', 'إنشاء عرض سعر'),
        ('invoicing.change_quotation', 'تعديل عرض سعر وتأكيده وتحويله لفاتورة'),
        ('invoicing.delete_quotation', 'حذف عرض سعر'),
    ]),
    ('📥', 'المشتريات', [
        ('invoicing.add_purchase_invoice', 'إنشاء فواتير الشراء ومرتجعات الشراء (من الفاتورة أو بدون فاتورة)'),
        ('invoicing.view_purchaseorder', 'عرض وطباعة أوامر الشراء'),
        ('invoicing.add_purchaseorder', 'إنشاء أوامر شراء (يدوي أو من اقتراح النواقص)'),
        ('invoicing.change_purchaseorder', 'تعليم أمر الشراء اتبعت / قفله / إلغاؤه'),
    ]),
    ('📦', 'المخزون والأصناف', [
        ('inventory.view_product', 'عرض الأصناف ولوحة المخزون'),
        ('inventory.sell_below_min_price', 'البيع بأقل من أدنى سعر بيع (نقطة البيع والمندوب)'),
        ('inventory.sell_expired_batch', 'بيع دفعات منتهية الصلاحية (من غير الصلاحية دي المنتهي ميتباعش)'),
        ('inventory.add_product', 'إضافة أصناف واستيرادها من إكسيل'),
        ('inventory.change_product', 'تعديل الأصناف وإدخال أرصدة أول المدة'),
        ('inventory.view_inventorybatch', 'عرض أرصدة المخزون والتحويلات المخزنية وتقارير التقييم والصلاحية'),
        ('inventory.change_inventorybatch', 'عمل تحويل مخزني بين المخازن'),
        ('inventory.view_stockadjustment', 'عرض تسويات الجرد'),
        ('inventory.add_stockadjustment', 'إنشاء تسوية جرد'),
        ('inventory.change_stockadjustment', 'ترحيل (تأكيد) تسوية جرد'),
        ('inventory.delete_stockadjustment', 'حذف تسوية جرد'),
        ('inventory.perform_stocktake', 'شاشة الجرد الفعلي واعتماد فروقات الجرد'),
        ('inventory.view_category', 'عرض الفئات'),
        ('inventory.add_category', 'إضافة فئة'),
        ('inventory.change_category', 'تعديل فئة'),
        ('inventory.view_unitofmeasure', 'عرض وحدات القياس'),
        ('inventory.add_unitofmeasure', 'إضافة وحدة قياس'),
        ('inventory.change_unitofmeasure', 'تعديل وحدة قياس'),
        ('inventory.view_warehouse', 'عرض المخازن'),
        ('inventory.add_warehouse', 'إضافة مخزن'),
        ('inventory.change_warehouse', 'تعديل مخزن'),
        ('inventory.delete_warehouse', 'حذف مخزن'),
    ]),
    ('🤝', 'العملاء والموردين', [
        ('partners.view_partner', 'عرض العملاء والموردين وكشوف الحساب'),
        ('partners.add_partner', 'إضافة عميل / مورد'),
        ('partners.change_partner', 'تعديل بيانات عميل / مورد'),
        ('pricing.view_customersector', 'عرض قطاعات العملاء'),
        ('pricing.add_customersector', 'إضافة قطاع عملاء'),
        ('pricing.change_customersector', 'تعديل قطاع عملاء'),
        ('pricing.delete_customersector', 'حذف قطاع عملاء'),
        ('pricing.view_pricelist', 'عرض قوائم الأسعار'),
        ('pricing.add_pricelist', 'إنشاء قائمة أسعار'),
        ('pricing.change_pricelist', 'تعديل قائمة أسعار وأصنافها'),
        ('pricing.delete_pricelist', 'حذف قائمة أسعار'),
    ]),
    ('💧', 'الخزائن والبنوك والسيولة', [
        ('accounting.view_treasury', 'عرض الخزائن ولوحة السيولة'),
        ('accounting.add_treasury', 'إضافة خزينة'),
        ('accounting.change_treasury', 'تعديل خزينة'),
        ('accounting.delete_treasury', 'حذف خزينة'),
        ('accounting.view_bankaccount', 'عرض الحسابات البنكية'),
        ('accounting.add_bankaccount', 'إضافة حساب بنكي'),
        ('accounting.change_bankaccount', 'تعديل حساب بنكي'),
        ('accounting.delete_bankaccount', 'حذف حساب بنكي'),
        ('accounting.view_ewallet', 'عرض المحافظ الإلكترونية'),
        ('accounting.add_ewallet', 'إضافة محفظة إلكترونية'),
        ('accounting.change_ewallet', 'تعديل محفظة إلكترونية'),
        ('accounting.delete_ewallet', 'حذف محفظة إلكترونية'),
        ('accounting.view_internaltransfer', 'عرض تحويلات الأموال'),
        ('accounting.add_internaltransfer', 'إنشاء تحويل أموال بين الخزائن والبنوك'),
        ('accounting.change_internaltransfer', 'ترحيل / إلغاء التحويل واستلام التحويلات الواردة من فرع تاني'),
        ('accounting.view_paymentmethod', 'عرض طرق الدفع'),
        ('accounting.add_paymentmethod', 'إضافة طريقة دفع'),
        ('accounting.change_paymentmethod', 'تعديل طريقة دفع'),
    ]),
    ('📒', 'الحسابات', [
        ('accounting.view_voucher', 'عرض سندات القبض والصرف'),
        ('accounting.add_voucher', 'إنشاء سند قبض / صرف'),
        ('accounting.change_voucher', 'تعديل وترحيل وإلغاء السندات'),
        ('accounting.view_fiscalyearclose', 'عرض إقفال السنوات المالية'),
        ('accounting.add_fiscalyearclose', 'إقفال / إعادة فتح سنة مالية'),
        ('accounting.view_taxsettlement', 'عرض التسوية الضريبية وأرصدة الضرائب'),
        ('accounting.add_taxsettlement', 'إقفال ضريبة القيمة المضافة وسداد الضرائب'),
        ('accounting.view_intermediarycompany', 'عرض شركات التحصيل الوسيطة (فوري، أمان…) وأرصدتها'),
        ('accounting.add_intermediarycompany', 'إضافة / إيقاف شركة تحصيل وسيطة'),
        ('accounting.add_intermediarysettlement', 'تسوية الشركة الوسيطة (تحويلها للبنك ورسومها)'),
        ('accounting.change_accountmapping', 'ربط الحسابات: تغيير الحساب اللي كل عملية بتترحّل عليه'),
        ('accounting.add_periodclose', 'إقفال الدفاتر لحد تاريخ (إقفال الشهور)'),
        ('accounting.reopen_periodclose', 'إعادة فتح فترة مقفولة (بسبب مكتوب)'),
        ('accounting.view_bankreconciliation', 'عرض التسويات البنكية'),
        ('accounting.add_bankreconciliation', 'بدء تسوية بنكية'),
        ('accounting.change_bankreconciliation', 'تعليم الحركات وإنهاء / فتح / حذف التسويات البنكية'),
        ('accounting.view_custody', 'عرض العهد النقدية'),
        ('accounting.add_custody', 'صرف عهدة نقدية لموظف'),
        ('accounting.change_custody', 'تسوية / إلغاء العهد'),
        ('accounting.view_expense', 'عرض المصروفات'),
        ('accounting.add_expense', 'تسجيل مصروف'),
        ('accounting.change_expense', 'ترحيل مصروف'),
        ('accounting.delete_expense', 'حذف مصروف'),
        ('accounting.view_journalentry', 'عرض القيود اليومية ولوحة المحاسبة'),
        ('accounting.add_journalentry', 'إنشاء قيد يدوي'),
        ('accounting.view_account', 'عرض شجرة الحسابات والميزانية وقائمة الدخل وميزان المراجعة'),
        ('accounting.add_account', 'إضافة حساب في الشجرة'),
        ('accounting.change_account', 'تعديل حساب في الشجرة'),
        ('accounting.view_cheque', 'عرض الشيكات'),
        ('accounting.add_cheque', 'تسجيل شيك'),
        ('accounting.change_cheque', 'تحصيل / صرف / رفض الشيكات'),
        ('accounting.view_chequebook', 'عرض دفاتر الشيكات'),
        ('accounting.add_chequebook', 'إضافة دفتر شيكات'),
        ('accounting.view_fixedasset', 'عرض الأصول الثابتة'),
        ('accounting.add_fixedasset', 'إضافة أصل ثابت'),
        ('accounting.change_fixedasset', 'تعديل الأصول وتشغيل الإهلاك'),
        ('accounting.view_loan', 'عرض القروض'),
        ('accounting.add_loan', 'إضافة قرض'),
        ('accounting.change_loan', 'سداد أقساط القروض'),
    ]),
    ('💳', 'خدمات الشحن والدفع', [
        ('accounting.view_posmachine', 'عرض ماكينات الدفع'),
        ('accounting.add_posmachine', 'إضافة ماكينة دفع'),
        ('accounting.change_posmachine', 'تعديل ماكينة دفع'),
        ('accounting.view_eservicetransaction', 'عرض حركات خدمات الدفع ولوحتها'),
        ('accounting.add_eservicetransaction', 'تسجيل حركة (شحن / سحب / إيداع) من مركز الخدمات'),
        ('accounting.change_eservicetransaction', 'ترحيل حركات خدمات الدفع'),
        ('accounting.cancel_eservicetransaction', 'إلغاء حركة خدمات دفع مرحّلة'),
        ('accounting.override_eservice_commission', 'تعديل عمولة الحركة يدوياً'),
        ('accounting.view_eservicecommissionrule', 'عرض قواعد عمولة الخدمات'),
        ('accounting.add_eservicecommissionrule', 'إضافة قاعدة عمولة خدمات'),
        ('accounting.change_eservicecommissionrule', 'تعديل قاعدة عمولة خدمات'),
        ('accounting.delete_eservicecommissionrule', 'حذف قاعدة عمولة خدمات'),
    ]),
    ('🧾', 'الفاتورة الإلكترونية', [
        ('einvoicing.view_einvoicelog', 'عرض سجل الفواتير الإلكترونية والفواتير الواردة من الموردين'),
        ('einvoicing.change_einvoicelog', 'تأكيد وإرسال الفواتير للضرائب وتحديث حالتها'),
        ('einvoicing.cancel_einvoice', 'إلغاء فاتورة مرسلة لدى الضرائب'),
        ('einvoicing.reject_received_einvoice', 'رفض فاتورة واردة من مورد'),
        ('einvoicing.manage_egs_codes', 'تسجيل أكواد الأصناف والفئات (EGS) لدى الضرائب'),
        ('einvoicing.change_taxintegrationsettings', 'تعديل إعدادات الربط مع الضرائب'),
    ]),
    ('📊', 'التقارير', [
        ('core.view_sales_reports', 'تقارير المبيعات والورديات'),
        ('core.view_purchases_reports', 'تقارير المشتريات'),
        ('core.view_inventory_reports', 'تقارير المخزون'),
        ('core.view_accounting_reports', 'التقارير المالية والضريبية'),
    ]),
    ('🛠️', 'الصيانة والعمولات', [
        ('maintenance.view_maintenanceticket', 'عرض تذاكر الصيانة'),
        ('maintenance.add_maintenanceticket', 'فتح تذكرة صيانة'),
        ('maintenance.change_maintenanceticket', 'تعديل وتسليم تذكرة صيانة'),
        ('maintenance.add_ticketpart', 'إضافة قطع غيار للتذكرة'),
        ('maintenance.delete_ticketpart', 'حذف قطع غيار من التذكرة'),
        ('maintenance.view_commissionrule', 'عرض قواعد العمولات'),
        ('maintenance.add_commissionrule', 'إضافة قاعدة عمولة'),
        ('maintenance.change_commissionrule', 'تعديل قاعدة عمولة'),
        ('maintenance.delete_commissionrule', 'حذف قاعدة عمولة'),
        ('maintenance.view_commissionrecord', 'تقرير العمولات'),
    ]),
    ('🚜', 'تأجير المعدات', [
        ('rentals.view_equipment', 'عرض المعدات'),
        ('rentals.add_equipment', 'إضافة معدة'),
        ('rentals.change_equipment', 'تعديل معدة'),
        ('rentals.view_rental', 'عرض عقود الإيجار وتقاريرها'),
        ('rentals.add_rental', 'إنشاء عقد إيجار'),
        ('rentals.change_rental', 'إنهاء / تعديل عقد إيجار'),
    ]),
    ('👥', 'الموارد البشرية', [
        ('hr.view_employeeprofile', 'عرض الموظفين وملفاتهم'),
        ('hr.change_employeeprofile', 'تعديل ملف الموظف ومستنداته وعهده العينية'),
        ('hr.change_department', 'إضافة الأقسام والوظائف'),
        ('hr.add_endofservice', 'مخالصة نهاية الخدمة وقفل حساب الموظف'),
        ('hr.change_loanrequest', 'صرف / رفض طلبات السلف من الموظفين'),
        ('hr.view_attendance', 'تقرير الحضور والغياب (أعمدة الرواتب تظهر لمن يملك عرض المسيرات)'),
        ('hr.change_attendance', 'إدخال وتعديل الحضور يدوياً (شيت الحضور اليومي وتسجيل فترة)'),
        ('hr.view_payroll', 'عرض مسيرات الرواتب'),
        ('hr.add_payroll', 'إعداد مسير رواتب'),
        ('hr.change_payroll', 'اعتماد مسير رواتب'),
        ('hr.add_payrollpayment', 'صرف المرتبات وسداد التأمينات والضرائب'),
        ('hr.change_payrolltaxsettings', 'إعدادات ضريبة كسب العمل والتأمينات في المسير'),
        ('hr.view_leaverequest', 'عرض الإجازات وأرصدتها'),
        ('hr.add_leaverequest', 'تسجيل طلب إجازة لموظف'),
        ('hr.change_leaverequest', 'اعتماد / رفض / إلغاء الإجازات'),
        ('hr.change_leavetype', 'تعديل أنواع الإجازات وأيامها'),
        ('hr.view_employeeloan', 'عرض سلف الموظفين وأقساطها'),
        ('hr.add_employeeloan', 'صرف سلفة لموظف من الخزينة'),
        ('hr.change_employeeloan', 'إلغاء سلفة لم يُخصم منها شيء'),
    ]),
    ('⚙️', 'الإعدادات والفروع', [
        ('core.view_systemsetting', 'فتح لوحة الإعدادات'),
        ('core.view_auditlog', 'سجل التعديلات (مين عدّل إيه وإمتى)'),
        ('core.view_branch', 'عرض الفروع'),
        ('core.add_branch', 'إضافة فرع'),
        ('core.change_branch', 'تعديل فرع'),
        ('core.delete_branch', 'حذف فرع'),
    ]),
]

ALL_PERMS = [code for _, _, items in SECTIONS for code, _ in items]
LABELS = {code: label for _, _, items in SECTIONS for code, label in items}

# Suggested starting permissions per role (the admin can still tick / untick).
_CASHIER = ['invoicing.view_invoice', 'invoicing.add_invoice', 'accounting.add_voucher', 'partners.view_partner',
            'partners.add_partner', 'invoicing.view_quotation', 'invoicing.add_quotation']
_INVENTORY = ['inventory.view_product', 'inventory.add_product', 'inventory.change_product',
              'inventory.view_inventorybatch', 'inventory.change_inventorybatch', 'inventory.view_stockadjustment',
              'inventory.add_stockadjustment', 'inventory.perform_stocktake', 'inventory.view_category', 'inventory.add_category',
              'inventory.change_category', 'inventory.view_unitofmeasure', 'inventory.add_unitofmeasure',
              'inventory.view_warehouse', 'invoicing.view_invoice', 'invoicing.add_purchase_invoice',
              'partners.view_partner', 'core.view_inventory_reports', 'core.view_purchases_reports',
              'invoicing.view_loadingorder', 'invoicing.approve_loadingorder', 'invoicing.issue_loadingorder',
              'invoicing.view_unloadingorder', 'invoicing.receive_unloadingorder']
_ACCOUNTANT = ([p for p in ALL_PERMS if p.startswith('accounting.') and '.delete_' not in p]
               + ['invoicing.view_invoice', 'invoicing.change_invoice', 'partners.view_partner',
                  'partners.change_partner', 'einvoicing.view_einvoicelog', 'einvoicing.change_einvoicelog',
                  'core.view_sales_reports', 'core.view_purchases_reports', 'core.view_accounting_reports',
                  'hr.view_payroll', 'hr.view_attendance', 'hr.view_employeeloan', 'hr.add_employeeloan',
                  'invoicing.view_salesrep', 'invoicing.view_repcashhandover', 'invoicing.add_repcashhandover'])
# A sales rep works only from his phone screen (invoicing.rep_app) and may add a customer there.
_SALES_REP = ['invoicing.use_rep_app', 'partners.add_partner']
_ADMIN_ONLY = {'core.view_systemsetting', 'core.view_auditlog', 'accounting.add_fiscalyearclose',
               'accounting.change_accountmapping', 'accounting.reopen_periodclose', 'core.add_branch', 'core.change_branch', 'core.delete_branch',
               'einvoicing.change_taxintegrationsettings'}
_MANAGER = [p for p in ALL_PERMS if p not in _ADMIN_ONLY]

ROLE_DEFAULTS = {
    'CASHIER': _CASHIER,
    'SALES_REP': _SALES_REP,
    'INVENTORY': _INVENTORY,
    'ACCOUNTANT': _ACCOUNTANT,
    'MANAGER': _MANAGER,
}


def catalog_permissions():
    """Permission rows of the catalog, keyed 'app.codename'."""
    from django.contrib.auth.models import Permission
    from django.db.models import Q
    q = Q()
    for code in ALL_PERMS:
        app, codename = code.split('.', 1)
        q |= Q(content_type__app_label=app, codename=codename)
    rows = Permission.objects.filter(q).select_related('content_type')
    return {f'{p.content_type.app_label}.{p.codename}': p for p in rows}


def grouped_for_form(checked_ids=()):
    """[{icon, title, items: [{id, code, label, checked}]}] in catalog order."""
    perms = catalog_permissions()
    checked_ids = set(checked_ids)
    out = []
    for icon, title, items in SECTIONS:
        rows = [{'id': perms[c].id, 'code': c, 'label': label, 'checked': perms[c].id in checked_ids}
                for c, label in items if c in perms]
        if rows:
            out.append({'icon': icon, 'title': title, 'items': rows})
    return out


def role_defaults_ids():
    """{role: [permission ids]} for the role preset button."""
    perms = catalog_permissions()
    return {role: [perms[c].id for c in codes if c in perms] for role, codes in ROLE_DEFAULTS.items()}
