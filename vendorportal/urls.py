from django.urls import path

from . import views

app_name = 'vendorportal'

urlpatterns = [
    path('t/<uuid:token>/', views.vendor_ticket_view, name='ticket'),
]
