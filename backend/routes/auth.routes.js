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
const fs = require('fs');
const path = require('path');
const db = require('../db');
const config = require('../config');
const { wrap, AppError, badRequest, unauthorized } = require('../utils/errors');
const { authenticate } = require('../middleware/auth');
const rateLimit = require('../middleware/rate-limit');

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
      user: { id: user.id, email: user.email, phone: user.phone, role: user.role },
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

    const safe = {
      id: user.id,
      email: user.email,
      phone: user.phone || null,
      role: user.role,
      is_super_admin: Boolean(user.is_super_admin),
      permissions: (() => {
        try {
          return JSON.parse(user.permissions || '[]');
        } catch (e) {
          return [];
        }
      })(),
    };

    // Disabled accounts (sub-admins toggled off by the super admin) must not
    // be able to obtain a session at all, not just fail on later requests.
    if (user.blocked) {
      throw unauthorized('تم إيقاف حسابك. تواصل مع الإدارة.');
    }

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

    res.json({
      token: signToken(user),
      user: safe,
      profile: profileFor(safe),
      must_change_password: Boolean(user.must_change_password),
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
        'SELECT id, email, phone, role, is_super_admin, must_change_password, permissions FROM users WHERE id = ?'
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
      permissions: (() => {
        try {
          return JSON.parse(user.permissions || '[]');
        } catch (e) {
          return [];
        }
      })(),
    };

    res.json({ user: safe });
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
