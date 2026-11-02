from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='ws_home'),
    path('home/', views.home),
    path('compliance/', views.compliance, name='ws_compliance'),
    path('projects/', views.projects, name='ws_projects'),
    path('financials/', views.financials, name='ws_financials'),
    path('lines/<slug:slug>/', views.line, name='ws_line'),
    path('onsite/properties/', views.week, name='ws_week'),
    path('onsite/properties/time/', views.week_time, name='ws_week_time'),
    path('onsite/properties/clean/', views.week_clean, name='ws_week_clean'),
    path('requests/decide/', views.request_decide, name='ws_request_decide'),
    path('admin/', views.admin_home, name='ws_admin'),
    path('admin/people/', views.people, name='ws_people'),
]
