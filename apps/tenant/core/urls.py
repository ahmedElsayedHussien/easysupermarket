from django.urls import path
from . import views
from . import reports_views
from . import reports_extra
from . import pwa

app_name = 'core'
urlpatterns = [
    path('', views.main_screen, name='main_screen'),
    path('dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('settings/', views.settings_dashboard, name='settings_dashboard'),
    path('settings/branches/', views.BranchListView.as_view(), name='branch_list'),
    path('settings/branches/create/', views.BranchCreateView.as_view(), name='branch_create'),
    path('settings/branches/<int:pk>/update/', views.BranchUpdateView.as_view(), name='branch_update'),
    path('settings/branches/<int:pk>/delete/', views.branch_delete, name='branch_delete'),
    path('settings/users/', views.UserListView.as_view(), name='user_list'),
    path('settings/audit-log/', views.audit_log, name='audit_log'),
    path('manifest.json', pwa.manifest, name='pwa_manifest'),
    path('sw.js', pwa.service_worker, name='pwa_sw'),
    path('api/alerts/', pwa.alerts_api, name='alerts_api'),
    path('settings/users/create/', views.UserCreateView.as_view(), name='user_create'),
    path('settings/users/<int:pk>/update/', views.UserUpdateView.as_view(), name='user_update'),
    path('reports/', views.ReportsIndexView.as_view(), name='reports_index'),
    path('reports/sales/product/', reports_views.ProductSalesReportView.as_view(), name='report_sales_product'),
    path('reports/sales/profitability/', reports_views.ItemProfitabilityReportView.as_view(), name='report_item_profitability'),
    path('reports/sales/users/', reports_views.UserSalesReportView.as_view(), name='report_sales_users'),
    path('reports/sales/debts/', reports_views.CustomerDebtsReportView.as_view(), name='report_customer_debts'),
    path('reports/sales/returns/', reports_views.SalesReturnsReportView.as_view(), name='report_sales_returns'),
    path('reports/sales/period/', reports_views.PeriodSalesReportView.as_view(), name='report_sales_period'),
    path('reports/sales/detailed/', reports_views.DetailedSalesReportView.as_view(), name='report_sales_detailed'),
    path('reports/sales/customer-balances/', reports_views.CustomerBalancesReportView.as_view(), name='report_customer_balances'),
    path('reports/sales/shifts/', reports_views.ShiftReportView.as_view(), name='report_shifts'),
    path('reports/sales/shifts/<int:pk>/', reports_views.ShiftDetailView.as_view(), name='report_shift_detail'),
    path('reports/sales/shifts/<int:pk>/excel/', reports_views.export_shift_detail_excel, name='report_shift_detail_excel'),
    path('reports/sales/shifts/<int:pk>/api/', reports_views.shift_detail_api, name='report_shift_detail_api'),

    path('settings/system/', views.SystemSettingUpdateView.as_view(), name='system_settings'),
    path('settings/backup/', views.database_backup_view, name='database_backup'),
    path('settings/backup/create/', views.database_backup_create, name='database_backup_create'),
    path('settings/backup/download/<str:filename>/', views.database_backup_download, name='database_backup_download'),
    path('settings/backup/delete/<str:filename>/', views.database_backup_delete, name='database_backup_delete'),
    path('settings/backup/restore/', views.database_backup_restore, name='database_backup_restore'),
    path('settings/backup/restore/<str:filename>/', views.database_backup_restore, name='database_backup_restore_file'),
    # Purchase Reports
    path('reports/purchases/product/', reports_views.ProductPurchasesReportView.as_view(), name='report_product_purchases'),
    path('reports/purchases/detailed/', reports_views.DetailedPurchasesReportView.as_view(), name='report_detailed_purchases'),
    path('reports/purchases/period/', reports_views.PeriodPurchasesReportView.as_view(), name='report_period_purchases'),
    path('reports/purchases/price-variation/', reports_views.PurchasePriceVariationReportView.as_view(), name='report_purchase_price_variation'),
    path('reports/purchases/supplier-balances/', reports_views.SupplierBalancesReportView.as_view(), name='report_supplier_balances'),
    path('reports/purchases/supplier-debts/', reports_views.SupplierDebtsReportView.as_view(), name='report_supplier_debts'),
    path('reports/purchases/user/', reports_views.UserPurchasesReportView.as_view(), name='report_user_purchases'),
    path('reports/purchases/returns/', reports_views.PurchaseReturnsReportView.as_view(), name='report_purchase_returns'),
    
    # Inventory Reports
    path('reports/inventory/low-stock/', reports_views.LowStockReportView.as_view(), name='report_low_stock'),
    path('reports/inventory/overstock/', reports_views.OverstockReportView.as_view(), name='report_overstock'),
    path('reports/inventory/expiry/', reports_views.ProductExpiryReportView.as_view(), name='report_product_expiry'),
    path('reports/inventory/warehouse-stock/', reports_views.WarehouseStockReportView.as_view(), name='report_warehouse_stock'),
    path('reports/inventory/movement/', reports_views.ItemMovementReportView.as_view(), name='report_item_movement'),
    path('reports/inventory/slow-moving/', reports_views.SlowMovingReportView.as_view(), name='report_slow_moving'),
    path('reports/accounting/income-statement/', reports_views.IncomeStatementReportView.as_view(), name='report_income_statement'),
    path('reports/accounting/cash-flow/', reports_views.CashFlowReportView.as_view(), name='report_cash_flow'),
    path('reports/inventory/turnover/', reports_extra.inventory_turnover, name='report_inventory_turnover'),
    path('reports/inventory/wastage/', reports_extra.wastage_report, name='report_wastage'),
    path('reports/inventory/batch-trace/', reports_extra.batch_traceability, name='report_batch_trace'),
    path('reports/accounting/income-statement/detail/', reports_views.IncomeStatementDetailView.as_view(), name='report_income_statement_detail'),
    path('reports/accounting/expenses/', reports_views.ExpenseReportView.as_view(), name='report_expenses'),
    path('reports/accounting/treasury/', reports_views.TreasuryStatementReportView.as_view(), name='report_treasury'),
    path('reports/accounting/trial-balance/', reports_views.TrialBalanceReportView.as_view(), name='report_trial_balance'),
    path('reports/accounting/general-ledger/', reports_views.GeneralLedgerReportView.as_view(), name='report_general_ledger'),
    path('reports/accounting/tax/', reports_views.TaxReportView.as_view(), name='report_taxes'),
    path('reports/accounting/einvoicing/', reports_views.EInvoicingReportView.as_view(), name='report_einvoicing'),
    path('reports/accounting/fixed-assets/', reports_views.FixedAssetReportView.as_view(), name='report_fixed_assets'),
    path('reports/accounting/loans/', reports_views.LoanReportView.as_view(), name='report_loans'),
    path('reports/inventory/valuation/', reports_views.InventoryValuationReportView.as_view(), name='report_inventory_valuation'),
    path('reports/partners/', reports_views.PartnersReportView.as_view(), name='report_partners'),
    
    # Liquidity Reports
    path('reports/liquidity/position/', reports_views.LiquidityPositionReportView.as_view(), name='report_liquidity_position'),
    path('reports/liquidity/vouchers/', reports_views.CashVouchersReportView.as_view(), name='report_cash_vouchers'),
    path('reports/liquidity/transfers/', reports_views.InternalTransfersReportView.as_view(), name='report_liquidity_transfers'),
    
    # Dashboard API routes
    path('api/branch-status/', views.branch_status_api, name='branch_status_api'),
    path('api/warehouses/', views.warehouses_by_branch_api, name='warehouses_by_branch_api'),
]

