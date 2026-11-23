from django.urls import path
from . import views
from . import liquidity_views

app_name = 'accounting'
urlpatterns = [
    # Journal Entries
    path('journals/', views.JournalEntryListView.as_view(), name='journal_list'),
    path('journals/create/', views.ManualJournalEntryCreateView.as_view(), name='journal_create'),
    path('journals/<int:pk>/', views.JournalEntryDetailView.as_view(), name='journal_detail'),
    
    # Reports & Chart of Accounts
    path('chart-of-accounts/', views.chart_of_accounts, name='chart_of_accounts'),
    path('accounts/create/', views.AccountCreateView.as_view(), name='account_create'),
    path('accounts/<int:pk>/update/', views.AccountUpdateView.as_view(), name='account_update'),
    path('setup-default-accounts/', views.setup_default_accounts, name='setup_default_accounts'),
    path('trial-balance/', views.trial_balance, name='trial_balance'),
    path('income-statement/', views.income_statement, name='income_statement'),
    path('balance-sheet/', views.balance_sheet, name='balance_sheet'),
    

    # Payment Methods
    path('payment-methods/', views.PaymentMethodListView.as_view(), name='payment_method_list'),
    path('payment-methods/create/', views.PaymentMethodCreateView.as_view(), name='payment_method_create'),
    path('payment-methods/<int:pk>/update/', views.PaymentMethodUpdateView.as_view(), name='payment_method_update'),
    
    # Treasuries
    path('liquidity/', liquidity_views.liquidity_dashboard, name='liquidity_dashboard'),
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
    path('vouchers/<int:pk>/update/', views.VoucherUpdateView.as_view(), name='voucher_update'),
    path('vouchers/<int:pk>/confirm/', views.VoucherConfirmView.as_view(), name='voucher_confirm'),
    path('vouchers/<int:pk>/cancel/', views.VoucherCancelView.as_view(), name='voucher_cancel'),

    # POS Machines (ماكينات الدفع الإلكتروني)
    path('pos-machines/', views.pos_machine_list, name='pos_machine_list'),
    path('pos-machines/create/', views.pos_machine_create, name='pos_machine_create'),
    path('pos-machines/<int:pk>/', views.pos_machine_detail, name='pos_machine_detail'),

    # E-Service Center (مركز خدمات الدفع)
    path('eservice/', views.eservice_center, name='eservice_center'),
    path('eservice/history/', views.eservice_history, name='eservice_history'),
    path('eservice/<int:pk>/', views.eservice_detail, name='eservice_detail'),
    path('eservice/<int:pk>/post/', views.eservice_post, name='eservice_post'),

    # Internal Transfers (تحويل الأموال بين الحسابات)
    path('transfers/', views.InternalTransferListView.as_view(), name='transfer_list'),
    path('transfers/create/', views.InternalTransferCreateView.as_view(), name='transfer_create'),
    path('transfers/<int:pk>/', views.InternalTransferDetailView.as_view(), name='transfer_detail'),
    path('transfers/<int:pk>/confirm/', views.InternalTransferConfirmView.as_view(), name='transfer_confirm'),
    path('transfers/<int:pk>/cancel/', views.InternalTransferCancelView.as_view(), name='transfer_cancel'),
    path('transfers/<int:pk>/receive/', views.InternalTransferReceiveView.as_view(), name='transfer_receive'),
    path('transfers/<int:pk>/reject/', views.InternalTransferRejectView.as_view(), name='transfer_reject'),

    # Cheques Management (إدارة الشيكات: أوراق القبض وأوراق الدفع)
    path('cheques/', views.ChequeListView.as_view(), name='cheque_list'),
    path('cheques/receivable/create/', views.ReceivableChequeCreateView.as_view(), name='cheque_receivable_create'),
    path('cheques/payable/create/', views.PayableChequeCreateView.as_view(), name='cheque_payable_create'),
    path('cheques/<int:pk>/', views.ChequeDetailView.as_view(), name='cheque_detail'),
    path('cheques/<int:pk>/collect/', views.ChequeCollectActionView.as_view(), name='cheque_collect'),
    path('cheques/<int:pk>/bounce/', views.ChequeBounceActionView.as_view(), name='cheque_bounce'),
    path('cheques/<int:pk>/clear-payable/', views.ChequeClearPayableActionView.as_view(), name='cheque_clear_payable'),
    path('cheques/<int:pk>/cancel-payable/', views.ChequeCancelPayableActionView.as_view(), name='cheque_cancel_payable'),

    # Cheque Books (دفاتر الشيكات)
    path('cheque-books/', views.ChequeBookListView.as_view(), name='cheque_book_list'),
    path('cheque-books/create/', views.ChequeBookCreateView.as_view(), name='cheque_book_create'),

    # Fixed Assets & Depreciation (الأصول الثابتة والإهلاك)
    path('fixed-assets/', views.FixedAssetListView.as_view(), name='fixed_asset_list'),
    path('fixed-assets/create/', views.FixedAssetCreateView.as_view(), name='fixed_asset_create'),
    path('fixed-assets/<int:pk>/', views.FixedAssetDetailView.as_view(), name='fixed_asset_detail'),
    path('fixed-assets/<int:pk>/update/', views.FixedAssetUpdateView.as_view(), name='fixed_asset_update'),
    path('fixed-assets/<int:pk>/dispose/', views.FixedAssetDisposalView.as_view(), name='fixed_asset_dispose'),
    path('fixed-assets/depreciation/run/', views.FixedAssetDepreciationRunView.as_view(), name='fixed_asset_depreciation_run'),

    # Loans & Financing (القروض والتسهيلات وسداد الأقساط)
    path('loans/', views.LoanListView.as_view(), name='loan_list'),
    path('loans/create/', views.LoanCreateView.as_view(), name='loan_create'),
    path('loans/<int:pk>/', views.LoanDetailView.as_view(), name='loan_detail'),
    path('loans/installments/<int:pk>/pay/', views.LoanInstallmentPayView.as_view(), name='loan_installment_pay'),
]


