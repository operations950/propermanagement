from django.urls import path

from . import views, views_cam, views_ltr, views_renewals

app_name = 'commercial'

urlpatterns = [
    path('renewals/', views_renewals.renewals_page, name='renewals'),
    path('<int:pk>/long-term/', views_ltr.ltr_leases, name='ltr_leases'),
    path('<int:pk>/long-term/<int:lease_pk>/', views_ltr.ltr_lease, name='ltr_lease'),
    path('<int:pk>/leases/', views.leases, name='leases'),
    path('<int:pk>/leases/new/', views.lease_form, name='lease_new'),
    path('<int:pk>/leases/<int:lease_pk>/', views.lease_detail, name='lease'),
    path('<int:pk>/leases/<int:lease_pk>/edit/', views.lease_form, name='lease_edit'),
    path('<int:pk>/owners/', views.owners, name='owners'),
    path('<int:pk>/invoices/', views.invoice_list, name='invoices'),
    path('<int:pk>/invoices/<int:inv_pk>/pdf/', views.invoice_pdf, name='invoice_pdf'),
    path('<int:pk>/cam/', views_cam.cam_home, name='cam'),
    path('<int:pk>/cam/pool/new/', views_cam.cam_pool, name='cam_pool_new'),
    path('<int:pk>/cam/pool/<int:pool_pk>/', views_cam.cam_pool, name='cam_pool'),
    path('<int:pk>/cam/reconcile/', views_cam.cam_reconcile, name='cam_reconcile'),
    path('<int:pk>/cam/statement/<int:line_pk>/pdf/', views_cam.cam_statement_pdf, name='cam_statement'),
]
