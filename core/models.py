"""
Django models for the legacy Dashawy SQLite schema.

Phase 1 of the Node.js -> Django migration. Every model below maps onto a table
that already exists in ``database/fixrequest.db``; ``Meta.db_table`` is set to
the exact legacy name so Django reads and writes the tables the Express app
created instead of inventing new ones.

Notes that apply across the module:

* ``USE_TZ`` is off (see settings.py), because the legacy schema stores
  datetimes as naive UTC text in SQLite's ``datetime('now')`` format. The
  ``utc_now`` helper writes that same naive-UTC shape.
* Money columns are ``FloatField`` rather than ``DecimalField`` because the
  existing columns are SQLite ``REAL``. Switching to Decimal would be a data
  migration, not a schema translation.
* ``User`` here is the application account. Django's own auth user remains
  separate (``auth_user``) for the admin site, because the legacy account has
  its own ``password_hash`` column and does not use Django's password hashers.
* Telegram linkage in this schema is stored as ``users.telegram_chat_id`` plus
  the short-lived ``telegram_link_tokens`` handshake. There is no separate
  ``telegram_links`` table in the database, so ``TelegramLinkToken`` below is
  the faithful translation; ``User.telegram_chat_id`` is the linked chat id.
* Partial UNIQUE indexes from schema.sql (``users.phone``, ``users.google_sub``,
  ``users.telegram_chat_id``, where the value is not NULL/empty) are not
  expressible as ordinary Django field options without changing the index
  names. They are left to the existing database and recreated by schema.sql.
"""

from datetime import datetime, timezone

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def utc_now():
    """Naive UTC timestamp, matching SQLite's ``datetime('now')`` format."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Enumerations (documentation + serializer/validation convenience; the live
# CHECK constraints remain the database-level source of truth)
# ---------------------------------------------------------------------------


class UserRole(models.TextChoices):
    CUSTOMER = 'customer', 'Customer'
    TECHNICIAN = 'technician', 'Technician'
    ADMIN = 'admin', 'Admin'


class Availability(models.TextChoices):
    AVAILABLE = 'available', 'Available'
    BUSY = 'busy', 'Busy'
    OFFLINE = 'offline', 'Offline'


class ApprovalStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    APPROVED = 'approved', 'Approved'
    REJECTED = 'rejected', 'Rejected'


class RequestStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    ASSIGNED = 'assigned', 'Assigned'
    IN_PROGRESS = 'in_progress', 'In progress'
    COMPLETED = 'completed', 'Completed'
    CANCELLED = 'cancelled', 'Cancelled'
    NO_TECHNICIAN = 'no_technician', 'No technician'


class BroadcastStatus(models.TextChoices):
    SENT = 'sent', 'Sent'
    ACCEPTED = 'accepted', 'Accepted'
    DECLINED = 'declined', 'Declined'
    EXPIRED = 'expired', 'Expired'


class WithdrawalStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    APPROVED = 'approved', 'Approved'
    PAID = 'paid', 'Paid'
    REJECTED = 'rejected', 'Rejected'


class MessageSenderRole(models.TextChoices):
    CUSTOMER = 'customer', 'Customer'
    TECHNICIAN = 'technician', 'Technician'


# ---------------------------------------------------------------------------
# Accounts and profiles
# ---------------------------------------------------------------------------


class User(models.Model):
    """Authentication account (``users``)."""

    id = models.AutoField(primary_key=True)
    email = models.CharField(max_length=254, unique=True)
    phone = models.CharField(max_length=32, null=True, blank=True)
    password_hash = models.CharField(max_length=255)
    role = models.CharField(max_length=20, choices=UserRole.choices)
    created_at = models.DateTimeField(default=utc_now)
    must_change_password = models.BooleanField(default=False, null=True, blank=True)
    is_super_admin = models.BooleanField(default=False, null=True, blank=True)
    blocked = models.BooleanField(default=False, null=True, blank=True)
    failed_login_attempts = models.IntegerField(default=0)
    login_locked_until = models.DateTimeField(null=True, blank=True)
    permissions = models.TextField(default='[]', null=True, blank=True)
    google_sub = models.CharField(max_length=255, null=True, blank=True)
    telegram_chat_id = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        db_table = 'users'
        indexes = [
            models.Index(fields=['login_locked_until'], name='idx_users_login_lock'),
        ]

    def __str__(self):
        return self.email

    # DRF's IsAuthenticated permission and SimpleJWT both expect the user
    # object to answer these. core.User is a legacy table, not an
    # AbstractBaseUser, so they are supplied explicitly.
    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    @property
    def is_active(self):
        return not bool(self.blocked)

    def get_username(self):
        return self.email


class Customer(models.Model):
    """Customer profile linked one-to-one to a user account (``customers``)."""

    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        db_column='user_id',
        related_name='customer',
    )
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=32, null=True, blank=True)
    area_key = models.CharField(max_length=64, null=True, blank=True)
    location_text = models.TextField(null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    blocked = models.BooleanField(default=False, null=True, blank=True)
    created_at = models.DateTimeField(default=utc_now)

    class Meta:
        db_table = 'customers'

    def __str__(self):
        return self.name


class Technician(models.Model):
    """Technician profile linked one-to-one to a user account (``technicians``)."""

    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        db_column='user_id',
        related_name='technician',
    )
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=32)
    specialty = models.CharField(max_length=100)
    location_text = models.TextField(null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    availability = models.CharField(
        max_length=20, choices=Availability.choices, default=Availability.AVAILABLE
    )
    criminal_record_path = models.CharField(max_length=500, null=True, blank=True)
    national_id_path = models.CharField(max_length=500, null=True, blank=True)
    documents_verified = models.BooleanField(default=False, null=True, blank=True)
    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
        null=True,
        blank=True,
    )
    approval_note = models.TextField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    # Plain integer in the legacy schema (no REFERENCES clause).
    approved_by = models.IntegerField(null=True, blank=True)
    current_latitude = models.FloatField(null=True, blank=True)
    current_longitude = models.FloatField(null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)
    location_sharing = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=utc_now)

    class Meta:
        db_table = 'technicians'
        indexes = [
            models.Index(fields=['availability'], name='idx_technicians_avail'),
            models.Index(fields=['specialty'], name='idx_technicians_specialty'),
        ]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# One-time passwords and Telegram linking
# ---------------------------------------------------------------------------


class Otp(models.Model):
    """Single-use sign-in code; only the SHA-256 digest is stored (``otps``)."""

    id = models.AutoField(primary_key=True)
    email = models.CharField(max_length=254)
    otp_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False, null=True, blank=True)
    created_at = models.DateTimeField(default=utc_now, null=True, blank=True)

    class Meta:
        db_table = 'otps'
        indexes = [
            models.Index(fields=['email', 'created_at'], name='idx_otps_email'),
        ]

    def __str__(self):
        return f'{self.email} ({self.created_at})'


class TelegramLinkToken(models.Model):
    """
    Single-use token connecting a website session to a Telegram chat
    (``telegram_link_tokens``).

    The bot consumes the row, writes ``User.telegram_chat_id`` and deletes the
    token. That delete is what makes the handshake single-use.
    """

    token = models.CharField(primary_key=True, max_length=128)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column='user_id',
        related_name='telegram_link_tokens',
        db_index=False,
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=utc_now)

    class Meta:
        db_table = 'telegram_link_tokens'
        indexes = [
            models.Index(fields=['user'], name='idx_telegram_link_tokens_user'),
        ]

    def __str__(self):
        return self.token


# ---------------------------------------------------------------------------
# Service requests
# ---------------------------------------------------------------------------


class AdminPasswordResetAudit(models.Model):
    """Who reset whose password, when, and from where.

    The temporary password is never stored here (or anywhere else): only the
    fact that a reset happened. ``target`` may be any account role, while
    ``admin`` is the authenticated administrator who performed the action.
    """

    id = models.AutoField(primary_key=True)
    admin = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column='admin_id',
        related_name='password_resets_performed',
        db_index=False,
    )
    target = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column='target_id',
        related_name='password_resets_received',
        db_index=False,
    )
    created_at = models.DateTimeField(default=utc_now)
    ip_address = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        db_table = 'admin_password_reset_audits'
        indexes = [
            models.Index(
                fields=['target', 'created_at'],
                name='idx_pwd_reset_target',
            ),
            models.Index(
                fields=['admin', 'created_at'],
                name='idx_pwd_reset_admin',
            ),
        ]

    def __str__(self):
        return f'admin #{self.admin_id} reset user #{self.target_id}'

    # Friendly aliases for callers that read the audit by semantic name.
    @property
    def admin_user_id(self):
        return self.admin_id

    @property
    def target_user_id(self):
        return self.target_id


class Request(models.Model):
    """A customer's service request (``requests``)."""

    id = models.AutoField(primary_key=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        db_column='customer_id',
        related_name='requests',
        db_index=False,
        null=True,
        blank=True,
        help_text=(
            'NULL for a guest order. Guest orders carry their own name/phone '
            'and a hashed tracking token instead of an account.'
        ),
    )
    technician = models.ForeignKey(
        Technician,
        on_delete=models.SET_NULL,
        db_column='technician_id',
        related_name='requests',
        null=True,
        blank=True,
        db_index=False,
    )
    specialty = models.CharField(max_length=100)
    custom_specialty_text = models.TextField(null=True, blank=True)
    description = models.TextField()
    location_text = models.TextField(null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=RequestStatus.choices, default=RequestStatus.PENDING
    )
    # JSON array of technician ids already offered/declined, kept as text to
    # match the legacy column.
    declined_technicians = models.TextField(default='[]')
    created_at = models.DateTimeField(default=utc_now)
    updated_at = models.DateTimeField(default=utc_now)
    assigned_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Doorstep start PIN (customer-visible; technician enters it).
    start_pin = models.CharField(max_length=16, null=True, blank=True)
    start_pin_attempts = models.IntegerField(default=0)
    start_pin_locked_until = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)

    # Pricing / commission. REAL in SQLite.
    service_price = models.FloatField(default=0)
    commission_percent = models.FloatField(default=0)
    commission_amount = models.FloatField(default=0)
    technician_earning = models.FloatField(default=0)

    # Payments.
    payment_method = models.CharField(max_length=20, null=True, blank=True)
    payment_status = models.CharField(max_length=30, null=True, blank=True)
    payment_reference = models.CharField(max_length=255, null=True, blank=True)
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)
    payment_confirmed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        db_column='payment_confirmed_by',
        related_name='+',
        null=True,
        blank=True,
        db_index=False,
    )

    # Two-sided cash confirmation.
    customer_reported_amount = models.FloatField(null=True, blank=True)
    technician_reported_amount = models.FloatField(null=True, blank=True)
    final_amount = models.FloatField(null=True, blank=True)
    amount_status = models.CharField(max_length=20, null=True, blank=True)
    customer_reported_at = models.DateTimeField(null=True, blank=True)
    technician_reported_at = models.DateTimeField(null=True, blank=True)
    dispute_resolved_at = models.DateTimeField(null=True, blank=True)
    dispute_resolved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        db_column='dispute_resolved_by',
        related_name='+',
        null=True,
        blank=True,
        db_index=False,
    )
    dispute_note = models.TextField(null=True, blank=True)

    work_notes = models.TextField(null=True, blank=True)
    completed_by_technician_at = models.DateTimeField(null=True, blank=True)

    # Waiting queue.
    is_queued = models.BooleanField(default=False)
    queued_at = models.DateTimeField(null=True, blank=True)
    queue_expires_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(null=True, blank=True)

    customer_latitude_updated_at = models.DateTimeField(null=True, blank=True)

    # ------------------------------------------------------------------
    # Guest ordering (no account required)
    # ------------------------------------------------------------------
    # ``guest_token_hash`` stores only a SHA-256 digest. The raw token is shown
    # once to the guest in the tracking link and is long/random enough that IDs
    # cannot be enumerated.
    guest_name = models.CharField(max_length=255, null=True, blank=True)
    guest_phone = models.CharField(max_length=32, null=True, blank=True)
    guest_token_hash = models.CharField(
        max_length=128, null=True, blank=True, db_index=True
    )
    guest_token_created_at = models.DateTimeField(null=True, blank=True)
    guest_linked_at = models.DateTimeField(null=True, blank=True)
    order_photo_path = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        db_table = 'requests'
        indexes = [
            models.Index(
                fields=['amount_status', 'technician_reported_at'],
                name='idx_requests_amount_status',
            ),
            models.Index(fields=['status'], name='idx_requests_status'),
            models.Index(fields=['customer'], name='idx_requests_customer'),
            models.Index(fields=['technician'], name='idx_requests_technician'),
            models.Index(fields=['specialty'], name='idx_requests_specialty'),
            models.Index(fields=['payment_status'], name='idx_requests_payment_status'),
            # The legacy index name (idx_requests_payment_confirmed_by) is 33
            # characters, over Django's 30-character portable limit. The index
            # already exists in the database, so it is not redeclared here.
        ]

    def __str__(self):
        return f'Request #{self.pk} ({self.status})'

    @property
    def is_guest(self):
        return self.customer_id is None

    @property
    def display_customer_name(self):
        if self.customer_id and self.customer:
            return self.customer.name
        return self.guest_name or ''

    @property
    def display_customer_phone(self):
        if self.customer_id and self.customer:
            return self.customer.phone or ''
        return self.guest_phone or ''

    @property
    def display_location(self):
        if self.customer_id and self.customer:
            return self.customer.location_text or self.location_text or ''
        return self.location_text or ''


class GuestSubmissionLog(models.Model):
    """One guest-order attempt, kept for IP/phone rate limiting and abuse review.

    The row deliberately stores no form content: the phone is kept because the
    per-phone limit needs it, and it is removed by the retention task together
    with the guest order.
    """

    id = models.AutoField(primary_key=True)
    ip_address = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    phone = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(default=utc_now, db_index=True)

    class Meta:
        db_table = 'guest_submission_logs'

    def __str__(self):
        return f'{self.phone or "?"} @ {self.created_at}'


class RequestBroadcast(models.Model):
    """
    One row per (request, technician) offer in the Uber-style broadcast
    (``request_broadcasts``). The first technician to accept wins.
    """

    id = models.AutoField(primary_key=True)
    request = models.ForeignKey(
        Request,
        on_delete=models.CASCADE,
        db_column='request_id',
        related_name='broadcasts',
        db_index=False,
    )
    technician = models.ForeignKey(
        Technician,
        on_delete=models.CASCADE,
        db_column='technician_id',
        related_name='broadcasts',
        db_index=False,
    )
    status = models.CharField(
        max_length=20, choices=BroadcastStatus.choices, default=BroadcastStatus.SENT
    )
    sent_at = models.DateTimeField(default=utc_now)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'request_broadcasts'
        constraints = [
            models.UniqueConstraint(
                fields=['request', 'technician'], name='idx_broadcast_unique_pair'
            ),
        ]
        indexes = [
            models.Index(
                fields=['technician', 'status'], name='idx_broadcast_tech_status'
            ),
            models.Index(fields=['request'], name='idx_broadcast_request'),
            models.Index(fields=['technician'], name='idx_broadcasts_tech'),
        ]

    def __str__(self):
        return f'{self.request_id} -> {self.technician_id} ({self.status})'


# ---------------------------------------------------------------------------
# Pricing, wallets and withdrawals
# ---------------------------------------------------------------------------


class ServicePricing(models.Model):
    """Admin-edited service tariff (``service_pricing``)."""

    service_key = models.CharField(primary_key=True, max_length=100)
    price = models.FloatField(default=0)
    is_active = models.BooleanField(default=True, null=True, blank=True)
    updated_at = models.DateTimeField(default=utc_now, null=True, blank=True)

    class Meta:
        db_table = 'service_pricing'

    def __str__(self):
        return f'{self.service_key}: {self.price}'


class TechnicianWallet(models.Model):
    """
    Two-pot technician wallet (``technician_wallet``).

    ``balance`` is withdrawable credit; ``total_debt`` is money owed to the
    platform. They are deliberately separate and both non-negative.
    """

    technician = models.OneToOneField(
        Technician,
        on_delete=models.CASCADE,
        db_column='technician_id',
        primary_key=True,
        related_name='wallet',
    )
    balance = models.FloatField(default=0, null=True, blank=True)
    total_debt = models.FloatField(default=0)
    total_earned = models.FloatField(default=0, null=True, blank=True)
    total_owed = models.FloatField(default=0, null=True, blank=True)
    total_paid_to_platform = models.FloatField(default=0, null=True, blank=True)
    total_withdrawn = models.FloatField(default=0)
    last_updated = models.DateTimeField(default=utc_now, null=True, blank=True)

    class Meta:
        db_table = 'technician_wallet'

    def __str__(self):
        return f'Wallet of technician #{self.technician_id}'


class WithdrawalRequest(models.Model):
    """Technician payout request (``withdrawal_requests``)."""

    id = models.AutoField(primary_key=True)
    technician = models.ForeignKey(
        Technician,
        on_delete=models.CASCADE,
        db_column='technician_id',
        related_name='withdrawal_requests',
        db_index=False,
    )
    amount = models.FloatField()
    status = models.CharField(
        max_length=20, choices=WithdrawalStatus.choices, default=WithdrawalStatus.PENDING
    )
    requested_at = models.DateTimeField(default=utc_now, null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        db_column='processed_by',
        related_name='+',
        null=True,
        blank=True,
        db_index=False,
    )
    note = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'withdrawal_requests'
        indexes = [
            models.Index(fields=['status'], name='idx_withdrawal_status'),
            models.Index(fields=['technician'], name='idx_withdrawal_tech'),
            models.Index(fields=['processed_by'], name='idx_withdrawals_processed_by'),
        ]

    def __str__(self):
        return f'Withdrawal #{self.pk} ({self.status})'


class WalletTransaction(models.Model):
    """Append-only wallet ledger (``wallet_transactions``)."""

    TYPE_PAYMENT = 'payment'
    TYPE_DEBT = 'debt'
    TYPE_PAYOUT = 'payout'
    TYPE_WITHDRAWAL = 'withdrawal'
    TYPE_ADJUSTMENT = 'adjustment'
    TYPE_CHOICES = [
        (TYPE_PAYMENT, 'Payment'),
        (TYPE_DEBT, 'Debt'),
        (TYPE_PAYOUT, 'Payout'),
        (TYPE_WITHDRAWAL, 'Withdrawal'),
        (TYPE_ADJUSTMENT, 'Adjustment'),
    ]

    id = models.AutoField(primary_key=True)
    technician = models.ForeignKey(
        Technician,
        on_delete=models.CASCADE,
        db_column='technician_id',
        related_name='wallet_transactions',
        db_index=False,
    )
    request = models.ForeignKey(
        Request,
        on_delete=models.SET_NULL,
        db_column='request_id',
        related_name='wallet_transactions',
        null=True,
        blank=True,
        db_index=False,
    )
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    amount = models.FloatField()
    balance_after = models.FloatField()
    debt_after = models.FloatField(default=0)
    note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=utc_now, null=True, blank=True)

    class Meta:
        db_table = 'wallet_transactions'
        indexes = [
            models.Index(fields=['technician'], name='idx_wallet_tx_tech'),
            models.Index(fields=['request'], name='idx_wallet_tx_request'),
        ]

    def __str__(self):
        return f'{self.type} {self.amount} (technician #{self.technician_id})'


# ---------------------------------------------------------------------------
# Reference data (specialties, settings)
# ---------------------------------------------------------------------------


class Specialty(models.Model):
    """DB-driven service specialty (``specialties``)."""

    id = models.AutoField(primary_key=True)
    key = models.CharField(max_length=100, unique=True)
    label = models.CharField(max_length=255)
    icon = models.CharField(max_length=100, default='fa-solid fa-wrench')
    enabled = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=utc_now)

    class Meta:
        db_table = 'specialties'

    def __str__(self):
        return self.label


class Setting(models.Model):
    """Key/value admin settings (``settings``)."""

    key = models.CharField(primary_key=True, max_length=100)
    value = models.TextField(default='')

    class Meta:
        db_table = 'settings'

    def __str__(self):
        return self.key


# ---------------------------------------------------------------------------
# Public technician profile, portfolio and reviews
# ---------------------------------------------------------------------------


class TechnicianProfile(models.Model):
    """Public biography / experience / avatar (``technician_profiles``)."""

    technician = models.OneToOneField(
        Technician,
        on_delete=models.CASCADE,
        db_column='technician_id',
        primary_key=True,
        related_name='profile',
    )
    bio = models.TextField(null=True, blank=True)
    years_experience = models.IntegerField(default=0, null=True, blank=True)
    profile_image_path = models.CharField(max_length=500, null=True, blank=True)
    updated_at = models.DateTimeField(default=utc_now, null=True, blank=True)

    class Meta:
        db_table = 'technician_profiles'

    def __str__(self):
        return f'Profile of technician #{self.technician_id}'


class TechnicianPortfolio(models.Model):
    """Sample photo of previous work (``technician_portfolio``)."""

    id = models.AutoField(primary_key=True)
    technician = models.ForeignKey(
        Technician,
        on_delete=models.CASCADE,
        db_column='technician_id',
        related_name='portfolio',
        db_index=False,
    )
    image_path = models.CharField(max_length=500)
    caption = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=utc_now, null=True, blank=True)

    class Meta:
        db_table = 'technician_portfolio'
        indexes = [
            models.Index(fields=['technician'], name='idx_portfolio_technician'),
        ]

    def __str__(self):
        return self.image_path


class Review(models.Model):
    """One customer review per completed request (``reviews``)."""

    id = models.AutoField(primary_key=True)
    request = models.OneToOneField(
        Request,
        on_delete=models.CASCADE,
        db_column='request_id',
        related_name='review',
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        db_column='customer_id',
        related_name='reviews',
        db_index=False,
    )
    technician = models.ForeignKey(
        Technician,
        on_delete=models.CASCADE,
        db_column='technician_id',
        related_name='reviews',
        db_index=False,
    )
    rating = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(null=True, blank=True)
    technician_reply = models.TextField(null=True, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=utc_now, null=True, blank=True)
    hidden = models.BooleanField(default=False, null=True, blank=True)
    hidden_reason = models.TextField(null=True, blank=True)
    hidden_at = models.DateTimeField(null=True, blank=True)
    hidden_by = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = 'reviews'
        indexes = [
            models.Index(fields=['technician'], name='idx_reviews_technician'),
            models.Index(fields=['customer'], name='idx_reviews_customer'),
        ]

    def __str__(self):
        return f'Review #{self.pk} ({self.rating}/5)'


# ---------------------------------------------------------------------------
# Legal compliance (Egyptian Personal Data Protection Law 151/2020)
# ---------------------------------------------------------------------------


class Consent(models.Model):
    """Explicit consent record (``consents``)."""

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column='user_id',
        related_name='consents',
        db_index=False,
    )
    consent_type = models.CharField(max_length=100)
    granted_at = models.DateTimeField(default=utc_now, null=True, blank=True)
    ip_address = models.CharField(max_length=64, null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    policy_version = models.CharField(max_length=20)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'consents'
        indexes = [
            models.Index(fields=['user'], name='idx_consents_user'),
        ]

    def __str__(self):
        return f'{self.consent_type} (user #{self.user_id})'


class DataSubjectRequest(models.Model):
    """Access / rectification / erasure etc. request (``data_subject_requests``)."""

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column='user_id',
        related_name='data_subject_requests',
        db_index=False,
    )
    request_type = models.CharField(max_length=50)
    status = models.CharField(max_length=20, default='pending')
    requested_at = models.DateTimeField(default=utc_now, null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    admin_note = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'data_subject_requests'
        indexes = [
            models.Index(fields=['user'], name='idx_dsr_user'),
        ]

    def __str__(self):
        return f'{self.request_type} (user #{self.user_id})'


class BreachLog(models.Model):
    """Breach notification log (``breach_logs``)."""

    id = models.AutoField(primary_key=True)
    detected_at = models.DateTimeField(default=utc_now, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    affected_users_count = models.IntegerField(null=True, blank=True)
    notified_center_at = models.DateTimeField(null=True, blank=True)
    notified_users_at = models.DateTimeField(null=True, blank=True)
    severity = models.CharField(max_length=20, null=True, blank=True)

    class Meta:
        db_table = 'breach_logs'

    def __str__(self):
        return f'Breach #{self.pk} ({self.severity})'


# ---------------------------------------------------------------------------
# Web push and chat
# ---------------------------------------------------------------------------


class PushSubscription(models.Model):
    """One browser/device Web Push subscription (``push_subscriptions``)."""

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column='user_id',
        related_name='push_subscriptions',
        db_index=False,
    )
    endpoint = models.TextField(unique=True)
    p256dh = models.TextField()
    auth = models.TextField()
    user_agent = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=utc_now, null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'push_subscriptions'
        indexes = [
            models.Index(fields=['user'], name='idx_push_user'),
        ]

    def __str__(self):
        return self.endpoint[:60]


class Message(models.Model):
    """Customer <-> technician chat message for one request (``messages``)."""

    id = models.AutoField(primary_key=True)
    request = models.ForeignKey(
        Request,
        on_delete=models.CASCADE,
        db_column='request_id',
        related_name='messages',
        db_index=False,
    )
    sender = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column='sender_id',
        related_name='messages',
        db_index=False,
    )
    sender_role = models.CharField(max_length=20, choices=MessageSenderRole.choices)
    body = models.TextField()
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=utc_now, null=True, blank=True)

    class Meta:
        db_table = 'messages'
        indexes = [
            models.Index(fields=['request', 'created_at'], name='idx_messages_request'),
            models.Index(fields=['request', 'read_at'], name='idx_messages_unread'),
            models.Index(fields=['sender'], name='idx_messages_sender'),
        ]

    def __str__(self):
        return f'Message #{self.pk} on request #{self.request_id}'
