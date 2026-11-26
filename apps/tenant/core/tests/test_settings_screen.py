"""System settings screen, branch POS defaults form and the main-screen low-stock count."""
import datetime
import io
import shutil
import tempfile
from decimal import Decimal

from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.tenant.core.forms import BranchForm, SystemSettingForm
from apps.tenant.core.models import Branch, SystemSetting
from apps.tenant.core.tests.factories import (
    build_world, login, make_bank_account, make_branch, make_ewallet, make_treasury, stock_in,
    system_settings,
)

D = Decimal
REMOVED_NAV = ['inventory_fifo', 'stocktake', 'adjustments', 'products', 'uom', 'suppliers', 'customers',
               'sectors', 'partners_report', 'treasuries', 'banks', 'ewallets']


def form_post_data(form):
    """POST payload reproducing ``form``'s current values (what the browser would send)."""
    data = {}
    for name, field in form.fields.items():
        if isinstance(field, forms.FileField):
            continue
        value = form[name].value()
        if isinstance(field.widget, forms.RadioSelect):
            data[name] = 'True' if value in (True, 'True') else 'False'
        elif isinstance(field.widget, forms.CheckboxInput):
            if value:
                data[name] = 'on'
        elif isinstance(value, (list, tuple)):
            data[name] = [str(v) for v in value]
        elif value is None:
            data[name] = ''
        elif isinstance(value, datetime.date):
            data[name] = value.isoformat()
        else:
            data[name] = str(value)
    return data


def png_bytes(size=(4, 4)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', size, (255, 115, 0)).save(buf, format='PNG')
    return buf.getvalue()


class SettingsScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.url = reverse('core:system_settings')

    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        login(self.client, self.w.admin)

    def _initial_data(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        return form_post_data(resp.context['form'])

    def test_page_renders_for_admin_with_every_field(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('enctype="multipart/form-data"', html)
        for name in resp.context['form'].fields:
            self.assertIn(f'name="{name}"', html, f'field {name} is not rendered (would be reset on save)')
        for name in REMOVED_NAV:
            self.assertNotIn(f'show_nav_{name}', html)
        self.assertIn(reverse('accounting:fiscal_years'), html)        # lock date: read-only + link
        self.assertIn('الافتراضي العام (يُستخدم عند عدم تحديد افتراضي للفرع)', html)
        self.assertIn(reverse('core:branch_list'), html)
        self.assertIn('حد طلب افتراضي للأصناف التي ليس لها حد خاص', html)
        self.assertIn('👁️ إظهار', html)
        self.assertIn('<option value="0.00" selected>', html)

    def test_cashier_cannot_open_settings(self):
        self.client.logout()
        login(self.client, self.w.cashier)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_weekly_off_days_initial_from_csv(self):
        system_settings(weekly_off_days='4,5')
        form = SystemSettingForm(instance=SystemSetting.get_settings())
        self.assertEqual(sorted(form.initial['weekly_off_days']), ['4', '5'])
        html = str(form['weekly_off_days'])
        self.assertEqual(html.count('type="checkbox"'), 7)
        self.assertIn('السبت', html)

    def test_post_saves_new_fields(self):
        data = self._initial_data()
        data.update({
            'weekly_off_days': ['4', '5'],
            'pos_cash_rounding': '0.25',
            'receipt_paper_width': '58',
            'accounting_lock_date': '2026-06-30',      # ignored: moves only via accounting.periods
            'stagnant_stock_days': '90',
            'expiry_warning_days': '45',
            'dormant_debt_days': '120',
            'credit_limit_warning_percent': '75',
            'payroll_day_basis': 'WORKING_DAYS',
            'late_grace_minutes': '10',
            'session_timeout_hours': '8',
            'login_max_attempts': '6',
            'login_lockout_minutes': '30',
            'auto_backup_enabled': 'on',
            'auto_backup_retention': '14',
            'show_nav_inventory_dashboard': 'False',
            'show_nav_liquidity_dashboard': 'True',
        })
        data.pop('show_branch_info_on_receipt', None)  # unchecked
        data['company_logo'] = SimpleUploadedFile('logo.png', png_bytes(), content_type='image/png')
        resp = self.client.post(self.url, data)
        self.assertEqual(resp.status_code, 302, getattr(resp, 'context', None) and resp.context['form'].errors)

        s = SystemSetting.objects.get(pk=1)
        self.assertEqual(s.weekly_off_days, '4,5')
        self.assertEqual(s.pos_cash_rounding, D('0.25'))
        self.assertEqual(s.receipt_paper_width, '58')
        self.assertIsNone(s.accounting_lock_date)
        self.assertEqual(s.stagnant_stock_days, 90)
        self.assertEqual(s.dormant_debt_days, 120)
        self.assertEqual(s.credit_limit_warning_percent, 75)
        self.assertEqual(s.payroll_day_basis, 'WORKING_DAYS')
        self.assertEqual(s.late_grace_minutes, 10)
        self.assertEqual(s.session_timeout_hours, 8)
        self.assertEqual(s.login_max_attempts, 6)
        self.assertEqual(s.login_lockout_minutes, 30)
        self.assertTrue(s.auto_backup_enabled)
        self.assertEqual(s.auto_backup_retention, 14)
        self.assertFalse(s.show_branch_info_on_receipt)
        self.assertFalse(s.show_nav_inventory_dashboard)
        self.assertTrue(s.show_nav_liquidity_dashboard)
        self.assertTrue(s.show_nav_dashboard)  # untouched toggles keep their value
        self.assertTrue(s.company_logo.name.startswith('company/'))

        # Round-trip: the saved CSV is shown checked again, rounding option pre-selected.
        resp = self.client.get(self.url)
        form = resp.context['form']
        self.assertEqual(sorted(form['weekly_off_days'].value()), ['4', '5'])
        self.assertEqual(str(form['pos_cash_rounding'].value()), '0.25')
        html = resp.content.decode()
        self.assertIn('مفيش فترة مقفولة', html)                    # lock date shown read-only
        self.assertIn('/media/company/', html)

        # Clearing every day off stores an empty CSV.
        data = form_post_data(form)
        data['weekly_off_days'] = []
        resp = self.client.post(self.url, data)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(SystemSetting.objects.get(pk=1).weekly_off_days, '')

    def test_rejects_out_of_range_values(self):
        data = self._initial_data()
        data.update({'login_max_attempts': '1', 'credit_limit_warning_percent': '150',
                     'stagnant_stock_days': '0', 'weekly_off_days': [str(i) for i in range(7)],
                     'pos_cash_rounding': '0.10'})
        resp = self.client.post(self.url, data)
        self.assertEqual(resp.status_code, 200)
        errors = resp.context['form'].errors
        for name in ('login_max_attempts', 'credit_limit_warning_percent', 'stagnant_stock_days',
                     'weekly_off_days', 'pos_cash_rounding'):
            self.assertIn(name, errors)
        self.assertIn('field-error', resp.content.decode())
        self.assertEqual(SystemSetting.objects.get(pk=1).login_max_attempts, 5)

    def test_rejects_bad_logo(self):
        data = self._initial_data()
        data['company_logo'] = SimpleUploadedFile('logo.gif', b'GIF89a\x01\x00\x01\x00\x00\x00\x00;',
                                                  content_type='image/gif')
        resp = self.client.post(self.url, data)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('company_logo', resp.context['form'].errors)

    def test_rejects_large_logo(self):
        form = SystemSettingForm(instance=SystemSetting.get_settings())
        big = SimpleUploadedFile('big.png', png_bytes(), content_type='image/png')
        big.size = 3 * 1024 * 1024
        form.cleaned_data = {'company_logo': big}
        with self.assertRaises(forms.ValidationError):
            form.clean_company_logo()


class BranchFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.other_branch = make_branch('فرع آخر', code='OTHER')
        cls.other_treasury = make_treasury(cls.other_branch, 'خزينة الفرع الآخر')
        cls.bank = make_bank_account('بنك مصر')
        cls.wallet = make_ewallet('فودافون كاش')

    def setUp(self):
        login(self.client, self.w.admin)

    def _data(self, branch, **extra):
        data = {'name': branch.name, 'code': branch.code, 'address': '', 'phone': '', 'is_active': 'on',
                'default_pos_treasury': '', 'default_pos_bank_account': '',
                'default_pos_instapay_account': '', 'default_pos_ewallet': ''}
        data.update(extra)
        return data

    def test_treasury_choices_limited_to_branch(self):
        form = BranchForm(instance=self.w.branch)
        self.assertEqual(list(form.fields['default_pos_treasury'].queryset), [self.w.treasury])
        self.assertIn(self.bank, form.fields['default_pos_bank_account'].queryset)
        self.assertIn(self.wallet, form.fields['default_pos_ewallet'].queryset)

    def test_create_form_has_no_treasury_choices(self):
        form = BranchForm()
        self.assertFalse(form.fields['default_pos_treasury'].queryset.exists())
        self.assertIn('احفظ الفرع أولاً ثم أضف خزينته', form.fields['default_pos_treasury'].help_text)

    def test_rejects_other_branch_treasury(self):
        form = BranchForm(self._data(self.w.branch, default_pos_treasury=str(self.other_treasury.pk)),
                          instance=self.w.branch)
        self.assertFalse(form.is_valid())
        self.assertIn('default_pos_treasury', form.errors)

        resp = self.client.post(reverse('core:branch_update', args=[self.w.branch.pk]),
                                self._data(self.w.branch, default_pos_treasury=str(self.other_treasury.pk)))
        self.assertEqual(resp.status_code, 200)
        self.w.branch.refresh_from_db()
        self.assertIsNone(self.w.branch.default_pos_treasury_id)

    def test_update_saves_pos_defaults(self):
        resp = self.client.post(reverse('core:branch_update', args=[self.w.branch.pk]), self._data(
            self.w.branch, default_pos_treasury=str(self.w.treasury.pk),
            default_pos_bank_account=str(self.bank.pk), default_pos_instapay_account=str(self.bank.pk),
            default_pos_ewallet=str(self.wallet.pk)))
        self.assertEqual(resp.status_code, 302)
        b = Branch.objects.get(pk=self.w.branch.pk)
        self.assertEqual(b.default_pos_treasury, self.w.treasury)
        self.assertEqual(b.default_pos_bank_account, self.bank)
        self.assertEqual(b.default_pos_instapay_account, self.bank)
        self.assertEqual(b.default_pos_ewallet, self.wallet)

    def test_create_and_update_pages_render(self):
        resp = self.client.get(reverse('core:branch_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'حسابات الدفع الافتراضية لنقطة البيع بالفرع')
        resp = self.client.get(reverse('core:branch_update', args=[self.w.branch.pk]))
        self.assertContains(resp, self.w.treasury.name)
        self.assertNotContains(resp, self.other_treasury.name)

    def test_create_branch(self):
        resp = self.client.post(reverse('core:branch_create'), {
            'name': 'فرع جديد', 'code': 'NEW1', 'address': '', 'phone': '', 'is_active': 'on',
            'default_pos_treasury': str(self.other_treasury.pk),  # ignored: disabled on create
            'default_pos_bank_account': str(self.bank.pk),
        })
        self.assertEqual(resp.status_code, 302)
        b = Branch.objects.get(code='NEW1')
        self.assertIsNone(b.default_pos_treasury_id)
        self.assertEqual(b.default_pos_bank_account, self.bank)


class MainScreenLowStockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        login(self.client, self.w.admin)

    def _count(self):
        resp = self.client.get(reverse('core:main_screen'))
        self.assertEqual(resp.status_code, 200)
        return resp.context['low_stock_count']

    def test_uses_product_level_then_global_threshold(self):
        p1, p2 = self.w.product, self.w.product2
        stock_in(p1, self.w.warehouse, 3, 5)   # 3 units
        stock_in(p2, self.w.warehouse, 8, 5)   # 8 units
        p1.min_stock_level = D('0'); p1.save(update_fields=['min_stock_level'])
        p2.min_stock_level = D('10'); p2.save(update_fields=['min_stock_level'])

        system_settings(low_stock_threshold=5)
        self.assertEqual(self._count(), 2)   # p1: 3 <= 5 (global), p2: 8 <= 10 (own)

        system_settings(low_stock_threshold=2)
        self.assertEqual(self._count(), 1)   # only p2

        system_settings(low_stock_threshold=0)
        self.assertEqual(self._count(), 1)   # threshold 0 = p1 not tracked
