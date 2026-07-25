from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('dashboard/<str:role>/', views.department_dashboard, name='department_dashboard'),
    path('tickets/', views.ticket_list, name='ticket_list'),
    path('tickets/pending/', views.ticket_pending, name='ticket_pending'),
    path('tickets/<int:pk>/pending/save/', views.ticket_pending_save, name='ticket_pending_save'),
    path('tickets/<int:pk>/pending/delete/', views.ticket_pending_delete, name='ticket_pending_delete'),
    path('tickets/new/', views.ticket_create, name='ticket_create'),
    path('templates/', views.ticket_template_list, name='ticket_template_list'),
    path('templates/new/', views.ticket_template_create, name='ticket_template_create'),
    path('templates/<int:pk>/', views.ticket_template_detail, name='ticket_template_detail'),
    path('templates/<int:pk>/edit/', views.ticket_template_edit, name='ticket_template_edit'),
    path('tickets/<int:pk>/', views.ticket_detail, name='ticket_detail'),
    path('tickets/<int:pk>/reassign/', views.ticket_reassign, name='ticket_reassign'),
    path('tickets/<int:pk>/assign-contractor/', views.ticket_assign_contractor, name='ticket_assign_contractor'),
    path(
        'tickets/<int:pk>/previous-conversation/<int:log_id>/', views.ticket_previous_conversation,
        name='ticket_previous_conversation',
    ),
    path('tickets/<int:pk>/property/', views.ticket_set_property, name='ticket_set_property'),
    path('tickets/<int:pk>/contacts/', views.ticket_set_contacts, name='ticket_set_contacts'),
    path('tickets/<int:pk>/status/', views.ticket_set_status, name='ticket_set_status'),
    path('tickets/<int:pk>/close-no-followup/', views.ticket_close_no_followup, name='ticket_close_no_followup'),
    path('tickets/checklist/<int:pk>/toggle/', views.ticket_checklist_toggle, name='ticket_checklist_toggle'),
    path('tickets/<int:pk>/quick-edit/', views.ticket_quick_edit, name='ticket_quick_edit'),
    path('tickets/<int:pk>/due-date/', views.ticket_set_due_date, name='ticket_set_due_date'),
    path('tickets/<int:pk>/delete/', views.ticket_delete, name='ticket_delete'),
    path('tickets/<int:pk>/followup/sms/', views.ticket_followup_sms, name='ticket_followup_sms'),
    path('tickets/<int:pk>/contractor-thread/', views.ticket_contractor_thread_refresh, name='ticket_contractor_thread_refresh'),
    path('tickets/<int:pk>/vendor-link/send/', views.ticket_send_vendor_link, name='ticket_send_vendor_link'),
    path('tickets/<int:pk>/followup/email/', views.ticket_followup_email, name='ticket_followup_email'),
]
