from django.urls import path

from . import views

urlpatterns = [
    path('integrations/gmail/connect/', views.gmail_connect, name='gmail_connect'),
    path('integrations/gmail/callback/', views.gmail_callback, name='gmail_callback'),
    path('integrations/gmail/disconnect/', views.gmail_disconnect, name='gmail_disconnect'),
]
