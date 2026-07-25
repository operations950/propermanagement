from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core.views import StaffLoginView, google_login_callback, google_login_start

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', StaffLoginView.as_view(), name='login'),
    path('login/google/', google_login_start, name='google_login_start'),
    path('login/google/callback/', google_login_callback, name='google_login_callback'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('vendor/', include('vendorportal.urls')),
    path('supplies/', include('supplies.urls')),
    path('', include('core.urls')),
    path('', include('intake.urls')),
    path('', include('tickets.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
