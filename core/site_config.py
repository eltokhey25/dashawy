"""Central, deployment-agnostic configuration for the public site.

Every public-facing value (domain, phone, WhatsApp, e-mail, social links and
commission rate) comes from here. Environment variables win; the admin-editable
``settings`` table is the fallback so the existing data keeps working; finally a
small, non-personal default is used.

Nothing in this module contains a real phone number, e-mail or domain: the
production values live in ``.env`` (see ``.env.example``) or in the database.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache

from core.validation import normalize_phone

CACHE_KEY = 'dashawy:site-config:v2'
CACHE_SECONDS = 300

# The Egypt-only district list used by the public/guest order form. Keys match
# ``core.validation.ALEXANDRIA_AREAS``. The rest of the app keeps the full list.
PUBLIC_AREA_KEYS = (
    'smouha',
    'agami',
    'sidi_gaber',
    'bahari',
    'montazah',
    'asafra',
    'miami',
    'san_stefano',
    'cleopatra',
    'ibrahimia',
    'moharam_bek',
    'karmouz',
    'wardian',
    'attarin',
    'abu_qir',
    'borg_el_arab',
)

PUBLIC_AREA_LABELS = {
    'smouha': 'سموحة',
    'agami': 'العجمي',
    'sidi_gaber': 'سيدي جابر',
    'bahari': 'بحري',
    'montazah': 'المنتزه',
    'asafra': 'العصافرة',
    'miami': 'ميامي',
    'san_stefano': 'سان ستيفانو',
    'cleopatra': 'كليوباترا',
    'ibrahimia': 'الإبراهيمية',
    'moharam_bek': 'محرم بك',
    'karmouz': 'كرموز',
    'wardian': 'الورديان',
    'attarin': 'العطارين',
    'abu_qir': 'أبو قير',
    'borg_el_arab': 'برج العرب',
}

# Service fallbacks mirror the seeded specialties table. They are labels, not
# invented statistics, and only apply to a completely unseeded database.
STATIC_SPECIALTIES = (
    {'key': 'dish_install', 'label': 'تركيب دش', 'icon': 'fa-solid fa-satellite-dish'},
    {'key': 'dish_repair', 'label': 'صيانة دش', 'icon': 'fa-solid fa-screwdriver-wrench'},
    {'key': 'signal_tuning', 'label': 'ضبط إشارة', 'icon': 'fa-solid fa-tower-broadcast'},
    {'key': 'receiver_service', 'label': 'تركيب / صيانة رسيفر', 'icon': 'fa-solid fa-tv'},
    {'key': 'other', 'label': 'خدمة أخرى', 'icon': 'fa-solid fa-circle-question'},
)

WHATSAPP_TEXT = 'السلام عليكم، محتاج فني دش في {area}.'


def _digits(value: str) -> str:
    return re.sub(r'\D', '', value or '')


def _env(name: str) -> str:
    return (getattr(settings, name, '') or '').strip()


def _db_settings() -> dict:
    """Read the whitelisted public keys from the legacy ``settings`` table."""
    try:
        from .models import Setting

        return {
            row.key: row.value
            for row in Setting.objects.filter(
                key__in=(
                    'site_name',
                    'support_phone',
                    'contact_phone',
                    'support_email',
                    'contact_email',
                    'contact_address',
                    'whatsapp',
                    'facebook_url',
                    'instagram_url',
                    'commission_percent',
                )
            )
        }
    except Exception:
        # A missing database during a management command must not make the site
        # configuration import fail.
        return {}


def get_site_config(force: bool = False) -> dict:
    """Return the resolved public configuration (never contains secrets)."""
    if not force:
        cached = cache.get(CACHE_KEY)
        if cached:
            return cached

    stored = _db_settings()

    phone = _env('BUSINESS_PHONE') or stored.get('contact_phone') or stored.get('support_phone') or ''
    email = _env('BUSINESS_EMAIL') or stored.get('contact_email') or stored.get('support_email') or ''
    whatsapp_raw = _env('BUSINESS_WHATSAPP') or stored.get('whatsapp') or phone
    whatsapp = _digits(whatsapp_raw)

    facebook = _env('SOCIAL_FACEBOOK') or stored.get('facebook_url') or ''
    instagram = _env('SOCIAL_INSTAGRAM') or stored.get('instagram_url') or ''
    tiktok = _env('SOCIAL_TIKTOK') or ''

    commission_raw = _env('COMMISSION_RATE') or stored.get('commission_percent') or '20'
    try:
        commission = float(commission_raw)
    except (TypeError, ValueError):
        commission = 20.0

    site_url = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    config = {
        'site_url': site_url,
        'site_name': stored.get('site_name') or 'دشاوي',
        'business_phone': phone,
        'business_phone_digits': _digits(phone),
        'business_phone_e164': normalize_phone(phone),
        'business_whatsapp': whatsapp,
        'business_email': email,
        'contact_address': stored.get('contact_address') or 'الإسكندرية، مصر',
        'social_facebook': facebook,
        'social_instagram': instagram,
        'social_tiktok': tiktok,
        'commission_rate': commission,
        'commission_percent_label': ('%g' % commission),
        'areas': [
            {'key': key, 'label': PUBLIC_AREA_LABELS[key]}
            for key in PUBLIC_AREA_KEYS
        ],
    }
    cache.set(CACHE_KEY, config, CACHE_SECONDS)
    return config


def whatsapp_link(area: str = 'الإسكندرية') -> str:
    config = get_site_config()
    number = config['business_whatsapp']
    if not number:
        return ''
    text = WHATSAPP_TEXT.format(area=area)
    return 'https://wa.me/%s?text=%s' % (number, quote(text))


def template_context() -> dict:
    """Values injected into every rendered HTML page."""
    config = get_site_config()
    return {
        **config,
        'business_whatsapp_url': whatsapp_link(),
        'brand_og_image': config['site_url'] + '/dashawy-brand/og-1200x630.png',
        'brand_logo_horizontal': config['site_url'] + '/dashawy-brand/logo-horizontal.svg',
        'brand_logo_icon': config['site_url'] + '/dashawy-brand/icon-512.png',
    }


def clear_site_config_cache():
    cache.delete(CACHE_KEY)
