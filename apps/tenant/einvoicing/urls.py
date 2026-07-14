from django.urls import path
from . import views
from . import api_views

app_name = 'einvoicing'

urlpatterns = [
    path('settings/', views.TaxIntegrationSettingsUpdateView.as_view(), name='tax_settings'),
    path('approve/', views.InvoiceApprovalListView.as_view(), name='invoice_approval_list'),
    path('approve/<int:log_id>/', views.approve_invoice_for_eta, name='approve_invoice'),
    path('history/', views.EInvoiceHistoryListView.as_view(), name='invoice_history'),
    path('history/confirm/<int:log_id>/', views.confirm_eta_submission, name='confirm_eta_submission'),
    path('history/resend/<int:log_id>/', views.resend_invoice, name='resend_invoice'),
    
    # API endpoints for Local Signer
    path('api/pending/', api_views.get_pending_invoices, name='api_pending'),
    path('api/signed/<int:invoice_id>/', api_views.submit_signed_invoice, name='api_signed'),
]
