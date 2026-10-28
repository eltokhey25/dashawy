"""API routes for the core app."""

from django.urls import re_path

from .guest_views import (
    GuestRequestCancelView,
    GuestRequestCreateView,
    GuestRequestDetailView,
    GuestRequestPhotoView,
)
from .google_views import GoogleConfigView, GoogleLoginView
from .public_views import (
    ConsentView,
    PublicAreasView,
    PublicConfigView,
    PublicReviewsView,
    PublicSettingsView,
    PublicSpecialtiesView,
    PublicStatsView,
)
from .views import (
    AdminCustomerListView,
    AdminRequestCancelView,
    AdminRequestListView,
    AdminStatsView,
    AdminTechnicianApproveView,
    AdminTechnicianDetailView,
    AdminTechnicianDocumentView,
    AdminTechnicianListView,
    AdminTechnicianRejectView,
    AdminTechnicianVerifyView,
    AdminUserResetPasswordView,
    ChangePasswordView,
    CompleteProfileView,
    CustomerRequestDetailView,
    CustomerRequestListCreateView,
    GuestOrderLinkView,
    LoginView,
    LogoutView,
    MeView,
    RegisterView,
    TechnicianRegisterView,
    TechnicianRequestAcceptView,
    TechnicianRequestListView,
    TechnicianRequestRejectView,
    TechnicianRequestStartView,
)

# The trailing slash is optional because the existing frontend calls these
# paths without one ("/api/auth/login"); APPEND_SLASH would otherwise issue a
# redirect that turns a POST into a GET.
urlpatterns = [
    # Public configuration / landing data
    re_path(r'^public/config/?$', PublicConfigView.as_view(), name='public-config'),
    re_path(r'^public/settings/?$', PublicSettingsView.as_view(), name='public-settings'),
    re_path(r'^public/stats/?$', PublicStatsView.as_view(), name='public-stats'),
    re_path(r'^public/reviews/?$', PublicReviewsView.as_view(), name='public-reviews'),
    re_path(r'^specialties/?$', PublicSpecialtiesView.as_view(), name='public-specialties'),
    re_path(r'^alexandria-areas/?$', PublicAreasView.as_view(), name='public-areas'),
    re_path(r'^consent/?$', ConsentView.as_view(), name='consent'),

    # Authentication
    re_path(r'^auth/login/?$', LoginView.as_view(), name='auth-login'),
    re_path(r'^auth/register/?$', RegisterView.as_view(), name='auth-register'),
    re_path(
        r'^auth/register-technician/?$',
        TechnicianRegisterView.as_view(),
        name='auth-register-technician',
    ),
    re_path(r'^auth/logout/?$', LogoutView.as_view(), name='auth-logout'),
    re_path(r'^auth/me/?$', MeView.as_view(), name='auth-me'),
    re_path(
        r'^auth/complete-profile/?$',
        CompleteProfileView.as_view(),
        name='auth-complete-profile',
    ),
    re_path(r'^auth/google/config/?$', GoogleConfigView.as_view(), name='google-config'),
    re_path(r'^auth/google/?$', GoogleLoginView.as_view(), name='google-login'),
    re_path(
        r'^auth/change-password/?$',
        ChangePasswordView.as_view(),
        name='auth-change-password',
    ),

    # Customer
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
        r'^customer/guest-orders/link/?$',
        GuestOrderLinkView.as_view(),
        name='customer-guest-link',
    ),

    # Guest orders (no account)
    re_path(r'^guest/requests/?$', GuestRequestCreateView.as_view(), name='guest-requests'),
    re_path(
        r'^guest/requests/(?P<token>[A-Za-z0-9_\-]{20,128})/?$',
        GuestRequestDetailView.as_view(),
        name='guest-request-detail',
    ),
    re_path(
        r'^guest/requests/(?P<token>[A-Za-z0-9_\-]{20,128})/cancel/?$',
        GuestRequestCancelView.as_view(),
        name='guest-request-cancel',
    ),
    re_path(
        r'^guest/requests/(?P<token>[A-Za-z0-9_\-]{20,128})/photo/?$',
        GuestRequestPhotoView.as_view(),
        name='guest-request-photo',
    ),

    # Technician jobs
    re_path(
        r'^technician/requests/?$',
        TechnicianRequestListView.as_view(),
        name='technician-requests',
    ),
    re_path(
        r'^technician/requests/(?P<pk>\d+)/start/?$',
        TechnicianRequestStartView.as_view(),
        name='technician-request-start',
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

    # Admin dashboard
    re_path(r'^admin/stats/?$', AdminStatsView.as_view(), name='admin-stats'),
    re_path(
        r'^admin/requests/?$',
        AdminRequestListView.as_view(),
        name='admin-requests',
    ),
    re_path(
        r'^admin/technicians/?$',
        AdminTechnicianListView.as_view(),
        name='admin-technicians',
    ),
    re_path(
        r'^admin/technicians/(?P<pk>\d+)/?$',
        AdminTechnicianDetailView.as_view(),
        name='admin-technician-detail',
    ),
    re_path(
        r'^admin/technicians/(?P<pk>\d+)/verify/?$',
        AdminTechnicianVerifyView.as_view(),
        name='admin-technician-verify',
    ),
    re_path(
        r'^admin/technicians/(?P<pk>\d+)/approve/?$',
        AdminTechnicianApproveView.as_view(),
        name='admin-technician-approve',
    ),
    re_path(
        r'^admin/technicians/(?P<pk>\d+)/reject/?$',
        AdminTechnicianRejectView.as_view(),
        name='admin-technician-reject',
    ),
    re_path(
        r'^admin/technicians/(?P<pk>\d+)/document/(?P<doc_type>[a-z_]+)/?$',
        AdminTechnicianDocumentView.as_view(),
        name='admin-technician-document',
    ),
    re_path(
        r'^admin/customers/?$',
        AdminCustomerListView.as_view(),
        name='admin-customers',
    ),
    re_path(
        r'^admin/users/(?P<pk>\d+)/reset-password/?$',
        AdminUserResetPasswordView.as_view(),
        name='admin-user-reset-password',
    ),
    re_path(
        r'^admin/requests/(?P<pk>\d+)/cancel/?$',
        AdminRequestCancelView.as_view(),
        name='admin-request-cancel',
    ),
]
