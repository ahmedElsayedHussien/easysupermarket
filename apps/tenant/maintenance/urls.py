from django.urls import path
from . import views
from . import commission_views

app_name = 'maintenance'

urlpatterns = [
    path('', views.ticket_list, name='ticket_list'),
    path('create/', views.ticket_create, name='ticket_create'),
    path('<int:pk>/', views.ticket_detail, name='ticket_detail'),
    path('<int:pk>/edit/', views.ticket_edit, name='ticket_edit'),
    path('<int:pk>/add-part/', views.add_part, name='add_part'),
    path('<int:pk>/deliver/', views.deliver_ticket, name='deliver_ticket'),
    path('api/part/<int:part_pk>/delete/', views.delete_part, name='delete_part'),
    path('api/warehouse/<int:warehouse_id>/products/', views.get_warehouse_products, name='get_warehouse_products'),

    # Commission
    path('commissions/', commission_views.commission_rules, name='commission_rules'),
    path('commissions/report/', commission_views.commission_report, name='commission_report'),
    path('commissions/rule/delete/<int:pk>/', commission_views.commission_rule_delete, name='commission_rule_delete'),
]
