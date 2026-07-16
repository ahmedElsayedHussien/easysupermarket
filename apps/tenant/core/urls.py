from django.urls import path
from . import views
from . import reports_views

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
    path('settings/system/', views.SystemSettingUpdateView.as_view(), name='system_settings'),
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
    path('reports/inventory/expiry/', reports_views.ProductExpiryReportView.as_view(), name='report_product_expiry'),
    path('reports/inventory/warehouse-stock/', reports_views.WarehouseStockReportView.as_view(), name='report_warehouse_stock'),
    path('reports/inventory/movement/', reports_views.ItemMovementReportView.as_view(), name='report_item_movement'),
    path('reports/inventory/slow-moving/', reports_views.SlowMovingReportView.as_view(), name='report_slow_moving'),
    path('reports/accounting/income-statement/', reports_views.IncomeStatementReportView.as_view(), name='report_income_statement'),
    path('reports/accounting/expenses/', reports_views.ExpenseReportView.as_view(), name='report_expenses'),
    path('reports/accounting/treasury/', reports_views.TreasuryStatementReportView.as_view(), name='report_treasury'),
    path('reports/accounting/trial-balance/', reports_views.TrialBalanceReportView.as_view(), name='report_trial_balance'),
    path('reports/accounting/general-ledger/', reports_views.GeneralLedgerReportView.as_view(), name='report_general_ledger'),
    path('reports/accounting/tax/', reports_views.TaxReportView.as_view(), name='report_taxes'),
    path('reports/accounting/einvoicing/', reports_views.EInvoicingReportView.as_view(), name='report_einvoicing'),
    path('reports/inventory/valuation/', reports_views.InventoryValuationReportView.as_view(), name='report_inventory_valuation'),
]
