import re

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve as serve_static

from core.views import StaffLoginView, google_login_callback, google_login_start

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', StaffLoginView.as_view(), name='login'),
    path('login/google/', google_login_start, name='google_login_start'),
    path('login/google/callback/', google_login_callback, name='google_login_callback'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('vendor/', include('vendorportal.urls')),
    path('supplies/', include('supplies.urls')),
    path('processes/', include('processes.urls')),
    path('', include('core.urls')),
    path('', include('intake.urls')),
    path('', include('tickets.urls')),
]

# Deliberately NOT using django.conf.urls.static.static() here — that helper
# silently no-ops whenever DEBUG is False, which meant every vendor-uploaded
# attachment's URL 404'd in production (WhiteNoise only serves STATIC_ROOT,
# collectstatic's output — never MEDIA_ROOT). django.views.static.serve isn't
# hardened for internet-scale traffic, but this app's media is low-volume,
# internally-facing photo uploads, not a public CDN — a fine tradeoff until
# MEDIA_ROOT moves to real object storage (see that setting's own note about
# Railway's filesystem being ephemeral, which is a separate problem from
# this one: this fixes photos being unreachable at all; it doesn't fix them
# vanishing on the next deploy/restart).
urlpatterns += [
    re_path(r'^%s(?P<path>.*)$' % re.escape(settings.MEDIA_URL.lstrip('/')), serve_static, {
        'document_root': settings.MEDIA_ROOT,
    }),
]
