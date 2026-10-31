"""API routes for the core app."""

from django.http import JsonResponse
from django.urls import re_path
from django.views.decorators.csrf import csrf_exempt

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
from .notification_views import PushSubscribeView, PushUnsubscribeView, VapidPublicKeyView
from .profile_views import (
    DataSubjectMyRequestsView,
    DataSubjectRequestView,
    PublicTechnicianDetailView,
    PublicTechnicianListView,
    TechnicianPortfolioItemView,
    TechnicianPortfolioView,
    TechnicianProfileDetailsView,
    TechnicianProfileImageView,
    TelegramLinkStatusView,
)
from .admin_manage_views import (
    AdminAccountDetailView,
    AdminAccountListView,
    AdminAccountResetPasswordView,
    AdminAccountToggleView,
    AdminCustomerBlockView,
    AdminCustomerDetailView,
    AdminDataRequestListView,
    AdminDataRequestUpdateView,
    AdminRequestAssignView,
    AdminRequestBroadcastsView,
    AdminRequestDeleteView,
    AdminRequestQueuedView,
    AdminRequestRebroadcastView,
    AdminRequestUnassignView,
    AdminReviewActionView,
    AdminReviewDeleteView,
    AdminReviewListView,
    AdminReviewStatsView,
    AdminTechnicianPendingView,
    AdminUserEraseView,
)
from .admin_config_views import (
    AdminPricingListView,
    AdminPricingUpdateView,
    AdminSettingsView,
    AdminSpecialtyDetailView,
    AdminSpecialtyListView,
)
from .admin_finance_views import (
    AdminPaymentConfirmView,
    AdminPaymentRefundView,
    AdminPaymentResolveView,
    AdminPaymentsDisputedView,
    AdminPaymentsPendingView,
    AdminWalletListView,
    AdminWalletPayoutView,
    AdminWalletTransactionsView,
    AdminWithdrawalActionView,
    AdminWithdrawalListView,
)
from .chat_views import MessageMarkReadView, MessageThreadView, MessageUnreadCountView
from .wallet_views import TechnicianWalletView, TechnicianWithdrawalsView
from .completion_views import (
    CustomerMyReviewsView,
    CustomerPayView,
    CustomerPendingActionsView,
    CustomerPendingReviewsView,
    CustomerReportPaymentView,
    CustomerReviewView,
    TechnicianReportPaymentView,
    TechnicianRequestCompleteView,
    TechnicianReviewReplyView,
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
    CustomerRequestCancelView,
    CustomerRequestDetailView,
    CustomerRequestListCreateView,
    GuestOrderLinkView,
    LoginView,
    LogoutView,
    MeView,
    RegisterView,
    TechnicianBroadcastAcceptView,
    TechnicianBroadcastDeclineView,
    TechnicianIncomingView,
    TechnicianRegisterView,
    TechnicianRequestAcceptView,
    TechnicianRequestListView,
    TechnicianRequestRejectView,
    TechnicianRequestStartView,
)


@csrf_exempt
def api_not_found(request, *args, **kwargs):
    """Answer every unknown ``/api/`` path with JSON, never the frontend.

    Without this catch-all, ``/api/<unknown>`` falls through to the frontend
    handler in ``dashawy_backend.urls``. That view is not CSRF-exempt, so a
    POST to a retired endpoint (for example the old
    ``/api/auth/request-otp`` still cached by a device) was rejected with a
    ``403 CSRF cookie not set`` before it could return a plain 404. Keeping
    the catch-all inside the API URLconf means unknown API calls always get
    the same JSON error shape as any other API failure.
    """

    return JsonResponse({'error': 'المسار غير موجود'}, status=404)


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
        r'^customer/requests/pending-actions/?$',
        CustomerPendingActionsView.as_view(),
        name='customer-pending-actions',
    ),
    re_path(
        r'^customer/requests/(?P<pk>\d+)/report-payment/?$',
        CustomerReportPaymentView.as_view(),
        name='customer-report-payment',
    ),
    re_path(
        r'^customer/requests/(?P<pk>\d+)/pay/?$',
        CustomerPayView.as_view(),
        name='customer-pay',
    ),
    re_path(
        r'^customer/requests/(?P<pk>\d+)/review/?$',
        CustomerReviewView.as_view(),
        name='customer-review',
    ),
    re_path(
        r'^customer/pending-reviews/?$',
        CustomerPendingReviewsView.as_view(),
        name='customer-pending-reviews',
    ),
    re_path(
        r'^customer/my-reviews/?$',
        CustomerMyReviewsView.as_view(),
        name='customer-my-reviews',
    ),
    re_path(
        r'^customer/requests/(?P<pk>\d+)/cancel/?$',
        CustomerRequestCancelView.as_view(),
        name='customer-request-cancel',
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
        r'^technician/wallet/?$',
        TechnicianWalletView.as_view(),
        name='technician-wallet',
    ),
    re_path(
        r'^technician/withdrawals/?$',
        TechnicianWithdrawalsView.as_view(),
        name='technician-withdrawals',
    ),
    # Chat (the literal unread-count route must come before <request_id>)
    re_path(
        r'^messages/unread-count/?$',
        MessageUnreadCountView.as_view(),
        name='messages-unread-count',
    ),
    re_path(
        r'^messages/(?P<request_id>\d+)/read/?$',
        MessageMarkReadView.as_view(),
        name='messages-read',
    ),
    re_path(
        r'^messages/(?P<request_id>\d+)/?$',
        MessageThreadView.as_view(),
        name='messages-thread',
    ),
    re_path(
        r'^technician/incoming/?$',
        TechnicianIncomingView.as_view(),
        name='technician-incoming',
    ),
    re_path(
        r'^technician/broadcasts/(?P<pk>\d+)/accept/?$',
        TechnicianBroadcastAcceptView.as_view(),
        name='technician-broadcast-accept',
    ),
    re_path(
        r'^technician/broadcasts/(?P<pk>\d+)/(?:decline|reject)/?$',
        TechnicianBroadcastDeclineView.as_view(),
        name='technician-broadcast-decline',
    ),
    re_path(
        r'^technician/requests/(?P<pk>\d+)/complete/?$',
        TechnicianRequestCompleteView.as_view(),
        name='technician-request-complete',
    ),
    re_path(
        r'^technician/requests/(?P<pk>\d+)/report-payment/?$',
        TechnicianReportPaymentView.as_view(),
        name='technician-report-payment',
    ),
    re_path(
        r'^technician/reviews/(?P<pk>\d+)/reply/?$',
        TechnicianReviewReplyView.as_view(),
        name='technician-review-reply',
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
    # Technician profile and public technician pages
    re_path(
        r'^technician/profile-details/?$',
        TechnicianProfileDetailsView.as_view(),
        name='technician-profile-details',
    ),
    re_path(
        r'^technician/profile-image/?$',
        TechnicianProfileImageView.as_view(),
        name='technician-profile-image',
    ),
    re_path(r'^technician/portfolio/?$', TechnicianPortfolioView.as_view(), name='technician-portfolio'),
    re_path(
        r'^technician/portfolio/(?P<pk>\d+)/?$',
        TechnicianPortfolioItemView.as_view(),
        name='technician-portfolio-item',
    ),
    re_path(r'^public/technicians/?$', PublicTechnicianListView.as_view(), name='public-technicians'),
    re_path(
        r'^public/technicians/(?P<pk>\d+)/?$',
        PublicTechnicianDetailView.as_view(),
        name='public-technician-detail',
    ),
    # Privacy requests, Telegram status and Web Push
    re_path(r'^data-subject/request/?$', DataSubjectRequestView.as_view(), name='data-subject-request'),
    re_path(
        r'^data-subject/my-requests/?$',
        DataSubjectMyRequestsView.as_view(),
        name='data-subject-my-requests',
    ),
    re_path(r'^auth/telegram-link/?$', TelegramLinkStatusView.as_view(), name='telegram-link-status'),
    re_path(
        r'^notifications/vapid-public-key/?$',
        VapidPublicKeyView.as_view(),
        name='vapid-public-key',
    ),
    re_path(r'^notifications/subscribe/?$', PushSubscribeView.as_view(), name='push-subscribe'),
    re_path(r'^notifications/unsubscribe/?$', PushUnsubscribeView.as_view(), name='push-unsubscribe'),
    # Admin management
    re_path(r'^admin/admins/?$', AdminAccountListView.as_view(), name='admin-admins'),
    re_path(
        r'^admin/admins/(?P<pk>\d+)/reset-password/?$',
        AdminAccountResetPasswordView.as_view(),
        name='admin-admin-reset-password',
    ),
    re_path(
        r'^admin/admins/(?P<pk>\d+)/toggle-active/?$',
        AdminAccountToggleView.as_view(),
        name='admin-admin-toggle',
    ),
    re_path(r'^admin/admins/(?P<pk>\d+)/?$', AdminAccountDetailView.as_view(), name='admin-admin-detail'),
    re_path(
        r'^admin/technicians/pending/?$',
        AdminTechnicianPendingView.as_view(),
        name='admin-technicians-pending',
    ),
    re_path(
        r'^admin/customers/(?P<pk>\d+)/block/?$',
        AdminCustomerBlockView.as_view(),
        name='admin-customer-block',
    ),
    re_path(r'^admin/customers/(?P<pk>\d+)/?$', AdminCustomerDetailView.as_view(), name='admin-customer-detail'),
    re_path(r'^admin/requests/queued/?$', AdminRequestQueuedView.as_view(), name='admin-requests-queued'),
    re_path(
        r'^admin/requests/(?P<pk>\d+)/assign/?$',
        AdminRequestAssignView.as_view(),
        name='admin-request-assign',
    ),
    re_path(
        r'^admin/requests/(?P<pk>\d+)/unassign/?$',
        AdminRequestUnassignView.as_view(),
        name='admin-request-unassign',
    ),
    re_path(
        r'^admin/requests/(?P<pk>\d+)/rebroadcast/?$',
        AdminRequestRebroadcastView.as_view(),
        name='admin-request-rebroadcast',
    ),
    re_path(
        r'^admin/requests/(?P<pk>\d+)/broadcasts/?$',
        AdminRequestBroadcastsView.as_view(),
        name='admin-request-broadcasts',
    ),
    re_path(r'^admin/requests/(?P<pk>\d+)/?$', AdminRequestDeleteView.as_view(), name='admin-request-delete'),
    re_path(r'^admin/reviews/?$', AdminReviewListView.as_view(), name='admin-reviews'),
    re_path(r'^admin/reviews/stats/?$', AdminReviewStatsView.as_view(), name='admin-review-stats'),
    re_path(
        r'^admin/reviews/(?P<pk>\d+)/(?P<action>hide|unhide)/?$',
        AdminReviewActionView.as_view(),
        name='admin-review-action',
    ),
    re_path(r'^admin/reviews/(?P<pk>\d+)/?$', AdminReviewDeleteView.as_view(), name='admin-review-delete'),
    re_path(
        r'^admin/data-subject-requests/?$',
        AdminDataRequestListView.as_view(),
        name='admin-data-requests',
    ),
    re_path(
        r'^admin/data-subject-requests/(?P<pk>\d+)/?$',
        AdminDataRequestUpdateView.as_view(),
        name='admin-data-request-update',
    ),
    re_path(r'^admin/users/(?P<pk>\d+)/erase/?$', AdminUserEraseView.as_view(), name='admin-user-erase'),
    # Admin configuration
    re_path(r'^admin/settings/?$', AdminSettingsView.as_view(), name='admin-settings'),
    re_path(r'^admin/pricing/?$', AdminPricingListView.as_view(), name='admin-pricing'),
    re_path(
        r'^admin/pricing/(?P<service_key>[a-z][a-z0-9_]*)/?$',
        AdminPricingUpdateView.as_view(),
        name='admin-pricing-update',
    ),
    re_path(r'^admin/specialties/?$', AdminSpecialtyListView.as_view(), name='admin-specialties'),
    re_path(
        r'^admin/specialties/(?P<pk>\d+)/?$',
        AdminSpecialtyDetailView.as_view(),
        name='admin-specialty-detail',
    ),
    # Admin money screens
    re_path(r'^admin/wallets/?$', AdminWalletListView.as_view(), name='admin-wallets'),
    re_path(
        r'^admin/wallets/(?P<technician_id>\d+)/transactions/?$',
        AdminWalletTransactionsView.as_view(),
        name='admin-wallet-transactions',
    ),
    re_path(
        r'^admin/wallets/(?P<technician_id>\d+)/payout/?$',
        AdminWalletPayoutView.as_view(),
        name='admin-wallet-payout',
    ),
    re_path(r'^admin/withdrawals/?$', AdminWithdrawalListView.as_view(), name='admin-withdrawals'),
    re_path(
        r'^admin/withdrawals/(?P<pk>\d+)/(?P<action>approve|paid|reject)/?$',
        AdminWithdrawalActionView.as_view(),
        name='admin-withdrawal-action',
    ),
    re_path(r'^admin/payments/pending/?$', AdminPaymentsPendingView.as_view(), name='admin-payments-pending'),
    re_path(r'^admin/payments/disputed/?$', AdminPaymentsDisputedView.as_view(), name='admin-payments-disputed'),
    re_path(
        r'^admin/payments/(?P<pk>\d+)/resolve-dispute/?$',
        AdminPaymentResolveView.as_view(),
        name='admin-payment-resolve',
    ),
    re_path(
        r'^admin/payments/(?P<pk>\d+)/confirm/?$',
        AdminPaymentConfirmView.as_view(),
        name='admin-payment-confirm',
    ),
    re_path(
        r'^admin/payments/(?P<pk>\d+)/refund/?$',
        AdminPaymentRefundView.as_view(),
        name='admin-payment-refund',
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

    # Must stay last: unknown API paths return JSON instead of being handed
    # to the frontend catch-all (which would reject a POST on CSRF grounds).
    re_path(r'^.*$', api_not_found, name='api-not-found'),
]
