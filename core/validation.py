"""
Input validation and identity normalisation for the legacy API.

These mirror ``backend/utils/validation.js`` so the Django endpoints accept and
store exactly the same values the Express app did. In particular, phones must be
normalised identically: ``users.phone`` carries a UNIQUE index and phone login
matches on the canonical form, so ``01000000000`` and ``+201000000000`` have to
resolve to the same account.
"""

import re

EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}$')
PHONE_RE = re.compile(r'^[+()\-\s0-9]{6,20}$')
# Admin sign-in names: start with a letter so a username can never look like
# an email (no '@') or a phone number (all digits) on the shared login API.
USERNAME_RE = re.compile(r'^[a-z][a-z0-9._-]{2,31}$')

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 200

# Mirrors config.SPECIALTIES in backend/config.js. Used only as a fallback when
# the specialties table has not been seeded yet; the database is authoritative.
STATIC_SPECIALTY_KEYS = (
    'dish_install',
    'dish_repair',
    'signal_tuning',
    'receiver_service',
    'other',
)

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
    {'key': 'miami', 'label': 'ميامي'},
    {'key': 'wardian', 'label': 'الورديان'},
]

# Egyptian mobile prefixes accepted by the public/guest form: 010, 011, 012, 015
# followed by 8 digits (11 digits in total).
EGYPT_MOBILE_RE = re.compile(r'^01[0125]\d{8}$')

AREAS_BY_KEY = {area['key']: area for area in ALEXANDRIA_AREAS}


def normalize_email(value):
    return str(value or '').strip().lower()


def normalize_username(value):
    return str(value or '').strip().lower()


def is_valid_username(value):
    return isinstance(value, str) and bool(USERNAME_RE.fullmatch(normalize_username(value)))


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


def is_egypt_mobile(value):
    """True for a local Egyptian mobile number: 010/011/012/015 + 8 digits."""
    return isinstance(value, str) and bool(EGYPT_MOBILE_RE.fullmatch(value.strip()))


def local_mobile(value):
    """Return a local ``01xxxxxxxxx`` form for a valid Egyptian mobile.

    Accepts ``+2010...`` and ``002010...`` as well, so a number typed with a
    country code is not rejected as invalid while still being stored in one
    canonical shape. Returns ``''`` when the value is not a mobile number.
    """
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits.startswith('0020'):
        digits = digits[2:]
    if digits.startswith('20') and len(digits) == 12:
        digits = '0' + digits[2:]
    return digits if EGYPT_MOBILE_RE.fullmatch(digits) else ''


MOBILE_FORMAT_ERROR = 'اكتب رقم موبايل مصري صحيح يبدأ بـ 010 أو 011 أو 012 أو 015'

# Six digits in a row is where a made-up number starts: a real subscriber
# number almost never holds a run that long, while the fakes people type to get
# past a form (01000000007, 01012345678, 01111111111) always do.
_PLACEHOLDER_RUN = 6


def _longest_run(digits, step):
    """Length of the longest run where each digit is the previous one + step."""
    longest = current = 1
    for prev, cur in zip(digits, digits[1:]):
        current = current + 1 if int(cur) - int(prev) == step else 1
        longest = max(longest, current)
    return longest


def is_placeholder_mobile(local):
    """True for a local mobile whose subscriber digits look made up.

    Looks at the 8 digits after the operator prefix: the same digit six or more
    times in a row, or six or more counting up or down by one.
    """
    subscriber = local[3:]
    return any(_longest_run(subscriber, step) >= _PLACEHOLDER_RUN for step in (0, 1, -1))


def checked_mobile(value):
    """Validate a mobile typed into a form.

    Returns ``(local, None)`` for a usable Egyptian mobile in ``01xxxxxxxxx``
    form, or ``('', reason)`` with the Arabic reason it was refused. A made-up
    number gets the same reason as a malformed one, so the form never tells
    the visitor it thinks they are faking.
    """
    local = local_mobile(value)
    if not local or is_placeholder_mobile(local):
        return '', MOBILE_FORMAT_ERROR
    return local, None


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
