from django.urls import path
from . import views
from . import employee_views

app_name = 'hr'

urlpatterns = [
    path('attendance/', views.attendance_dashboard, name='attendance_dashboard'),
    path('attendance/api/check/', views.api_attendance_check, name='api_attendance_check'),
    path('attendance/manage/', views.attendance_manage, name='attendance_manage'),
    path('attendance/report/', views.attendance_report, name='attendance_report'),
    path('payroll/', views.payroll_list, name='payroll_list'),
    path('payroll/tax-settings/', views.payroll_tax_settings, name='payroll_tax_settings'),
    path('payroll/pay/', views.payroll_pay, name='payroll_pay'),
    path('loans/', views.loan_list, name='loan_list'),
    path('loans/requests/<int:pk>/', employee_views.loan_request_decide, name='loan_request_decide'),
    path('employees/', employee_views.employee_list, name='employee_list'),
    path('employees/<int:pk>/', employee_views.employee_file, name='employee_file'),
    path('employees/<int:pk>/end-of-service/', employee_views.end_of_service, name='end_of_service'),
    path('me/', employee_views.my_portal, name='my_portal'),
    path('leaves/', views.leave_list, name='leave_list'),
    path('loans/<int:pk>/cancel/', views.loan_cancel, name='loan_cancel'),
    path('payroll/process/', views.payroll_process, name='payroll_process'),
    path('payroll/<int:pk>/confirm/', views.payroll_confirm, name='payroll_confirm'),
]
