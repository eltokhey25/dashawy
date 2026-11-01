"""Admin configuration: site settings, service prices and specialties.

Ported from ``backend/routes/admin.routes.js``. Settings and prices need the
``settings`` permission; specialties need ``specialties``. Saving clears the
cached site configuration so the public pages pick the change up.
"""

import re

from django.db import IntegrityError
from django.db.models import Max
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import ApiError, bad_request
from .models import Request, ServicePricing, Setting, Specialty, Technician, utc_now
from .permissions import AdminPermission
from .site_config import clear_site_config_cache
from .validation import is_non_empty_string
from .views import _body, _json_ready

DEFAULT_SPECIALTY_ICON = 'fa-solid fa-wrench'
EDITABLE_SETTINGS = frozenset(
    {
        'site_name',
        'site_tagline',
        'contact_phone',
        'contact_email',
        'contact_address',
        'whatsapp',
        'facebook_url',
        'instagram_url',
        'instapay_number',
        'instapay_name',
        'wallet_debt_limit',
        'wallet_debt_warning',
        'backup_telegram_chat_id',
        'payment_methods',
        'commission_percent',
        'working_hours',
        'hero_title',
        'hero_subtitle',
        'support_phone',
        'support_email',
        'auto_assign_enabled',
        'auto_assign_radius_km',
        'min_withdrawal',
        'withdrawal_eta_hours',
    }
)


def _all_settings():
    return dict(Setting.objects.values_list('key', 'value'))


def _truthy(value):
    return value in (True, 1, '1', 'true', 'True')


def _specialty_json(specialty):
    row = _json_ready(Specialty.objects.filter(pk=specialty.pk).values().first())
    row['enabled'] = 1 if row.get('enabled') else 0
    return row


class AdminSettingsView(APIView):
    """GET/PUT /api/admin/settings — the editable key/value settings."""

    permission_classes = [IsAuthenticated, AdminPermission('settings')]

    def get(self, request):
        return Response({'settings': _all_settings()})

    def put(self, request):
        data = _body(request)
        patch = data.get('settings') if isinstance(data.get('settings'), dict) else data
        for key, value in patch.items():
            if key not in EDITABLE_SETTINGS:
                continue
            text = '' if value is None else str(value)
            if len(text) > 500:
                raise bad_request('قيمة الإعداد غير صالحة')
            Setting.objects.update_or_create(key=key, defaults={'value': text.strip()})
        clear_site_config_cache()
        return Response({'message': 'تم حفظ الإعدادات', 'settings': _all_settings()})


class AdminPricingListView(APIView):
    """GET /api/admin/pricing — one row per specialty with its price."""

    permission_classes = [IsAuthenticated, AdminPermission('settings')]

    def get(self, request):
        prices = {row.service_key: row for row in ServicePricing.objects.all()}
        pricing = []
        for specialty in Specialty.objects.order_by('sort_order', 'id'):
            row = prices.get(specialty.key)
            pricing.append(
                {
                    'service_key': specialty.key,
                    'label': specialty.label,
                    'icon': specialty.icon,
                    'sort_order': specialty.sort_order,
                    'price': float(row.price or 0) if row else 0,
                    'is_active': (1 if row.is_active is not False else 0) if row else 1,
                    'updated_at': (
                        row.updated_at.strftime('%Y-%m-%d %H:%M:%S')
                        if row and row.updated_at
                        else None
                    ),
                }
            )
        return Response({'pricing': pricing})


class AdminPricingUpdateView(APIView):
    """PUT /api/admin/pricing/<service_key> — {price?, is_active?}."""

    permission_classes = [IsAuthenticated, AdminPermission('settings')]

    def put(self, request, service_key):
        data = _body(request)
        exists = (
            Specialty.objects.filter(key=service_key).exists()
            or ServicePricing.objects.filter(service_key=service_key).exists()
        )
        if not exists:
            raise ApiError('الخدمة غير موجودة', status.HTTP_404_NOT_FOUND)
        updates = {}
        if 'price' in data:
            try:
                price = float(data.get('price'))
            except (TypeError, ValueError):
                price = -1
            if not 0 <= price <= 100000:
                raise bad_request('سعر غير صالح')
            updates['price'] = price
        if 'is_active' in data:
            updates['is_active'] = _truthy(data.get('is_active'))
        if not updates:
            raise bad_request('لا توجد تغييرات')
        updates['updated_at'] = utc_now()
        ServicePricing.objects.update_or_create(service_key=service_key, defaults=updates)
        return Response({'message': 'تم تحديث السعر'})


class AdminSpecialtyListView(APIView):
    """GET/POST /api/admin/specialties — list with usage counts, or add one."""

    permission_classes = [IsAuthenticated, AdminPermission('specialties')]

    def get(self, request):
        rows = []
        for specialty in Specialty.objects.order_by('sort_order', 'id'):
            row = _specialty_json(specialty)
            row['technicians_count'] = Technician.objects.filter(specialty=specialty.key).count()
            row['requests_count'] = Request.objects.filter(specialty=specialty.key).count()
            rows.append(row)
        return Response({'specialties': rows})

    def post(self, request):
        data = _body(request)
        key = str(data.get('key') or '').strip()
        if not re.fullmatch(r'[a-z][a-z0-9_]*', key):
            raise bad_request('مفتاح التخصص غير صالح (حروف لاتينية صغيرة)')
        label = data.get('label')
        if not is_non_empty_string(label, 100):
            raise bad_request('اسم التخصص غير صالح')
        if Specialty.objects.filter(key=key).exists():
            raise bad_request('هذا المفتاح مستخدم بالفعل')
        sort_order = data.get('sort_order')
        if sort_order in (None, ''):
            sort_order = (Specialty.objects.aggregate(m=Max('sort_order'))['m'] or 0) + 1
        try:
            specialty = Specialty.objects.create(
                key=key,
                label=label.strip(),
                icon=str(data.get('icon') or DEFAULT_SPECIALTY_ICON).strip(),
                enabled=data.get('enabled') not in (False, 0, '0', 'false'),
                sort_order=int(sort_order),
            )
        except (IntegrityError, ValueError):
            raise bad_request('هذا المفتاح مستخدم بالفعل')
        ServicePricing.objects.get_or_create(service_key=key, defaults={'price': 0})
        clear_site_config_cache()
        return Response(
            {'message': 'تم إضافة التخصص', 'specialty': _specialty_json(specialty)},
            status=status.HTTP_201_CREATED,
        )


class AdminSpecialtyDetailView(APIView):
    """PUT/DELETE /api/admin/specialties/<id>."""

    permission_classes = [IsAuthenticated, AdminPermission('specialties')]

    def _get(self, pk):
        specialty = Specialty.objects.filter(pk=pk).first()
        if specialty is None:
            raise ApiError('التخصص غير موجود', status.HTTP_404_NOT_FOUND)
        return specialty

    def put(self, request, pk):
        specialty = self._get(pk)
        data = _body(request)
        updates = {}
        if 'label' in data:
            if not is_non_empty_string(data.get('label'), 100):
                raise bad_request('اسم التخصص غير صالح')
            updates['label'] = data['label'].strip()
        if 'icon' in data:
            if not is_non_empty_string(data.get('icon'), 80):
                raise bad_request('أيقونة التخصص غير صالحة')
            updates['icon'] = data['icon'].strip()
        if 'enabled' in data:
            updates['enabled'] = _truthy(data.get('enabled'))
        if 'sort_order' in data:
            try:
                updates['sort_order'] = int(data.get('sort_order'))
            except (TypeError, ValueError):
                raise bad_request('الترتيب غير صالح')
        if updates:
            Specialty.objects.filter(pk=specialty.pk).update(**updates)
            clear_site_config_cache()
        return Response({'message': 'تم تحديث التخصص', 'specialty': _specialty_json(specialty)})

    def delete(self, request, pk):
        specialty = self._get(pk)
        used = (
            Technician.objects.filter(specialty=specialty.key).exists()
            or Request.objects.filter(specialty=specialty.key).exists()
        )
        if used:
            raise bad_request('لا يمكن حذف تخصص مستخدم من الفنيين أو الطلبات — عطّله بدلاً من ذلك')
        specialty.delete()
        clear_site_config_cache()
        return Response({'message': 'تم حذف التخصص'})
