from django.urls import path
from . import views

app_name = 'hr'

urlpatterns = [
    path('attendance/', views.attendance_dashboard, name='attendance_dashboard'),
    path('attendance/api/check/', views.api_attendance_check, name='api_attendance_check'),
    path('payroll/', views.payroll_list, name='payroll_list'),
    path('payroll/process/', views.payroll_process, name='payroll_process'),
    path('payroll/<int:pk>/confirm/', views.payroll_confirm, name='payroll_confirm'),
]
