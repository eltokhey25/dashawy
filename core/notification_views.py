"""Web Push subscription endpoints (port of notifications.routes.js)."""

from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import bad_request
from .services import push
from .views import _body


class VapidPublicKeyView(APIView):
    """GET /api/notifications/vapid-public-key — what the browser subscribes with."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'publicKey': push.public_key(), 'enabled': push.is_enabled()})


class PushSubscribeView(APIView):
    """POST /api/notifications/subscribe — {subscription}."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        subscription = _body(request).get('subscription')
        if not subscription:
            raise bad_request('الاشتراك مطلوب')
        try:
            push.save_subscription(
                request.user.pk, subscription, request.META.get('HTTP_USER_AGENT')
            )
        except (ValueError, AttributeError, TypeError):
            raise bad_request('الاشتراك غير صالح')
        return Response({'ok': True})


class PushUnsubscribeView(APIView):
    """POST /api/notifications/unsubscribe — {endpoint}."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        endpoint = _body(request).get('endpoint')
        if not endpoint:
            raise bad_request('endpoint مطلوب')
        removed = push.remove_subscription(request.user.pk, endpoint)
        return Response({'ok': True, 'removed': removed > 0})
