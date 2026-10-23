"""
Error formatting for the Django API.

The legacy frontend's fetch wrapper reads ``data.error`` and treats any non-2xx
response as an exception. DRF's default shape (``{"detail": ...}`` or a dict of
field errors) would therefore show only the generic fallback message, so every
API exception is flattened to ``{"error": "...", "code": "..."}`` here.
"""

from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    NotAuthenticated,
    PermissionDenied,
)
from rest_framework.response import Response


class ApiError(APIException):
    """An API failure with an Arabic message and an HTTP status."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, detail, status_code=None, code=None):
        if status_code is not None:
            self.status_code = status_code
        self.api_code = code
        super().__init__(detail)


def bad_request(message, code=None):
    return ApiError(message, status.HTTP_400_BAD_REQUEST, code)


def unauthorized(message, code=None):
    return ApiError(message, status.HTTP_401_UNAUTHORIZED, code)


def forbidden(message, code=None):
    return ApiError(message, status.HTTP_403_FORBIDDEN, code)


def _first_message(data):
    """Pull the first human-readable string out of any DRF error structure."""
    if isinstance(data, dict):
        if 'detail' in data:
            return str(data['detail'])
        if 'error' in data:
            return str(data['error'])
        for value in data.values():
            message = _first_message(value)
            if message:
                return message
        return ''
    if isinstance(data, (list, tuple)):
        for item in data:
            message = _first_message(item)
            if message:
                return message
        return ''
    return str(data) if data not in (None, '') else ''


def api_exception_handler(exc, context):
    # Imported here rather than at module scope: DRF imports the configured
    # authentication class while rest_framework.views is still initialising, so
    # a top-level ``from rest_framework.views import exception_handler`` would
    # close an import cycle (views -> settings -> authentication -> exceptions).
    from rest_framework.views import exception_handler

    response = exception_handler(exc, context)
    if response is None:
        return None

    message = _first_message(response.data)

    # The login/register views already raise ApiError with Arabic text. The
    # remaining branches translate the stock DRF/SimpleJWT messages the
    # frontend would otherwise display in English.
    if isinstance(exc, ApiError):
        payload = {'error': message}
        if exc.api_code:
            payload['code'] = exc.api_code
        response.data = payload
        return response

    if isinstance(exc, NotAuthenticated):
        message = 'يجب تسجيل الدخول أولاً'
    elif isinstance(exc, AuthenticationFailed):
        message = 'الجلسة غير صالحة أو منتهية. يرجى تسجيل الدخول مجدداً'
    elif isinstance(exc, PermissionDenied):
        message = 'ليس لديك صلاحية لتنفيذ هذا الإجراء'

    response.data = {'error': message or 'حدث خطأ غير متوقع'}
    return response
