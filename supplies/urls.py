from django.urls import path

from . import views

app_name = 'supplies'

urlpatterns = [
    path('', views.cart, name='cart'),
    path('send/<int:property_id>/', views.send_order, name='send_order'),
    path('order/<int:pk>/', views.order_detail, name='order_detail'),
    path('order/<int:pk>/undo/', views.undo_order, name='undo_order'),
    path('catalog/', views.catalog, name='catalog'),
    path('blind-spots/', views.blind_spots, name='blind_spots'),
    path('blind-spots/clone/<int:property_id>/', views.clone_kit, name='clone_kit'),
]
