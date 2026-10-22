/**
 * @file   Authentication API
 * @module auth.routes
 *
 * Register, login, change-password and profile endpoints. Signs and verifies HS256 tokens and
 * applies the strict per-route rate limiters.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Auth routes: customer registration + login for all roles.
const express = require('express');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const db = require('../db');
const config = require('../config');
const { wrap, AppError, badRequest, unauthorized, forbidden } = require('../utils/errors');
const { authenticate, requireRole } = require('../middleware/auth');
const rateLimit = require('../middleware/rate-limit');
const { verifyGoogleIdToken } = require('../services/google-identity');
const telegram = require('../services/telegram');
const logger = require('../utils/logger');

// Strict limiter for the credential endpoints: brute-forcing a password or
// spamming registrations is the only thing an attacker wants from these.
// A dedicated bucket per route so hitting /login can't lock a user out of
// /change-password. Only FAILED attempts are counted, so a legitimate user
// who mistypes twice is not penalised.
//
// `keyBy` optionally switches the bucket from the caller's IP to the
// authenticated user. It is used where the endpoint is already behind
// `authenticate` and the attack being stopped is one IP spraying many guesses
// at ONE account (see changePasswordLimiter below).
const authLimiter = (name, max, keyBy) =>
  rateLimit({
    windowMs: 15 * 60 * 1000,
    max: max || 10,
    name,
    keyBy: keyBy || undefined,
    standardHeaders: true,
    legacyHeaders: false,
    skipSuccessful: true,
    message: { error: 'محاولات كثيرة. حاول بعد 15 دقيقة.' },
  });

const loginLimiter = authLimiter('auth-login', 10);
const registerLimiter = authLimiter('auth-register', 10);

// Google sign-in is a credential exchange like any other, and it is unauthenticated,
// so it gets its own bucket rather than sharing one with the password flow.
// Unlike the password buckets it does NOT skip successful requests: replaying a
// captured ID token (which stays valid for an hour) is a real attempt to mint a
// session, and skipping successes would let it be free. 20 per 15 minutes per IP
// is far above what one person needs to sign in, and the endpoint is not on any
// page a user reloads.
const googleLimiter = authLimiter('auth-google', 20);

// 5 per 15 minutes, keyed on the USER rather than the IP.
//
// This endpoint is already behind `authenticate`, so the token identifies the
// account being attacked. A per-IP bucket is the wrong shape here: the attacker
// already holds a valid session, so spreading attempts over several addresses
// defeats an IP limit entirely, while a shared-office or carrier-CGNAT user
// would be punished for other people's failures. 5 is tighter than login's 10
// because a session holder guessing a current password is a far more focused
// attack than an anonymous one.
const changePasswordLimiter = authLimiter('auth-change-password', 5, rateLimit.userKey);

// Per-ACCOUNT lockout, complementing the per-IP limiter above.
//
// The IP bucket is necessary but not sufficient: it is keyed on the caller's
// address, so a botnet gets a fresh budget of MAX_ACCOUNT_FAILURES per address
// against ONE victim account, indefinitely. This window is keyed on the account
// itself, which is what actually makes a distributed guessing run converge.
//
// Every outcome here answers with the SAME generic authError, so the lockout
// reveals nothing about whether the account exists — an attacker cannot tell a
// locked known account from a rejected guess, and a real user locked out by a
// spray still sees only "wrong credentials".
const MAX_ACCOUNT_FAILURES = 5;
const ACCOUNT_LOCK_MINUTES = 15;

/**
 * Records a failed login against an account and applies the lock if the
 * threshold is reached.
 *
 * @param {number} userId
 * @returns {number} the failure count now recorded
 */
function noteFailedLogin(userId) {
  const row = db.prepare('SELECT failed_login_attempts FROM users WHERE id = ?').get(userId);
  const attempts = (row && row.failed_login_attempts ? row.failed_login_attempts : 0) + 1;

  const lockedUntil =
    attempts >= MAX_ACCOUNT_FAILURES
      ? new Date(Date.now() + ACCOUNT_LOCK_MINUTES * 60 * 1000)
          .toISOString()
          .replace('T', ' ')
          .slice(0, 19)
      : null;

  // The counter resets when the lock is applied, so the account gets a full
  // fresh budget once the window passes instead of being re-locked by the very
  // next mistake.
  db.prepare('UPDATE users SET failed_login_attempts = ?, login_locked_until = ? WHERE id = ?').run(
    attempts >= MAX_ACCOUNT_FAILURES ? 0 : attempts,
    lockedUntil,
    userId
  );

  return attempts;
}

/**
 * Clears the failure counter after a successful authentication.
 *
 * @param {number} userId
 * @returns {void}
 */
function clearFailedLogins(userId) {
  db.prepare(
    'UPDATE users SET failed_login_attempts = 0, login_locked_until = NULL WHERE id = ?'
  ).run(userId);
}

/**
 * Whether the account is inside its lock window.
 *
 * @param {{login_locked_until?: string|null}} user
 * @returns {boolean}
 */
function isAccountLocked(user) {
  if (!user || !user.login_locked_until) return false;
  const until = new Date(String(user.login_locked_until).replace(' ', 'T') + 'Z');
  // An unparseable value is treated as NOT locked: failing open here would lock
  // everyone out, failing closed would be a self-inflicted outage.
  return !Number.isNaN(until.getTime()) && until.getTime() > Date.now();
}
const { technicianDocsUpload, magicBytesGuard, UPLOAD_ROOT } = require('../middleware/upload');
const {
  isValidEmail,
  isValidPhone,
  isValidName,
  isNonEmptyString,
  isStrongPassword,
  passwordPolicyError,
  parseLatLng,
  normalizeEmail,
  normalizePhone,
  normalizeEgyptianMobile,
} = require('../utils/validation');

const router = express.Router();

// Rejects a phone that is already owned by another account. The UNIQUE index on
// users.phone is the real guarantee — this check exists purely to turn a would-be
// IntegrityError into a clean, field-level 400 instead of a 500. Both the write
// and the read below go through normalizePhone, so "01025330520" and
// "+201025330520" are recognised as the same identity.
function assertPhoneAvailable(normPhone) {
  const taken = db.prepare('SELECT id FROM users WHERE phone = ?').get(normPhone);
  if (taken) throw badRequest('هذا الرقم مسجّل بالفعل. جرّب تسجيل الدخول أو استخدم رقمًا آخر.');
}

// Identity documents may be a photo or a scan; the type is confirmed from the
// file's magic bytes rather than the client-supplied Content-Type.
const technicianDocsGuard = magicBytesGuard({
  allowedMimes: ['image/jpeg', 'image/png', 'image/webp', 'application/pdf'],
});

// Resolve area_key (if provided) to its Arabic label; falls back to free text.
function resolveLocation(areaKey, locationText) {
  if (areaKey !== undefined && areaKey !== '') {
    const area = config.ALEXANDRIA_AREAS.find((a) => a.key === areaKey);
    if (!area) throw badRequest('المنطقة غير صالحة');
    // Keep the customer's free-text address alongside the area: the area is the
    // neighbourhood, the text is the street/building/floor, and dropping the
    // text meant a technician saw only "سموحة". Same "area — detail" shape the
    // request route builds, so both paths store comparable location strings.
    const detail = (locationText || '').trim();
    return detail ? area.label + ' — ' + detail : area.label;
  }
  return (locationText || '').trim();
}

function signToken(user) {
  return jwt.sign(
    {
      userId: user.id,
      role: user.role,
      is_super_admin: Boolean(user.is_super_admin),
    },
    config.JWT_SECRET,
    {
      algorithm: 'HS256',
      expiresIn: config.JWT_EXPIRES_IN,
    }
  );
}

/**
 * Sends the "new sign-in" notice to a linked Telegram chat.
 *
 * Deliberately fire-and-forget: Telegram being slow, unreachable, or having
 * revoked the bot must never fail or delay a login that has already been
 * authenticated. The promise is returned only so tests can await it; every
 * caller ignores it.
 *
 * @param {{id:number, telegram_chat_id?:string|null}} user
 * @returns {Promise<void>|undefined}
 */
function notifyTelegramLogin(user) {
  if (!user || !user.telegram_chat_id) return undefined;

  let when;
  try {
    when = new Date().toLocaleString('ar-EG', {
      timeZone: 'Africa/Cairo',
      dateStyle: 'medium',
      timeStyle: 'short',
    });
  } catch (err) {
    when = new Date().toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
  }

  const text = '🔔 تم تسجيل الدخول إلى حسابك في دشاوي بنجاح.\nالوقت: ' + when;

  return telegram.sendMessage(user.telegram_chat_id, text).catch((err) => {
    // Login succeeded; a notification failure is logged, never surfaced.
    logger.warn('auth', '[auth] Telegram login notification failed:', err.message);
  });
}

// Includes the role-specific profile object (customer / technician record).
function profileFor(user) {
  if (user.role === config.LEVELS.customer) {
    return db.prepare('SELECT * FROM customers WHERE user_id = ?').get(user.id);
  }
  if (user.role === config.LEVELS.technician) {
    return db.prepare('SELECT * FROM technicians WHERE user_id = ?').get(user.id);
  }
  return null;
}

// The user object handed to the browser. Shared by every route that issues or
// refreshes a session, so a field added here (profile_complete) cannot appear
// on one path and be missing on another.
function safeUserFor(user, profileComplete) {
  return {
    id: user.id,
    email: user.email,
    phone: user.phone || null,
    role: user.role,
    is_super_admin: Boolean(user.is_super_admin),
    // Unparseable JSON degrades to "no permissions" rather than throwing: a
    // corrupt value must deny access, never grant it and never 500 a login.
    permissions: (() => {
      try {
        return JSON.parse(user.permissions || '[]');
      } catch (e) {
        return [];
      }
    })(),
    profile_complete:
      profileComplete !== undefined
        ? Boolean(profileComplete)
        : user.role !== config.LEVELS.customer || Boolean(user.phone),
  };
}

// Refuses an account that must not obtain a session at all, for the same
// reasons /login does: a blocked user row, a customer blocked on the profile,
// and (for completeness, since only the Google path can reach a technician row
// with a valid password) the technician approval states.
//
// Shared by the password and Google paths so a blocked account cannot get in by
// choosing the other one.
function assertAccountCanSignIn(user) {
  if (user.blocked) throw unauthorized('تم إيقاف حسابك. تواصل مع الإدارة.');

  if (user.role === config.LEVELS.technician) {
    const t = db
      .prepare(
        'SELECT approval_status, approval_note, availability FROM technicians WHERE user_id = ?'
      )
      .get(user.id);

    if (t && t.approval_status === 'pending') {
      throw unauthorized('حسابك قيد المراجعة من قبل الإدارة. سيتم إشعارك عند الموافقة.');
    }
    if (t && t.approval_status === 'rejected') {
      const note = t.approval_note ? ' — السبب: ' + t.approval_note : '';
      throw unauthorized('تم رفض طلب انضمامك.' + note);
    }
    if (t && t.availability === 'offline') throw unauthorized('هذا الحساب غير نشط');
  }
  if (user.role === config.LEVELS.customer) {
    const c = db.prepare('SELECT blocked FROM customers WHERE user_id = ?').get(user.id);
    if (c && c.blocked) {
      throw unauthorized('حسابك موقوف من قبل الإدارة. يرجى التواصل مع الدعم.');
    }
  }
}

// POST /api/auth/register  (customer self-registration)
router.post(
  '/register',
  registerLimiter,
  wrap(async (req, res) => {
    const { name, phone, email, password, area_key, location_text, latitude, longitude } =
      req.body || {};

    if (!isValidName(name)) throw badRequest('الاسم غير صالح (حرفان على الأقل)');
    if (!isValidPhone(phone)) throw badRequest('رقم الهاتف غير صالح');
    if (!isValidEmail(email)) throw badRequest('البريد الإلكتروني غير صالح');
    const pwProblem = passwordPolicyError(password);
    if (pwProblem) throw badRequest(pwProblem);
    if (location_text !== undefined && !isNonEmptyString(location_text, 300)) {
      throw badRequest('وصف الموقع غير صالح');
    }
    const resolvedLocation = resolveLocation(area_key, location_text);
    const coords = parseLatLng(latitude, longitude);
    if (latitude !== undefined && latitude !== '' && !coords) {
      throw badRequest('إحداثيات الموقع غير صالحة');
    }

    const normEmail = normalizeEmail(email);
    const existing = db.prepare('SELECT id FROM users WHERE email = ?').get(normEmail);
    if (existing) throw badRequest('هذا البريد الإلكتروني مسجّل بالفعل');

    const normPhone = normalizePhone(phone);
    if (!normPhone || normPhone.replace(/\D/g, '').length < 8) {
      throw badRequest('رقم الهاتف غير صالح');
    }
    assertPhoneAvailable(normPhone);

    const passwordHash = bcrypt.hashSync(password, 12);

    const run = db.transaction(() => {
      const userRes = db
        .prepare(
          "INSERT INTO users (email, phone, password_hash, role) VALUES (?, ?, ?, 'customer')"
        )
        .run(normEmail, normPhone, passwordHash);
      db.prepare(
        `INSERT INTO customers (user_id, name, phone, location_text, latitude, longitude)
         VALUES (?, ?, ?, ?, ?, ?)`
      ).run(
        userRes.lastInsertRowid,
        name.trim(),
        // Store the same canonical form as users.phone so the account, the
        // customer profile and phone login can never disagree about the number.
        normPhone,
        resolvedLocation,
        coords ? coords.latitude : null,
        coords ? coords.longitude : null
      );
      return userRes.lastInsertRowid;
    });

    const userId = run();
    const user = {
      id: userId,
      role: 'customer',
      email: normEmail,
      phone: normPhone,
    };
    res.status(201).json({
      token: signToken(user),
      // safeUserFor, not an ad-hoc object, so the browser receives exactly the
      // same user shape from registration as it does from login and from Google.
      user: safeUserFor(user),
      profile: profileFor(user),
    });
  })
);

// POST /api/auth/register-technician  (technician self-registration)
// Multipart: fields + two mandatory documents (criminal record, national ID).
// The account is created in 'pending' state — no JWT, cannot log in until approved.
router.post(
  '/register-technician',
  registerLimiter,
  technicianDocsUpload,
  // Normalise multer errors into readable 400s before hitting the handler.
  (err, req, res, next) => {
    if (err)
      return next(err.status ? err : new AppError(err.message || 'حدث خطأ أثناء رفع الملفات', 400));
    return next();
  },
  // Content-based check of the saved documents. Runs after multer (which has
  // already written the files) and before the handler touches the database, so
  // a spoofed document never reaches the registration transaction. Placed after
  // the multer error normaliser to keep that error path adjacent to multer.
  technicianDocsGuard,
  wrap(async (req, res) => {
    const { name, phone, email, password, area_key, location_text, latitude, longitude } =
      req.body || {};

    // Documents are REQUIRED — validate before touching the DB.
    const crim = req.files && req.files.criminal_record && req.files.criminal_record[0];
    const natId = req.files && req.files.national_id && req.files.national_id[0];
    if (!crim) throw badRequest('يرجى رفع صورة الفيش الجنائي');
    if (!natId) throw badRequest('يرجى رفع صورة بطاقة الرقم القومي');

    // Explicit consent required to process the sensitive documents (Law 151/2020).
    const sensitiveConsent =
      req.body.sensitive_consent === 'true' ||
      req.body.sensitive_consent === '1' ||
      req.body.sensitive_consent === true;
    if (!sensitiveConsent)
      throw badRequest(
        'يجب الموافقة على معالجة البيانات الحساسة (صورة البطاقة والفيش) وفقاً لقانون 151 لسنة 2020'
      );
    const policyVersion = req.body.policy_version || 'v1.0';

    if (!isValidName(name)) throw badRequest('الاسم غير صالح (حرفان على الأقل)');
    if (!isValidPhone(phone)) throw badRequest('رقم الهاتف غير صالح');
    if (!isValidEmail(email)) throw badRequest('البريد الإلكتروني غير صالح');
    const pwProblem = passwordPolicyError(password);
    if (pwProblem) throw badRequest(pwProblem);
    if (location_text !== undefined && !isNonEmptyString(location_text, 300)) {
      throw badRequest('وصف الموقع غير صالح');
    }
    const resolvedLocation = resolveLocation(area_key, location_text);
    const coords = parseLatLng(latitude, longitude);
    if (latitude !== undefined && latitude !== '' && !coords) {
      throw badRequest('إحداثيات الموقع غير صالحة');
    }

    const normEmail = normalizeEmail(email);
    const existing = db.prepare('SELECT id FROM users WHERE email = ?').get(normEmail);
    if (existing) throw badRequest('هذا البريد الإلكتروني مسجّل بالفعل');

    const normPhone = normalizePhone(phone);
    if (!normPhone || normPhone.replace(/\D/g, '').length < 8) {
      throw badRequest('رقم الهاتف غير صالح');
    }
    assertPhoneAvailable(normPhone);

    const passwordHash = bcrypt.hashSync(password, 12);

    const run = db.transaction(() => {
      const userRes = db
        .prepare(
          "INSERT INTO users (email, phone, password_hash, role) VALUES (?, ?, ?, 'technician')"
        )
        .run(normEmail, normPhone, passwordHash);
      const userId = userRes.lastInsertRowid;

      // Move staged documents into the per-technician folder.
      const userFolder = path.join(UPLOAD_ROOT, 'user-' + userId);
      fs.mkdirSync(userFolder, { recursive: true });

      const crimDst = path.join(userFolder, path.basename(crim.path));
      const natDst = path.join(userFolder, path.basename(natId.path));
      fs.renameSync(crim.path, crimDst);
      fs.renameSync(natId.path, natDst);

      const criminalRecordPath =
        '/uploads/technicians/user-' + userId + '/' + path.basename(crimDst);
      const nationalIdPath = '/uploads/technicians/user-' + userId + '/' + path.basename(natDst);

      db.prepare(
        `INSERT INTO technicians
           (user_id, name, phone, specialty, location_text, latitude, longitude,
            availability, criminal_record_path, national_id_path, approval_status)
         VALUES (?, ?, ?, 'dish_general', ?, ?, ?, 'offline', ?, ?, 'pending')`
      ).run(
        userId,
        name.trim(),
        // Canonical form, same as users.phone (see the customer route).
        normPhone,
        resolvedLocation,
        coords ? coords.latitude : null,
        coords ? coords.longitude : null,
        criminalRecordPath,
        nationalIdPath
      );

      // Record the explicit consent for sensitive-data processing (with IP + UA).
      db.prepare(
        `INSERT INTO consents (user_id, consent_type, ip_address, user_agent, policy_version)
         VALUES (?, 'sensitive_data_processing', ?, ?, ?)`
      ).run(
        userId,
        req.headers['x-forwarded-for'] || req.socket.remoteAddress || null,
        req.headers['user-agent'] || null,
        policyVersion
      );
      return userId;
    });

    const userId = run();
    const user = {
      id: userId,
      role: 'technician',
      email: normEmail,
      phone: normPhone,
    };
    res.status(201).json({
      message:
        'تم إنشاء حسابك بنجاح. سيتم مراجعة مستنداتك من قبل الإدارة وسيتم إشعارك عند الموافقة.',
      pending: true,
      user,
      profile: profileFor(user),
    });
  })
);

// POST /api/auth/login  (customers, technicians, admins)
// `identifier` (or the legacy `email` field) may be an email address OR a phone
// number. The field is kept under both names so older clients that still post
// { email } keep working unchanged. Admins have no phone, so they log in by email
// only — that falls out of the lookup rather than needing a role check.
router.post(
  '/login',
  loginLimiter,
  wrap(async (req, res) => {
    const { identifier, email, password } = req.body || {};
    const loginId = String(identifier || email || '').trim();

    const authError = 'البريد الإلكتروني/الهاتف أو كلمة المرور غير صحيحة';
    if (!loginId) throw unauthorized(authError);
    if (typeof password !== 'string' || password.length === 0) {
      throw unauthorized(authError);
    }

    // '@' is the discriminator between the two identity namespaces: it is
    // required by the email grammar and cannot appear in a phone number.
    const user = loginId.includes('@')
      ? db.prepare('SELECT * FROM users WHERE email = ?').get(normalizeEmail(loginId))
      : db.prepare('SELECT * FROM users WHERE phone = ?').get(normalizePhone(loginId));

    // Same message for "no such account" and "wrong password" so the endpoint
    // cannot be used to enumerate registered numbers or addresses.
    if (!user || !bcrypt.compareSync(password, user.password_hash)) {
      // Only counted when the account actually exists. Counting a miss for an
      // unknown identifier would let an attacker lock an arbitrary account just
      // by guessing its address — a denial-of-service against real users.
      if (user) noteFailedLogin(user.id);
      throw unauthorized(authError);
    }

    // A locked account is refused AFTER a successful password check, and with
    // the same generic message. Refusing earlier (before the bcrypt compare)
    // would leak which addresses exist, because the work done differs.
    if (isAccountLocked(user)) {
      throw unauthorized(authError);
    }

    clearFailedLogins(user.id);

    // Disabled accounts (sub-admins toggled off by the super admin) must not
    // be able to obtain a session at all, not just fail on later requests, and
    // the technician approval states are checked here too — all of it is the
    // shared gate the Google path uses, so neither route can be the way around it.
    assertAccountCanSignIn(user);

    const safe = safeUserFor(user);

    // Fire-and-forget: a Telegram outage must not turn a valid login into an
    // error or hold the response open.
    notifyTelegramLogin(user);

    res.json({
      token: signToken(user),
      user: safe,
      profile: profileFor(safe),
      must_change_password: Boolean(user.must_change_password),
    });
  })
);

// ---------------------------------------------------------------------------
// Passwordless sign-in (Telegram OTP)
// ---------------------------------------------------------------------------
//
// A user who cannot remember their password enters their address, receives a
// six-digit code in the Telegram chat linked to the account, and trades it for
// a session. No password is reset and no code is ever emailed.
//
// Security properties, same shape as the email flow this replaces:
//
//   * Only a SHA-256 digest of the code is stored. A database dump inside the
//     ten-minute window is not a set of working codes.
//   * An unknown address gets the ordinary success answer, so the endpoint
//     cannot be used to enumerate accounts.
//   * A registered address with no Telegram link gets a distinct 409 tagged
//     `telegram_not_linked`. That is deliberate: the browser must be able to
//     offer the linking page, and the address is one the caller just typed.
//   * Codes are single-use and retired when a newer one is issued.
//   * verify-otp passes through assertAccountCanSignIn, the same gate /login
//     applies, so a blocked or unapproved account cannot get in this way.

const OTP_TTL_MINUTES = 10;
const OTP_REQUEST_COOLDOWN_SECONDS = 60;

const OTP_SENT_MESSAGE = 'تم إرسال الكود إلى تليجرام الخاص بك.';
const OTP_RATE_LIMIT_MESSAGE = 'من فضلك استنى دقيقة قبل ما تطلب كود جديد';
const TELEGRAM_NOT_LINKED_MESSAGE =
  'حسابك غير مرتبط بتليجرام. اربط حسابك أولاً عشان توصلك أكواد الدخول.';

/**
 * Buckets OTP requests by the address rather than the IP, because the attack
 * this limits is one host grinding codes for one account.
 *
 * @param {import('express').Request} req
 * @returns {string}
 */
function otpEmailKey(req) {
  const address = normalizeEmail((req.body || {}).email || '');
  if (!address) return 'ip:' + (req.ip || (req.socket && req.socket.remoteAddress) || 'unknown');
  return 'email:' + address;
}

// Three per minute, not one: a user who arrives here before linking Telegram
// must be able to come straight back from /telegram-link.html and request the
// code they came for. The durable cooldown below is still one code a minute;
// this bucket exists to bound the unauthenticated endpoint.
const otpRequestLimiter = rateLimit({
  windowMs: OTP_REQUEST_COOLDOWN_SECONDS * 1000,
  max: 3,
  name: 'auth-request-otp',
  keyBy: otpEmailKey,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: OTP_RATE_LIMIT_MESSAGE },
});

const otpVerifyLimiter = authLimiter('auth-verify-otp', 10, otpEmailKey);
const otpVerifyIpLimiter = authLimiter('auth-verify-otp-ip', 10);

/**
 * @param {string} otp
 * @returns {string} SHA-256 hex digest stored in otps.otp_hash
 */
function hashOtp(otp) {
  return crypto.createHash('sha256').update(String(otp)).digest('hex');
}

/**
 * Constant-time comparison of a submitted code against a stored digest.
 *
 * @param {string} storedHash
 * @param {string} otp
 * @returns {boolean}
 */
function otpMatches(storedHash, otp) {
  const submitted = Buffer.from(hashOtp(otp), 'hex');
  const stored = Buffer.from(String(storedHash), 'hex');
  if (stored.length !== submitted.length) return false;
  return crypto.timingSafeEqual(submitted, stored);
}

/**
 * @returns {AppError} the shared "link Telegram first" failure
 */
function telegramNotLinked() {
  return new AppError(TELEGRAM_NOT_LINKED_MESSAGE, 409, 'telegram_not_linked');
}

// POST /api/auth/request-otp  body: { email }
//
// PUBLIC. The code is delivered through the Telegram bot, so the account must
// already be linked; that is the one case where this endpoint says something
// account-specific.
router.post(
  '/request-otp',
  otpRequestLimiter,
  wrap(async (req, res) => {
    const address = normalizeEmail((req.body || {}).email || '');
    if (!isValidEmail(address)) throw badRequest('البريد الإلكتروني غير صالح');

    const user = db
      .prepare('SELECT id, email, telegram_chat_id FROM users WHERE email = ?')
      .get(address);

    // Unknown address: identical answer to a registered one, and nothing is
    // written or sent. Everything below only happens for a real account.
    if (!user) {
      res.json({ success: true, message: OTP_SENT_MESSAGE });
      return;
    }

    if (!user.telegram_chat_id) throw telegramNotLinked();

    // Durable half of the cooldown: survives a restart and holds across
    // instances, where each rate-limit bucket is in its own memory.
    const recent = db
      .prepare("SELECT 1 AS hit FROM otps WHERE email = ? AND created_at > datetime('now', ?)")
      .get(address, '-' + OTP_REQUEST_COOLDOWN_SECONDS + ' seconds');
    if (recent) throw new AppError(OTP_RATE_LIMIT_MESSAGE, 429, 'otp_rate_limited');

    const otp = String(crypto.randomInt(100000, 1000000));
    const otpHash = hashOtp(otp);

    // scrypt is overkill for a six-digit code; the digest plus the strict
    // verify limiter is the design. Purge is bounded so this table cannot grow
    // forever from an endpoint anonymous callers can reach.
    db.transaction(() => {
      db.prepare('UPDATE otps SET used = 1 WHERE email = ? AND used = 0').run(address);
      db.prepare(
        "INSERT INTO otps (email, otp_hash, expires_at) VALUES (?, ?, datetime('now', ?))"
      ).run(address, otpHash, '+' + OTP_TTL_MINUTES + ' minutes');
      db.prepare("DELETE FROM otps WHERE expires_at < datetime('now', '-1 day')").run();
    })();

    try {
      await telegram.sendOtp(user.telegram_chat_id, otp);
    } catch (err) {
      // The code never reached a chat, so it must not stay valid.
      db.prepare('DELETE FROM otps WHERE email = ? AND otp_hash = ?').run(address, otpHash);

      if (err.code === 'telegram_blocked') {
        // The stored chat id is unusable (blocked bot / deleted chat). Clear it
        // so the next request-otp answers with the not-linked code and the
        // browser offers the linking page instead of failing forever.
        db.prepare('UPDATE users SET telegram_chat_id = NULL WHERE id = ?').run(user.id);
        throw telegramNotLinked();
      }

      throw new AppError('تعذر إرسال الكود حالياً، حاول مرة أخرى بعد قليل', 503);
    }

    res.json({ success: true, message: OTP_SENT_MESSAGE });
  })
);

// POST /api/auth/verify-otp  body: { email, otp }
//
// PUBLIC, for the same reason as its counterpart above.
router.post(
  '/verify-otp',
  otpVerifyLimiter,
  otpVerifyIpLimiter,
  wrap(async (req, res) => {
    const address = normalizeEmail((req.body || {}).email || '');
    const code = String((req.body || {}).otp || '').trim();
    if (!isValidEmail(address) || !/^\d{6}$/.test(code)) throw badRequest('الكود غير صحيح');

    const record = db
      .prepare(
        `SELECT id, otp_hash FROM otps
          WHERE email = ? AND used = 0 AND expires_at > datetime('now')
          ORDER BY id DESC LIMIT 1`
      )
      .get(address);

    if (!record || !otpMatches(record.otp_hash, code)) throw badRequest('الكود غير صحيح');

    // Conditional UPDATE: two requests racing with one code mint one session.
    const spent = db.prepare('UPDATE otps SET used = 1 WHERE id = ? AND used = 0').run(record.id);
    if (spent.changes !== 1) throw badRequest('الكود غير صحيح');

    const user = db.prepare('SELECT * FROM users WHERE email = ?').get(address);
    if (!user) throw new AppError('الحساب غير موجود', 404);

    assertAccountCanSignIn(user);
    clearFailedLogins(user.id);

    notifyTelegramLogin(user);

    res.json({
      token: signToken(user),
      user: safeUserFor(user),
      profile: profileFor(user),
      must_change_password: Boolean(user.must_change_password),
    });
  })
);

// ---------------------------------------------------------------------------
// Telegram account linking
// ---------------------------------------------------------------------------

// 15 minutes is long enough to open Telegram and press Start, short enough that
// a token left in browser history is not a lasting key to the account.
const TELEGRAM_LINK_TTL_MINUTES = 15;

// GET /api/auth/telegram-link
// Reports whether the signed-in account is linked, so the page can show the
// right state without a second endpoint.
router.get(
  '/telegram-link',
  authenticate,
  wrap(async (req, res) => {
    const user = db.prepare('SELECT telegram_chat_id FROM users WHERE id = ?').get(req.user.id);
    res.json({
      linked: Boolean(user && user.telegram_chat_id),
      bot_username: telegram.getBotUsername(),
    });
  })
);

// POST /api/auth/telegram-link  body: { email }
//
// PUBLIC, because the page that needs it is reached from the forgot-password
// flow, where the visitor has no session yet. The address identifies the
// account; no req.user is consulted.
//
// SECURITY NOTE: this is deliberately unauthenticated, which means anyone who
// knows an email can attach a Telegram chat to that account and then receive
// its login codes. The flow is bounded — the token expires in 15 minutes, is
// single-use, and an account that is already linked is refused — but it is
// still weaker than requiring proof of ownership. Revisit this if the product
// ever needs stronger account-recovery guarantees.
const telegramLinkLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 10,
  name: 'auth-telegram-link',
  keyBy: otpEmailKey,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'محاولات كثيرة. حاول بعد 15 دقيقة.' },
});

router.post(
  '/telegram-link',
  telegramLinkLimiter,
  wrap(async (req, res) => {
    const address = normalizeEmail((req.body || {}).email || '');
    if (!isValidEmail(address)) throw badRequest('البريد الإلكتروني غير صالح');

    const user = db.prepare('SELECT id, telegram_chat_id FROM users WHERE email = ?').get(address);

    if (!user) {
      throw new AppError('لا يوجد حساب مسجل بهذا البريد الإلكتروني', 404, 'email_not_found');
    }

    // Do not let an anonymous caller steal an existing link. Changing the chat
    // for an already-linked account needs the authenticated route on an account
    // page, not this recovery path.
    if (user.telegram_chat_id) {
      throw new AppError('هذا الحساب مرتبط بتليجرام بالفعل.', 409, 'telegram_already_linked');
    }

    const token = crypto.randomBytes(24).toString('hex');

    db.transaction(() => {
      // One live token per account; a fresh one makes the previous link dead.
      db.prepare('DELETE FROM telegram_link_tokens WHERE user_id = ?').run(user.id);
      db.prepare("DELETE FROM telegram_link_tokens WHERE expires_at <= datetime('now')").run();
      db.prepare(
        "INSERT INTO telegram_link_tokens (token, user_id, expires_at) VALUES (?, ?, datetime('now', ?))"
      ).run(token, user.id, '+' + TELEGRAM_LINK_TTL_MINUTES + ' minutes');
    })();

    let botUsername = telegram.getBotUsername();
    if (!botUsername) {
      // The bot may not have completed getMe yet. Asking here means the page
      // still gets a usable deep link on the first request after a cold boot;
      // a failure is not fatal, the page can show the token instead.
      try {
        botUsername = await telegram.fetchBotUsername();
      } catch (err) {
        botUsername = null;
      }
    }

    res.json({
      token,
      email: address,
      expires_in_minutes: TELEGRAM_LINK_TTL_MINUTES,
      bot_username: botUsername,
      deep_link: botUsername ? `https://t.me/${botUsername}?start=${token}` : null,
    });
  })
);

// GET /api/auth/me
// Returns the current user's fresh data (including permissions, flags).
// The frontend caches the user in localStorage, so every admin page calls this
// on load to pick up permission/flag changes made by the super admin.
router.get(
  '/me',
  authenticate,
  wrap(async (req, res) => {
    const user = db
      .prepare(
        'SELECT id, email, phone, role, is_super_admin, must_change_password, permissions, telegram_chat_id FROM users WHERE id = ?'
      )
      .get(req.user.id);

    if (!user) throw unauthorized('الحساب غير موجود');

    const safe = {
      id: user.id,
      email: user.email,
      phone: user.phone || null,
      role: user.role,
      is_super_admin: Boolean(user.is_super_admin),
      must_change_password: Boolean(user.must_change_password),
      // Re-derived here as well, so a page that reloads mid-onboarding gets
      // the same answer the guard would give rather than acting on a stale
      // localStorage value and bouncing the customer off a page they have
      // legitimately finished.
      profile_complete: user.role !== config.LEVELS.customer || Boolean(user.phone),
      // Lets the account page show "linked" without exposing the chat id
      // itself: it is an identifier for a private conversation, not a public
      // profile field.
      telegram_linked: Boolean(user.telegram_chat_id),
      permissions: (() => {
        try {
          return JSON.parse(user.permissions || '[]');
        } catch (e) {
          return [];
        }
      })(),
    };

    res.json({ user: safe, profile: profileFor(safe) });
  })
);

// ---------------------------------------------------------------------------
// Google sign-in (customers only)
// ---------------------------------------------------------------------------

// GET /api/auth/google/config
// Tells the page whether the feature is switched on and, if so, which client ID
// the button must be rendered with. Nothing secret is returned: the client ID
// is published in the Google Cloud console and is already in the page source.
router.get(
  '/google/config',
  wrap(async (req, res) => {
    res.json({
      enabled: config.GOOGLE_ENABLED,
      client_id: config.GOOGLE_ENABLED ? config.GOOGLE_CLIENT_ID : null,
    });
  })
);

// POST /api/auth/google  body: { id_token }
//
// The `id_token` is treated as an untrusted string. Nothing about it is read
// until verifyGoogleIdToken() has checked Google's signature over it, and only
// what that function returns — never the decoded token itself — reaches the
// account lookup below.
//
// Three outcomes, in order:
//   1. the email already belongs to a technician or an admin  -> refused, never
//      merged. One person holding two roles is a support decision, not
//      something an OAuth callback may decide silently.
//   2. the email belongs to a customer -> signed in, and google_sub linked.
//   3. the email is unknown -> a customer account is created with NO phone, so
//      the response carries profile_complete:false and the API then refuses
//      every customer route until the number is claimed.
router.post(
  '/google',
  googleLimiter,
  wrap(async (req, res) => {
    const { id_token: idToken } = req.body || {};
    if (!isNonEmptyString(idToken, 4096)) {
      throw badRequest('بيانات تسجيل الدخول من جوجل غير صالحة');
    }

    // Throws a 401 AppError for anything that does not verify. No claim from
    // the request body is consulted at any point below.
    const google = await verifyGoogleIdToken(idToken);

    let user = db.prepare('SELECT * FROM users WHERE google_sub = ?').get(google.sub);

    if (user) {
      // Already linked, so this is a returning visitor. If the email Google
      // reports no longer matches the one on the account, the identity was
      // moved or the token is from a different Google account than the one
      // linked — either way, silently re-pointing an existing account at a new
      // email would hand it to whoever controls that email now.
      if (normalizeEmail(user.email) !== google.email) {
        throw forbidden(
          'حساب جوجل ده مرتبط بالفعل بحساب مسجّل بإيميل تاني. سجّل دخولك بالإيميل وكلمة المرور.'
        );
      }
    } else {
      const byEmail = db.prepare('SELECT * FROM users WHERE email = ?').get(google.email);

      if (byEmail) {
        // Requirement: never merge across roles. A technician or admin keeps
        // using the email/password flow; their account is left completely
        // untouched, including google_sub, so the two paths stay independent.
        if (byEmail.role !== config.LEVELS.customer) {
          throw forbidden(
            'هذا البريد الإلكتروني مسجّل بالفعل كحساب ' +
              (byEmail.role === config.LEVELS.technician ? 'فني' : 'مدير') +
              '. استخدم تسجيل الدخول بالإيميل وكلمة المرور.'
          );
        }
        if (byEmail.google_sub && byEmail.google_sub !== google.sub) {
          // Same customer email, different Google identity. Refused rather than
          // re-linked: taking the new link would let anyone who later controls
          // this Google account walk into an existing customer's history.
          throw forbidden(
            'حساب العميل ده مرتبط بحساب جوجل مختلف. سجّل دخولك بالإيميل وكلمة المرور.'
          );
        }
        db.prepare('UPDATE users SET google_sub = ? WHERE id = ?').run(google.sub, byEmail.id);
        user = db.prepare('SELECT * FROM users WHERE id = ?').get(byEmail.id);
      } else {
        const create = db.transaction(() => {
          const inserted = db
            .prepare(
              `INSERT INTO users (email, phone, password_hash, role, google_sub)
               VALUES (?, NULL, ?, ?, ?)`
            )
            .run(
              google.email,
              // A Google account has no password, but password_hash is NOT NULL
              // and the phone login path compares against it. This is 256 bits
              // of CSPRNG output, hashed like any other password and never
              // stored anywhere: it makes email/password login impossible for
              // this account rather than merely unlikely, and it can never be
              // guessed or brute-forced. must_change_password stays 0, because
              // there is no password for them to change.
              bcrypt.hashSync(crypto.randomBytes(32).toString('hex'), 12),
              config.LEVELS.customer,
              google.sub
            );
          const userId = inserted.lastInsertRowid;

          // The profile is created now so the name Google returned is on the
          // account from the first moment — the customer row carries no phone
          // until they claim one, which is exactly the state the gate expects.
          db.prepare('INSERT INTO customers (user_id, name, phone) VALUES (?, ?, NULL)').run(
            userId,
            // Google may send no name at all (some accounts are email-only), and
            // the column is NOT NULL. The address' local part is a placeholder
            // the customer overwrites on the completion form.
            google.name && google.name.length >= 2 ? google.name : google.email.split('@')[0]
          );
          return userId;
        });
        user = db.prepare('SELECT * FROM users WHERE id = ?').get(create());
      }
    }

    // The one thing this route must never allow, whatever the token says.
    if (user.role !== config.LEVELS.customer) {
      throw forbidden('تسجيل الدخول بحساب جوجل متاح للعملاء فقط');
    }

    assertAccountCanSignIn(user);
    clearFailedLogins(user.id);

    notifyTelegramLogin(user);

    // A returning customer who is complete is a plain sign-in. Anyone without a
    // phone — a brand-new Google account, or an existing customer whose phone
    // was never claimed — gets a token that is valid but carries no authority:
    // every /api/customer route answers 403 until /complete-profile is posted.
    const complete = Boolean(user.phone);
    res.json({
      token: signToken(user),
      user: safeUserFor(user, complete),
      // Returned even when the profile is not finished: the customer row exists
      // from the moment the account was created, and the completion form
      // prefills the name Google gave us from it.
      profile: profileFor(user),
      must_change_password: Boolean(user.must_change_password),
      profile_complete: complete,
    });
  })
);

// POST /api/auth/complete-profile  body: { phone, area_key, location_text? }
//
// The only way out of the onboarding gate. Customers only — the route is
// behind requireRole('customer') as well as `authenticate`, so neither a
// technician nor an admin can use it to edit a profile.
router.post(
  '/complete-profile',
  authenticate,
  requireRole(config.LEVELS.customer),
  wrap(async (req, res) => {
    const { name, phone, area_key, location_text } = req.body || {};

    // Format first, database second: a bad number never reaches a UNIQUE index
    // and never has to be turned back into a 400.
    const normPhone = normalizeEgyptianMobile(phone);
    if (!normPhone) {
      throw badRequest('رقم الموبايل غير صالح — اكتبه بالشكل 01xxxxxxxxx');
    }
    const area = config.ALEXANDRIA_AREAS.find((a) => a.key === area_key);
    if (!area) throw badRequest('اختر منطقتك');
    if (location_text !== undefined && !isNonEmptyString(location_text, 300)) {
      throw badRequest('وصف الموقع غير صالح');
    }
    // The name arrived from Google, but the form lets the customer correct it,
    // so it is validated the same way any other name in this file is. Omitting
    // it keeps whatever is already stored rather than blanking the column,
    // which is NOT NULL.
    const trimmedName = name === undefined ? null : String(name).trim();
    if (trimmedName !== null && !isValidName(trimmedName)) {
      throw badRequest('الاسم غير صالح (حرفان على الأقل)');
    }

    const current = db.prepare('SELECT * FROM users WHERE id = ?').get(req.user.id);
    if (!current) throw unauthorized('الحساب غير موجود');
    if (current.role !== config.LEVELS.customer) {
      throw forbidden('هذه الصفحة متاحة للعملاء فقط');
    }
    assertAccountCanSignIn(current);

    // The UNIQUE index on users.phone is the real guarantee; this check is here
    // to turn a would-be IntegrityError into a field-level message instead of a
    // 500. Both this read and the write go through normalizeEgyptianMobile, so
    // "01025330520" and "+201025330520" are recognised as the same number.
    const takenBy = db.prepare('SELECT id FROM users WHERE phone = ?').get(normPhone);
    if (takenBy && takenBy.id !== current.id) {
      throw badRequest('هذا الرقم مسجّل بالفعل. استخدم رقمًا آخر.');
    }

    // Re-writing the whole location string from the area would append the area
    // label a second time to a customer who already has one (the form is
    // re-submittable, and a legacy account may have a free-text location), so
    // the area is stored in its own column and only the free text is rebuilt.
    const detail = (location_text || '').trim();

    const run = db.transaction(() => {
      db.prepare('UPDATE users SET phone = ? WHERE id = ?').run(normPhone, current.id);

      const existingProfile = db
        .prepare('SELECT id FROM customers WHERE user_id = ?')
        .get(current.id);

      if (existingProfile) {
        db.prepare(
          'UPDATE customers SET phone = ?, area_key = ?, location_text = ? WHERE id = ?'
        ).run(normPhone, area.key, detail, existingProfile.id);
        if (trimmedName) {
          db.prepare('UPDATE customers SET name = ? WHERE id = ?').run(
            trimmedName,
            existingProfile.id
          );
        }
      } else {
        // Defensive: a customer row is created with the account, so this only
        // runs if one was deleted underneath us. Without it the customer would
        // have a phone and still no profile.
        db.prepare(
          'INSERT INTO customers (user_id, name, phone, area_key, location_text) VALUES (?, ?, ?, ?, ?)'
        ).run(current.id, trimmedName || current.email.split('@')[0], normPhone, area.key, detail);
      }
    });
    run();

    const user = db.prepare('SELECT * FROM users WHERE id = ?').get(current.id);
    res.json({
      message: 'تم حفظ بياناتك بنجاح',
      user: safeUserFor(user, true),
      profile: profileFor(user),
    });
  })
);

// POST /api/auth/change-password
// Allows the logged-in user to change their own password.
// Required fields: current_password, new_password
router.post(
  '/change-password',
  authenticate,
  changePasswordLimiter,
  wrap(async (req, res) => {
    const { current_password, new_password } = req.body || {};

    if (typeof current_password !== 'string' || current_password.length === 0) {
      throw badRequest('كلمة المرور الحالية مطلوبة');
    }
    const newPwProblem = passwordPolicyError(new_password);
    if (newPwProblem) throw badRequest(newPwProblem);
    if (current_password === new_password) {
      throw badRequest('كلمة المرور الجديدة يجب أن تختلف عن الحالية');
    }

    const user = db.prepare('SELECT id, password_hash FROM users WHERE id = ?').get(req.user.id);
    if (!user) throw unauthorized('المستخدم غير موجود');

    if (!bcrypt.compareSync(current_password, user.password_hash)) {
      throw unauthorized('كلمة المرور الحالية غير صحيحة');
    }

    const hash = bcrypt.hashSync(new_password, 12);
    db.prepare('UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?').run(
      hash,
      user.id
    );

    res.json({ message: 'تم تغيير كلمة المرور بنجاح' });
  })
);

module.exports = router;
