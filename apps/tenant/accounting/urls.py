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
    
    # Taxes
    path('taxes/', views.TaxListView.as_view(), name='tax_list'),
    path('taxes/create/', views.TaxCreateView.as_view(), name='tax_create'),
    path('taxes/<int:pk>/update/', views.TaxUpdateView.as_view(), name='tax_update'),
    
    # Payment Methods
    path('payment-methods/', views.PaymentMethodListView.as_view(), name='payment_method_list'),
    path('payment-methods/create/', views.PaymentMethodCreateView.as_view(), name='payment_method_create'),
    path('payment-methods/<int:pk>/update/', views.PaymentMethodUpdateView.as_view(), name='payment_method_update'),
    
    # Treasuries
    path('treasuries/', views.TreasuryListView.as_view(), name='treasury_list'),
    path('treasuries/create/', views.TreasuryCreateView.as_view(), name='treasury_create'),
    path('treasuries/<int:pk>/update/', views.TreasuryUpdateView.as_view(), name='treasury_update'),

    # Bank Accounts
    path('bank-accounts/', views.BankAccountListView.as_view(), name='bank_list'),
    path('bank-accounts/create/', views.BankAccountCreateView.as_view(), name='bank_create'),
    path('bank-accounts/<int:pk>/update/', views.BankAccountUpdateView.as_view(), name='bank_update'),

    # E-Wallets
    path('ewallets/', views.EWalletListView.as_view(), name='ewallet_list'),
    path('ewallets/create/', views.EWalletCreateView.as_view(), name='ewallet_create'),
    path('ewallets/<int:pk>/update/', views.EWalletUpdateView.as_view(), name='ewallet_update'),
]
