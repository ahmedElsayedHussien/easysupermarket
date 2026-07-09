from django.urls import path
from . import views

app_name = 'core'
urlpatterns = [
    path('', views.main_screen, name='main_screen'),
    path('dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('settings/', views.settings_dashboard, name='settings_dashboard'),
    path('settings/branches/', views.BranchListView.as_view(), name='branch_list'),
    path('settings/branches/create/', views.BranchCreateView.as_view(), name='branch_create'),
    path('settings/branches/<int:pk>/update/', views.BranchUpdateView.as_view(), name='branch_update'),
    path('settings/users/', views.UserListView.as_view(), name='user_list'),
    path('settings/users/create/', views.UserCreateView.as_view(), name='user_create'),
    path('settings/users/<int:pk>/update/', views.UserUpdateView.as_view(), name='user_update'),
]
