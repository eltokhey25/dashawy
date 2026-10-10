/**
 * @file   Push notification routes
 * @module notifications.routes
 *
 * The browser-facing half of Web Push: hand out the VAPID public key, store a
 * subscription, drop one. Mounted BEFORE the global /api authenticate guard
 * because the public key is needed by a page that may not have a session yet;
 * the two mutating routes run `authenticate` themselves.
 *
 * @author Eltokhey
 * @since  2026-10
 */
const express = require('express');
const { wrap, badRequest } = require('../utils/errors');
const { authenticate } = require('../middleware/auth');
const notifications = require('../services/notifications');
const config = require('../config');

const router = express.Router();

// GET /api/notifications/vapid-public-key
// Public on purpose: the key is meant for browsers, and the subscribe flow calls
// it before the user has necessarily subscribed to anything.
router.get('/vapid-public-key', (req, res) => {
  res.json({
    // Null rather than the raw config value when push is off: a key that failed
    // validation in notifications.js would otherwise be handed to a browser that
    // then calls urlBase64ToUint8Array() on it and fails with a far more confusing
    // error. The client checks `enabled` anyway, but the key itself is useless.
    publicKey: notifications.ready ? config.VAPID_PUBLIC_KEY : null,
    enabled: notifications.ready,
  });
});

// POST /api/notifications/subscribe
// Body: { subscription: { endpoint, keys: { p256dh, auth } } }
router.post(
  '/subscribe',
  authenticate,
  wrap(async (req, res) => {
    const { subscription } = req.body || {};
    if (!subscription) throw badRequest('الاشتراك مطلوب');

    try {
      // user_id comes from the token, never the body: a subscription can only
      // ever be stored against the account that owns the token.
      notifications.saveSubscription(req.user.id, subscription, req.headers['user-agent']);
      res.json({ ok: true });
    } catch (e) {
      throw badRequest('الاشتراك غير صالح');
    }
  })
);

// POST /api/notifications/unsubscribe
// Body: { endpoint } — only the caller's own subscription is removed.
router.post(
  '/unsubscribe',
  authenticate,
  wrap(async (req, res) => {
    const { endpoint } = req.body || {};
    if (!endpoint) throw badRequest('endpoint مطلوب');
    const removed = notifications.removeSubscription(req.user.id, endpoint);
    res.json({ ok: true, removed: removed > 0 });
  })
);

module.exports = router;
