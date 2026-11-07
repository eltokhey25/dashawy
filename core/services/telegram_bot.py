"""The Dashawy Telegram bot: commands, inline buttons and short conversations.

``handle_update`` receives one Telegram update from the webhook and answers
it. A chat is identified by ``User.telegram_chat_id``; an unlinked chat only
gets the linking instructions (and ``/start <token>`` from the site's link
page). Everything else is routed by role:

* customer: their open orders and each one's status, the details of one
  order (technician, phone, doorstep code), a new order placed step by step
  (service, area, address, description, confirm), cancelling, and a chat
  with the technician or the support contacts;
* technician: the offers waiting for an answer (accept / decline), the
  current jobs with "on my way", "started" (asks for the doorstep code),
  "finished" (asks for the work notes) and "hand back", the wallet, and a
  chat with the customer;
* admin: a short summary and the dashboard link.

Multi-step flows (new order, the doorstep code, the work notes, a chat
message) keep their state in the cache under the chat id for a few minutes,
which survives Vercel's separate function invocations. Every transition
reuses the same service functions as the API, so the guards are identical.

Button presses arrive as ``callback_query`` updates whose ``data`` is a
short ``kind:action:id`` string; see ``_handle_callback``.
"""

import json
import logging

from django.core.cache import cache
from django.db.models import Q

from ..exceptions import ApiError
from ..models import Customer, Message, Request, RequestBroadcast, Technician, User
from ..validation import ALEXANDRIA_AREAS, AREAS_BY_KEY
from . import telegram
from .telegram import inline_keyboard

logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 15 * 60
STATE_KEY = 'dashawy:telegram:state:{chat_id}'
ON_THE_WAY_TEXT = '🚗 أنا في الطريق إليك دلوقتي.'

STATUS_LABELS = {
    'pending': '⏳ مستني فني يقبله',
    'assigned': '👨‍🔧 فني في الطريق',
    'in_progress': '🛠️ الشغل جاري',
    'completed': '✅ خلص',
    'cancelled': '❌ اتلغى',
    'no_technician': '😔 مفيش فني متاح حالياً',
}

LINK_HELP = (
    'حسابك لسه مش مربوط بالبوت.\n'
    'افتح الموقع من حسابك، ادخل على صفحة «ربط تليجرام» واضغط على رابط الربط، '
    'أو ابعتلي الكود اللي ظاهر في الصفحة هنا.'
)
UNKNOWN_COMMAND = 'مش فاهم الأمر ده 🤔\nاختار من القائمة تحت أو ابعت /help.'
MENU_PROMPT = 'اختار اللي محتاجه من القائمة 👇'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _site_url():
    from ..site_config import get_site_config

    return get_site_config()['site_url'].rstrip('/')


def _specialty_label(key):
    from ..guest_views import _label_for

    return _label_for(key)


def _service_label(job):
    if job.specialty == 'other' and job.custom_specialty_text:
        return job.custom_specialty_text
    return _specialty_label(job.specialty)


def _status_label(status):
    return STATUS_LABELS.get(status, status)


def _price_line(job):
    from .pricing import price_status

    price = price_status(job)
    if price['amount'] is None:
        return price['label']
    return f"{price['label']}: {price['amount']:g} ج.م"


def _when(value):
    return value.strftime('%Y-%m-%d %H:%M') if value else '—'


def _user_for_chat(chat_id):
    return User.objects.filter(telegram_chat_id=str(chat_id)).first()


def _error_text(exc):
    detail = getattr(exc, 'detail', None) or str(exc)
    if isinstance(detail, (list, dict)):
        from ..exceptions import _first_message

        return _first_message(detail) or 'حصلت مشكلة، حاول تاني.'
    return str(detail)


# Conversation state ---------------------------------------------------------


def get_state(chat_id):
    raw = cache.get(STATE_KEY.format(chat_id=chat_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def set_state(chat_id, state):
    cache.set(STATE_KEY.format(chat_id=chat_id), json.dumps(state, ensure_ascii=False), STATE_TTL_SECONDS)


def clear_state(chat_id):
    cache.delete(STATE_KEY.format(chat_id=chat_id))


# Replies --------------------------------------------------------------------


class Reply:
    """Where an answer goes: a new message, or the edited menu message."""

    def __init__(self, chat_id, message_id=None):
        self.chat_id = chat_id
        self.message_id = message_id

    def send(self, text, rows=None, edit=False):
        markup = inline_keyboard(rows) if rows else None
        if edit and self.message_id and telegram.edit_message(
            self.chat_id, self.message_id, text, markup
        ):
            return True
        return telegram.send_message(self.chat_id, text, markup)


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------


def main_menu_rows(user):
    if user.role == 'customer':
        return [
            [('📋 طلباتي', 'menu:requests'), ('➕ طلب جديد', 'new:start')],
            [('💬 الدعم', 'menu:support'), ('❓ مساعدة', 'menu:help')],
        ]
    if user.role == 'technician':
        return [
            [('📥 الطلبات الواردة', 'menu:incoming'), ('🛠️ شغلي الحالي', 'menu:jobs')],
            [('💰 محفظتي', 'menu:wallet'), ('💬 الدعم', 'menu:support')],
            [('❓ مساعدة', 'menu:help')],
        ]
    return [
        [('📊 ملخص اليوم', 'menu:admin'), ('🌐 لوحة الإدارة', f'{_site_url()}/admin/dashboard.html')],
        [('❓ مساعدة', 'menu:help')],
    ]


def _back_row(user):
    return [('⬅️ القائمة الرئيسية', 'menu:main')]


def help_text(user):
    common = (
        'الأوامر المتاحة:\n'
        '/menu — القائمة الرئيسية\n'
        '/help — الشرح ده\n'
        '/support — التواصل مع الدعم\n'
    )
    if user.role == 'customer':
        return (
            'أهلاً بيك في بوت دشاوي 👋\n'
            'من هنا تقدر تتابع طلباتك وتطلب فني من غير ما تفتح الموقع.\n\n'
            + common
            + '/requests — طلباتك الحالية وحالة كل واحد\n'
            '/track <رقم الطلب> — تفاصيل طلب معيّن\n'
            '/new — طلب فني جديد خطوة بخطوة\n\n'
            'لما فني يقبل طلبك هتوصلك رسالة هنا فيها اسمه ورقمه وكود البداية. '
            'الفني بيقولك الكود لما يوصل عشان الشغل يبدأ رسمي.'
        )
    if user.role == 'technician':
        return (
            'أهلاً بيك في بوت دشاوي 👋\n'
            'من هنا تقدر تقبل الطلبات وتحدّث حالة شغلك وتشوف محفظتك.\n\n'
            + common
            + '/incoming — الطلبات المعروضة عليك (قبول / رفض)\n'
            '/jobs — شغلك الحالي: في الطريق، بدأت، خلّصت\n'
            '/wallet — رصيدك والمستحق عليك للمنصة\n\n'
            'لما يوصلك طلب جديد هتلاقي زرار قبول ورفض في الرسالة نفسها.'
        )
    return (
        'أهلاً بيك 👋 ده حساب إدارة.\n\n'
        + common
        + '/menu — ملخص سريع ورابط لوحة الإدارة'
    )


def show_main_menu(reply, user, edit=False):
    reply.send(MENU_PROMPT, main_menu_rows(user), edit=edit)


def show_help(reply, user, edit=False):
    reply.send(help_text(user), [_back_row(user)], edit=edit)


def show_support(reply, user, edit=False):
    from ..site_config import get_site_config, whatsapp_link

    config = get_site_config()
    lines = ['فريق دشاوي جاهز يساعدك 🤝']
    rows = []
    if config['business_phone']:
        lines.append(f"📞 {config['business_phone']}")
    if config['business_email']:
        lines.append(f"✉️ {config['business_email']}")
    link = whatsapp_link()
    if link:
        rows.append([('💬 كلّمنا على واتساب', link)])
    rows.append([('🌐 الموقع', _site_url())])
    rows.append(_back_row(user))
    if len(lines) == 1:
        lines.append('كلّمنا من صفحة التواصل على الموقع.')
    reply.send('\n'.join(lines), rows, edit=edit)


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


def _customer(user):
    from ..views import _customer_for

    return _customer_for(user)


def _customer_jobs(customer):
    return (
        Request.objects.filter(customer_id=customer.pk)
        .select_related('technician')
        .order_by('-created_at', '-id')
    )


def show_customer_requests(reply, user, edit=False):
    customer = _customer(user)
    jobs = list(_customer_jobs(customer).filter(status__in=('pending', 'assigned', 'in_progress', 'no_technician'))[:10])
    if not jobs:
        recent = list(_customer_jobs(customer)[:3])
        text = 'مفيش طلبات شغالة دلوقتي.'
        if recent:
            text += '\n\nآخر طلباتك:\n' + '\n'.join(
                f'#{j.pk} — {_service_label(j)} — {_status_label(j.status)}' for j in recent
            )
        rows = [[(f'🔎 #{j.pk}', f'req:view:{j.pk}') for j in recent[:3]]] if recent else []
        rows += [[('➕ طلب جديد', 'new:start')], _back_row(user)]
        reply.send(text, rows, edit=edit)
        return
    lines = ['طلباتك الحالية:']
    for job in jobs:
        tech = f' — الفني: {job.technician.name}' if job.technician_id and job.technician else ''
        lines.append(f'#{job.pk} — {_service_label(job)} — {_status_label(job.status)}{tech}')
    lines.append('\nاضغط على رقم الطلب عشان تشوف تفاصيله.')
    rows = []
    for i in range(0, len(jobs), 3):
        rows.append([(f'🔎 #{j.pk}', f'req:view:{j.pk}') for j in jobs[i:i + 3]])
    rows.append([('🔄 تحديث', 'menu:requests'), ('➕ طلب جديد', 'new:start')])
    rows.append(_back_row(user))
    reply.send('\n'.join(lines), rows, edit=edit)


def show_customer_request(reply, user, request_id, edit=False):
    customer = _customer(user)
    job = _customer_jobs(customer).filter(pk=request_id).first()
    if job is None:
        reply.send('الطلب غير موجود.', [[('📋 طلباتي', 'menu:requests')], _back_row(user)], edit=edit)
        return
    lines = [
        f'طلب #{job.pk} — {_service_label(job)}',
        f'الحالة: {_status_label(job.status)}',
        f'المكان: {job.location_text or "—"}',
        f'الوصف: {job.description}',
        _price_line(job),
        f'اتعمل: {_when(job.created_at)}',
    ]
    rows = []
    if job.technician_id and job.technician and job.status in ('assigned', 'in_progress', 'completed'):
        lines.append(f'الفني: {job.technician.name} — {job.technician.phone}')
    if job.status == 'assigned' and job.start_pin:
        lines.append(f'🔐 كود البداية: {job.start_pin}\nقوله للفني لما يوصلك عشان يبدأ الشغل.')
    if job.status == 'completed' and job.amount_status in (None, '', 'pending'):
        lines.append('الفني خلّص الشغل. أكّد المبلغ وقيّم الخدمة من الموقع.')
        rows.append([('⭐ أكّد وقيّم', f'{_site_url()}/customer/my-requests.html?review={job.pk}')])
    if job.status in ('assigned', 'in_progress'):
        rows.append([('💬 كلّم الفني', f'chat:open:{job.pk}')])
    if job.status in ('pending', 'no_technician', 'assigned'):
        rows.append([('❌ إلغاء الطلب', f'req:cancel:{job.pk}')])
    rows.append([('🔄 تحديث', f'req:view:{job.pk}'), ('📋 طلباتي', 'menu:requests')])
    rows.append(_back_row(user))
    reply.send('\n'.join(lines), rows, edit=edit)


def confirm_cancel(reply, user, request_id):
    reply.send(
        f'متأكد إنك عايز تلغي طلب #{request_id}؟',
        [
            [('نعم، ألغيه', f'req:cancelyes:{request_id}'), ('لا، رجوع', f'req:view:{request_id}')],
        ],
        edit=True,
    )


def cancel_customer_request(reply, user, request_id):
    from .broadcast import cancel_request
    from .notifications import notify_request_cancelled

    customer = _customer(user)
    job = Request.objects.filter(pk=request_id, customer_id=customer.pk).first()
    if job is None:
        reply.send('الطلب غير موجود.', [_back_row(user)], edit=True)
        return
    cancellable = ('pending', 'no_technician', 'assigned')
    done = cancel_request(job.pk, cancellable, cancel_reason='ألغى العميل الطلب') if job.status in cancellable else None
    if done is None:
        reply.send('مش ممكن تلغي الطلب في حالته الحالية.', [[('🔎 الطلب', f'req:view:{job.pk}')], _back_row(user)], edit=True)
        return
    if done['technician_id']:
        tech_user_id = (
            Technician.objects.filter(pk=done['technician_id']).values_list('user_id', flat=True).first()
        )
        if tech_user_id:
            notify_request_cancelled(tech_user_id, job.pk)
    reply.send(f'تم إلغاء طلب #{job.pk} ✅', [[('📋 طلباتي', 'menu:requests')], _back_row(user)], edit=True)


# New order (step by step) ---------------------------------------------------


def _specialty_choices():
    from ..models import Specialty
    from ..site_config import STATIC_SPECIALTIES

    rows = list(Specialty.objects.filter(enabled=True).order_by('sort_order', 'id').values_list('key', 'label'))
    return rows or [(r['key'], r['label']) for r in STATIC_SPECIALTIES]


def start_new_order(reply, user, edit=False):
    _customer(user)  # the same gates as the site (complete profile, not blocked)
    set_state(reply.chat_id, {'flow': 'new', 'step': 'specialty'})
    choices = _specialty_choices()
    rows = [[(label, f'new:spec:{key}') for key, label in choices[i:i + 2]] for i in range(0, len(choices), 2)]
    rows.append([('❌ إلغاء', 'new:abort')])
    reply.send('طلب جديد 🛠️\nالخطوة 1 من 4: محتاج أنهي خدمة؟', rows, edit=edit)


def _ask_area(reply, state):
    areas = ALEXANDRIA_AREAS
    rows = [[(a['label'], f"new:area:{a['key']}") for a in areas[i:i + 3]] for i in range(0, len(areas), 3)]
    rows.append([('❌ إلغاء', 'new:abort')])
    state['step'] = 'area'
    set_state(reply.chat_id, state)
    reply.send('الخطوة 2 من 4: إنت في أنهي منطقة في إسكندرية؟', rows, edit=True)


def _ask_address(reply, state):
    state['step'] = 'address'
    set_state(reply.chat_id, state)
    reply.send(
        'الخطوة 3 من 4: اكتب العنوان بالتفصيل (الشارع والعمارة والدور)، '
        'أو اضغط «تخطي» لو هتتفق مع الفني.',
        [[('⏭️ تخطي', 'new:skipaddress')], [('❌ إلغاء', 'new:abort')]],
    )


def _ask_description(reply, state):
    state['step'] = 'description'
    set_state(reply.chat_id, state)
    reply.send('الخطوة 4 من 4: اوصف المشكلة في كلمتين (مثلاً: الدش مش بيجيب إشارة).', [[('❌ إلغاء', 'new:abort')]])


def _confirm_order(reply, state):
    state['step'] = 'confirm'
    set_state(reply.chat_id, state)
    area = AREAS_BY_KEY.get(state.get('area_key'), {}).get('label', '')
    service = state.get('custom_text') or _specialty_label(state.get('specialty', ''))
    address = state.get('address') or '—'
    reply.send(
        'راجع طلبك:\n'
        f'الخدمة: {service}\n'
        f'المنطقة: {area}\n'
        f'العنوان: {address}\n'
        f"الوصف: {state.get('description', '')}\n\n"
        'أأكّد الطلب وأبعته للفنيين؟',
        [[('✅ أكّد الطلب', 'new:confirm'), ('❌ إلغاء', 'new:abort')]],
    )


def _place_order(reply, user, state):
    from ..serializers import CreateRequestSerializer
    from .jobs import place_order

    customer = _customer(user)
    serializer = CreateRequestSerializer(
        data={
            'specialty': state.get('specialty', ''),
            'custom_specialty_text': state.get('custom_text', ''),
            'description': state.get('description', ''),
            'area_key': state.get('area_key', ''),
            'location_text': state.get('address', ''),
        }
    )
    if not serializer.is_valid():
        from ..exceptions import _first_message

        clear_state(reply.chat_id)
        reply.send(
            'الطلب ما اتعملش: ' + (_first_message(serializer.errors) or 'بيانات ناقصة') + '\nابدأ من جديد بـ /new.',
            [_back_row(user)],
            edit=True,
        )
        return
    created, offers = place_order(customer, serializer.validated_data)
    clear_state(reply.chat_id)
    reply.send(
        f'تم استلام طلبك #{created.pk} ✅\n'
        + (
            f"اتعرض على {offers['count']} فني، وهبلّغك هنا أول ما حد يقبله."
            if offers['count']
            else 'مفيش فني متاح في اللحظة دي، وهنعرضه على أول فني يبقى متاح.'
        ),
        [[('🔎 تفاصيل الطلب', f'req:view:{created.pk}')], _back_row(user)],
        edit=True,
    )


def _new_order_callback(reply, user, action, value, state):
    if action == 'start':
        start_new_order(reply, user, edit=True)
        return
    if action == 'abort':
        clear_state(reply.chat_id)
        reply.send('اتلغى الطلب الجديد. لو احتجت حاجة أنا هنا 👋', [_back_row(user)], edit=True)
        return
    if not state or state.get('flow') != 'new':
        reply.send('الطلب ده انتهت صلاحيته. ابدأ من جديد:', [[('➕ طلب جديد', 'new:start')], _back_row(user)], edit=True)
        return
    if action == 'spec':
        if value not in {k for k, _ in _specialty_choices()}:
            reply.send('الخدمة دي مش متاحة حالياً.', [[('➕ طلب جديد', 'new:start')]], edit=True)
            return
        state['specialty'] = value
        if value == 'other':
            state['step'] = 'custom'
            set_state(reply.chat_id, state)
            reply.send('اكتب اسم الخدمة اللي محتاجها:', [[('❌ إلغاء', 'new:abort')]], edit=True)
            return
        _ask_area(reply, state)
        return
    if action == 'area':
        if value not in AREAS_BY_KEY:
            reply.send('المنطقة غير صالحة.', [[('❌ إلغاء', 'new:abort')]], edit=True)
            return
        state['area_key'] = value
        _ask_address(reply, state)
        return
    if action == 'skipaddress':
        state['address'] = ''
        _ask_description(reply, state)
        return
    if action == 'confirm':
        if state.get('step') != 'confirm':
            reply.send('كمّل الخطوات الأول.', [[('❌ إلغاء', 'new:abort')]])
            return
        _place_order(reply, user, state)
        return
    reply.send(UNKNOWN_COMMAND, main_menu_rows(user))


def _new_order_text(reply, user, text, state):
    """A typed answer during the new-order flow."""
    step = state.get('step')
    if step == 'custom':
        if not text or len(text) > 200:
            reply.send('اكتب اسم الخدمة (لحد 200 حرف).')
            return
        state['custom_text'] = text
        _ask_area(reply, state)
    elif step == 'address':
        if len(text) > 300:
            reply.send('العنوان طويل، اختصره شوية (300 حرف كحد أقصى).')
            return
        state['address'] = text
        _ask_description(reply, state)
    elif step == 'description':
        if not text or len(text) > 2000:
            reply.send('اكتب وصف للمشكلة (لحد 2000 حرف).')
            return
        state['description'] = text
        _confirm_order(reply, state)
    else:
        reply.send('اختار من الأزرار اللي فوق عشان نكمّل، أو اضغط إلغاء.', [[('❌ إلغاء', 'new:abort')]])


# ---------------------------------------------------------------------------
# Technician
# ---------------------------------------------------------------------------


def _technician(user):
    from ..views import _technician_for

    return _technician_for(user)


def _offer_rows(offers):
    rows = []
    for offer in offers:
        rows.append(
            [
                (f"✅ قبول #{offer['request_id']}", f"offer:accept:{offer['broadcast_id']}"),
                (f"❌ رفض #{offer['request_id']}", f"offer:decline:{offer['broadcast_id']}"),
            ]
        )
    return rows


def show_incoming(reply, user, edit=False):
    from .broadcast import incoming_for_technician

    technician = _technician(user)
    offers = incoming_for_technician(technician)
    if not offers:
        reply.send(
            'مفيش طلبات معروضة عليك دلوقتي. هبلّغك هنا أول ما طلب جديد يوصل 🔔',
            [[('🔄 تحديث', 'menu:incoming')], _back_row(user)],
            edit=edit,
        )
        return
    lines = ['الطلبات المعروضة عليك:']
    for offer in offers:
        label = offer['custom_specialty_text'] if offer['specialty'] == 'other' and offer['custom_specialty_text'] else _specialty_label(offer['specialty'])
        price = f" — {offer['service_price']:g} ج.م" if offer.get('service_price') else ''
        star = ' ⭐ العميل طلبك بالاسم' if offer.get('preferred') else ''
        lines.append(f"#{offer['request_id']} — {label} — {offer['customer_area'] or '—'}{price}{star}\n{offer['description'][:120]}")
    rows = _offer_rows(offers)
    rows.append([('🔄 تحديث', 'menu:incoming')])
    rows.append(_back_row(user))
    reply.send('\n\n'.join(lines), rows, edit=edit)


def _broadcast_for_request(technician, request_id):
    return RequestBroadcast.objects.filter(request_id=request_id, technician_id=technician.pk).first()


def accept_offer(reply, user, broadcast_id, by_request=False):
    from .jobs import accept_offer as accept

    technician = _technician(user)
    if by_request:
        row = _broadcast_for_request(technician, broadcast_id)
        if row is None:
            reply.send('العرض ده مش موجود أو اتسحب.', [[('📥 الطلبات الواردة', 'menu:incoming')]], edit=True)
            return
        broadcast_id = row.pk
    job = accept(technician, broadcast_id)
    show_job(reply, user, job.pk, edit=True, prefix='قبلت الطلب ✅ العميل اتبلّغ وهو مستنيك.\n\n')


def decline_offer(reply, user, broadcast_id, by_request=False):
    from .broadcast import BroadcastError, decline_broadcast

    technician = _technician(user)
    if by_request:
        row = _broadcast_for_request(technician, broadcast_id)
        if row is None:
            reply.send('العرض ده مش موجود أو اتسحب.', [[('📥 الطلبات الواردة', 'menu:incoming')]], edit=True)
            return
        broadcast_id = row.pk
    try:
        decline_broadcast(broadcast_id, technician)
    except BroadcastError as exc:
        raise ApiError(exc.message, exc.status_code)
    reply.send('تم رفض الطلب. مش هيتعرض عليك تاني.', [[('📥 الطلبات الواردة', 'menu:incoming')], _back_row(user)], edit=True)


def _technician_jobs(technician):
    scope = Q(status__in=('assigned', 'in_progress')) | Q(status='completed', amount_status__isnull=True)
    return (
        Request.objects.filter(scope, technician_id=technician.pk)
        .select_related('customer')
        .order_by('-created_at', '-id')
    )


def show_jobs(reply, user, edit=False):
    technician = _technician(user)
    jobs = list(_technician_jobs(technician)[:10])
    if not jobs:
        reply.send(
            'مفيش شغل حالي. شوف الطلبات الواردة 👇',
            [[('📥 الطلبات الواردة', 'menu:incoming')], _back_row(user)],
            edit=edit,
        )
        return
    lines = ['شغلك الحالي:']
    for job in jobs:
        lines.append(f'#{job.pk} — {_service_label(job)} — {_status_label(job.status)} — {job.display_customer_name}')
    rows = []
    for i in range(0, len(jobs), 3):
        rows.append([(f'🔎 #{j.pk}', f'job:view:{j.pk}') for j in jobs[i:i + 3]])
    rows.append([('🔄 تحديث', 'menu:jobs')])
    rows.append(_back_row(user))
    reply.send('\n'.join(lines), rows, edit=edit)


def show_job(reply, user, request_id, edit=False, prefix=''):
    technician = _technician(user)
    job = Request.objects.filter(pk=request_id, technician_id=technician.pk).select_related('customer').first()
    if job is None:
        reply.send('الطلب غير موجود أو مش معاك.', [[('🛠️ شغلي الحالي', 'menu:jobs')], _back_row(user)], edit=edit)
        return
    lines = [
        f'طلب #{job.pk} — {_service_label(job)}',
        f'الحالة: {_status_label(job.status)}',
        f'العميل: {job.display_customer_name} — {job.display_customer_phone or "—"}',
        f'المكان: {job.display_location or "—"}',
        f'الوصف: {job.description}',
        _price_line(job),
    ]
    rows = []
    if job.status == 'assigned':
        rows.append([('🚗 في الطريق', f'job:onway:{job.pk}'), ('▶️ بدأت الشغل', f'job:start:{job.pk}')])
        rows.append([('💬 كلّم العميل', f'chat:open:{job.pk}'), ('🙅 اعتذار عن الطلب', f'job:reject:{job.pk}')])
    elif job.status == 'in_progress':
        rows.append([('✅ خلّصت الشغل', f'job:complete:{job.pk}'), ('💬 كلّم العميل', f'chat:open:{job.pk}')])
    elif job.status == 'completed':
        lines.append('سجّل المبلغ اللي استلمته من الموقع عشان يتحسب في محفظتك.')
        rows.append([('💵 تسجيل المبلغ', f'{_site_url()}/technician/dashboard.html')])
    rows.append([('🔄 تحديث', f'job:view:{job.pk}'), ('🛠️ شغلي الحالي', 'menu:jobs')])
    rows.append(_back_row(user))
    reply.send(prefix + '\n'.join(lines), rows, edit=edit)


def on_the_way(reply, user, request_id):
    from .notifications import notify_on_the_way

    technician = _technician(user)
    job = Request.objects.filter(pk=request_id, technician_id=technician.pk).select_related('customer').first()
    if job is None or job.status != 'assigned':
        reply.send('الطلب ده مش في مرحلة «في الطريق».', [[('🛠️ شغلي الحالي', 'menu:jobs')]], edit=True)
        return
    already = Message.objects.filter(request_id=job.pk, sender_id=user.pk, body=ON_THE_WAY_TEXT).exists()
    if already:
        reply.send('إنت بلّغت العميل قبل كده إنك في الطريق 🚗', [[('🔎 الطلب', f'job:view:{job.pk}')]], edit=True)
        return
    Message.objects.create(request_id=job.pk, sender_id=user.pk, sender_role='technician', body=ON_THE_WAY_TEXT)
    notify_on_the_way(job)
    show_job(reply, user, job.pk, edit=True, prefix='بلّغنا العميل إنك في الطريق 🚗\n\n')


def ask_start_pin(reply, user, request_id):
    technician = _technician(user)
    if not Request.objects.filter(pk=request_id, technician_id=technician.pk, status='assigned').exists():
        reply.send('الطلب ده مش جاهز للبدء.', [[('🛠️ شغلي الحالي', 'menu:jobs')]], edit=True)
        return
    set_state(reply.chat_id, {'flow': 'pin', 'request_id': int(request_id)})
    reply.send(
        f'اكتب كود البداية المكوّن من 4 أرقام اللي مع العميل لطلب #{request_id}:',
        [[('❌ إلغاء', f'job:view:{request_id}')]],
    )


def start_with_pin(reply, user, request_id, pin):
    from .jobs import start_job

    technician = _technician(user)
    try:
        job = start_job(technician, request_id, pin)
    except ApiError as exc:
        # A wrong code keeps the prompt open for another try.
        if exc.status_code == 400 and 'الكود غلط' in _error_text(exc):
            reply.send(_error_text(exc) + '\nجرّب تاني أو اضغط إلغاء.', [[('❌ إلغاء', f'job:view:{request_id}')]])
            return
        raise
    clear_state(reply.chat_id)
    show_job(reply, user, job.pk, prefix='بدأ الشغل ✅ بالتوفيق!\n\n')


def ask_work_notes(reply, user, request_id):
    technician = _technician(user)
    if not Request.objects.filter(pk=request_id, technician_id=technician.pk, status='in_progress').exists():
        reply.send('الطلب ده مش جاري دلوقتي.', [[('🛠️ شغلي الحالي', 'menu:jobs')]], edit=True)
        return
    set_state(reply.chat_id, {'flow': 'notes', 'request_id': int(request_id)})
    reply.send(
        f'اكتب باختصار إيه اللي اتعمل في طلب #{request_id} (5 أحرف على الأقل):',
        [[('❌ إلغاء', f'job:view:{request_id}')]],
    )


def complete_with_notes(reply, user, request_id, notes):
    from .jobs import complete_job

    technician = _technician(user)
    job = complete_job(technician, request_id, notes)
    clear_state(reply.chat_id)
    show_job(reply, user, job.pk, prefix='تم تسجيل إنهاء الشغل ✅ العميل هيأكّد المبلغ ويقيّمك.\n\n')


def confirm_reject(reply, user, request_id):
    reply.send(
        f'متأكد إنك عايز تعتذر عن طلب #{request_id}؟ هيتعرض على فنيين تانيين.',
        [[('نعم، اعتذر', f'job:rejectyes:{request_id}'), ('لا، رجوع', f'job:view:{request_id}')]],
        edit=True,
    )


def reject_job(reply, user, request_id):
    from .jobs import reject_assigned_job

    technician = _technician(user)
    offers = reject_assigned_job(technician, request_id)
    reply.send(
        f'تم الاعتذار عن طلب #{request_id}. '
        + (f"اتعرض على {offers['count']} فني تاني." if offers['count'] else 'مفيش فني تاني متاح حالياً.'),
        [[('📥 الطلبات الواردة', 'menu:incoming')], _back_row(user)],
        edit=True,
    )


def show_wallet(reply, user, edit=False):
    from .payments import can_receive_requests, debt_limit, ensure_wallet, pending_withdrawal

    technician = _technician(user)
    wallet = ensure_wallet(technician.pk)
    limit = debt_limit()
    debt = float(wallet.total_debt or 0)
    lines = [
        '💰 محفظتك:',
        f'الرصيد القابل للسحب: {float(wallet.balance or 0):g} ج.م',
        f'المستحق عليك للمنصة: {debt:g} ج.م (الحد {limit:g} ج.م)',
        f'إجمالي أرباحك: {float(wallet.total_earned or 0):g} ج.م',
        f'إجمالي اللي سحبته: {float(wallet.total_withdrawn or 0):g} ج.م',
    ]
    pending = pending_withdrawal(technician.pk)
    if pending:
        lines.append(f"طلب سحب قيد التنفيذ: {float(pending.get('amount') or 0):g} ج.م")
    if not can_receive_requests(technician.pk):
        lines.append('⛔ وصلت لحد المديونية، فمش بتوصلك طلبات جديدة لحد ما تسدد.')
    elif debt > 0:
        lines.append(f'باقي لك {max(limit - debt, 0):g} ج.م قبل ما توصل للحد.')
    reply.send(
        '\n'.join(lines),
        [[('💸 طلب سحب / سداد', f'{_site_url()}/technician/dashboard.html?tab=wallet')], _back_row(user)],
        edit=edit,
    )


# ---------------------------------------------------------------------------
# Chat (both sides)
# ---------------------------------------------------------------------------


def open_chat(reply, user, request_id):
    from ..chat_views import SENDABLE_STATES, _authorize

    job, role = _authorize(user, request_id)
    if job.status not in SENDABLE_STATES:
        reply.send('المحادثة مقفولة في المرحلة دي.', [_back_row(user)], edit=True)
        return
    other = job.technician.name if role == 'customer' else job.display_customer_name
    set_state(reply.chat_id, {'flow': 'chat', 'request_id': job.pk})
    reply.send(
        f'اكتب رسالتك لـ {other} بخصوص طلب #{job.pk}، وهتوصله فوراً.\n'
        'كل رسالة تكتبها هنا هتتبعت في المحادثة لحد ما تضغط «إنهاء».',
        [[('⛔ إنهاء المحادثة', 'chat:close:0')]],
    )


def send_chat_message(reply, user, request_id, text):
    from ..chat_views import MAX_BODY_LENGTH, SENDABLE_STATES, _authorize
    from .notifications import notify_chat_message

    if len(text) > MAX_BODY_LENGTH:
        reply.send(f'الرسالة طويلة جدًا (الحد الأقصى {MAX_BODY_LENGTH} حرف).')
        return
    job, role = _authorize(user, request_id)
    if job.status not in SENDABLE_STATES:
        clear_state(reply.chat_id)
        reply.send('المحادثة مقفولة في المرحلة دي.', [_back_row(user)])
        return
    message = Message.objects.create(request_id=job.pk, sender_id=user.pk, sender_role=role, body=text)
    if role == 'customer':
        notify_chat_message(job.technician.user_id, 'العميل', message.body, job.pk, 'technician', message.pk)
    else:
        notify_chat_message(
            job.customer.user_id, job.technician.name or 'الفني', message.body, job.pk, 'customer', message.pk
        )
    set_state(reply.chat_id, {'flow': 'chat', 'request_id': job.pk})
    reply.send('اتبعتت ✅', [[('⛔ إنهاء المحادثة', 'chat:close:0')]])


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


def show_admin_summary(reply, user, edit=False):
    pending_techs = Technician.objects.filter(approval_status='pending').count()
    open_requests = Request.objects.filter(status__in=('pending', 'no_technician')).count()
    active = Request.objects.filter(status__in=('assigned', 'in_progress')).count()
    disputed = Request.objects.filter(amount_status='disputed').count()
    customers = Customer.objects.count()
    reply.send(
        '📊 ملخص دشاوي:\n'
        f'فنيين مستنيين الاعتماد: {pending_techs}\n'
        f'طلبات مستنية فني: {open_requests}\n'
        f'طلبات شغالة دلوقتي: {active}\n'
        f'خلافات على المبلغ: {disputed}\n'
        f'عدد العملاء: {customers}',
        [[('🌐 لوحة الإدارة', f'{_site_url()}/admin/dashboard.html')], [('🔄 تحديث', 'menu:admin')], _back_row(user)],
        edit=edit,
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _linking(reply, chat_id, token):
    """``/start <token>`` or a pasted token from the link page."""
    user_id = telegram.consume_link_token(token, chat_id)
    if user_id is None:
        reply.send(telegram.LINK_INVALID)
        return 'invalid'
    user = User.objects.filter(pk=user_id).first()
    reply.send(telegram.LINK_SUCCESS, main_menu_rows(user) if user else None)
    return 'linked'


def _unlinked(reply):
    reply.send(LINK_HELP, [[('🔗 افتح صفحة الربط', f'{_site_url()}/telegram-link.html')]])
    return 'unlinked'


def _account_ok(reply, user):
    if user.blocked:
        reply.send('حسابك موقوف من قبل الإدارة. كلّم الدعم لو محتاج مساعدة.')
        return False
    return True


def _handle_command(reply, user, command, argument):
    chat_id = reply.chat_id
    if command in ('/start', '/menu'):
        clear_state(chat_id)
        show_main_menu(reply, user)
    elif command == '/help':
        show_help(reply, user)
    elif command == '/support':
        show_support(reply, user)
    elif command == '/cancel':
        clear_state(chat_id)
        reply.send('تمام، اتلغى اللي كنا فيه.', main_menu_rows(user))
    elif command == '/requests' and user.role == 'customer':
        clear_state(chat_id)
        show_customer_requests(reply, user)
    elif command == '/track' and user.role == 'customer':
        if not argument.lstrip('#').isdigit():
            reply.send('اكتب رقم الطلب بعد الأمر، مثلاً: /track 12', [[('📋 طلباتي', 'menu:requests')]])
        else:
            show_customer_request(reply, user, int(argument.lstrip('#')))
    elif command == '/new' and user.role == 'customer':
        start_new_order(reply, user)
    elif command == '/incoming' and user.role == 'technician':
        clear_state(chat_id)
        show_incoming(reply, user)
    elif command == '/jobs' and user.role == 'technician':
        clear_state(chat_id)
        show_jobs(reply, user)
    elif command == '/wallet' and user.role == 'technician':
        show_wallet(reply, user)
    elif command in ('/requests', '/jobs') and user.role == 'admin':
        show_admin_summary(reply, user)
    else:
        reply.send(UNKNOWN_COMMAND, main_menu_rows(user))
        return 'unknown'
    return command.lstrip('/')


def _handle_text(reply, user, text):
    """Free text: an answer to the open flow, or a stray message."""
    state = get_state(reply.chat_id)
    if not state:
        reply.send(UNKNOWN_COMMAND, main_menu_rows(user))
        return 'unknown'
    flow = state.get('flow')
    if flow == 'new' and user.role == 'customer':
        _new_order_text(reply, user, text, state)
    elif flow == 'pin' and user.role == 'technician':
        start_with_pin(reply, user, state['request_id'], text)
    elif flow == 'notes' and user.role == 'technician':
        complete_with_notes(reply, user, state['request_id'], text)
    elif flow == 'chat':
        send_chat_message(reply, user, state['request_id'], text)
    else:
        clear_state(reply.chat_id)
        reply.send(UNKNOWN_COMMAND, main_menu_rows(user))
        return 'unknown'
    return flow


def _handle_callback(reply, user, data):
    kind, _, rest = data.partition(':')
    action, _, value = rest.partition(':')
    state = get_state(reply.chat_id)

    if kind == 'menu':
        clear_state(reply.chat_id)
        handlers = {
            'main': lambda: show_main_menu(reply, user, edit=True),
            'help': lambda: show_help(reply, user, edit=True),
            'support': lambda: show_support(reply, user, edit=True),
            'requests': lambda: show_customer_requests(reply, user, edit=True),
            'incoming': lambda: show_incoming(reply, user, edit=True),
            'jobs': lambda: show_jobs(reply, user, edit=True),
            'wallet': lambda: show_wallet(reply, user, edit=True),
            'admin': lambda: show_admin_summary(reply, user, edit=True),
        }
        role_only = {'requests': 'customer', 'incoming': 'technician', 'jobs': 'technician', 'wallet': 'technician', 'admin': 'admin'}
        if action not in handlers or (action in role_only and user.role != role_only[action]):
            show_main_menu(reply, user, edit=True)
            return 'unknown'
        handlers[action]()
        return f'menu:{action}'

    if kind == 'new' and user.role == 'customer':
        _new_order_callback(reply, user, action, value, state)
        return f'new:{action}'

    if kind == 'req' and user.role == 'customer' and value.isdigit():
        request_id = int(value)
        if action == 'view':
            show_customer_request(reply, user, request_id, edit=True)
        elif action == 'cancel':
            confirm_cancel(reply, user, request_id)
        elif action == 'cancelyes':
            cancel_customer_request(reply, user, request_id)
        else:
            show_main_menu(reply, user, edit=True)
            return 'unknown'
        return f'req:{action}'

    if kind == 'offer' and user.role == 'technician':
        by_request = value.startswith('r')
        ident = value.lstrip('r')
        if not ident.isdigit():
            return 'unknown'
        if action == 'accept':
            accept_offer(reply, user, int(ident), by_request=by_request)
        elif action == 'decline':
            decline_offer(reply, user, int(ident), by_request=by_request)
        else:
            return 'unknown'
        return f'offer:{action}'

    if kind == 'job' and user.role == 'technician' and value.isdigit():
        request_id = int(value)
        if action == 'view':
            clear_state(reply.chat_id)
            show_job(reply, user, request_id, edit=True)
        elif action == 'onway':
            on_the_way(reply, user, request_id)
        elif action == 'start':
            ask_start_pin(reply, user, request_id)
        elif action == 'complete':
            ask_work_notes(reply, user, request_id)
        elif action == 'reject':
            confirm_reject(reply, user, request_id)
        elif action == 'rejectyes':
            reject_job(reply, user, request_id)
        else:
            return 'unknown'
        return f'job:{action}'

    if kind == 'chat':
        if action == 'open' and value.isdigit():
            open_chat(reply, user, int(value))
        elif action == 'close':
            clear_state(reply.chat_id)
            reply.send('اتقفلت المحادثة. لو عايز تبعت تاني افتحها من الطلب.', main_menu_rows(user))
        else:
            return 'unknown'
        return f'chat:{action}'

    show_main_menu(reply, user, edit=True)
    return 'unknown'


def handle_update(update):
    """Route one update; returns a short label the webhook echoes (and tests read)."""
    callback = update.get('callback_query')
    if callback:
        message = callback.get('message') or {}
        chat_id = (message.get('chat') or {}).get('id')
        telegram.answer_callback(callback.get('id'))
        if chat_id is None:
            return 'ignored'
        reply = Reply(chat_id, message.get('message_id'))
        user = _user_for_chat(chat_id)
        if user is None:
            return _unlinked(reply)
        if not _account_ok(reply, user):
            return 'blocked'
        try:
            return _handle_callback(reply, user, str(callback.get('data') or ''))
        except ApiError as exc:
            reply.send('⚠️ ' + _error_text(exc), main_menu_rows(user))
            return 'error'

    message = update.get('message') or update.get('edited_message')
    chat = (message or {}).get('chat') or {}
    chat_id = chat.get('id')
    if chat_id is None:
        return 'ignored'
    if chat.get('type') not in (None, 'private'):
        return 'ignored'
    text = str(message.get('text') or '').strip()
    if not text:
        return 'ignored'
    reply = Reply(chat_id)

    if text.lower().startswith('/start'):
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ''
        if token:
            return _linking(reply, chat_id, token)
    user = _user_for_chat(chat_id)
    if user is None:
        if len(text) == 48 and all(c in '0123456789abcdef' for c in text.lower()):
            return _linking(reply, chat_id, text.lower())
        if text.lower().startswith('/start'):
            reply.send(telegram.LINK_WELCOME, [[('🔗 افتح صفحة الربط', f'{_site_url()}/telegram-link.html')]])
            return 'welcome'
        return _unlinked(reply)
    if not _account_ok(reply, user):
        return 'blocked'

    try:
        if text.startswith('/'):
            command, _, argument = text.partition(' ')
            command = command.split('@', 1)[0].lower()
            return _handle_command(reply, user, command, argument.strip())
        return _handle_text(reply, user, text)
    except ApiError as exc:
        reply.send('⚠️ ' + _error_text(exc), main_menu_rows(user))
        return 'error'
