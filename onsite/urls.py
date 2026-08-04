from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='onsite_dashboard'),
    path('import/', views.booking_import_upload, name='onsite_booking_import'),
    path('import/<int:batch_id>/apply/', views.booking_import_apply, name='onsite_booking_import_apply'),
    path('visit/<int:pk>/', views.visit_detail, name='onsite_visit_detail'),
    path('v/<uuid:token>/', views.visit_public, name='onsite_visit_public'),
    path('v/<uuid:token>/signature/', views.visit_public_signature, name='onsite_visit_public_signature'),
    path('checklist-items/', views.checklist_custom_items, name='onsite_checklist_custom_items'),
]
