/**
 * @file   Central application configuration
 * @module config
 *
 * Reads environment variables once and exposes them as validated constants (port, paths, JWT
 * secret, CORS origins, upload limits). Requiring this module is what enforces the production
 * JWT secret policy.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Central application configuration.
const path = require('path');
const logger = require('./utils/logger');

const ROOT_DIR = path.resolve(__dirname, '..');

const IS_PRODUCTION = process.env.NODE_ENV === 'production';

/**
 * Resolves the JWT signing secret.
 *
 * The previous code fell back to a hardcoded default, which meant a
 * misconfigured production deployment would sign and accept tokens that any
 * reader of the source could forge. Now a production boot fails loudly instead,
 * and development gets an explicit, clearly-labelled throwaway secret.
 */
function getJwtSecret() {
  const secret = process.env.JWT_SECRET;
  if (!secret || !secret.trim()) {
    if (IS_PRODUCTION) {
      throw new Error(
        'JWT_SECRET must be set in production. Generate one with: ' +
          "node -e \"console.log(require('crypto').randomBytes(48).toString('hex'))\""
      );
    }
    logger.info(
      'config',
      '[config] JWT_SECRET is not set — using an insecure development-only secret. Set JWT_SECRET in .env before deploying.'
    );
    return 'dev-only-insecure-secret-change-me';
  }
  if (secret.length < 32) {
    const message = 'JWT_SECRET is shorter than 32 characters';
    if (IS_PRODUCTION) throw new Error(message + ' and must be strengthened before deploying.');
    logger.info('config', '[config] ' + message + ' — it is still accepted in development.');
  }
  return secret;
}

/**
 * Origins allowed to call the API from a browser. The site is normally served
 * same-origin, so this mainly matters for a split dev/prod deployment.
 * Configure with a comma-separated CORS_ORIGINS env var.
 */
function getAllowedOrigins() {
  const fromEnv = (process.env.CORS_ORIGINS || '')
    .split(',')
    .map((o) => o.trim().replace(/\/$/, ''))
    .filter(Boolean);

  const defaults = ['http://localhost:3000', 'http://127.0.0.1:3000'];
  const list = Array.from(new Set(defaults.concat(fromEnv)));

  if (IS_PRODUCTION && fromEnv.length === 0) {
    logger.info(
      'config',
      `[config] CORS_ORIGINS is not set — only the localhost origins are allowed.`
    );
  }
  return list;
}

/**
 * Platform commission as a percentage of the service price.
 *
 * Read lazily from the settings table rather than cached, because admins can
 * change the rate at runtime and pricing.js calls this on every request. The
 * require of ./db is INSIDE the function on purpose: db.js requires this module
 * at load time, so a top-level require would deadlock on the circular
 * dependency and yield a half-initialised module.
 *
 * Anything unparseable or out of the 0..100 range falls back to 20 rather than
 * propagating NaN into a money calculation.
 */
function getCommissionPercent() {
  try {
    const db = require('./db');
    const row = db.prepare('SELECT value FROM settings WHERE key = ?').get('commission_percent');
    const val = parseFloat(row && row.value);
    return isNaN(val) || val < 0 || val > 100 ? 20 : val;
  } catch (e) {
    return 20;
  }
}

/**
 * Google Sign-In credentials, read from the environment only.
 *
 * Both halves must be present for the feature to be on, and neither is ever
 * hard-coded: the client ID is public by design (the browser needs it, and it
 * ends up in the page source anyway) while the client secret stays on this
 * side. The ID-token flow this app uses — the browser button hands the server
 * an ID token, and the server verifies its signature against Google's published
 * public keys — never transmits the secret, so it is validated here and kept
 * for deployments that later add a server-side consent redirect.
 *
 * A half-configured environment is reported at boot instead of failing: the
 * service is optional, and a typo in one variable should not read as "Google
 * sign-in is misconfigured" when the real answer is simply "off".
 */
const GOOGLE_CLIENT_ID = (process.env.GOOGLE_CLIENT_ID || '').trim();
const GOOGLE_CLIENT_SECRET = (process.env.GOOGLE_CLIENT_SECRET || '').trim();
const GOOGLE_ENABLED = Boolean(GOOGLE_CLIENT_ID && GOOGLE_CLIENT_SECRET);

if (Boolean(GOOGLE_CLIENT_ID) !== Boolean(GOOGLE_CLIENT_SECRET)) {
  logger.warn(
    'config',
    '[config] GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set together — Google sign-in is disabled.'
  );
}

/**
 * Reads a positive integer from the environment, falling back to `fallback`.
 *
 * Every duration in this file is a fallback-guarded read rather than a bare
 * parse: a typo in .env must not silently become a 0-second broadcast window
 * (which would expire every offer before a technician's poll could display it)
 * or an instant queue cancellation.
 */
function positiveIntEnv(name, fallback) {
  const raw = parseInt(process.env[name], 10);
  return Number.isInteger(raw) && raw > 0 ? raw : fallback;
}

module.exports = {
  PORT: process.env.PORT || 3000,
  IS_PRODUCTION,
  ALLOWED_ORIGINS: getAllowedOrigins(),
  DATABASE_PATH: process.env.DATABASE_PATH || path.join(ROOT_DIR, 'database', 'fixrequest.db'),
  SCHEMA_PATH: path.join(ROOT_DIR, 'database', 'schema.sql'),
  JWT_SECRET: getJwtSecret(),
  JWT_EXPIRES_IN: process.env.JWT_EXPIRES_IN || '7d',
  getCommissionPercent,

  // Google Sign-In (customers only — technicians keep the email/password
  // flow, since their signup requires identity documents).
  //
  // GOOGLE_CLIENT_ID is safe to serve to the browser (it is published in the
  // Google Cloud console); GOOGLE_CLIENT_SECRET is a credential and is only
  // ever read here. GOOGLE_ENABLED is the single source of truth the route and
  // the frontend button both read, so "configured" can never disagree between
  // the two halves of the feature.
  GOOGLE_CLIENT_ID,
  GOOGLE_CLIENT_SECRET,
  GOOGLE_ENABLED,

  // How long an offer stays open. Hours, not seconds: a real customer expects a
  // technician to have most of a working day to answer, and the old 60s window
  // expired offers before anyone could plausibly have opened the app.
  BROADCAST_TIMEOUT_HOURS: positiveIntEnv('BROADCAST_TIMEOUT_HOURS', 24),

  // Waiting queue: how often unaccepted queued requests are offered again to
  // newly-available technicians, and how long they may sit before being cancelled.
  QUEUE_RECHECK_MINUTES: positiveIntEnv('QUEUE_RECHECK_MINUTES', 5),
  QUEUE_MAX_HOURS: positiveIntEnv('QUEUE_MAX_HOURS', 24),

  // Default broadcast radius in km, used only when the `auto_assign_radius_km`
  // settings row is absent (a fresh database seeds it to 25, so this normally
  // does nothing). The admin setting is the runtime override; this is the
  // deployment default for installations that never created the row.
  BROADCAST_RADIUS_KM: positiveIntEnv('BROADCAST_RADIUS_KM', 10),

  // Web Push (VAPID). Generate a pair once with:
  //   node -e "console.log(require('web-push').generateVAPIDKeys())"
  // The public half is served to browsers at GET /api/notifications/vapid-public-key;
  // the private half must never leave the server. Push is disabled (rather than
  // fatal) when the pair is absent, so a development checkout still boots.
  VAPID_PUBLIC_KEY: process.env.VAPID_PUBLIC_KEY || '',
  VAPID_PRIVATE_KEY: process.env.VAPID_PRIVATE_KEY || '',
  VAPID_SUBJECT: process.env.VAPID_SUBJECT || 'mailto:admin@dashawy.app',

  // Cash confirmation: how long a technician's report of a cash payment waits for
  // the customer to confirm the same figure before the queue sweep settles it on
  // the technician's number. The same value is seeded into settings as
  // `cash_confirm_timeout_hours` so an admin can see it; this env var is what the
  // service actually reads.
  CASH_CONFIRM_TIMEOUT_HOURS: positiveIntEnv('CASH_CONFIRM_TIMEOUT_HOURS', 24),

  // Supported specialties. 'key' is stored in DB, 'label' is the Arabic UI label.
  // 'price' is the launch tariff in EGP, used only to seed the service_pricing
  // table on a fresh database; afterwards the admin tariff editor is the source
  // of truth and these values are ignored (INSERT OR IGNORE never overwrites).
  SPECIALTIES: [
    { key: 'dish_install', label: 'تركيب دش', price: 150 },
    { key: 'dish_repair', label: 'صيانة دش', price: 100 },
    { key: 'signal_tuning', label: 'ضبط إشارة', price: 80 },
    { key: 'receiver_service', label: 'تركيب / صيانة رسيفر', price: 120 },
    // Quoted on the spot, so it has no fixed base price.
    { key: 'other', label: 'خدمة أخرى', price: 0 },
  ],

  // Default icon used when a specialty row lacks one.
  DEFAULT_SPECIALTY_ICON: 'fa-solid fa-wrench',

  // Alexandria neighborhoods. 'key' is the stored value, 'label' is the Arabic UI label.
  ALEXANDRIA_AREAS: [
    { key: 'sidi_gaber', label: 'سيدي جابر' },
    { key: 'smouha', label: 'سموحة' },
    { key: 'cleopatra', label: 'كليوباترا' },
    { key: 'sporting', label: 'سبورتنج' },
    { key: 'ibrahimia', label: 'الإبراهيمية' },
    { key: 'camp_chezar', label: 'كامب شيزار' },
    { key: 'louran', label: 'لوران' },
    { key: 'glime', label: 'جليم' },
    { key: 'san_stefano', label: 'سان ستيفانو' },
    { key: 'sidi_bishr', label: 'سيدي بشر' },
    { key: 'mustafa_kamel', label: 'مصطفى كامل' },
    { key: 'mandara', label: 'المندرة' },
    { key: 'asafra', label: 'العصافرة' },
    { key: 'abu_qir', label: 'أبو قير' },
    { key: 'maamoura', label: 'المعمورة' },
    { key: 'montazah', label: 'المنتزه' },
    { key: 'bahari', label: 'بحري' },
    { key: 'anfushi', label: 'الأنفوشي' },
    { key: 'mansheya', label: 'المنشية' },
    { key: 'attarin', label: 'العطارين' },
    { key: 'gomrok', label: 'الجمرك' },
    { key: 'labban', label: 'اللبان' },
    { key: 'moharam_bek', label: 'محرم بك' },
    { key: 'karmouz', label: 'كرموز' },
    { key: 'gharbal', label: 'غربال' },
    { key: 'bab_sharq', label: 'باب شرق' },
    { key: 'hadra', label: 'الحضرة' },
    { key: 'fleming', label: 'فلمنج' },
    { key: 'zizinia', label: 'زيزينيا' },
    { key: 'rosHDY', label: 'رشدي' },
    { key: 'shatby', label: 'الشاطبي' },
    { key: 'azarita', label: 'الأزاريطة' },
    { key: 'kom_el_dekka', label: 'كوم الدكة' },
    { key: 'mahsousa', label: 'المحروسة' },
    { key: 'dekhila', label: 'الدخيلة' },
    { key: 'agami', label: 'العجمي' },
    { key: 'bitash', label: 'البيطاش' },
    { key: 'hanoville', label: 'الهانوفيل' },
    { key: 'amreya', label: 'العامرية' },
    { key: 'max', label: 'المكس' },
    { key: 'qabari', label: 'القباري' },
    { key: 'borg_el_arab', label: 'برج العرب' },
  ],

  // Request lifecycle statuses.
  //
  // 'no_technician' is the terminal state of a broadcast that nobody answered
  // inside BROADCAST_TIMEOUT_HOURS. It exists so an unanswered request stops
  // looking "in progress forever" and an admin can see the backlog. It is NOT a
  // lifecycle step a technician drives: nothing accepts from it. A request that
  // reached the waiting queue is cancelled instead of flagged, so that the queue
  // service owns its whole lifecycle.
  STATUSES: ['pending', 'assigned', 'in_progress', 'completed', 'cancelled', 'no_technician'],

  // Which Alexandria neighbourhoods count as adjacent, for the waiting queue's
  // area filter. Each list includes the area itself, so the caller only has to
  // test membership.
  //
  // Deliberately hand-maintained rather than computed from coordinates: the queue
  // runs on free-text locations (a technician's stored address is whatever they
  // typed at sign-up), so there is nothing reliable to compute a distance from,
  // and a wrong adjacency map here is easy to eyeball and correct.
  //
  // Any area missing from this map falls back to "only this area", so adding a
  // neighbourhood to ALEXANDRIA_AREAS cannot accidentally widen the pool to the
  // whole city.
  AREA_NEIGHBORS: {
    smouha: ['smouha', 'sidi_gaber', 'cleopatra', 'ibrahimia', 'sporting'],
    sidi_gaber: ['sidi_gaber', 'smouha', 'cleopatra', 'sporting', 'camp_chezar'],
    cleopatra: ['cleopatra', 'smouha', 'sidi_gaber', 'sporting', 'ibrahimia'],
    sporting: ['sporting', 'smouha', 'sidi_gaber', 'cleopatra', 'ibrahimia', 'azarita'],
    ibrahimia: ['ibrahimia', 'smouha', 'cleopatra', 'sporting', 'azarita', 'shatby'],
    camp_chezar: ['camp_chezar', 'sidi_gaber', 'cleopatra', 'sporting', 'louran'],
    louran: ['louran', 'camp_chezar', 'sporting', 'glime', 'sidi_gaber'],
    glime: ['glime', 'louran', 'san_stefano', 'sporting'],
    san_stefano: ['san_stefano', 'glime', 'sidi_bishr', 'louran'],
    sidi_bishr: ['sidi_bishr', 'san_stefano', 'mustafa_kamel', 'glime'],
    mustafa_kamel: ['mustafa_kamel', 'sidi_bishr', 'mandara', 'san_stefano'],
    mandara: ['mandara', 'mustafa_kamel', 'asafra'],
    asafra: ['asafra', 'mandara', 'abu_qir', 'maamoura'],
    abu_qir: ['abu_qir', 'asafra', 'maamoura'],
    maamoura: ['maamoura', 'asafra', 'abu_qir', 'montazah'],
    montazah: ['montazah', 'maamoura', 'asafra'],
    bahari: ['bahari', 'anfushi', 'mansheya', 'attarin'],
    anfushi: ['anfushi', 'bahari', 'mansheya', 'gomrok'],
    mansheya: ['mansheya', 'bahari', 'anfushi', 'attarin', 'gomrok'],
    attarin: ['attarin', 'mansheya', 'bahari', 'gomrok', 'labban'],
    gomrok: ['gomrok', 'anfushi', 'mansheya', 'attarin', 'labban'],
    labban: ['labban', 'attarin', 'gomrok', 'moharam_bek'],
    moharam_bek: ['moharam_bek', 'labban', 'karmouz', 'gharbal'],
    karmouz: ['karmouz', 'moharam_bek', 'gharbal', 'bab_sharq'],
    gharbal: ['gharbal', 'moharam_bek', 'karmouz', 'bab_sharq'],
    bab_sharq: ['bab_sharq', 'karmouz', 'gharbal', 'hadra', 'fleming'],
    hadra: ['hadra', 'bab_sharq', 'fleming', 'sidi_gaber'],
    fleming: ['fleming', 'hadra', 'bab_sharq', 'zizinia', 'sidi_gaber'],
    zizinia: ['zizinia', 'fleming', 'sporting', 'sidi_gaber'],
    rosHDY: ['rosHDY', 'sporting', 'shatby', 'azarita'],
    shatby: ['shatby', 'rosHDY', 'azarita', 'ibrahimia'],
    azarita: ['azarita', 'shatby', 'rosHDY', 'sporting', 'ibrahimia'],
    kom_el_dekka: ['kom_el_dekka', 'attarin', 'mansheya', 'gomrok'],
    mahsousa: ['mahsousa', 'sidi_gaber', 'hadra'],
    dekhila: ['dekhila', 'agami', 'bitash', 'hanoville'],
    agami: ['agami', 'dekhila', 'bitash', 'hanoville'],
    bitash: ['bitash', 'dekhila', 'agami', 'hanoville'],
    hanoville: ['hanoville', 'dekhila', 'agami', 'bitash', 'amreya'],
    amreya: ['amreya', 'hanoville', 'max', 'borg_el_arab'],
    max: ['max', 'amreya', 'qabari'],
    qabari: ['qabari', 'max', 'moharam_bek', 'gharbal'],
    borg_el_arab: ['borg_el_arab', 'amreya'],
  },

  LEVELS: {
    customer: 'customer',
    technician: 'technician',
    admin: 'admin',
  },
};
