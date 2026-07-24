from django.urls import path

from . import views

urlpatterns = [
    path('calendar/connect/', views.calendar_connect, name='calendar_connect'),
    path('calendar/callback/', views.calendar_callback, name='calendar_callback'),
    path('calendar/disconnect/', views.calendar_disconnect, name='calendar_disconnect'),
    path('properties/', views.property_list, name='property_list'),
    path('properties/new/', views.property_create, name='property_create'),
    path('properties/<int:pk>/', views.property_detail, name='property_detail'),
    path('properties/<int:pk>/edit/', views.property_edit, name='property_edit'),
    path('properties/<int:pk>/recurring-tasks/', views.property_recurring_tasks, name='property_recurring_tasks'),
    path('properties/<int:pk>/followup/sms/', views.property_followup_sms, name='property_followup_sms'),
    path('properties/<int:pk>/followup/email/', views.property_followup_email, name='property_followup_email'),
    path('properties/<int:pk>/contacts/<int:contact_pk>/thread/', views.property_contact_thread, name='property_contact_thread'),
    path('properties/address-autocomplete/', views.property_address_autocomplete, name='property_address_autocomplete'),
    path('properties/address-lookup/<str:place_id>/', views.property_address_lookup, name='property_address_lookup'),
    path('contacts/', views.contact_list, name='contact_list'),
    path('contacts/new/', views.contact_create, name='contact_create'),
    path('contacts/<int:pk>/edit/', views.contact_edit, name='contact_edit'),
    path('contacts/review/', views.contact_review, name='contact_review'),
    path('contacts/review/<int:pk>/approve/', views.contact_review_approve, name='contact_review_approve'),
    path('contacts/review/<int:pk>/reject/', views.contact_review_reject, name='contact_review_reject'),
]
