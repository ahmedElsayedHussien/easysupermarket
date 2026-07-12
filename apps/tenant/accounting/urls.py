from django.urls import path
from . import views

app_name = 'accounting'
urlpatterns = [
    # Journal Entries
    path('journals/', views.JournalEntryListView.as_view(), name='journal_list'),
    path('journals/<int:pk>/', views.JournalEntryDetailView.as_view(), name='journal_detail'),
    
    # Reports
    path('chart-of-accounts/', views.chart_of_accounts, name='chart_of_accounts'),
    path('setup-default-accounts/', views.setup_default_accounts, name='setup_default_accounts'),
    path('trial-balance/', views.trial_balance, name='trial_balance'),
    path('income-statement/', views.income_statement, name='income_statement'),
    path('balance-sheet/', views.balance_sheet, name='balance_sheet'),
    

    # Payment Methods
    path('payment-methods/', views.PaymentMethodListView.as_view(), name='payment_method_list'),
    path('payment-methods/create/', views.PaymentMethodCreateView.as_view(), name='payment_method_create'),
    path('payment-methods/<int:pk>/update/', views.PaymentMethodUpdateView.as_view(), name='payment_method_update'),
    
    # Treasuries
    path('treasuries/', views.TreasuryListView.as_view(), name='treasury_list'),
    path('treasuries/create/', views.TreasuryCreateView.as_view(), name='treasury_create'),
    path('treasuries/<int:pk>/update/', views.TreasuryUpdateView.as_view(), name='treasury_update'),
    path('treasuries/<int:pk>/delete/', views.TreasuryDeleteView.as_view(), name='treasury_delete'),

    # Bank Accounts
    path('bank-accounts/', views.BankAccountListView.as_view(), name='bank_list'),
    path('bank-accounts/create/', views.BankAccountCreateView.as_view(), name='bank_create'),
    path('bank-accounts/<int:pk>/update/', views.BankAccountUpdateView.as_view(), name='bank_update'),
    path('bank-accounts/<int:pk>/delete/', views.BankAccountDeleteView.as_view(), name='bank_delete'),

    # E-Wallets
    path('ewallets/', views.EWalletListView.as_view(), name='ewallet_list'),
    path('ewallets/create/', views.EWalletCreateView.as_view(), name='ewallet_create'),
    path('ewallets/<int:pk>/update/', views.EWalletUpdateView.as_view(), name='ewallet_update'),
    path('ewallets/<int:pk>/delete/', views.EWalletDeleteView.as_view(), name='ewallet_delete'),

    # Expenses
    path('expenses/', views.ExpenseListView.as_view(), name='expense_list'),
    path('expenses/create/', views.ExpenseCreateView.as_view(), name='expense_create'),
    path('expenses/<int:pk>/', views.ExpenseDetailView.as_view(), name='expense_detail'),
    path('expenses/<int:pk>/confirm/', views.ExpenseConfirmView.as_view(), name='expense_confirm'),
    path('expenses/<int:pk>/delete/', views.ExpenseDeleteView.as_view(), name='expense_delete'),
    
    # Receipts (سندات القبض)
    path('receipts/', views.ReceiptListView.as_view(), name='receipt_list'),
    path('receipts/create/', views.VoucherCreateView.as_view(), name='receipt_create'),
    
    # Payments (سندات الصرف)
    path('payments/', views.PaymentListView.as_view(), name='payment_list'),
    path('payments/create/', views.VoucherCreateView.as_view(), name='payment_create'),
    
    # Common Voucher endpoints
    path('vouchers/<int:pk>/', views.VoucherDetailView.as_view(), name='voucher_detail'),
    path('vouchers/<int:pk>/confirm/', views.VoucherConfirmView.as_view(), name='voucher_confirm'),

    # POS Machines (ماكينات الدفع الإلكتروني)
    path('pos-machines/', views.pos_machine_list, name='pos_machine_list'),
    path('pos-machines/create/', views.pos_machine_create, name='pos_machine_create'),
    path('pos-machines/<int:pk>/', views.pos_machine_detail, name='pos_machine_detail'),

    # E-Service Center (مركز خدمات الدفع)
    path('eservice/', views.eservice_center, name='eservice_center'),
    path('eservice/history/', views.eservice_history, name='eservice_history'),
    path('eservice/<int:pk>/', views.eservice_detail, name='eservice_detail'),
    path('eservice/<int:pk>/post/', views.eservice_post, name='eservice_post'),
]
