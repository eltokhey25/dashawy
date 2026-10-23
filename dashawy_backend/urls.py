"""URL configuration for the Dashawy Django project.

Two things share this URLconf during the migration:

* ``/api/`` is the Django REST API.
* every other path is a file in ``frontend/``. The legacy Express server did
  the same thing with ``express.static`` plus explicit routes for the
  extensionless admin pages (``/admin/dashboard`` and friends), so the
  catch-all below reproduces both behaviours.

Django's own admin site is mounted at ``/django-admin/`` rather than
``/admin/`` because ``/admin/...`` is the product's admin dashboard.
"""

from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.views import serve as staticfiles_serve
from django.http import Http404
from django.urls import include, path, re_path
from django.views.static import serve as static_serve

FRONTEND_ROOT = Path(settings.BASE_DIR) / 'frontend'


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
            return static_serve(
                request, candidate, document_root=str(FRONTEND_ROOT)
            )

    raise Http404('Frontend asset not found')


urlpatterns = [
    # API first: it must win over the frontend catch-all.
    path('api/', include('core.urls')),
    # Django's built-in admin lives here so /admin/... can serve the frontend.
    path('django-admin/', admin.site.urls),
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
