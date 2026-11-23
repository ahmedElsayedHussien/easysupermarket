from django.urls import path
from . import views

app_name = 'rentals'

urlpatterns = [
    path('equipments/', views.EquipmentListView.as_view(), name='equipment_list'),
    path('equipments/create/', views.EquipmentCreateView.as_view(), name='equipment_create'),
    path('equipments/<int:pk>/edit/', views.EquipmentUpdateView.as_view(), name='equipment_edit'),
    
    path('', views.RentalListView.as_view(), name='rental_list'),
    path('create/', views.RentalCreateView.as_view(), name='rental_create'),
    path('<int:pk>/', views.RentalDetailView.as_view(), name='rental_detail'),
    path('<int:pk>/print/', views.RentalPrintView.as_view(), name='rental_print'),
    path('<int:pk>/return/', views.RentalReturnView.as_view(), name='rental_return'),
    path('reports/', views.RentalReportsView.as_view(), name='rental_reports'),
    path('reports/detailed/', views.DetailedRentalsReportView.as_view(), name='report_detailed_rentals'),
    path('reports/detailed/excel/', views.export_detailed_rentals_excel, name='report_detailed_rentals_excel'),
    path('reports/utilization/', views.EquipmentUtilizationReportView.as_view(), name='report_equipment_utilization'),
    path('reports/overdue/', views.OverdueRentalsReportView.as_view(), name='report_overdue_rentals'),
]
