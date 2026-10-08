/**
 * @file   Per-request chat between a customer and the assigned technician.
 * @module routes/messages.routes
 *
 * Polling transport, no WebSockets: the client GETs the thread every 3 seconds and
 * the server pushes a Web Push notification on every new message, so a message
 * still reaches the other party while they are on a different screen.
 *
 * Access is strictly per request: only the customer who owns it and the
 * technician assigned to it may read or write. Everyone else gets 403.
 *
 * Two ordering rules in this file are load-bearing, because Express matches in
 * registration order and ':request_id' would swallow both of them:
 *   - '/unread-count' is registered BEFORE '/:request_id'
 *   - '/:request_id/read' has its own segment, so it cannot collide with POST
 *     '/:request_id'
 *
 * @author Eltokhey
 * @since  2026-10
 */
const express = require('express');
const db = require('../db');
const { wrap, badRequest, forbidden, AppError } = require('../utils/errors');
const { authenticate } = require('../middleware/auth');
const notifications = require('../services/notifications');
const rateLimit = require('../middleware/rate-limit');

const router = express.Router();
router.use(authenticate);

// Per-USER limit on sending. The global /api/ budget is 300 requests per 15
// minutes for the whole API, which is far too generous for one chat endpoint:
// without this, one authenticated user can flood a thread, and because every
// message fans out to a Web Push notification, flooding is also a push
// amplification attack against the other party's device.
//
// Keyed by user id rather than IP because a shared connection must not let one
// real person exceed their budget, and an attacker with many addresses must not
// exceed theirs.
const messageSendLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 30,
  name: 'chat-send',
  keyBy: rateLimit.userKey,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'تمهّل قليلاً — رسائل كثيرة في وقت قصير' },
});

// A request is only chat-able while somebody is actually doing the work. Sending
// is refused outside that window; reading stays open forever so a finished job
// keeps its transcript.
const SENDABLE_STATES = ['assigned', 'in_progress'];
const MAX_BODY_LENGTH = 2000;

// How many of the most recent messages one GET returns. See the query below.
const THREAD_PAGE_SIZE = 200;

/**
 * Resolve the caller against a request and decide which side of the thread they
 * are on. Throws 404 when the request does not exist, and 403 for anyone who is
 * neither its customer nor its technician.
 */
function authorizeRequest(req, requestId) {
  if (!Number.isInteger(requestId) || requestId <= 0) {
    throw new AppError('الطلب غير موجود', 404);
  }

  const request = db
    .prepare(
      `SELECT r.id, r.status, r.customer_id, r.technician_id,
              c.user_id AS customer_user_id,
              t.user_id AS technician_user_id
         FROM requests r
         JOIN customers c ON c.id = r.customer_id
         LEFT JOIN technicians t ON t.id = r.technician_id
        WHERE r.id = ?`
    )
    .get(requestId);

  if (!request) throw new AppError('الطلب غير موجود', 404);
  if (!request.technician_id) throw badRequest('لا يوجد فني مرتبط بالطلب');

  const uid = req.user.id;
  const isCustomer = uid === request.customer_user_id;
  const isTechnician = request.technician_user_id != null && uid === request.technician_user_id;

  if (!isCustomer && !isTechnician) {
    throw forbidden('غير مصرح لك بالوصول لهذه المحادثة');
  }

  return {
    request,
    role: isCustomer ? 'customer' : 'technician',
    otherUserId: isCustomer ? request.technician_user_id : request.customer_user_id,
  };
}

function otherRoleOf(role) {
  return role === 'customer' ? 'technician' : 'customer';
}

// ------------------------------------------------------------------
// GET /api/messages/unread-count
//
// MUST stay above '/:request_id'. Declared after it, the string "unread-count"
// would be taken as a request id, Number() would make it NaN and the endpoint
// would answer 404 for every caller.
//
// This is deliberately side-effect free: the badge is polled on every dashboard,
// so it must not mark anything read. Only loading the thread does that.
// ------------------------------------------------------------------
router.get(
  '/unread-count',
  wrap(async (req, res) => {
    const uid = req.user.id;
    const role = req.user.role;
    const other = otherRoleOf(role);

    let rows = [];
    if (role === 'customer') {
      rows = db
        .prepare(
          `SELECT m.request_id, COUNT(*) AS n
             FROM messages m
             JOIN requests r ON r.id = m.request_id
             JOIN customers c ON c.id = r.customer_id
            WHERE c.user_id = ? AND m.sender_role = ? AND m.read_at IS NULL
            GROUP BY m.request_id`
        )
        .all(uid, other);
    } else if (role === 'technician') {
      rows = db
        .prepare(
          `SELECT m.request_id, COUNT(*) AS n
             FROM messages m
             JOIN requests r ON r.id = m.request_id
             JOIN technicians t ON t.id = r.technician_id
            WHERE t.user_id = ? AND m.sender_role = ? AND m.read_at IS NULL
            GROUP BY m.request_id`
        )
        .all(uid, other);
    }

    const total = rows.reduce((sum, r) => sum + r.n, 0);
    res.json({ total, byRequest: rows });
  })
);

// ------------------------------------------------------------------
// GET /api/messages/:request_id
//
// Loading the thread is also what marks the other side's messages read: the
// recipient has demonstrably seen them. read_at is applied after the rows are
// selected so the response can report the pre-read state honestly.
// ------------------------------------------------------------------
router.get(
  '/:request_id',
  wrap(async (req, res) => {
    const rid = Number(req.params.request_id);
    const { role, request, otherUserId } = authorizeRequest(req, rid);

    // The thread is capped to its most recent THREAD_PAGE_SIZE messages. The
    // client polls every 3 seconds, so an unbounded thread would cross the wire
    // in full, over and over, on a metered mobile connection. Taking the tail is
    // the right end to keep: it is the part being read and replied to, and
    // older history stays available in the database.
    const rows = db
      .prepare(
        `SELECT id, sender_id, sender_role, body, read_at, created_at
           FROM messages
          WHERE request_id = ?
          ORDER BY created_at DESC, id DESC
          LIMIT ?`
      )
      .all(rid, THREAD_PAGE_SIZE);

    // Re-sorted ascending for rendering: the query takes the newest N (which
    // needs the DESC order to apply the LIMIT), the client wants oldest-first.
    rows.reverse();

    db.prepare(
      `UPDATE messages SET read_at = datetime('now')
        WHERE request_id = ? AND sender_role = ? AND read_at IS NULL`
    ).run(rid, otherRoleOf(role));

    res.json({
      messages: rows,
      role,
      peerUserId: otherUserId,
      // Authoritative from the request row we already hold, so the composer can
      // lock itself the moment the job completes without a second round trip.
      canSend: SENDABLE_STATES.includes(request.status),
      requestStatus: request.status,
    });
  })
);

// ------------------------------------------------------------------
// POST /api/messages/:request_id     { body }
// ------------------------------------------------------------------
router.post(
  '/:request_id',
  messageSendLimiter,
  wrap(async (req, res) => {
    const rid = Number(req.params.request_id);
    const raw = req.body ? req.body.body : null;

    if (typeof raw !== 'string' || raw.trim().length === 0) {
      throw badRequest('الرسالة لا يمكن أن تكون فارغة');
    }
    if (raw.length > MAX_BODY_LENGTH) {
      throw badRequest('الرسالة طويلة جدًا (الحد الأقصى 2000 حرف)');
    }

    const { request, role, otherUserId } = authorizeRequest(req, rid);

    if (!SENDABLE_STATES.includes(request.status)) {
      throw badRequest('لا يمكن إرسال رسائل في هذه المرحلة');
    }

    const body = raw.trim();
    const result = db
      .prepare(
        `INSERT INTO messages (request_id, sender_id, sender_role, body)
         VALUES (?, ?, ?, ?)`
      )
      .run(rid, req.user.id, role, body);

    // created_at is read back from the row instead of being stamped here with
    // toISOString(): the column holds SQLite's 'YYYY-MM-DD HH:MM:SS' and the
    // client formats timestamps with the same convention. Returning an ISO string
    // for a fresh message and a SQLite string for every older one would make the
    // bubble times in a single thread disagree.
    const saved = db
      .prepare(
        'SELECT id, sender_id, sender_role, body, read_at, created_at FROM messages WHERE id = ?'
      )
      .get(result.lastInsertRowid);

    // Push the other party, so a message still lands while they are elsewhere in
    // the app. Fire-and-forget: a failed push must not fail the send.
    if (otherUserId) {
      let senderName = 'الفني';
      if (role === 'customer') {
        senderName = 'العميل';
      } else {
        const tech = db
          .prepare('SELECT name FROM technicians WHERE id = ?')
          .get(request.technician_id);
        if (tech && tech.name) senderName = tech.name;
      }
      notifications.notify(otherUserId, {
        title: '💬 رسالة جديدة',
        // One line only: the preview is truncated by the OS anyway, and the full
        // text is one tap away.
        body: senderName + ': ' + body.slice(0, 50),
        url: role === 'customer' ? '/technician/dashboard.html' : '/customer/my-requests.html',
        tag: 'chat-' + rid,
      });
    }

    res.status(201).json({ message: saved });
  })
);

// ------------------------------------------------------------------
// POST /api/messages/:request_id/read
// Marks the other side's messages read without fetching the whole thread.
// ------------------------------------------------------------------
router.post(
  '/:request_id/read',
  wrap(async (req, res) => {
    const rid = Number(req.params.request_id);
    const { role } = authorizeRequest(req, rid);

    db.prepare(
      `UPDATE messages SET read_at = datetime('now')
        WHERE request_id = ? AND sender_role = ? AND read_at IS NULL`
    ).run(rid, otherRoleOf(role));

    res.json({ ok: true });
  })
);

module.exports = router;
