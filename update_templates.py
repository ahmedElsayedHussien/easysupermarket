import os
import re

def wrap_arabic(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Let's add {% load i18n %} to the top if not present
    if '{% load' not in content:
        content = '{% load i18n %}\n' + content
    elif '{% load i18n' not in content and '{% load' in content:
        content = content.replace('{% load ', '{% load i18n ')

    arabic_phrases = [
        'القائمة', 'ميزا', 'كل الفروع', 'مدير النظام', 'مشرف', 'كاشير',
        'التنقل السريع', 'لوحة التحكم', 'نقطة البيع', 'المبيعات', 'فواتير المبيعات',
        'المشتريات', 'فاتورة شراء', 'المخزون', 'تتبع المخزون FIFO', 'الأصناف',
        'الشركاء', 'الموردون', 'العملاء', 'المحاسبة', 'قيود اليومية', 'دليل الحسابات',
        'الإدارة', 'لوحة المدير', 'الفروع والكيانات', 'سياق المستخدم', 'الفرع',
        'المخزن', 'الدور', 'إيرادات اليوم', 'ج.م', 'نقدي', 'بطاقة', 'محفظة',
        'إحصاء سريع', 'فواتير اليوم', 'المخزون المنخفض', 'مستخدمون نشطون',
        'المركز الرئيسي', 'نقطة البيع (كاشير)', 'تحديث سريع', 'بحث عن صنف (F3)',
        'باركود، اسم، كود...', 'مسح (F4)', 'تعليق (F5)', 'دفع (F12)', 'الإجمالي',
        'الضريبة', 'الصافي', 'طريقة الدفع', 'أدخل المبلغ المستلم', 'الباقي',
        'تأكيد الدفع', 'إلغاء', 'عنصر', 'السعر', 'الكمية', 'المجموع', 'إدارة المبيعات',
        'إدارة المشتريات', 'إدارة المخزون', 'المرتجعات', 'الفروع', 'الحسابات والمالية',
        'التقارير', 'إعدادات النظام', 'مبيعات اليوم', 'الأصناف في المخزون',
        'عن أمس', 'فعال', 'يحتاج مراجعة', 'مكتملة', 'اجمالي', 'صنف', 'إجمالي', 'ميزا ERP - MIRA MARKET',
        'اختر الفرع', 'Search modules... ابحث عن وحدة',
        'إدارة الفروع', 'مشتريات', 'نظام ميزا ERP', 'كود', 'الباركود', 'المتاح', 'وحدة',
        'الخصم', 'لا توجد منتجات', 'أضف منتجات للسلة', 'سداد', 'طباعة الباركود', 'حفظ',
        'الدفع النقدي', 'الإجمالي المطلوب:', 'المبلغ المدفوع:', 'تأكيد', 'تمت العملية بنجاح',
        'طباعة الإيصال', 'بيع جديد'
    ]
    # Sort phrases by length descending to avoid partial matches
    arabic_phrases.sort(key=len, reverse=True)

    for phrase in arabic_phrases:
        if phrase in content:
            # We want to replace exactly the phrase when it's text between tags, or in quotes
            # Let's use regex to find it outside of django tags
            # Actually simple replacement might be easier but we have to be careful not to replace already translated ones
            # Check if it's already translated: "{% trans 'Phrase' %}"
            # We temporarily replace the target phrase
            # We only replace if it's not already enclosed in ' or " with {% trans
            
            # Simple replace logic
            # Find all occurrences of the phrase
            content = content.replace(f">{{phrase}}<", f">{{% trans '{phrase}' %}}<")
            content = content.replace(f"'{phrase}'", f"\"{{% trans '{phrase}' %}}\"")
            content = content.replace(f'"{phrase}"', f'"{{% trans \'{phrase}\' %}}"')
            content = content.replace(f"title=\"{phrase}\"", f"title=\"{{% trans '{phrase}' %}}\"")
            content = content.replace(f"placeholder=\"{phrase}\"", f"placeholder=\"{{% trans '{phrase}' %}}\"")
            content = content.replace(f" {phrase} ", f" {{% trans '{phrase}' %}} ")
            # Catch tags like <span>Phrase</span> -> <span>{% trans 'Phrase' %}</span>
            # since >Phrase< captures that
            
            # Also catch phrases inside <button>Phrase<br>
            content = content.replace(f">{phrase}<br>", f">{{% trans '{phrase}' %}}<br>")
            
            # Catch phrases at the end of lines or before <
            # For simplicity, let's use re.sub with word boundaries? Arabic word boundaries are tricky.
            # Using specific known patterns is safer.
            content = re.sub(rf"(>|\s){phrase}(<|\s)", rf"\1{{% trans '{phrase}' %}}\2", content)

    # Some manual fixes if anything is duplicated like {% trans '{% trans
    content = content.replace("{% trans '{% trans", "{% trans")
    content = content.replace("%}' %}", "%}")

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

for f in ['e:/easysupermarket/templates/base.html', 'e:/easysupermarket/templates/pos/index.html', 'e:/easysupermarket/templates/dashboard/main.html']:
    wrap_arabic(f)
print("Done updating templates.")
