from django.urls import path

from . import views

app_name = 'legalforms'

urlpatterns = [
    path('', views.document_list, name='list'),
    path('new/<slug:slug>/', views.document_new, name='new'),
    path('<int:pk>/', views.document_detail, name='detail'),
    path('<int:pk>/edit/', views.document_edit, name='edit'),
    path('<int:pk>/print/', views.document_print, name='print'),
    path('<int:pk>/act/', views.document_act, name='act'),
]
