from django.urls import path

from . import views

urlpatterns = [
    path('templates/', views.process_template_list, name='process_template_list'),
    path('templates/new/', views.process_template_create, name='process_template_create'),
    path('templates/<int:pk>/', views.process_template_edit, name='process_template_edit'),
    path('templates/<int:pk>/preview/', views.process_template_preview, name='process_template_preview'),
    path('templates/<int:template_pk>/steps/add/', views.process_template_step_add, name='process_template_step_add'),
    path('steps/<int:step_pk>/edit/', views.process_template_step_edit, name='process_template_step_edit'),
    path('steps/<int:step_pk>/delete/', views.process_template_step_delete, name='process_template_step_delete'),
    path('steps/<int:step_pk>/move/', views.process_template_step_move, name='process_template_step_move'),

    path('runs/attach/', views.process_run_attach, name='process_run_attach'),
    path('runs/<int:run_pk>/external-link/', views.process_run_external_link_create, name='process_run_external_link_create'),
    path('run-steps/<int:step_pk>/update/', views.process_run_step_update, name='process_run_step_update'),
    path('run-steps/<int:step_pk>/upload/', views.process_run_step_upload, name='process_run_step_upload'),
    path('run-steps/<int:step_pk>/complete-upload/', views.process_run_step_complete_upload, name='process_run_step_complete_upload'),
    path('run-steps/<int:step_pk>/assign-task/', views.process_run_step_assign_task, name='process_run_step_assign_task'),
    path('run-steps/<int:step_pk>/complete-task/', views.process_run_step_complete_task, name='process_run_step_complete_task'),
    path('run-steps/<int:step_pk>/schedule-event/', views.process_run_step_schedule_event, name='process_run_step_schedule_event'),
    path('run-steps/<int:step_pk>/decide/', views.process_run_step_decide, name='process_run_step_decide'),
    path('run-steps/<int:step_pk>/mark-sent/', views.process_run_step_mark_sent, name='process_run_step_mark_sent'),
    path('run-steps/<int:step_pk>/resume-wait/', views.process_run_step_resume_wait, name='process_run_step_resume_wait'),

    path('access/<uuid:token>/', views.process_external_access, name='process_external_access'),
]
