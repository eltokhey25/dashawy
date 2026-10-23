"""API routes for the core app."""

from django.urls import re_path

from .views import LoginView, MeView, RegisterView

# The trailing slash is optional because the existing frontend calls these
# paths without one ("/api/auth/login"); APPEND_SLASH would otherwise issue a
# redirect that turns a POST into a GET.
urlpatterns = [
    re_path(r'^auth/login/?$', LoginView.as_view(), name='auth-login'),
    re_path(r'^auth/register/?$', RegisterView.as_view(), name='auth-register'),
    re_path(r'^auth/me/?$', MeView.as_view(), name='auth-me'),
]
