"""
Input validation and identity normalisation for the legacy API.

These mirror ``backend/utils/validation.js`` so the Django endpoints accept and
store exactly the same values the Express app did. In particular, phones must be
normalised identically: ``users.phone`` carries a UNIQUE index and phone login
matches on the canonical form, so ``01025330520`` and ``+201025330520`` have to
resolve to the same account.
"""

import re

EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}$')
PHONE_RE = re.compile(r'^[+()\-\s0-9]{6,20}$')

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 200

# Mirrors config.ALEXANDRIA_AREAS in backend/config.js. Kept in Python so the
# registration endpoint can turn an area_key into the same "area — detail"
# location string the browser used to receive.
ALEXANDRIA_AREAS = [
    {'key': 'sidi_gaber', 'label': 'سيدي جابر'},
    {'key': 'smouha', 'label': 'سموحة'},
    {'key': 'cleopatra', 'label': 'كليوباترا'},
    {'key': 'sporting', 'label': 'سبورتنج'},
    {'key': 'ibrahimia', 'label': 'الإبراهيمية'},
    {'key': 'camp_chezar', 'label': 'كامب شيزار'},
    {'key': 'louran', 'label': 'لوران'},
    {'key': 'glime', 'label': 'جليم'},
    {'key': 'san_stefano', 'label': 'سان ستيفانو'},
    {'key': 'sidi_bishr', 'label': 'سيدي بشر'},
    {'key': 'mustafa_kamel', 'label': 'مصطفى كامل'},
    {'key': 'mandara', 'label': 'المندرة'},
    {'key': 'asafra', 'label': 'العصافرة'},
    {'key': 'abu_qir', 'label': 'أبو قير'},
    {'key': 'maamoura', 'label': 'المعمورة'},
    {'key': 'montazah', 'label': 'المنتزه'},
    {'key': 'bahari', 'label': 'بحري'},
    {'key': 'anfushi', 'label': 'الأنفوشي'},
    {'key': 'mansheya', 'label': 'المنشية'},
    {'key': 'attarin', 'label': 'العطارين'},
    {'key': 'gomrok', 'label': 'الجمرك'},
    {'key': 'labban', 'label': 'اللبان'},
    {'key': 'moharam_bek', 'label': 'محرم بك'},
    {'key': 'karmouz', 'label': 'كرموز'},
    {'key': 'gharbal', 'label': 'غربال'},
    {'key': 'bab_sharq', 'label': 'باب شرق'},
    {'key': 'hadra', 'label': 'الحضرة'},
    {'key': 'fleming', 'label': 'فلمنج'},
    {'key': 'zizinia', 'label': 'زيزينيا'},
    {'key': 'rosHDY', 'label': 'رشدي'},
    {'key': 'shatby', 'label': 'الشاطبي'},
    {'key': 'azarita', 'label': 'الأزاريطة'},
    {'key': 'kom_el_dekka', 'label': 'كوم الدكة'},
    {'key': 'mahsousa', 'label': 'المحروسة'},
    {'key': 'dekhila', 'label': 'الدخيلة'},
    {'key': 'agami', 'label': 'العجمي'},
    {'key': 'bitash', 'label': 'البيطاش'},
    {'key': 'hanoville', 'label': 'الهانوفيل'},
    {'key': 'amreya', 'label': 'العامرية'},
    {'key': 'max', 'label': 'المكس'},
    {'key': 'qabari', 'label': 'القباري'},
    {'key': 'borg_el_arab', 'label': 'برج العرب'},
]

AREAS_BY_KEY = {area['key']: area for area in ALEXANDRIA_AREAS}


def normalize_email(value):
    return str(value or '').strip().lower()


def normalize_phone(value):
    """Canonical phone form: digits only, with a single leading '+'.

    Copies ``normalizePhone`` from the Node validation module, including the
    Egyptian trunk-prefix rewrite ('010…' -> '+2010…').
    """
    raw = str(value or '').strip()
    if not raw:
        return ''

    had_plus = raw.startswith('+')
    digits = ''.join(ch for ch in raw if ch in '0123456789')
    if not digits:
        return ''

    # '0020...' (international dialling prefix) collapses to the country code.
    if not had_plus and digits.startswith('00'):
        digits = digits[2:]

    # No country code begins with '0', so a leading zero is a local trunk
    # prefix: rewrite it to Egypt's +20 and drop the zero.
    if digits.startswith('0'):
        national = digits[1:]
        return '+20' + national if national else ''

    # Country code already present but missing its '+'.
    if digits.startswith('20') and len(digits) == 12:
        return '+' + digits

    return ('+' if had_plus else '') + digits


def is_valid_email(value):
    return isinstance(value, str) and bool(EMAIL_RE.fullmatch(value.strip()))


def is_valid_phone(value):
    return isinstance(value, str) and bool(PHONE_RE.fullmatch(value.strip()))


def is_valid_name(value):
    return isinstance(value, str) and 2 <= len(value.strip()) <= 100


def is_non_empty_string(value, max_length=1000):
    return isinstance(value, str) and 0 < len(value.strip()) <= max_length


def password_policy_error(value):
    """Arabic reason a password was rejected, or None when acceptable."""
    if not isinstance(value, str):
        return 'كلمة المرور مطلوبة'
    if len(value) < PASSWORD_MIN_LENGTH:
        return 'كلمة المرور يجب ألا تقل عن ' + str(PASSWORD_MIN_LENGTH) + ' أحرف'
    if len(value) > PASSWORD_MAX_LENGTH:
        return 'كلمة المرور طويلة جدًا'
    if not re.search(r'[a-zA-Z]', value):
        return 'كلمة المرور يجب أن تحتوي على حرف واحد على الأقل'
    if not re.search(r'\d', value):
        return 'كلمة المرور يجب أن تحتوي على رقم واحد على الأقل'
    return None


def parse_lat_lng(latitude, longitude):
    """Return (lat, lng) floats, or None when both are absent/invalid."""
    if latitude in (None, '') or longitude in (None, ''):
        return None
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def resolve_location(area_key, location_text):
    """Resolve an Alexandria area key to the same display string as Node.

    Raises KeyError for an unknown area_key (the view turns it into a 400).
    """
    if area_key not in (None, ''):
        area = AREAS_BY_KEY[str(area_key)]
        detail = str(location_text or '').strip()
        return area['label'] + ' — ' + detail if detail else area['label']
    return str(location_text or '').strip()
