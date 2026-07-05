from django.contrib import admin
from django.urls import path

# django-absurd auto-registers its read-only queue models on the admin site.
# Mount the admin and browse Tasks / Runs / Checkpoints / Events / Waits / Queues.
urlpatterns = [
    path("admin/", admin.site.urls),
]
