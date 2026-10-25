"""Small, dependency-free security header middleware.

Django already sets HSTS, X-Content-Type-Options and X-Frame-Options in
production through ``SecurityMiddleware``; this adds the headers Django does not
provide and a Content-Security-Policy that is deliberately compatible with the
existing pages (Google Fonts, Font Awesome, Google Identity Services and the
inline bootstrap scripts the frontend has always used).
"""

from django.conf import settings


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if 'X-Content-Type-Options' not in response:
            response['X-Content-Type-Options'] = 'nosniff'
        if 'Referrer-Policy' not in response:
            response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if 'Permissions-Policy' not in response:
            response['Permissions-Policy'] = (
                'geolocation=(self), camera=(), microphone=(), payment=()'
            )
        # Django's XFrameOptionsMiddleware already sets DENY; frame-ancestors is
        # the modern equivalent and covers browsers that ignore the legacy one.
        if 'X-Frame-Options' not in response:
            response['X-Frame-Options'] = 'DENY'

        # Do not add a policy to the Django admin: it uses its own inline
        # scripts/styles and is not part of the public product surface.
        if not request.path.startswith('/django-admin/'):
            csp = (
                "default-src 'self'; "
                "base-uri 'self'; "
                "object-src 'none'; "
                "frame-ancestors 'none'; "
                "form-action 'self'; "
                "script-src 'self' 'unsafe-inline' "
                "https://accounts.google.com https://cdnjs.cloudflare.com; "
                "style-src 'self' 'unsafe-inline' "
                "https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
                "font-src 'self' data: https://fonts.gstatic.com "
                "https://cdnjs.cloudflare.com; "
                "img-src 'self' data: blob: https:; "
                "connect-src 'self' https://accounts.google.com; "
                "frame-src https://accounts.google.com; "
                "worker-src 'self' blob:; "
                "manifest-src 'self'"
            )
            if not settings.DEBUG:
                csp += '; upgrade-insecure-requests'
            if 'Content-Security-Policy' not in response:
                response['Content-Security-Policy'] = csp

        # API responses may contain personal data; never let a shared cache
        # keep them. Public config endpoints override this explicitly.
        if request.path.startswith('/api/') and 'Cache-Control' not in response:
            response['Cache-Control'] = 'no-store'

        return response
