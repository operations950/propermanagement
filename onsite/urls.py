from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='onsite_dashboard'),
    path('today/', views.str_today, name='onsite_str_today'),
    path('performance/', views.performance, name='onsite_performance'),
    path('reservations/', views.reservation_list, name='onsite_reservation_list'),
    path('reservations/new/', views.reservation_create, name='onsite_reservation_create'),
    path('reservations/<int:pk>/', views.reservation_edit, name='onsite_reservation_edit'),
    path('calendar/', views.calendar_view, name='onsite_calendar'),
    path('import/', views.booking_import_upload, name='onsite_booking_import'),
    path('import/slot/<int:slot_id>/', views.upload_slot, name='onsite_upload_slot'),
    path('import/<int:batch_id>/', views.booking_import_preview, name='onsite_booking_import_preview'),
    path('import/<int:batch_id>/apply/', views.booking_import_apply, name='onsite_booking_import_apply'),
    path('import/<int:batch_id>/quick-add-property/', views.quick_add_property, name='onsite_quick_add_property'),
    path('visit/new/', views.visit_create, name='onsite_visit_create'),
    path('rules/', views.visit_rule_list, name='onsite_visit_rule_list'),
    path('feeds/', views.booking_feeds, name='onsite_booking_feeds'),
    path('visit/<int:pk>/', views.visit_detail, name='onsite_visit_detail'),
    path('v/<uuid:token>/', views.visit_public, name='onsite_visit_public'),
    path('v/<uuid:token>/signature/', views.visit_public_signature, name='onsite_visit_public_signature'),
    path('cleaning-payments/', views.cleaning_payments, name='onsite_cleaning_payments'),
    path('checklist-items/', views.checklist_custom_items, name='onsite_checklist_custom_items'),
    path('checklist-templates/', views.checklist_templates, name='onsite_checklist_templates'),
    path('checklist-templates/<int:type_id>/', views.checklist_template_detail, name='onsite_checklist_template_detail'),
]
