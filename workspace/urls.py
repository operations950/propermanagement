from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='ws_home'),
    path('home/', views.home),
    path('compliance/', views.compliance, name='ws_compliance'),
    path('projects/', views.projects, name='ws_projects'),
    path('financials/', views.financials, name='ws_financials'),
    path('lines/<slug:slug>/', views.line, name='ws_line'),
    path('admin/', views.admin_home, name='ws_admin'),
    path('admin/people/', views.people, name='ws_people'),
]
