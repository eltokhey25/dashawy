"""Backup endpoints: the daily cron trigger and an admin download.

* ``GET /api/cron/backup`` — called by Vercel's cron (``vercel.json``). Vercel
  sends ``Authorization: Bearer <CRON_SECRET>``; anything else gets 404.
* ``GET /api/admin/backup`` — a super admin downloads the same file by hand.
* ``GET /api/cron/settle`` — the other daily cron: auto-settlement of stalled
  cash confirmations (``core.services.settlement``).
"""

import os
import secrets

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from .permissions import IsSuperAdmin
from .services.backup import build_backup, run_daily_backup
from .services.settlement import settle_stale_reports


def cron_authorized(request):
    """Vercel's cron sends ``Authorization: Bearer <CRON_SECRET>``."""
    expected = os.environ.get('CRON_SECRET', '').strip()
    header = request.META.get('HTTP_AUTHORIZATION', '')
    return bool(
        request.method == 'GET'
        and expected
        and header.startswith('Bearer ')
        and secrets.compare_digest(header[7:].strip(), expected)
    )


@csrf_exempt
def cron_backup(request):
    if not cron_authorized(request):
        return HttpResponse(status=404)
    result = run_daily_backup()
    return JsonResponse(result, status=200 if result.get('ok') else 500)


@csrf_exempt
def cron_settle(request):
    """GET /api/cron/settle — settle cash confirmations nobody finished."""
    if not cron_authorized(request):
        return HttpResponse(status=404)
    return JsonResponse(settle_stale_reports())


class AdminBackupDownloadView(APIView):
    """GET /api/admin/backup — the gzipped fixture as a download."""

    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        filename, data = build_backup()
        response = HttpResponse(data, content_type='application/gzip')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = str(len(data))
        response['Cache-Control'] = 'private, no-store'
        return response
