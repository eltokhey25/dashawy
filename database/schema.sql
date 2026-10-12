-- Fix Request - Database Schema
-- SQLite schema for users, technicians, customers, and service requests.

-- Users table holds the single authentication account (email/password/role).
-- Roles: 'customer' | 'technician' | 'admin'
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE,
  -- Login identity alongside email. NULL for admins (email-only) and for any
  -- legacy account whose phone collides with another profile. Uniqueness is
  -- enforced by the partial index idx_users_phone_unique (created in db.js),
  -- not by a column constraint, so that the backfill can run first.
  --
  -- A customer account with no phone here is INCOMPLETE: the API refuses every
  -- customer route for them until POST /api/auth/complete-profile claims one.
  -- That is the state a Google sign-in lands in, since Google never hands us a
  -- number.
  phone         TEXT,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('customer', 'technician', 'admin')),
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  -- Google's stable per-application account identifier, set the first time
  -- this account signs in with Google and never changed afterwards. Linkage is
  -- done on this rather than on the email because the email can change at
  -- Google while `sub` cannot. NULL means "this account has never used Google".
  -- Uniqueness is enforced by the partial index idx_users_google_sub_unique
  -- (created in db.js), which is what stops two local accounts from ever
  -- claiming the same Google identity.
  google_sub    TEXT,
  must_change_password INTEGER DEFAULT 0,
  is_super_admin INTEGER DEFAULT 0,
  blocked INTEGER DEFAULT 0,
  -- Per-ACCOUNT brute-force brake, complementary to the per-IP limiter on
  -- /api/auth/login. The IP limiter alone cannot stop a distributed attack: a
  -- botnet gets a fresh 10-attempt budget per address against one victim. These
  -- two columns are what make that attack converge.
  -- failed_login_attempts is reset to 0 on any successful login and whenever a
  -- lock is applied, so a real user who fumbles twice is never punished and a
  -- locked account gets a fresh budget once the window passes.
  failed_login_attempts INTEGER NOT NULL DEFAULT 0,
  login_locked_until     TEXT,
  permissions TEXT DEFAULT '[]'
);

-- One-time passwords for the passwordless sign-in flow
-- (POST /api/auth/request-otp -> POST /api/auth/verify-otp).
--
-- Only the SHA-256 of the code is stored, never the code itself: this table is
-- read by nothing but the verify route, and a leaked database must not hand an
-- attacker working codes for the next ten minutes. A 6-digit code has a small
-- enough space that a hash is not a brute-force obstacle on its own, which is
-- why the verify route is rate limited per email AND per IP.
--
-- expires_at is SQLite's own datetime('now', '+10 minutes') text, so the
-- "is it still valid" test stays a comparison in SQL instead of a JS parse, and
-- the column sorts as text against every other timestamp in the database.
-- `used` is flipped to 1 the moment a code is spent, which is what stops the
-- same code from being replayed for a second session.
CREATE TABLE IF NOT EXISTS otps (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email      TEXT NOT NULL,
  otp_hash   TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used       INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);

-- Backs both OTP queries: "the newest live code for this address" (verify) and
-- "was a code issued for this address in the last minute" (the cooldown).
CREATE INDEX IF NOT EXISTS idx_otps_email ON otps(email, created_at);

-- Customers: profile data linked to a user account.
CREATE TABLE IF NOT EXISTS customers (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  -- Nullable on purpose: a profile is created the moment a Google sign-in
  -- succeeds, from the name Google returned, and the number is claimed a step
  -- later. Making this NOT NULL would have forced a placeholder value into the
  -- column and every reader would then have to guess whether an empty string
  -- meant "not claimed yet" or "broken row".
  phone         TEXT,
  -- The selected Alexandria neighbourhood, stored as the key from
  -- config.ALEXANDRIA_AREAS rather than only being flattened into
  -- location_text, so the "complete your profile" form can prefill itself and
  -- an admin can filter on a neighbourhood rather than on free text.
  -- Nullable: accounts registered before this column existed have their
  -- neighbourhood only inside location_text.
  area_key      TEXT,
  location_text TEXT,
  latitude      REAL,
  longitude     REAL,
  blocked       INTEGER DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Technicians: profile data linked to a user account.
-- availability: 'available' | 'busy' | 'offline'
CREATE TABLE IF NOT EXISTS technicians (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  phone         TEXT NOT NULL,
  specialty     TEXT NOT NULL,
  location_text TEXT,
  latitude      REAL,
  longitude     REAL,
  availability  TEXT NOT NULL DEFAULT 'available'
    CHECK (availability IN ('available', 'busy', 'offline')),
  criminal_record_path TEXT,
  national_id_path     TEXT,
  documents_verified   INTEGER DEFAULT 0,
  approval_status      TEXT DEFAULT 'pending',   -- 'pending' | 'approved' | 'rejected'
  approval_note        TEXT,
  approved_at          TEXT,
  approved_by          INTEGER,
  -- Live location, written by POST /api/technician/location while the
  -- technician is working a job. current_* are the last reported point (a
  -- customer/technician may well be offline between reports), location_updated_at
  -- is when that point was taken, and location_sharing is 1 only while sharing
  -- is deliberately on — a stale point must never read as "live".
  current_latitude     REAL,
  current_longitude    REAL,
  location_updated_at  TEXT,
  location_sharing     INTEGER NOT NULL DEFAULT 0
    CHECK (location_sharing IN (0, 1)),
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Service requests created by customers.
-- status: 'pending' | 'assigned' | 'in_progress' | 'completed' | 'cancelled'
-- declined_technicians: JSON array of technician ids already rejected this request,
-- used by the auto-assignment logic to pick the NEXT best candidate.
CREATE TABLE IF NOT EXISTS requests (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id          INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  technician_id        INTEGER REFERENCES technicians(id) ON DELETE SET NULL,
  specialty            TEXT NOT NULL,
  custom_specialty_text TEXT,
  description          TEXT NOT NULL,
  location_text        TEXT,
  latitude             REAL,
  longitude            REAL,
  status               TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'assigned', 'in_progress', 'completed', 'cancelled', 'no_technician')),
  declined_technicians TEXT NOT NULL DEFAULT '[]',
  created_at           TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
  assigned_at          TEXT,
  completed_at         TEXT,
  -- Start PIN (Uber-style doorstep confirmation). Minted when a technician is
  -- assigned the request and shown ONLY to the customer; the technician has to
  -- type it at the door to move the job to 'in_progress'. NULL means no PIN was
  -- ever issued (a request assigned before this feature existed, or one placed
  -- directly without a broadcast).
  start_pin            TEXT,
  -- Brute-force brake on the 4-digit code: wrong tries so far, and the instant
  -- until which further tries are refused. Both reset on a correct PIN.
  start_pin_attempts   INTEGER NOT NULL DEFAULT 0,
  start_pin_locked_until TEXT,
  started_at           TEXT,
  -- Money. Populated by services/pricing.js when the request is created.
  -- 0 means "not priced yet", which is the correct value for a request whose
  -- specialty has no base price configured.
  service_price        REAL NOT NULL DEFAULT 0,
  -- The commission rate actually applied to this request. Stored per request so
  -- a later tariff change does not retroactively alter settled amounts.
  commission_percent   REAL NOT NULL DEFAULT 0,
  commission_amount    REAL NOT NULL DEFAULT 0,
  technician_earning   REAL NOT NULL DEFAULT 0,
  -- Payments. payment_method: 'cash' | 'instapay'.
  -- payment_status: NULL/'unpaid' | 'pending_verification' | 'confirmed' | 'refunded'.
  payment_method       TEXT,
  payment_status       TEXT,
  payment_reference    TEXT,
  payment_confirmed_at TEXT,
  payment_confirmed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  -- Two-sided confirmation of a CASH payment. The customer handed the money to the
  -- technician directly, so the amount each side reports is the only evidence the
  -- platform has of what was actually collected. The technician's figure alone is
  -- self-reported and under-reporting it silently undercuts the commission, so both
  -- sides report and the service compares them (services/payments.js).
  --
  -- amount_status is the state machine:
  --   NULL       nothing reported yet (or an InstaPay request, which needs no
  --              confirmation because the platform saw the money itself)
  --   'pending'  one side reported, waiting on the other
  --   'matched'   both sides reported the same amount; commission booked
  --   'disputed' the two figures differ; frozen until an admin resolves it
  --   'resolved'  an admin (or the confirmation timeout) settled it
  --
  -- Deliberately not a CHECK constraint: SQLite cannot ALTER one in place, and
  -- adding it would force a full table rebuild of requests — the same trap
  -- migrateRequestStatuses() documents. The vocabulary is enforced in the service
  -- instead, and the value is only ever written from there.
  customer_reported_amount   REAL,
  technician_reported_amount REAL,
  -- The amount the commission was actually computed from, i.e. the agreed figure
  -- after a match or an admin decision. NULL until the request is settled.
  final_amount         REAL,
  amount_status        TEXT,
  customer_reported_at TEXT,
  technician_reported_at TEXT,
  dispute_resolved_at  TEXT,
  dispute_resolved_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  dispute_note         TEXT,
  -- Free-text summary of the work carried out, supplied by the technician when
  -- completing the request.
  work_notes           TEXT,
  -- Distinguishes a technician-driven completion from an admin-driven one.
  completed_by_technician_at TEXT,
  -- Waiting queue. is_queued=1 means nobody was available when the request was
  -- created, so the queue service keeps re-offering it to newly-available
  -- technicians until someone accepts or QUEUE_MAX_HOURS is reached.
  -- queue_expires_at is stored as well as being derivable from queued_at, so the
  -- deadline survives a QUEUE_MAX_HOURS change without rewriting history.
  is_queued            INTEGER NOT NULL DEFAULT 0,
  queued_at            TEXT,
  queue_expires_at     TEXT,
  -- Why a request ended up cancelled. The queue writes a human-readable reason
  -- here so the admin queue and the customer's card can explain the cancellation
  -- instead of showing a bare 'cancelled'.
  cancel_reason        TEXT,
  -- When the customer last refreshed THEIR OWN position for this job. NULL until
  -- they do. The request's own latitude/longitude are the point captured when the
  -- request was placed, so this timestamp is what tells the tracking endpoints
  -- whether that point is still worth drawing as "where the customer is".
  customer_latitude_updated_at TEXT
);

-- Serves the two hot lookups of the cash-confirmation flow, which would
-- otherwise full-scan requests: the admin dispute queue filters on amount_status
-- and orders by technician_reported_at, and the auto-resolve sweep filters on
-- amount_status='pending' with a technician report outstanding.
CREATE INDEX IF NOT EXISTS idx_requests_amount_status ON requests(amount_status, technician_reported_at);

CREATE INDEX IF NOT EXISTS idx_requests_status       ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_customer     ON requests(customer_id);
CREATE INDEX IF NOT EXISTS idx_requests_technician   ON requests(technician_id);
CREATE INDEX IF NOT EXISTS idx_requests_specialty    ON requests(specialty);
CREATE INDEX IF NOT EXISTS idx_requests_payment_status ON requests(payment_status);

-- Service tariff. The DB is the single source of truth for pricing; the admin
-- UI edits these rows and services/pricing.js reads them.
-- is_active = 0 keeps history intact while hiding a service from new requests.
CREATE TABLE IF NOT EXISTS service_pricing (
  service_key TEXT PRIMARY KEY,
  price       REAL NOT NULL DEFAULT 0,
  is_active   INTEGER DEFAULT 1,
  updated_at  TEXT DEFAULT (datetime('now'))
);

-- Technician wallet. TWO separate non-negative pots, deliberately not one signed
-- number:
--   balance      — credit the technician can withdraw (InstaPay earnings not yet
--                  transferred). Never negative.
--   total_debt   — what the technician currently owes the platform (cash
--                  commission collected but not handed over). Never negative, and
--                  the debt limit is checked against THIS, not against balance.
-- They used to be one `balance` that went negative, which made "has money" and
-- "owes money" indistinguishable in the API and in every screen that read it.
-- total_owed is a third, different figure: the LIFETIME total of commissions ever
-- charged. It only grows, and is not what a technician currently owes.
CREATE TABLE IF NOT EXISTS technician_wallet (
  technician_id          INTEGER PRIMARY KEY,
  balance                REAL NOT NULL DEFAULT 0,
  total_debt             REAL NOT NULL DEFAULT 0,
  total_earned           REAL NOT NULL DEFAULT 0,
  total_owed             REAL NOT NULL DEFAULT 0,
  total_paid_to_platform REAL NOT NULL DEFAULT 0,
  total_withdrawn        REAL NOT NULL DEFAULT 0,
  last_updated           TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE CASCADE
);

-- Withdrawal requests. The balance is only debited when an admin marks the request
-- 'paid' (the money has actually left), not when it is requested or approved, so a
-- technician can keep earning while a transfer is in flight.
CREATE TABLE IF NOT EXISTS withdrawal_requests (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  technician_id INTEGER NOT NULL REFERENCES technicians(id) ON DELETE CASCADE,
  amount        REAL NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'paid', 'rejected')),
  requested_at  TEXT DEFAULT (datetime('now')),
  processed_at  TEXT,
  processed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_withdrawal_status ON withdrawal_requests(status);
CREATE INDEX IF NOT EXISTS idx_withdrawal_tech   ON withdrawal_requests(technician_id);

-- Append-only wallet ledger. type:
--   'payment'    — earnings credited to balance (InstaPay)
--   'debt'       — commission charged to total_debt (cash)
--   'payout'     — technician paid debt back, total_debt reduced
--   'withdrawal' — balance paid out to the technician
--   'adjustment' — manual correction
--
-- balance_after AND debt_after are both recorded because the two pots move
-- independently: a debt entry leaves the balance untouched, so a ledger with only
-- balance_after would show "-40" next to an unchanged balance and read as a bug.
CREATE TABLE IF NOT EXISTS wallet_transactions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  technician_id INTEGER NOT NULL,
  request_id    INTEGER,
type        TEXT NOT NULL,
  amount      REAL NOT NULL,
  balance_after REAL NOT NULL,
  debt_after    REAL NOT NULL DEFAULT 0,
  note        TEXT,
  created_at    TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE CASCADE,
  FOREIGN KEY (request_id)    REFERENCES requests(id)    ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_tech    ON wallet_transactions(technician_id);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_request ON wallet_transactions(request_id);

-- Uber-style broadcast: one row per (request, technician) the request was
-- offered to, so the first technician to accept wins it.
--   'sent'     — offer is live and waiting for an answer
--   'accepted' — this technician won the request (exactly one row per request)
--   'declined' — the technician said no
--   'expired'  — nobody answered in time, another technician won it, or the
--                request was closed underneath the offer
-- UNIQUE(request_id, technician_id) makes a repeated broadcast a no-op.
CREATE TABLE IF NOT EXISTS request_broadcasts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id    INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  technician_id INTEGER NOT NULL REFERENCES technicians(id) ON DELETE CASCADE,
  status        TEXT NOT NULL DEFAULT 'sent'
    CHECK (status IN ('sent', 'accepted', 'declined', 'expired')),
  sent_at       TEXT NOT NULL DEFAULT (datetime('now')),
  responded_at  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_unique_pair
  ON request_broadcasts(request_id, technician_id);
CREATE INDEX IF NOT EXISTS idx_broadcast_tech_status
  ON request_broadcasts(technician_id, status);
CREATE INDEX IF NOT EXISTS idx_broadcast_request
  ON request_broadcasts(request_id);
CREATE INDEX IF NOT EXISTS idx_technicians_avail     ON technicians(availability);
CREATE INDEX IF NOT EXISTS idx_technicians_specialty ON technicians(specialty);

-- Service specialties (DB-driven; seeded from config, editable via admin).
-- enabled=0 hides the specialty from public endpoints but keeps history intact.
CREATE TABLE IF NOT EXISTS specialties (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  key        TEXT NOT NULL UNIQUE,
  label      TEXT NOT NULL,
  icon       TEXT NOT NULL DEFAULT 'fa-solid fa-wrench',
  enabled    INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Key/value settings exposed to admin (and optionally public).
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL DEFAULT ''
);

-- Public-facing technician profile: biography, experience and profile image.
-- One row per technician; created lazily on the first profile update.
CREATE TABLE IF NOT EXISTS technician_profiles (
  technician_id      INTEGER PRIMARY KEY,
  bio                TEXT,
  years_experience   INTEGER DEFAULT 0,
  profile_image_path TEXT,
  updated_at         TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE CASCADE
);

-- Portfolio gallery: sample photos of previous work (up to 5 per upload).
CREATE TABLE IF NOT EXISTS technician_portfolio (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  technician_id INTEGER NOT NULL,
  image_path    TEXT NOT NULL,
  caption       TEXT,
  created_at    TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_portfolio_technician ON technician_portfolio(technician_id);

-- Customer reviews. One review per completed request (UNIQUE on request_id).
-- rating: 1..5. technician_reply holds the technician's public response.
-- hidden*: moderation — a hidden review is excluded from the public profile and
-- from the public rating averages, but is never deleted from the database.
CREATE TABLE IF NOT EXISTS reviews (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id       INTEGER NOT NULL UNIQUE,
  customer_id      INTEGER NOT NULL,
  technician_id    INTEGER NOT NULL,
  rating           INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  comment          TEXT,
  technician_reply TEXT,
  replied_at       TEXT,
  created_at       TEXT DEFAULT (datetime('now')),
  hidden           INTEGER DEFAULT 0,
  hidden_reason    TEXT,
  hidden_at        TEXT,
  hidden_by        INTEGER,
  FOREIGN KEY (request_id)    REFERENCES requests(id)    ON DELETE CASCADE,
  FOREIGN KEY (customer_id)   REFERENCES customers(id)   ON DELETE CASCADE,
  FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reviews_technician ON reviews(technician_id);

-- Legal compliance (Egyptian Personal Data Protection Law No. 151 of 2020).

-- Explicit consent records for each data subject.
CREATE TABLE IF NOT EXISTS consents (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL,
  consent_type  TEXT NOT NULL,        -- 'sensitive_data_processing', 'privacy_policy', 'terms_of_service'
  granted_at    TEXT DEFAULT (datetime('now')),
  ip_address    TEXT,
  user_agent    TEXT,
  policy_version TEXT NOT NULL,       -- e.g. 'v1.0'
  withdrawn_at  TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Data subject requests (access, correction, deletion).
CREATE TABLE IF NOT EXISTS data_subject_requests (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL,
  request_type  TEXT NOT NULL,        -- 'access', 'rectification', 'erasure', 'restriction', 'portability'
  status        TEXT DEFAULT 'pending', -- 'pending', 'completed', 'rejected'
  requested_at  TEXT DEFAULT (datetime('now')),
  completed_at  TEXT,
  admin_note    TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Breach notification logs (Law 151/2020 article 7).
CREATE TABLE IF NOT EXISTS breach_logs (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  detected_at          TEXT DEFAULT (datetime('now')),
  description          TEXT,
  affected_users_count INTEGER,
  notified_center_at   TEXT,
  notified_users_at    TEXT,
  severity             TEXT            -- 'low', 'medium', 'high', 'critical'
);

-- Web Push subscriptions (one row per browser/device).
-- endpoint is UNIQUE because a browser mints a fresh endpoint per subscription:
-- re-subscribing reuses the row, and web-push drops it again on 404/410.
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  endpoint     TEXT NOT NULL UNIQUE,
  p256dh       TEXT NOT NULL,
  auth         TEXT NOT NULL,
  user_agent   TEXT,
  created_at   TEXT DEFAULT (datetime('now')),
  last_used_at TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id);

-- Chat between the customer and the assigned technician of one request.
-- sender_role is denormalised from the sender at write time so the unread count
-- is a single indexed predicate instead of a join back to customers/technicians.
-- read_at is NULL until the OTHER side loads the thread, which is what the badge
-- counts. Rows cascade away with the request.
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  INTEGER NOT NULL,
  sender_id   INTEGER NOT NULL,
  sender_role TEXT NOT NULL CHECK (sender_role IN ('customer', 'technician')),
  body        TEXT NOT NULL,
  read_at     TEXT,
  created_at  TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE,
  FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Thread order, and the unread predicate (request + read_at IS NULL).
CREATE INDEX IF NOT EXISTS idx_messages_request ON messages(request_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(request_id, read_at);
-- The cascade on messages.sender_id has to scan without this index.
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);

-- Foreign-key indexes.
--
-- SQLite does NOT create these automatically (unlike MySQL/InnoDB), and every
-- one of them is on the path a cascade or a join takes: deleting a row in the
-- parent has to scan the child table to fire ON DELETE CASCADE. An audit found
-- ten of them missing, which turned a `DELETE FROM requests` into a full table
-- scan of `messages` and `reviews`.
CREATE INDEX IF NOT EXISTS idx_consents_user ON consents(user_id);
CREATE INDEX IF NOT EXISTS idx_dsr_user ON data_subject_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_tech_profiles_tech ON technician_profiles(technician_id);
CREATE INDEX IF NOT EXISTS idx_reviews_customer ON reviews(customer_id);
CREATE INDEX IF NOT EXISTS idx_tech_wallet_tech ON technician_wallet(technician_id);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_request ON wallet_transactions(request_id);
CREATE INDEX IF NOT EXISTS idx_broadcasts_tech ON request_broadcasts(technician_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_processed_by ON withdrawal_requests(processed_by);
CREATE INDEX IF NOT EXISTS idx_requests_payment_confirmed_by
  ON requests(payment_confirmed_by);
-- Backs the per-account login lockout read.
CREATE INDEX IF NOT EXISTS idx_users_login_lock ON users(login_locked_until);
