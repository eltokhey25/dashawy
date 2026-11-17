"""Read an InstaPay receipt with Tesseract OCR, on the server, for free.

The result has the same shape as the vision model's answer
(``instapay.RECEIPT_SCHEMA``) plus ``reader='ocr'`` and the raw ``ocr_text``,
so the same checks run on it. OCR only reads characters: it cannot tell an
edited screenshot from a real one, and its layout guesses can be wrong, which
is why :func:`instapay.evaluate` is stricter with an OCR reading (anything
that does not match exactly waits for an admin instead of being refused).

How it reads:

* the image is read twice, once with the English model (digits, payment
  addresses, Latin labels) and once with the Arabic one (Arabic labels and
  names), and the words of each script are taken from the pass that reads it;
* words are grouped into rows by their vertical position. Every value on an
  InstaPay receipt is a single token (amount, reference, address, time), so
  nothing depends on the left-to-right order inside a row, which mixed
  Arabic/English rows make unreliable;
* labels ("Reference", "الرقم المرجعي", "To", "إلى" ...) tell which row holds
  which field; a label row also claims the unlabelled rows right below it
  (a name and its address are often printed on two lines).
"""

import functools
import io
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

TESSDATA_DIR = Path(__file__).resolve().parent.parent / 'ocr' / 'tessdata'
# Phone screenshots are ~1080 px wide; much larger photos only cost time.
MAX_WIDTH = 1600
MIN_WIDTH = 1000
MIN_CONFIDENCE = 40

ARABIC = re.compile(r'[؀-ۿݐ-ݿ]')
LATIN_OR_DIGIT = re.compile(r'[A-Za-z0-9]')


@functools.lru_cache(maxsize=1)
def available():
    """Tesseract loads and both language models are in place (checked once)."""
    if not all((TESSDATA_DIR / f'{lang}.traineddata').is_file() for lang in ('eng', 'ara')):
        return False
    try:
        import tesserocr
        from PIL import Image  # noqa: F401

        with tesserocr.PyTessBaseAPI(path=str(TESSDATA_DIR), lang='ara+eng'):
            pass
    except Exception:  # missing native library, broken model file
        log.error('Receipt OCR is unavailable; receipts go to manual review', exc_info=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def normalize(text):
    """Lower-case, Arabic letter variants folded, digits as ASCII."""
    text = ''.join(
        str(unicodedata.digit(c)) if c.isdigit() and not c.isascii() else c for c in str(text or '')
    ).lower()
    for src, dst in (('أ', 'ا'), ('إ', 'ا'), ('آ', 'ا'), ('ى', 'ي'), ('ة', 'ه'), ('ـ', '')):
        text = text.replace(src, dst)
    return text


def _phrases(*items):
    return tuple(tuple(normalize(item).split()) for item in items)


LABELS = {
    # Checked in this order: "المرسل اليه" is a recipient, not a sender.
    'to': _phrases(
        'to', 'receiver', 'recipient', 'beneficiary', 'payee', 'إلى', 'الى', 'المستلم',
        'المستفيد', 'المرسل إليه', 'المحول إليه', 'المحول له',
    ),
    'from': _phrases('from', 'sender', 'payer', 'من', 'المرسل', 'المحول', 'الراسل'),
    'reference': _phrases(
        'reference', 'ref', 'ref.', 'reference number', 'transaction id', 'txn', 'رقم المرجع',
        'الرقم المرجعي', 'المرجع', 'مرجع', 'رقم العملية', 'رقم المعاملة', 'المرجعي',
    ),
    'date': _phrases('date', 'time', 'التاريخ', 'تاريخ', 'الوقت'),
    'fees': _phrases('fee', 'fees', 'charges', 'commission', 'الرسوم', 'رسوم', 'العمولة'),
    'balance': _phrases('balance', 'الرصيد', 'رصيد'),
    'note': _phrases(
        'note', 'notes', 'description', 'remarks', 'purpose', 'reason', 'message', 'ملاحظات',
        'ملاحظة', 'الغرض', 'الوصف', 'البيان', 'سبب',
    ),
    'amount': _phrases('amount', 'total', 'المبلغ', 'مبلغ', 'الإجمالي', 'الاجمالي'),
    'type': _phrases('type', 'نوع', 'النوع', 'status', 'الحالة'),
}
# Rows under these labels hold the money that counts; fees/balance never do.
NOT_THE_AMOUNT = ('fees', 'balance', 'note', 'reference', 'date')

CURRENCY = {normalize(c) for c in ('egp', 'le', 'l.e', 'l.e.', 'ج.م', 'جم', 'جنيه', 'جنيها', 'جنيهات', 'ج')}

STATUS_WORDS = {
    'failed': _phrases(
        'failed', 'failure', 'unsuccessful', 'declined', 'rejected', 'فشل', 'فشلت', 'مرفوضة',
        'لم تتم', 'غير ناجحة',
    ),
    'pending': _phrases('pending', 'processing', 'قيد', 'جاري', 'معلقة'),
    'successful': _phrases(
        'successful', 'success', 'succeeded', 'completed', 'بنجاح', 'ناجحة', 'نجاح', 'تمت',
    ),
}

INSTAPAY_MARKERS = {normalize(m) for m in ('instapay', 'إنستاباي', 'انستاباي', 'انستا', 'ipn')}

MONTHS = {
    **{m: i for i, m in enumerate(
        ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)},
    **{normalize(m): i for i, m in enumerate(
        ('يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس', 'سبتمبر',
         'أكتوبر', 'نوفمبر', 'ديسمبر'), 1)},
    normalize('ابريل'): 4, normalize('اغسطس'): 8, normalize('اكتوبر'): 10, 'sept': 9,
}

VOCABULARY = (
    {word for phrases in LABELS.values() for phrase in phrases for word in phrase}
    | {word for phrases in STATUS_WORDS.values() for phrase in phrases for word in phrase}
    | CURRENCY
    | INSTAPAY_MARKERS
    | set(MONTHS)
)


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


def _prepare(image_bytes):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image).convert('L')
    if image.width > MAX_WIDTH:
        image = image.resize((MAX_WIDTH, round(image.height * MAX_WIDTH / image.width)), Image.LANCZOS)
    elif image.width < MIN_WIDTH:
        factor = MIN_WIDTH / image.width
        image = image.resize((MIN_WIDTH, round(image.height * factor)), Image.LANCZOS)
    # Dark-mode screenshots: Tesseract wants dark text on a light page.
    thumb = image.resize((32, 32))
    if sum(thumb.tobytes()) / 1024 < 110:
        image = ImageOps.invert(image)
    return image


STRIP_PAD = 8
MIN_STRIP_HEIGHT = 8


def text_strips(image):
    """The horizontal bands of the image that hold a line of text.

    Tesseract's page layout analysis drops words on sparse two-column
    screens (label on one side, value on the other), so the page is cut into
    lines first: a pixel row holds ink when enough of it differs from the
    background; full-width bars and divider lines are not text.
    """
    histogram = image.histogram()
    background = max(range(256), key=histogram.__getitem__)
    width, height = image.size
    data = image.tobytes()
    step = 2
    samples = width // step
    strips, start = [], None
    for y in range(height + 1):
        if y < height:
            row = data[y * width:(y + 1) * width:step]
            ink = sum(1 for value in row if abs(value - background) > 50)
            on = 0 < ink < samples * 0.6
        else:
            on = False
        if on and start is None:
            start = y
        elif not on and start is not None:
            if y - start >= MIN_STRIP_HEIGHT:
                strips.append((max(0, start - STRIP_PAD), min(height, y + STRIP_PAD)))
            start = None
    return strips, background


def _recognize(api, crop, top, left_pad):
    import tesserocr

    level = tesserocr.RIL.WORD
    api.SetImage(crop)
    api.Recognize()
    iterator = api.GetIterator()
    words = []
    if iterator is None:
        return words
    for word in tesserocr.iterate_level(iterator, level):
        try:
            text = (word.GetUTF8Text(level) or '').strip()
        except RuntimeError:  # "No text returned" for an empty box
            continue
        box = word.BoundingBox(level)
        if text and box:
            x0, y0, x1, y1 = box
            words.append(
                {
                    'text': text,
                    'conf': word.Confidence(level),
                    'box': (x0 - left_pad, y0 + top - left_pad, x1 - left_pad, y1 + top - left_pad),
                }
            )
    return words


def read_rows(image):
    """OCR every text line twice (English, Arabic): a list of rows of words."""
    import tesserocr
    from PIL import ImageOps

    strips, background = text_strips(image)
    rows = []
    path = str(TESSDATA_DIR)
    with tesserocr.PyTessBaseAPI(path=path, lang='eng', psm=tesserocr.PSM.SPARSE_TEXT) as english, \
            tesserocr.PyTessBaseAPI(path=path, lang='ara', psm=tesserocr.PSM.SINGLE_LINE) as arabic_line, \
            tesserocr.PyTessBaseAPI(path=path, lang='ara', psm=tesserocr.PSM.SPARSE_TEXT) as arabic_sparse:
        for top, bottom in strips:
            crop = ImageOps.expand(image.crop((0, top, image.width, bottom)), STRIP_PAD, fill=background)
            latin = _recognize(english, crop, top, STRIP_PAD)
            arabic = _recognize(arabic_line, crop, top, STRIP_PAD)
            if not any(ARABIC.search(w['text']) and w['conf'] >= MIN_CONFIDENCE for w in arabic):
                arabic = _recognize(arabic_sparse, crop, top, STRIP_PAD)
            row = merge_passes(latin, arabic)
            if row:
                rows.append(sorted(row, key=lambda w: w['box'][0]))
        headings = []
        if _status([t for row in rows for t in _tokens(row)]) == 'unknown':
            # Big headings ("تمت العملية بنجاح") are often dropped by the
            # layout analysis; read the top lines again as one raw line each.
            with tesserocr.PyTessBaseAPI(path=path, lang='ara+eng', psm=tesserocr.PSM.RAW_LINE) as raw:
                for top, bottom in strips[:4]:
                    crop = image.crop((0, top, image.width, bottom))
                    crop = crop.resize((round(crop.width * 0.5), round(crop.height * 0.5)))
                    raw.SetImage(ImageOps.expand(crop, STRIP_PAD, fill=background))
                    headings.append(raw.GetUTF8Text().strip())
    return rows, headings


def _overlaps(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return min(ax1, bx1) > max(ax0, bx0) and min(ay1, by1) > max(ay0, by0)


def merge_passes(english, arabic):
    """Latin/digit words from the English pass, Arabic words from the Arabic one."""
    merged = [
        w for w in english
        if w['conf'] >= MIN_CONFIDENCE and LATIN_OR_DIGIT.search(w['text']) and not ARABIC.search(w['text'])
    ]
    for word in arabic:
        if word['conf'] < MIN_CONFIDENCE:
            continue
        if ARABIC.search(word['text']):
            # The Arabic model "reads" Latin text as Arabic noise.
            if not any(_overlaps(word['box'], o['box']) and o['conf'] >= 75 for o in merged):
                merged.append(word)
        elif not any(_overlaps(word['box'], other['box']) for other in merged):
            # Arabic-Indic digits, or Latin the English pass missed.
            merged.append(word)
    return merged


def _orient_arabic(rows):
    """Some Tesseract builds return Arabic words in visual (reversed) order.

    Whichever orientation spells more known words wins, for the whole image.
    """
    forward = backward = 0
    for row in rows:
        for word in row:
            if ARABIC.search(word['text']):
                token = normalize(word['text'])
                forward += token in VOCABULARY
                backward += token[::-1] in VOCABULARY
    if backward > forward:
        for row in rows:
            for word in row:
                if ARABIC.search(word['text']):
                    word['text'] = word['text'][::-1]
    return rows


# ---------------------------------------------------------------------------
# Understanding the rows
# ---------------------------------------------------------------------------

MONEY = re.compile(r'^\d{1,3}(?:[,٬]\d{3})+(?:[.٫]\d{1,2})?$|^\d{1,6}(?:[.٫]\d{1,2})?$')
ADDRESS = re.compile(r'[a-z0-9._\-]+@[a-z0-9._\-]+', re.IGNORECASE)
REFERENCE = re.compile(r'^[A-Za-z0-9]{6,40}$')
TIME = re.compile(r'^(\d{1,2}):(\d{2})(?::\d{2})?$')
NUMERIC_DATE = re.compile(r'^(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{1,4})$')


def _tokens(row):
    """A row's tokens, normalized; punctuation around a value is dropped."""
    out = []
    for word in row:
        token = normalize(word['text']).strip('|:;,()[]{}"\'')
        if token:
            out.append(token)
    return out


def _has_phrase(tokens, phrase):
    return all(part in tokens for part in phrase)


def _labels(tokens):
    found = []
    for key, phrases in LABELS.items():
        if any(_has_phrase(tokens, p) for p in phrases):
            if key == 'from' and 'to' in found:
                continue
            found.append(key)
    return found


def _label_words():
    return {word for phrases in LABELS.values() for phrase in phrases for word in phrase}


def _money_in(tokens):
    values = []
    for token in tokens:
        cleaned = token.replace('egp', '').replace('le', '') if token[:1].isdigit() else token
        if MONEY.match(cleaned):
            values.append(float(cleaned.replace(',', '').replace('٬', '').replace('٫', '.')))
    return values


def _parse_time(tokens):
    """A date and time anywhere in ``tokens``, as ``YYYY-MM-DD HH:MM``, or ''."""
    hour = minute = None
    year = month = day = None
    meridiem = None
    loose_numbers = []
    for token in tokens:
        time_match = TIME.match(token)
        if time_match and hour is None:
            hour, minute = int(time_match.group(1)), int(time_match.group(2))
            continue
        date_match = NUMERIC_DATE.match(token)
        if date_match and year is None:
            a, b, c = (int(g) for g in date_match.groups())
            if len(date_match.group(1)) == 4:
                year, month, day = a, b, c
            elif len(date_match.group(3)) == 4:
                year, month, day = c, b, a
            continue
        if token in ('am', 'a.m', 'ص', 'صباحا'):
            meridiem = 'am'
        elif token in ('pm', 'p.m', 'م', 'مساء', 'مساءا'):
            meridiem = 'pm'
        elif token.rstrip('.,') in MONTHS and month is None:
            month = MONTHS[token.rstrip('.,')]
        elif re.fullmatch(r'\d{4}', token) and 2000 <= int(token) <= 2100 and year is None:
            year = int(token)
        elif re.fullmatch(r'\d{1,2}(st|nd|rd|th)?,?', token):
            loose_numbers.append(int(re.match(r'\d+', token).group()))
    if day is None and month is not None and loose_numbers:
        day = loose_numbers[0]
    if None in (year, month, day, hour, minute):
        return ''
    if meridiem == 'pm' and hour < 12:
        hour += 12
    elif meridiem == 'am' and hour == 12:
        hour = 0
    try:
        moment = datetime(year, month, day, hour, minute)
    except ValueError:
        return ''
    return moment.strftime('%Y-%m-%d %H:%M')


def _accounts(tokens):
    """Payment addresses and mobile numbers (full or masked) in a row."""
    found = [m.group(0).lower() for t in tokens for m in ADDRESS.finditer(t)]
    for token in tokens:
        if '@' in token:
            continue
        digits = re.sub(r'[^0-9*xX•]', '', token)
        if re.fullmatch(r'(\+?20|0)?1[0-9*xX•]{8,10}', token.replace(' ', '')) or (
            '*' in token and sum(c.isdigit() for c in digits) >= 3
        ):
            found.append(token)
    # A mobile number OCR'd as several groups: "010 1234 5678".
    joined = ''.join(t for t in tokens if t.isdigit() and len(t) <= 5)
    if re.fullmatch(r'01\d{9}', joined):
        found.append(joined)
    return found


NAME_NOISE = {normalize(w) for w in ('name', 'account', 'mobile', 'number', 'الاسم', 'اسم', 'حساب', 'الحساب', 'رقم')}


def _name_words(row, label_words):
    label_words = label_words | NAME_NOISE
    words = []
    for word in row:
        token = normalize(word['text']).strip('|:;,()[]{}"\'')
        if not token or token in label_words or '@' in token:
            continue
        if any(ch.isdigit() for ch in token) and '*' not in token:
            continue
        if re.search(r'[a-z؀-ۿ]', token):
            words.append(word['text'].strip('|:;,()[]{}"\''))
    return ' '.join(words)


def _status(tokens):
    for state in ('failed', 'pending', 'successful'):
        if any(_has_phrase(tokens, p) for p in STATUS_WORDS[state]):
            return state
    return 'unknown'


# The same, inside text whose word breaks were lost ("تمتالعمليةبنجاح").
STATUS_FRAGMENTS = {
    'failed': ('failed', 'unsuccessful', 'declined', 'فشل', 'لمتتم', 'غيرناجح', 'مرفوض'),
    'pending': ('pending', 'processing', 'قيدالتنفيذ', 'قيدالمعالج', 'معلق'),
    'successful': ('successful', 'success', 'بنجاح', 'ناجحه', 'تمتالعمليه', 'تمتالمعامله', 'تمالتحويل', 'تمتعمليه'),
}


def _status_in_text(texts):
    joined = ''.join(re.sub(r'\s+', '', normalize(t)) for t in texts)
    candidates = (joined, joined[::-1])
    for state in ('failed', 'pending', 'successful'):
        if any(f in text for f in STATUS_FRAGMENTS[state] for text in candidates):
            return state
    return 'unknown'


def understand(rows, headings=()):
    """The receipt fields from OCR rows (see the module docstring).

    ``headings`` are the top lines re-read as raw text; they only help to
    find the transaction's status.
    """
    label_words = _label_words()
    all_tokens = [t for row in rows for t in _tokens(row)]
    text = ' '.join(all_tokens)

    status = _status(all_tokens)
    if status == 'unknown':
        status = _status_in_text(list(headings) + [' '.join(row) for row in (_tokens(r) for r in rows)])

    sections = []  # (labels, tokens, row) per row, with inherited section
    current, carried = None, 0
    for row in rows:
        tokens = _tokens(row)
        labels = _labels(tokens)
        if labels:
            current = next((l for l in labels if l in ('to', 'from')), None) or labels[0]
            carried = 0
        elif current is not None:
            carried += 1
            if carried > 2:
                current = None
        sections.append((labels, current, tokens, row))

    # Amount: currency-marked or amount-labelled numbers, never fees/balance.
    amounts = set()
    for labels, current, tokens, _row in sections:
        if current in NOT_THE_AMOUNT or any(l in NOT_THE_AMOUNT for l in labels):
            continue
        marked = any(t in CURRENCY or t.endswith('egp') for t in tokens) or 'amount' in labels
        if marked:
            amounts.update(v for v in _money_in(tokens) if v > 0)
    amount = f'{amounts.pop():.2f}' if len(amounts) == 1 else ''

    reference = ''
    for labels, current, tokens, _row in sections:
        if current != 'reference':
            continue
        candidates = [
            t for t in tokens
            if REFERENCE.match(t) and sum(c.isdigit() for c in t) >= 6 and t not in label_words
        ]
        if candidates:
            reference = max(candidates, key=len)
            break

    transferred_at = ''
    for labels, current, tokens, _row in sections:
        if current == 'date':
            transferred_at = _parse_time(tokens)
            if transferred_at:
                break
    if not transferred_at:
        # An unlabelled date (often under the amount), with its time on the
        # same row; never the phone's status-bar clock on a row of its own.
        for _row_labels, current, tokens, _row in sections:
            if current != 'reference':
                transferred_at = _parse_time(tokens)
                if transferred_at:
                    break

    parties = {'to': {'accounts': [], 'names': []}, 'from': {'accounts': [], 'names': []}}
    for labels, current, tokens, row in sections:
        if current in parties:
            parties[current]['accounts'].extend(_accounts(tokens))
            name = _name_words(row, label_words | INSTAPAY_MARKERS)
            if name:
                parties[current]['names'].append(name)

    word_count = len(all_tokens)
    receipt_like = bool(
        (any(m in text for m in INSTAPAY_MARKERS) or reference)
        and (amount or amounts)
    )
    return {
        'status': status,
        'amount': amount,
        'reference': reference,
        'transferred_at': transferred_at,
        'recipient_accounts': parties['to']['accounts'],
        'recipient_name': ' '.join(parties['to']['names'])[:120],
        'sender_accounts': parties['from']['accounts'],
        'sender_name': ' '.join(parties['from']['names'])[:120],
        'receipt_like': receipt_like,
        'word_count': word_count,
    }


def _pick_account(accounts, config):
    """Ours if every account in the section is ours, else the first foreign one."""
    if not accounts:
        return ''
    from .instapay import match_recipient

    for account in accounts:
        if match_recipient(account, '', config) == 'mismatch':
            return account
    return accounts[0]


def read_receipt(image_bytes, config):
    """``RECEIPT_SCHEMA``-shaped fields read by OCR, or ``None`` on failure."""
    if not available():
        return None
    try:
        image = _prepare(image_bytes)
        rows, headings = read_rows(image)
    except Exception:  # a broken image or a Tesseract failure: an admin looks
        log.warning('OCR could not read a receipt', exc_info=True)
        return None
    rows = _orient_arabic(rows)
    found = understand(rows, headings)

    confidences = [w['conf'] for row in rows for w in row]
    mean = sum(confidences) / len(confidences) if confidences else 0
    if found['word_count'] < 5 or mean < 50:
        legibility = 'unreadable'
    elif mean < 75:
        legibility = 'partial'
    else:
        legibility = 'clear'

    return {
        'is_transfer_receipt': found['receipt_like'],
        'legibility': legibility,
        'transaction_status': found['status'],
        'amount': found['amount'],
        'currency': 'EGP',
        'reference': found['reference'],
        'transferred_at': found['transferred_at'],
        'recipient_name': found['recipient_name'],
        'recipient_account': _pick_account(found['recipient_accounts'], config),
        'sender_name': found['sender_name'],
        'sender_account': found['sender_accounts'][0] if found['sender_accounts'] else '',
        'tampering_suspected': False,
        'tampering_notes': '',
        'reader': 'ocr',
        'word_count': found['word_count'],
        'ocr_text': '\n'.join(' '.join(w['text'] for w in row) for row in rows)[:4000],
    }
