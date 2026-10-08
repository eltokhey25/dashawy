/**
 * @file   Push notification service using web-push
 * @module notifications
 *
 * Sends Web Push notifications to a user through every browser subscription they
 * registered for their account. A subscription that the push service has since
 * invalidated is deleted on the first 404/410 instead of being retried forever.
 *
 * Everything here is best-effort: a notification must never be able to fail the
 * request that triggered it, so senders are fire-and-forget and delivery errors
 * are logged, not thrown.
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Web Push delivery. Reads its configuration from config.js like every other service.
const webpush = require('web-push');
const db = require('../db');
const config = require('../config');
const logger = require('../utils/logger');

let ready = false;

if (config.VAPID_PUBLIC_KEY && config.VAPID_PRIVATE_KEY) {
  webpush.setVapidDetails(config.VAPID_SUBJECT, config.VAPID_PUBLIC_KEY, config.VAPID_PRIVATE_KEY);
  ready = true;
} else {
  logger.info('push', `[push] VAPID keys not set — push notifications disabled`);
}

// Never let a full mailbox stall the accept/complete path.
const MAX_PER_MESSAGE = 10;

// ---------------------------------------------------------------------------
// Subscriptions
// ---------------------------------------------------------------------------

// Stores (or refreshes) a subscription. Keyed on the endpoint, so the same
// browser re-subscribing after a token refresh updates the existing row instead
// of piling up duplicates.
function saveSubscription(userId, subscription, userAgent) {
  const endpoint = subscription && subscription.endpoint;
  const keys = (subscription && subscription.keys) || {};
  const p256dh = keys.p256dh;
  const auth = keys.auth;

  if (!endpoint || !p256dh || !auth) {
    throw new Error('Invalid subscription');
  }

  db.prepare(
    `INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
     VALUES (?, ?, ?, ?, ?)
     ON CONFLICT(endpoint) DO UPDATE SET
       user_id = excluded.user_id,
       p256dh = excluded.p256dh,
       auth = excluded.auth,
       user_agent = excluded.user_agent,
       last_used_at = datetime('now')`
  ).run(userId, endpoint, p256dh, auth, userAgent || null);
}

// Deletes a subscription.
//
// Scoped to the owner on purpose: taking a bare endpoint would let any signed-in
// user delete somebody else's device by guessing/stealing its endpoint, which is
// a cheap way to silence another account's notifications.
function removeSubscription(userId, endpoint) {
  return db
    .prepare('DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?')
    .run(endpoint, userId).changes;
}

function subscriptionsFor(userId) {
  return db.prepare('SELECT * FROM push_subscriptions WHERE user_id = ?').all(userId);
}

// ---------------------------------------------------------------------------
// Sending
// ---------------------------------------------------------------------------

async function sendToUser(userId, payload) {
  if (!ready) return { sent: 0, failed: 0, reason: 'not_configured' };

  const subs = subscriptionsFor(userId);
  if (subs.length === 0) return { sent: 0, failed: 0, reason: 'no_subscriptions' };

  let sent = 0;
  let failed = 0;
  const body = JSON.stringify(payload);

  for (const sub of subs.slice(0, MAX_PER_MESSAGE)) {
    const pushSub = { endpoint: sub.endpoint, keys: { p256dh: sub.p256dh, auth: sub.auth } };
    try {
      await webpush.sendNotification(pushSub, body);
      db.prepare("UPDATE push_subscriptions SET last_used_at = datetime('now') WHERE id = ?").run(
        sub.id
      );
      sent++;
    } catch (err) {
      failed++;
      // 404/410 = the browser or push service dropped this subscription; keeping
      // it would retry a dead endpoint on every notification from now on.
      if (err.statusCode === 404 || err.statusCode === 410) {
        db.prepare('DELETE FROM push_subscriptions WHERE id = ?').run(sub.id);
      } else {
        logger.info(
          'push',
          `[push] delivery failed for user`,
          userId,
          err.statusCode || '',
          err.message
        );
      }
    }
  }

  return { sent, failed };
}

// Fire-and-forget: the caller must never await or be interrupted by a push.
function notify(userId, payload) {
  if (!userId) return;
  sendToUser(userId, payload).catch((err) => {
    logger.info('push', `[push] notify error:`, err.message);
  });
}

// ---------------------------------------------------------------------------
// Notification vocabulary
// ---------------------------------------------------------------------------
// One place that decides what a notification says and where it takes you, so the
// wording cannot drift between the routes that fire them.

function serviceLabel(key, customText) {
  if (key === 'other' && customText) return customText;
  const m = config.SPECIALTIES.find((s) => s.key === key);
  return m ? m.label : key;
}

function newRequestForTechnician({ specialty, customText, areaLabel }) {
  return {
    title: '🔔 طلب جديد',
    body: serviceLabel(specialty, customText) + (areaLabel ? ' — ' + areaLabel : ' — قريب منك'),
    url: '/technician/dashboard.html',
    tag: 'new-request',
    requireInteraction: true,
  };
}

function requestAcceptedForCustomer({ technicianName }) {
  return {
    title: '✅ تم قبول طلبك',
    body: (technicianName || 'الفني') + ' في الطريق إليك',
    url: '/customer/my-requests.html',
    tag: 'request-accepted',
  };
}

// Sent the moment a technician completes a job. This is the ONLY notification
// that asks for something back, so it carries three extra things the others do
// not need: the amount the technician declared, a deep link carrying the request
// id so the client knows which job to rate, and requireInteraction so it stays
// on screen (a rating request that silently disappears gets ignored).
//
// `amount` is the technician's own figure and is often still null at completion
// time — the cash report is a separate step — so the wording degrades to a
// generic "rate us" instead of printing "0 ج.م".
function requestCompletedForCustomer({ technicianName, amount, requestId } = {}) {
  const hasAmount = amount !== null && amount !== undefined && Number(amount) > 0;
  const body = hasAmount
    ? (technicianName || 'الفني') + ' أنهى الخدمة وسجّل مبلغ ' + amount + ' ج.م — قيّم تجربتك الآن'
    : 'تم إنهاء الخدمة — قيّم تجربتك الآن، رأيك مهم';

  return {
    title: '🎉 تم إنهاء الخدمة — قيّم الفني',
    body,
    // A customer-facing path that force-review.js watches. The id lets the
    // client put THIS job first if several are waiting.
    url: '/customer/home.html?review=' + requestId,
    tag: 'review-' + requestId,
    requireInteraction: true,
  };
}

function paymentConfirmedForTechnician({ amount }) {
  return {
    title: '💰 تم تأكيد الدفع',
    body: amount ? 'تم تسجيل عمولة ' + amount + ' على حسابك' : 'عمولة المنصة تم تسجيلها على حسابك',
    url: '/technician/dashboard.html',
    tag: 'payment-confirmed',
  };
}

module.exports = {
  saveSubscription,
  removeSubscription,
  subscriptionsFor,
  sendToUser,
  notify,
  serviceLabel,
  newRequestForTechnician,
  requestAcceptedForCustomer,
  requestCompletedForCustomer,
  paymentConfirmedForTechnician,
  isReady: () => ready,
  ready,
};
