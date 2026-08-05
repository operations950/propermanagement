from django.urls import path

from . import views

app_name = 'supplies'

urlpatterns = [
    path('', views.cart, name='cart'),
    path('send/<int:property_id>/', views.send_order, name='send_order'),
    path('order/<int:pk>/', views.order_detail, name='order_detail'),
    path('order/<int:pk>/undo/', views.undo_order, name='undo_order'),
    path('blind-spots/', views.blind_spots, name='blind_spots'),
    path('blind-spots/clone/<int:property_id>/', views.clone_kit, name='clone_kit'),
    # Legacy SMS/AI-era screens — still mounted until Phase 6 of the supply
    # reorder redesign formally decommissions them (see the build brief).
    path('digest/', views.digest, name='digest'),
    path('request/<int:pk>/property/', views.supply_request_set_property, name='supply_request_set_property'),
    path('batch/<int:pk>/', views.batch_detail, name='batch_detail'),
]
