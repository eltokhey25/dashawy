"""JSON lockout responses for the Dashawy API.

``django-axes`` defaults to an HTML 429 page. The existing frontend expects the
API's flattened ``{"error": ...}`` shape, so API lockouts return JSON while the
Django admin keeps a plain readable page.
"""

from axes.conf import settings as axes_settings
from django.http import HttpResponse, JsonResponse

LOCKOUT_MESSAGE = (
    'تم قفل الحساب مؤقتاً بسبب محاولات دخول متكررة. '
    'حاول مرة أخرى بعد 15 دقيقة.'
)


def lockout_response(request, original_response=None, credentials=None):
    status_code = axes_settings.AXES_HTTP_RESPONSE_CODE
    if request.path.startswith('/api/'):
        return JsonResponse(
            {'error': LOCKOUT_MESSAGE, 'code': 'account_locked'},
            status=status_code,
        )
    return HttpResponse(LOCKOUT_MESSAGE, status=status_code)
