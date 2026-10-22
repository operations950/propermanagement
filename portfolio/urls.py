from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='portfolio_dashboard'),
    path('<slug:slug>/', views.business_detail, name='portfolio_business_detail'),
    path('<slug:slug>/tasks/<int:pk>/', views.task_detail, name='portfolio_task_detail'),
]
