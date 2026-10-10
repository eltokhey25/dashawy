/**
 * @file   Request input validation helpers
 * @module validation
 *
 * Small validators for the request payloads (email, password strength, coordinates, IDs)
 * shared by the route modules.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Lightweight input validation helpers for forms and API bodies.
const config = require('../config');
// NOTE: refs (and therefore db) is required lazily inside isValidSpecialty, not
// here at module scope. db.js pulls this module in to normalise phone numbers,
// and a top-level require would form the cycle
// db -> validation -> refs -> db, which resolves to a half-initialised
// validation module whenever this file is loaded before db.js.

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
// Accepts common international formats: digits, spaces, +, -, parentheses.
const PHONE_RE = /^[+()\-\s\d]{6,20}$/;

// Static fallback set + live DB-driven keys.
let specialtyKeysCache = new Set(config.SPECIALTIES.map((s) => s.key));

function isValidEmail(v) {
  return typeof v === 'string' && EMAIL_RE.test(v.trim());
}

function isValidPhone(v) {
  return typeof v === 'string' && PHONE_RE.test(v.trim());
}

function isValidName(v) {
  return typeof v === 'string' && v.trim().length >= 2 && v.trim().length <= 100;
}

function isNonEmptyString(v, max = 1000) {
  return typeof v === 'string' && v.trim().length > 0 && v.trim().length <= max;
}

/**
 * Password strength policy, enforced everywhere a password is set.
 *
 * The rule is deliberately small — length plus two character classes — because a
 * policy that is easy to satisfy is one that is actually obeyed, and a rule nobody
 * can satisfy just pushes people toward `Password1` reused across sites.
 *
 *   * at least PASSWORD_MIN_LENGTH characters
 *   * at least one letter
 *   * at least one digit
 *
 * The previous policy was a bare `length < 6` check applied independently at nine
 * call sites, which accepted `123456`. Centralising it here is the point: one
 * place to change, and no site can drift behind.
 */
const PASSWORD_MIN_LENGTH = 8;
const PASSWORD_MAX_LENGTH = 200;

function isStrongPassword(v) {
  return (
    typeof v === 'string' &&
    v.length >= PASSWORD_MIN_LENGTH &&
    v.length <= PASSWORD_MAX_LENGTH &&
    /[a-zA-Z]/.test(v) &&
    /\d/.test(v)
  );
}

/**
 * Human-readable reason a password was rejected, for the 400 response.
 *
 * @param {unknown} v
 * @returns {string|null} Arabic message, or null when the password is acceptable.
 */
function passwordPolicyError(v) {
  if (typeof v !== 'string') return 'كلمة المرور مطلوبة';
  if (v.length < PASSWORD_MIN_LENGTH) {
    return 'كلمة المرور يجب ألا تقل عن ' + PASSWORD_MIN_LENGTH + ' أحرف';
  }
  if (v.length > PASSWORD_MAX_LENGTH) {
    return 'كلمة المرور طويلة جدًا';
  }
  if (!/[a-zA-Z]/.test(v)) return 'كلمة المرور يجب أن تحتوي على حرف واحد على الأقل';
  if (!/\d/.test(v)) return 'كلمة المرور يجب أن تحتوي على رقم واحد على الأقل';
  return null;
}

function isValidSpecialty(v) {
  if (typeof v !== 'string' || !v) return false;
  if (specialtyKeysCache.has(v)) return true;
  try {
    const refs = require('../services/refs');
    const dbKeys = new Set(refs.getSpecialtyKeys());
    specialtyKeysCache = dbKeys;
    return dbKeys.has(v);
  } catch (err) {
    return config.SPECIALTIES.some((s) => s.key === v);
  }
}

// Returns { latitude, longitude } or null. Accepts numbers or numeric strings.
function parseLatLng(lat, lng) {
  const la = lat === null || lat === undefined || lat === '' ? null : Number(lat);
  const ln = lng === null || lng === undefined || lng === '' ? null : Number(lng);
  if (la === null || ln === null) return null;
  if (!Number.isFinite(la) || !Number.isFinite(ln)) return null;
  if (la < -90 || la > 90 || ln < -180 || ln > 180) return null;
  return { latitude: la, longitude: ln };
}

function normalizeEmail(v) {
  return String(v).trim().toLowerCase();
}

// Canonical form of a phone used as a login identifier: digits only, with a
// single leading '+'. Because users.phone carries a UNIQUE index, every code
// path that writes or matches a phone MUST go through here — otherwise
// "01025330520" and "+201025330520" become two different accounts that both
// pass the uniqueness check.
function normalizePhone(v) {
  if (v === null || v === undefined) return '';
  const raw = String(v).trim();
  if (!raw) return '';

  const hadPlus = raw.startsWith('+');
  let s = raw.replace(/\D/g, '');
  if (!s) return '';

  // '0020...' (international dialling prefix) collapses to the country code.
  if (!hadPlus && s.startsWith('00')) s = s.slice(2);

  // No country code begins with '0', so a leading zero is unambiguously a local
  // trunk prefix: rewrite it to Egypt's +20 and drop the zero. This is
  // deliberately keyed on the trunk prefix rather than on a digit count — an
  // exact-length rule (the earlier `length === 11` version) silently left
  // "0111 222 333" and "(011) 111 222 333" un-normalised, minting extra
  // identities that the canonical form could never reach. NOT gated on hadPlus,
  // because '+01025330520' is a common typo for the same number.
  if (s.startsWith('0')) {
    const national = s.slice(1);
    return national ? '+20' + national : '';
  }

  // Country code already present but missing its '+'.
  if (s.startsWith('20') && s.length === 12) return '+' + s;

  return (hadPlus ? '+' : '') + s;
}

// A real Egyptian MOBILE number, in the canonical +20 form: '+20' plus ten
// national digits whose first is always 1, whose second is one of the prefixes
// actually sold here (10 / 11 / 12 / 15 — the three operators plus the old
// 012 range), and whose remaining eight are free.
//
// This is stricter than normalizePhone() on purpose. normalizePhone answers
// "are these two strings the same number", which is the only question login
// asks; here the question is "is this a mobile number a customer could be
// reached on", and +201234567890 is neither a real prefix nor reachable. Land
// lines, short codes and a too-short digit string all fail here.
//
// Only digits, spaces, dashes, plus and parentheses are accepted before
// normalising: normalizePhone() throws away anything non-numeric, so without
// this guard the string "abc01012345678" would normalise into a valid number.
const EGYPTIAN_MOBILE_RE = /^\+201[0125]\d{8}$/;

/**
 * Normalises an Egyptian mobile number, or returns '' when the input is not
 * one. Callers treat '' as "invalid" and never store it.
 *
 * @param {unknown} v
 * @returns {string} '+201XXXXXXXXX', or '' when invalid.
 */
function normalizeEgyptianMobile(v) {
  if (v === null || v === undefined) return '';
  const raw = String(v).trim();
  if (!raw) return '';
  if (!/^[+\d\s\-()]+$/.test(raw)) return '';
  const normalized = normalizePhone(raw);
  return EGYPTIAN_MOBILE_RE.test(normalized) ? normalized : '';
}

/**
 * @param {unknown} v
 * @returns {boolean} whether v is a reachable Egyptian mobile number.
 */
function isValidEgyptianMobile(v) {
  return normalizeEgyptianMobile(v) !== '';
}

module.exports = {
  isValidEmail,
  isValidPhone,
  isValidName,
  isNonEmptyString,
  isStrongPassword,
  passwordPolicyError,
  PASSWORD_MIN_LENGTH,
  PASSWORD_MAX_LENGTH,
  isValidSpecialty,
  parseLatLng,
  normalizeEmail,
  normalizePhone,
  normalizeEgyptianMobile,
  isValidEgyptianMobile,
};
