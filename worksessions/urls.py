from django.urls import path

from . import views

urlpatterns = [
    path('', views.my_sessions, name='my_sessions'),
    path('<int:pk>/', views.session_detail, name='session_detail'),
    path('rules/', views.session_template_list, name='session_template_list'),
    path('rules/new/', views.session_template_form_view, name='session_template_create'),
    path('rules/<int:pk>/', views.session_template_detail, name='session_template_detail'),
    path('rules/<int:pk>/edit/', views.session_template_form_view, name='session_template_edit'),
    path('rules/preview/', views.session_template_preview, name='session_template_preview'),
]
