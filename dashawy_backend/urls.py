"""URL configuration for the Dashawy Django project.

Two things share this URLconf:

* ``/api/`` is the Django REST API.
* every other path is a file in ``frontend/``. HTML pages are rendered through
  the Django template engine so the deployment domain (``SITE_URL``) and the
  central contact config can be injected without hard-coding them. CSS, JS and
  images are still served as plain static files.

Django's own admin site is mounted at ``/django-admin/`` rather than ``/admin/``
because ``/admin/...`` is the product's admin dashboard.
"""

from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.views import serve as staticfiles_serve
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.urls import include, path, re_path
from django.views.static import serve as static_serve

from core.site_config import template_context

FRONTEND_ROOT = Path(settings.BASE_DIR) / 'frontend'

# Pages that must never appear in search results even if a crawler finds a link.
NOINDEX_PREFIXES = (
    'admin/',
    'customer/',
    'technician/',
    'django-admin/',
    'change-password',
    'complete-profile',
    'verify-otp',
    'telegram-link',
    'chat',
    'auth',
    'offline',
    'track',
)


def _render_page(request, template_name, status_code=200):
    html = render_to_string(
        template_name, template_context(), request=request
    )
    response = HttpResponse(
        html, status=status_code, content_type='text/html; charset=utf-8'
    )
    response['Cache-Control'] = 'no-cache'
    if status_code >= 400 or any(
        template_name.startswith(prefix) for prefix in NOINDEX_PREFIXES
    ):
        response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def serve_frontend(request, path=''):
    """Serve a file from ``frontend/``, including extensionless page routes.

    ``/`` becomes ``index.html`` and ``/admin`` becomes the dashboard's login
    page. A path with no suffix also tries ``.html`` and ``/index.html`` so the
    existing links such as ``/admin/dashboard`` keep working unchanged.
    """
    clean = str(path or '').strip('/')
    if clean == '':
        clean = 'index.html'
    elif clean == 'admin':
        clean = 'admin/login.html'
    # Template partials are included by pages, never served as standalone URLs.
    if clean == 'partials' or clean.startswith('partials/'):
        raise Http404('Frontend asset not found')

    candidates = [clean]
    if not Path(clean).suffix:
        candidates.append(clean + '.html')
        candidates.append(str(Path(clean) / 'index.html'))

    root = FRONTEND_ROOT.resolve()
    for candidate in candidates:
        target = (FRONTEND_ROOT / candidate).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            # ``..`` or an absolute path escaped the frontend directory.
            continue
        if target.is_file():
            if target.suffix.lower() == '.html':
                return _render_page(request, candidate)
            return static_serve(
                request, candidate, document_root=str(FRONTEND_ROOT)
            )

    raise Http404('Frontend asset not found')


def _renderer_file(request, filename, content_type):
    root = FRONTEND_ROOT.resolve()
    target = (FRONTEND_ROOT / filename).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise Http404('Frontend text file not found')
    if not target.is_file():
        raise Http404('Frontend text file not found')
    content = render_to_string(filename, template_context(), request=request)
    return HttpResponse(content, content_type=content_type)


def serve_frontend_text(request, filename):
    """Plain-text crawler files with an explicit content type."""
    return _renderer_file(request, filename, 'text/plain; charset=utf-8')


def serve_sitemap(request):
    return _renderer_file(request, 'sitemap.xml', 'application/xml; charset=utf-8')


def serve_manifest(request):
    """Render the web app manifest so its start URL follows ``SITE_URL``."""
    return _renderer_file(
        request, 'manifest.webmanifest', 'application/manifest+json; charset=utf-8'
    )


def serve_public_upload(request, path):
    """Serve only the non-sensitive technician pictures.

    Identity documents live in ``uploads/technicians/<user>/`` (and legacy
    ``pending`` folders) and are deliberately not reachable here. They are
    streamed by the admin-only API endpoint instead.
    """
    import mimetypes

    clean = str(path or '').replace('\\', '/').lstrip('/')
    allowed = (
        'technicians/profiles/',
        'technicians/portfolio/',
    )
    if not any(clean.startswith(prefix) for prefix in allowed):
        raise Http404('File not found')
    if '..' in clean.split('/'):
        raise Http404('File not found')

    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / clean).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise Http404('File not found')
    if not target.is_file():
        raise Http404('File not found')

    mime, _ = mimetypes.guess_type(str(target))
    if not (mime or '').startswith(('image/', 'video/')):
        raise Http404('File not found')
    response = FileResponse(open(target, 'rb'), content_type=mime)
    response['Cache-Control'] = 'public, max-age=86400'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def api_not_found(message='المسار غير موجود'):
    return JsonResponse({'error': message}, status=404)


def handler404(request, exception=None):
    if request.path.startswith('/api/'):
        return api_not_found()
    return _render_page(request, '404.html', status_code=404)


def handler500(request):
    if request.path.startswith('/api/'):
        return JsonResponse({'error': 'حدث خطأ غير متوقع'}, status=500)
    return _render_page(request, '500.html', status_code=500)


urlpatterns = [
    # API first: it must win over the frontend catch-all.
    path('api/', include('core.urls')),
    # Django's built-in admin lives here so /admin/... can serve the frontend.
    path('django-admin/', admin.site.urls),
    # Crawler files are rendered so the domain comes from SITE_URL.
    path('llms.txt', serve_frontend_text, {'filename': 'llms.txt'}, name='llms'),
    path(
        'robots.txt',
        serve_frontend_text,
        {'filename': 'robots.txt'},
        name='robots',
    ),
    path('sitemap.xml', serve_sitemap, name='sitemap'),
    path('manifest.webmanifest', serve_manifest, name='manifest'),
    # Public technician photos only (never identity documents).
    re_path(r'^uploads/(?P<path>.*)$', serve_public_upload, name='public-upload'),
    # Also expose the same assets under STATIC_URL. `runserver` normally
    # intercepts this prefix, but this keeps the test client and other WSGI
    # servers consistent.
    re_path(
        r'^static/(?P<path>.*)$',
        staticfiles_serve,
        {'insecure': True},
        name='static',
    ),
    # The landing page.
    re_path(r'^$', serve_frontend, name='home'),
    # HTML/CSS/JS/images/manifest/service worker, with extensionless fallback.
    re_path(r'^(?P<path>.*)$', serve_frontend, name='frontend'),
]
