from django.urls import path

from . import views

urlpatterns = [
    path('calendar/connect/', views.calendar_connect, name='calendar_connect'),
    path('calendar/callback/', views.calendar_callback, name='calendar_callback'),
    path('calendar/disconnect/', views.calendar_disconnect, name='calendar_disconnect'),
]
