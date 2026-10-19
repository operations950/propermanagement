from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='timeclock_home'),
    path('in/', views.clock_in, name='timeclock_in'),
    path('out/', views.clock_out, name='timeclock_out'),
    path('time-off/', views.request_time_off, name='timeclock_time_off'),
    path('change-a-day/', views.request_change, name='timeclock_change'),
    path('fix-a-punch/', views.request_fix, name='timeclock_fix'),
    path('withdraw/<str:kind>/<int:pk>/', views.cancel_request, name='timeclock_withdraw'),
    path('policy/', views.policy_page, name='timeclock_policy'),
    path('policy/download/', views.policy_download, name='timeclock_policy_download'),
    path('calendar/', views.team_calendar, name='timeclock_calendar'),
    path('manage/', views.manage_home, name='timeclock_manage'),
    path('manage/decide/', views.manage_decide, name='timeclock_decide'),
    path('manage/hours/', views.manage_hours, name='timeclock_hours'),
    path('manage/hours/lock/', views.manage_lock, name='timeclock_lock'),
    path('manage/punch/', views.manage_punch, name='timeclock_punch'),
    path('manage/setup/', views.manage_setup, name='timeclock_setup'),
    path('manage/setup/<int:pk>/', views.manage_setup_save, name='timeclock_setup_save'),
    path('manage/office/', views.office_connection, name='timeclock_office'),
]
