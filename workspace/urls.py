from django.urls import path

from onsite import views_turnovers

from . import views

urlpatterns = [
    path('', views.home, name='ws_home'),
    path('home/', views.home),
    path('compliance/', views.compliance, name='ws_compliance'),
    path('projects/', views.projects, name='ws_projects'),
    path('projects/start/', views.start_project_new, name='ws_start_project'),
    path('financials/', views.financials, name='ws_financials'),
    path('lines/<slug:slug>/', views.line, name='ws_line'),
    path('onsite/properties/', views_turnovers.old_properties, name='ws_week'),       # the old On-Site Properties board: it is the Turnovers screen now
    path('tickets/<int:pk>/project/', views.make_project, name='ws_make_project'),
    path('runs/<int:pk>/', views.run_detail, name='ws_run_detail'),
    path('runs/<int:pk>/notes/', views.run_note, name='ws_run_note'),
    path('run-steps/<int:pk>/note/', views.step_note, name='ws_step_note'),
    path('requests/decide/', views.request_decide, name='ws_request_decide'),
    path('admin/', views.admin_home, name='ws_admin'),
    path('admin/people/', views.people, name='ws_people'),
]
