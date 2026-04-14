from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render
from django.conf import settings
from django.conf.urls.static import static


def home(request):
    return render(request, 'home.html')


urlpatterns = [
    path('', home, name='home'),  # 👈 THIS IS IMPORTANT
    path('admin/', admin.site.urls),

    # Main apps
    path('accounts/', include('accounts.urls')),
    path('chat/', include('chatapp.urls')),
    path('', include('rentals.urls')),
]


# Media files (profile images, property images)
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


# Static files (optional but helpful during development)
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")