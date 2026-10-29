from django.urls import path

from . import views, views_money, views_setup

app_name = 'accounting'

urlpatterns = [
    path('', views.home, name='home'),
    path('<int:pk>/', views.hub, name='hub'),
    path('<int:pk>/close/<str:month>/', views.close_sheet, name='close'),
    path('<int:pk>/units/', views.units, name='units'),
    path('<int:pk>/units/import/', views.units_import, name='units_import'),
    path('<int:pk>/unit/<int:unit_pk>/', views.unit_detail, name='unit'),
    path('<int:pk>/owner/<int:contact_pk>/', views.owner_detail, name='owner'),
    path('<int:pk>/shares/', views.shares, name='shares'),
    path('<int:pk>/charges/', views.charges_list, name='charges'),
    path('<int:pk>/charges/new/', views.charge_form, name='charge_new'),
    path('<int:pk>/charges/<int:rule_pk>/', views.charge_form, name='charge_edit'),
    path('<int:pk>/charge/', views.charge_one, name='charge_one'),
    path('<int:pk>/payments/new/', views.receipt_new, name='receipt_new'),
    path('<int:pk>/bank/<int:bank_pk>/<str:month>/', views_money.bank, name='bank'),
    path('<int:pk>/bills/', views_money.bills, name='bills'),
    path('<int:pk>/bills/pay/', views_money.pay, name='pay'),
    path('<int:pk>/bills/<int:bill_pk>/', views_money.bill_detail, name='bill'),
    path('<int:pk>/checks/', views_money.checks, name='checks'),
    path('<int:pk>/checks/print/', views_money.check_print, name='check_print'),
    path('<int:pk>/checks/setup/', views_money.check_setup, name='check_setup'),
    path('<int:pk>/recurring/', views_money.recurring, name='recurring'),
    path('<int:pk>/journal/', views_money.journal, name='journal'),
    path('<int:pk>/reports/', views_money.reports, name='reports'),
    path('<int:pk>/reports/trial-balance/', views_money.report_tb, name='report_tb'),
    path('<int:pk>/reports/balance-sheet/', views_money.report_bs, name='report_bs'),
    path('<int:pk>/reports/income/', views_money.report_is, name='report_is'),
    path('<int:pk>/reports/ledger/', views_money.report_gl, name='report_gl'),
    path('<int:pk>/reports/aging/', views_money.report_aging, name='report_aging'),
    path('<int:pk>/reports/payables/', views_money.report_payables, name='report_payables'),
    path('<int:pk>/reports/checks/', views_money.report_checks, name='report_checks'),
    path('<int:pk>/reports/budget/', views_money.budget, name='budget'),
    path('<int:pk>/reports/bank/<int:bank_pk>/<str:month>/', views_money.report_rec, name='report_rec'),
    path('<int:pk>/settings/', views_setup.settings, name='settings'),
]
