from django.urls import path

from . import views

urlpatterns = [
    path('calendar/connect/', views.calendar_connect, name='calendar_connect'),
    path('calendar/callback/', views.calendar_callback, name='calendar_callback'),
    path('calendar/disconnect/', views.calendar_disconnect, name='calendar_disconnect'),
    path('properties/', views.property_list, name='property_list'),
    path('properties/new/', views.property_create, name='property_create'),
    path('properties/<int:pk>/edit/', views.property_edit, name='property_edit'),
]
