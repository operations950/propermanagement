from django.urls import path

from . import views

app_name = 'supplies'

urlpatterns = [
    path('', views.digest, name='digest'),
    path('request/<int:pk>/property/', views.supply_request_set_property, name='supply_request_set_property'),
    path('batch/<int:pk>/', views.batch_detail, name='batch_detail'),
]
