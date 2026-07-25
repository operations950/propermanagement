from django.urls import path

from . import views

urlpatterns = [
    path('integrations/gmail/connect/', views.gmail_connect, name='gmail_connect'),
    path('integrations/gmail/callback/', views.gmail_callback, name='gmail_callback'),
    path('integrations/gmail/disconnect/', views.gmail_disconnect, name='gmail_disconnect'),
    path('webhooks/quo/log/', views.quo_webhook_log, name='quo_webhook_log'),
    path('webhooks/quo/<str:token>/', views.quo_webhook, name='quo_webhook'),
]
