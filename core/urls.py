"""API routes for the core app."""

from django.urls import re_path

from .views import (
    CustomerRequestDetailView,
    CustomerRequestListCreateView,
    LoginView,
    MeView,
    RegisterView,
    TechnicianRequestAcceptView,
    TechnicianRequestListView,
    TechnicianRequestRejectView,
)

# The trailing slash is optional because the existing frontend calls these
# paths without one ("/api/auth/login"); APPEND_SLASH would otherwise issue a
# redirect that turns a POST into a GET.
urlpatterns = [
    re_path(r'^auth/login/?$', LoginView.as_view(), name='auth-login'),
    re_path(r'^auth/register/?$', RegisterView.as_view(), name='auth-register'),
    re_path(r'^auth/me/?$', MeView.as_view(), name='auth-me'),
    re_path(
        r'^customer/requests/?$',
        CustomerRequestListCreateView.as_view(),
        name='customer-requests',
    ),
    re_path(
        r'^customer/requests/(?P<pk>\d+)/?$',
        CustomerRequestDetailView.as_view(),
        name='customer-request-detail',
    ),
    re_path(
        r'^technician/requests/?$',
        TechnicianRequestListView.as_view(),
        name='technician-requests',
    ),
    re_path(
        r'^technician/requests/(?P<pk>\d+)/accept/?$',
        TechnicianRequestAcceptView.as_view(),
        name='technician-request-accept',
    ),
    re_path(
        r'^technician/requests/(?P<pk>\d+)/reject/?$',
        TechnicianRequestRejectView.as_view(),
        name='technician-request-reject',
    ),
]
