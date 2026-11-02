"""Apply database migrations from the admin panel.

Vercel deploys code but never runs ``manage.py migrate``, and a deploy whose
migrations were not applied fails wherever it touches a new table. The super
admin can apply them here with one button instead of a terminal:

* ``GET  /api/admin/system/migrations`` lists the migrations not yet applied;
* ``POST /api/admin/system/migrate`` applies them and reports the outcome.

Only migrations shipped in the deployed code can run, and the endpoint takes
no input, so it cannot be steered to do anything else.
"""

import io
import logging

from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .permissions import IsSuperAdmin

logger = logging.getLogger(__name__)


def pending_migrations():
    connection = connections[DEFAULT_DB_ALIAS]
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [f'{migration.app_label}.{migration.name}' for migration, _ in plan]


class AdminMigrationsView(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        pending = pending_migrations()
        return Response({'pending': pending, 'count': len(pending)})


class AdminMigrateView(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def post(self, request):
        before = pending_migrations()
        if not before:
            return Response({'message': 'قاعدة البيانات محدّثة بالفعل', 'applied': [], 'pending': []})
        output = io.StringIO()
        try:
            call_command('migrate', interactive=False, verbosity=1, stdout=output, stderr=output)
        except Exception as exc:  # noqa: BLE001 - report it to the admin instead of a bare 500
            logger.exception('Migration from the admin panel failed')
            detail = str(exc)
            if 'Duplicate account contacts' in detail:
                message = (
                    'فيه حسابات متكررة لازم تتصلح الأول (نفس رقم الموبايل أو حساب جوجل أو '
                    'تليجرام على أكتر من حساب). التفاصيل تحت.'
                )
            else:
                message = 'التحديث ما كملش. التفاصيل تحت.'
            return Response(
                {
                    'error': message,
                    'detail': detail[:2000],
                    'pending': pending_migrations(),
                },
                status=status.HTTP_409_CONFLICT,
            )
        after = pending_migrations()
        return Response(
            {
                'message': 'تم تحديث قاعدة البيانات',
                'applied': [name for name in before if name not in after],
                'pending': after,
                'output': output.getvalue()[-2000:],
            }
        )
