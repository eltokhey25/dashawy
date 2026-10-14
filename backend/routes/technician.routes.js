/**
 * @file   Technician API
 * @module technician.routes
 *
 * Everything a signed-in technician can do: see assigned jobs, update job status, manage
 * availability, and maintain a profile with portfolio uploads.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Technician routes: assigned jobs, accept / reject / complete, history.
const express = require('express');
const fs = require('fs');
const path = require('path');
const db = require('../db');
const config = require('../config');
const { wrap, AppError, badRequest, forbidden, serviceError } = require('../utils/errors');
const { requireRole } = require('../middleware/auth');
const {
  assignTechnician,
  reassignAfterRejection,
  releaseTechnician,
} = require('../services/assignment');
const broadcast = require('../services/broadcast');
const notifications = require('../services/notifications');
const {
  profileImageUpload,
  portfolioUpload,
  magicBytesGuard,
  UPLOAD_ROOT,
} = require('../middleware/upload');
const { isNonEmptyString } = require('../utils/validation');
const logger = require('../utils/logger');

const router = express.Router();

// Every technician route requires a technician role.
router.use(requireRole('technician'));

function ensureTechnician(user) {
  if (!user.technicianId) throw forbidden('حساب الفني غير مكتمل');
  return user.technicianId;
}

// Status for each typed failure from the payments service. Unlisted codes stay
// 500 on purpose rather than being flattened into a misleading 400.
const PAYMENT_ERROR_STATUS = {
  NOT_FOUND: 404,
  INVALID_STATUS: 400,
  NOT_CASH: 400,
  ALREADY_REPORTED: 409,
  NOT_DISPUTED: 400,
};

// What the client needs to render the confirmation step after a report: the agreed
// amount and commission once both sides are in, the current state machine position
// while waiting, and the technician's debt balance so the number moves visibly.
function settlementView(requestId, technicianId) {
  const r = db
    .prepare(
      `SELECT amount_status, final_amount, customer_reported_amount,
              technician_reported_amount
         FROM requests WHERE id = ?`
    )
    .get(requestId);
  const finalAmount = r && r.final_amount != null ? Number(r.final_amount) : null;
  return {
    status: r ? r.amount_status : null,
    final_amount: finalAmount,
    commission:
      finalAmount != null ? Math.round(finalAmount * config.getCommissionPercent()) / 100 : null,
    customer_amount: r ? r.customer_reported_amount : null,
    technician_amount: r ? r.technician_reported_amount : null,
    total_debt: require('../services/pricing').ensureWallet(technicianId).total_debt,
  };
}

// GET /api/technician/requests
// ?filter=active  -> assigned + in_progress
// ?filter=history -> completed + cancelled + rejected by this technician
router.get(
  '/requests',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const filter = req.query.filter === 'history' ? 'history' : 'active';

    const base = `
      SELECT r.*,
             r.start_pin_attempts, r.start_pin_locked_until, r.started_at,
             c.name AS customer_name, c.phone AS customer_phone,
             c.location_text AS customer_location,
             c.latitude AS customer_latitude, c.longitude AS customer_longitude
      FROM requests r
      JOIN customers c ON c.id = r.customer_id
      WHERE r.technician_id = ?`;

    let rows;
    if (filter === 'active') {
      // A completed job whose cash amount has not been reported yet is still
      // "active" from the technician's side: the commission is unbooked and the
      // customer is waiting on them. Leaving those out would strand the job in
      // history with no way to finish it, since the report button lives here.
      // amount_status IS NULL is the "nobody has reported" case — 'pending' means
      // a report is in and the customer owes an answer, which is no longer the
      // technician's move.
      rows = db
        .prepare(
          `${base} AND (r.status IN ('assigned', 'in_progress')
                        OR (r.status = 'completed' AND r.amount_status IS NULL))
           ORDER BY r.created_at DESC`
        )
        .all(technicianId);
    } else {
      rows = db
        .prepare(`${base} AND r.status IN ('completed', 'cancelled') ORDER BY r.created_at DESC`)
        .all(technicianId);
    }
    // start_pin is r.*'d by the query above, and it must never reach this
    // response: it is the customer's secret for the doorstep handshake, so a
    // technician holding their own answer in the network tab would make the
    // Start PIN feature decorative. Deleted here rather than masked in SQL
    // because r.* and an aliased column produce two columns of the same name,
    // and which one wins would be an implementation detail of the driver —
    // too fragile to hang a privacy guarantee on. The attempt counter and the
    // lock are kept, because the dashboard has to be able to show them.
    rows.forEach((row) => {
      delete row.start_pin;
    });

    res.json({ requests: rows, filter });
  })
);

// --- Broadcast inbox (Uber-style distribution) ---

// GET /api/technician/incoming
// Requests currently offered to this technician. Only 'sent' broadcasts whose
// request is still unassigned are returned, so a request somebody else already
// won disappears from the list without an explicit notification.
//
// The expiry sweep runs first so an offer whose window closed while the tab was in
// the background is gone before it is rendered, rather than showing up as
// accept-able and failing on click.
router.get(
  '/incoming',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    broadcast.sweepIfDue(false);
    broadcast.expireForTechnician(technicianId);
    const requests = broadcast.incomingForTechnician(technicianId);
    res.json({ requests, count: requests.length, timeoutSeconds: broadcast.timeoutSeconds() });
  })
);

// POST /api/technician/broadcasts/:id/accept
// First acceptance wins. A loser gets 409 'تم قبض الطلب من فني آخر'.
router.post(
  '/broadcasts/:id/accept',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const broadcastId = Number(req.params.id);
    if (!Number.isInteger(broadcastId)) throw badRequest('معرّف البث غير صالح');

    const result = broadcast.acceptBroadcast(broadcastId, technicianId);
    if (!result.ok) {
      if (result.code === 404) throw new AppError(result.error, 404);
      if (result.code === 403) throw forbidden(result.error);
      // The service distinguishes "you lost the race" (409) from "this is not a
      // valid action" (400); passing the code through is what lets a client tell
      // "somebody else got it" apart from "you pressed the wrong thing".
      throw new AppError(result.error, result.code);
    }

    res.json({ message: 'تم قبول الطلب بنجاح', request_id: result.request_id });

    // Tell the customer somebody took the job. Fire-and-forget: a failed push
    // must not turn a successful claim into an error the technician sees.
    const customer = db
      .prepare('SELECT user_id FROM customers WHERE id = ?')
      .get(result.customer_id);
    if (customer) {
      notifications.notify(
        customer.user_id,
        notifications.requestAcceptedForCustomer({ technicianName: result.technician_name })
      );
    }
  })
);

// POST /api/technician/broadcasts/:id/decline
// Declines one offer; the request stays live for every other technician.
//
// /reject is the same handler under the name the technician dashboard already
// calls, and stays valid so no frontend change is needed.
router.post(
  ['/broadcasts/:id/decline', '/broadcasts/:id/reject'],
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const broadcastId = Number(req.params.id);
    if (!Number.isInteger(broadcastId)) throw badRequest('معرّف البث غير صالح');

    const result = broadcast.declineBroadcast(broadcastId, technicianId);
    if (!result.ok) {
      if (result.code === 404) throw new AppError(result.error, 404);
      throw new AppError(result.error, result.code);
    }

    res.json({ message: 'تم رفض الطلب' });
  })
);

// POST /api/technician/requests/:id/accept   (assigned -> in_progress)
//
// RETIRED. This used to be the "start job" button, and it is now closed on
// purpose: the customer collects a Start PIN at the door precisely so a job
// cannot be marked started by whoever happens to be assigned it. Leaving this
// open would keep a PIN-free way to start every job, so the only route that can
// reach 'in_progress' is /start below.
//
// It is kept rather than deleted because a dashboard cached by the service
// worker can still be running the old JavaScript; a clear 400 explains the
// change, whereas a 404 would just look like a broken button.
router.post(
  '/requests/:id/accept',
  wrap(async (req, res) => {
    ensureTechnician(req.user);
    throw badRequest('بدء المهمة يتم بالرقم السري — استخدم نقطة /start');
  })
);

// POST /api/technician/requests/:id/start   { pin }
// The Start PIN handshake: the customer reads a 4-digit code off their request
// screen and the technician types it in at the door to move the job to
// 'in_progress'. This is the ONLY route that can start a job.
router.post(
  '/requests/:id/start',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const rid = Number(req.params.id);
    const pin = req.body ? req.body.pin : null;

    if (!/^\d{4}$/.test(String(pin == null ? '' : pin).trim())) {
      throw badRequest('الرقم السري مطلوب (4 أرقام)');
    }

    const request = db
      .prepare('SELECT * FROM requests WHERE id = ? AND technician_id = ?')
      .get(rid, technicianId);
    if (!request) throw new AppError('الطلب غير موجود', 404);
    if (request.status !== 'assigned') {
      throw badRequest('لا يمكن بدء هذا الطلب في حالته الحالية');
    }

    // An older job assigned before this feature existed carries no PIN, so there
    // is nothing the customer could say. Say so plainly instead of comparing
    // their code against NULL and reporting it as wrong.
    if (!request.start_pin) {
      throw badRequest('لا يوجد رقم سري لهذا الطلب — تواصل مع الدعم');
    }

    // Lockout. Both sides of this use ISO-8601 UTC: the value is written with
    // toISOString() and parsed with new Date(string), which understands that
    // format natively. Parsing SQLite's own 'YYYY-MM-DD HH:MM:SS' would need a
    // 'T' and a 'Z' bolted on, and getting that wrong yields an Invalid Date —
    // every comparison against it is false and the lock silently never applies.
    if (request.start_pin_locked_until) {
      const lockedUntil = new Date(String(request.start_pin_locked_until));
      const now = new Date();
      if (!Number.isNaN(lockedUntil.getTime()) && lockedUntil > now) {
        const mins = Math.max(1, Math.ceil((lockedUntil - now) / 60000));
        throw forbidden('تم قفل المحاولات. حاول بعد ' + mins + ' دقيقة');
      }
    }

    // Constant-time compare. A 4-digit PIN has only 10,000 possibilities, so this
    // is not about making brute force hard — it is about not leaking the answer
    // through response timing.
    const expected = String(request.start_pin);
    const given = String(pin).trim();
    let mismatch = expected.length ^ given.length;
    for (let i = 0; i < Math.max(expected.length, given.length); i++) {
      mismatch |= (expected.charCodeAt(i) || 0) ^ (given.charCodeAt(i) || 0);
    }

    if (mismatch !== 0) {
      const attempts = (request.start_pin_attempts || 0) + 1;
      const MAX_ATTEMPTS = 5;
      const LOCK_MINUTES = 15;
      const lockedUntil =
        attempts >= MAX_ATTEMPTS
          ? new Date(Date.now() + LOCK_MINUTES * 60 * 1000).toISOString()
          : null;
      // The counter restarts when the lock is applied, so once the 15 minutes
      // are up the technician gets a fresh set of tries. Carrying the count over
      // would re-lock on the very next mistake, with no way to serve a customer.
      db.prepare(
        `UPDATE requests
            SET start_pin_attempts = ?, start_pin_locked_until = ?, updated_at = datetime('now')
          WHERE id = ?`
      ).run(attempts >= MAX_ATTEMPTS ? 0 : attempts, lockedUntil, rid);

      // Tell them how close they are to being locked out, but never whether the
      // lock has just been applied — the 409 below is the honest channel for that.
      if (lockedUntil) {
        throw forbidden(
          'تم قفل المحاولات لمدة ' + LOCK_MINUTES + ' دقيقة بعد ' + MAX_ATTEMPTS + ' محاولات خاطئة'
        );
      }
      throw badRequest(
        'الرقم السري غير صحيح — باقي ' + (MAX_ATTEMPTS - attempts) + ' محاولات قبل القفل'
      );
    }

    // A spent code is a liability, not evidence: /start already refuses anything that
    // is not 'assigned', so the PIN cannot be replayed — and clearing it means the
    // customer's own API stops echoing a secret they no longer have any use for.
    db.prepare(
      `UPDATE requests
          SET status = 'in_progress',
              started_at = datetime('now'),
              start_pin = NULL,
              start_pin_attempts = 0,
              start_pin_locked_until = NULL,
              updated_at = datetime('now')
        WHERE id = ? AND status = 'assigned'`
    ).run(rid);

    // Tell the customer the technician is on site and working.
    const customer = db
      .prepare('SELECT user_id FROM customers WHERE id = ?')
      .get(request.customer_id);
    if (customer) {
      // The requests row has no technician_name on it; read it rather than
      // sending an unnamed "الفني".
      const tech = db.prepare('SELECT name FROM technicians WHERE id = ?').get(technicianId);
      notifications.notify(customer.user_id, {
        title: '🚀 بدأت المهمة',
        body: (tech ? tech.name : 'الفني') + ' بدأ العمل الآن',
        url: '/customer/my-requests.html',
        tag: 'started-' + rid,
      });
    }

    const startedRow = db.prepare('SELECT started_at FROM requests WHERE id = ?').get(rid);
    res.json({
      message: 'بدأت المهمة بنجاح',
      status: 'in_progress',
      started_at: startedRow ? startedRow.started_at : null,
    });
  })
);

// POST /api/technician/requests/:id/reject
// Frees the technician and automatically reassigns to the next best match.
router.post(
  '/requests/:id/reject',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const request = db
      .prepare('SELECT * FROM requests WHERE id = ? AND technician_id = ?')
      .get(Number(req.params.id), technicianId);
    if (!request) throw new AppError('الطلب غير موجود', 404);
    if (request.status !== 'assigned') throw badRequest('لا يمكن رفض هذا الطلب في حالته الحالية');

    // Reset to pending, then attempt reassignment.
    db.prepare(
      `UPDATE requests
       SET technician_id = NULL, status = 'pending',
           assigned_at = NULL, updated_at = datetime('now')
       WHERE id = ?`
    ).run(request.id);

    const next = reassignAfterRejection(request.id, technicianId);

    // Rejecting frees this technician just as completing does, so the same offer goes
    // out — only when there is nothing else for them to take first. Swallowed for the
    // same reason as on /complete: a pickup failure is never the reject's failure.
    let pickup = { offered: 0, request_id: null };
    if (!next.assigned) {
      try {
        pickup = require('../services/auto-pickup').forTechnician(technicianId);
      } catch (e) {
        logger.info('technician', '[AUTO-PICKUP] rejection pickup failed:', e.message);
      }
    }

    res.json({
      message: next.assigned
        ? 'تم رفض الطلب وإعادة تعيينه لفني آخر'
        : 'تم رفض الطلب، ولا يوجد فني متاح حالياً',
      reassigned: next.assigned,
      pickup,
      // Null rather than absent, so the client can read nextTechnician.id without a
      // guard on whether the key exists.
      nextTechnician: next.assigned
        ? {
            id: next.technicianId,
            name: next.technician.name,
            phone: next.technician.phone,
          }
        : null,
    });
  })
);

// POST /api/technician/requests/:id/complete  (in_progress -> completed)
router.post(
  '/requests/:id/complete',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const request = db
      .prepare('SELECT * FROM requests WHERE id = ? AND technician_id = ?')
      .get(Number(req.params.id), technicianId);
    if (!request) throw new AppError('الطلب غير موجود', 404);
    if (request.status !== 'in_progress') throw badRequest('فقط طلبات العمل الجارية يمكن إكمالها');

    // A completion without a description of the work is worthless to the
    // customer and to an admin settling the payment later, so it is required
    // rather than optional. Capped at 2000 chars to bound the column.
    const workNotes =
      req.body && req.body.work_notes ? String(req.body.work_notes).slice(0, 2000) : null;
    if (!workNotes || workNotes.trim().length < 5) {
      throw badRequest('يرجى كتابة ما تم إنجازه (5 أحرف على الأقل)');
    }

    const run = db.transaction(() => {
      db.prepare(
        `UPDATE requests
         SET status = 'completed', completed_at = datetime('now'),
             completed_by_technician_at = datetime('now'),
             work_notes = ?, updated_at = datetime('now')
         WHERE id = ?`
      ).run(workNotes.trim(), request.id);
      releaseTechnician(technicianId);
    });
    run();

    // This technician is free again, so the oldest request nobody could be found for
    // goes to their inbox right now rather than on the next queue tick. An OFFER,
    // never an assignment: they accept it like any other job, and the first
    // acceptance still wins. Wrapped because a pickup that fails must not turn a
    // finished job into an error the technician sees — the sweep would retry anyway.
    let pickup = { offered: 0, request_id: null };
    try {
      pickup = require('../services/auto-pickup').forTechnician(technicianId);
    } catch (e) {
      logger.info('technician', '[AUTO-PICKUP] completion pickup failed:', e.message);
    }

    res.json({
      message: 'تم إكمال المهمة بنجاح',
      status: 'completed',
      // Tells the dashboard whether to mention that something is waiting, so the
      // technician is not left wondering why a job appeared with no explanation.
      pickup,
    });

    // The rating is MANDATORY before this customer can carry on using the app,
    // so the completion notification is the prompt that starts it. It quotes the
    // technician's own declared figure — the customer's half of the cash
    // confirmation is still outstanding, and they see their two numbers side by
    // side inside the review.
    const customer = db
      .prepare('SELECT user_id FROM customers WHERE id = ?')
      .get(request.customer_id);
    if (customer) {
      const declared = request.technician_reported_amount || request.final_amount || 0;
      const amount = declared > 0 ? Math.round(Number(declared)) : null;
      // 'requests' has no technician name on it, so read it rather than sending
      // an unnamed "الفني" when the real name is one lookup away.
      const tech = db.prepare('SELECT name FROM technicians WHERE id = ?').get(technicianId);
      notifications.notify(
        customer.user_id,
        notifications.requestCompletedForCustomer({
          technicianName: tech ? tech.name : null,
          amount,
          requestId: request.id,
        })
      );
    }
  })
);

// GET /api/technician/wallet
// The whole wallet in one call: both pots, the running totals, the 50 most recent
// ledger entries, and the three settings a withdrawal needs.
//
// `balance` (what they can withdraw) and `total_debt` (what they owe) are separate
// non-negative figures, so the UI never has to infer one from the sign of the
// other. The debt ceiling and the withdrawal minimum are returned alongside them
// so the screen can show what is left before they are taken off the broadcast, and
// the smallest amount they could ask to withdraw, rather than only ever reporting
// "no" / "unavailable".
router.get(
  '/wallet',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const pricing = require('../services/pricing');
    const wallet = pricing.ensureWallet(technicianId);
    const recentTx = db
      .prepare(
        'SELECT * FROM wallet_transactions WHERE technician_id = ? ORDER BY created_at DESC, id DESC LIMIT 50'
      )
      .all(technicianId);
    const settingsRow = db
      .prepare('SELECT value FROM settings WHERE key = ?')
      .get('wallet_debt_limit');
    const limit = parseFloat(settingsRow && settingsRow.value) || 300;
    const withdrawal = pricing.withdrawalSettings();

    res.json({
      wallet: {
        technician_id: wallet.technician_id,
        balance: Number(wallet.balance || 0),
        total_debt: Number(wallet.total_debt || 0),
        total_earned: Number(wallet.total_earned || 0),
        total_owed: Number(wallet.total_owed || 0),
        total_paid_to_platform: Number(wallet.total_paid_to_platform || 0),
        total_withdrawn: Number(wallet.total_withdrawn || 0),
        last_updated: wallet.last_updated,
      },
      transactions: recentTx,
      debtLimit: limit,
      minWithdrawal: withdrawal.min,
      withdrawalEtaHours: withdrawal.etaHours,
      canReceiveRequests: pricing.canReceiveRequests(technicianId),
      pendingWithdrawal: pricing.pendingWithdrawal(technicianId),
    });
  })
);

// GET /api/technician/withdrawals
// The technician's own withdrawal history, including rejected ones.
router.get(
  '/withdrawals',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const pricing = require('../services/pricing');
    res.json({
      withdrawals: pricing.listWithdrawals(technicianId, 30),
      minWithdrawal: pricing.withdrawalSettings().min,
    });
  })
);

// POST /api/technician/withdrawals   { amount }
// Queues a withdrawal for an admin to approve.
//
// This does NOT touch the balance: the money is only debited when an admin marks
// the request paid, because that is when it has actually left the platform.
router.post(
  '/withdrawals',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const amt = Number((req.body || {}).amount);
    if (isNaN(amt) || amt <= 0) throw badRequest('مبلغ غير صالح');

    const pricing = require('../services/pricing');
    try {
      const request = pricing.requestWithdrawal(technicianId, amt);
      res.status(201).json({
        message:
          'تم استلام طلب السحب. سيتم التحويل خلال ' +
          pricing.withdrawalSettings().etaHours +
          ' ساعة.',
        request,
      });
    } catch (e) {
      throw serviceError(e, WALLET_ERROR_STATUS);
    }
  })
);

// POST /api/technician/requests/:id/report-payment   { amount }
//
// The technician's half of the two-sided cash confirmation. The customer paid THEM
// in cash, so the platform never saw the money: this figure is self-reported, and
// an under-report quietly undercuts the commission. The customer confirms the same
// amount from their side and the service compares the two — a match books the
// commission, a mismatch freezes the request for an admin.
//
// This replaces the single-sided /cash-received, which booked the commission from
// the technician's word alone.
router.post(
  '/requests/:id/report-payment',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const rid = Number(req.params.id);
    const amt = Number((req.body || {}).amount);
    if (isNaN(amt) || amt <= 0 || amt > 100000) throw badRequest('المبلغ غير صالح');

    let result;
    try {
      result = require('../services/payments').technicianReport(rid, technicianId, amt);
    } catch (e) {
      throw serviceError(e, PAYMENT_ERROR_STATUS);
    }

    // The service's own status/message/waiting_on come first; the re-read of the
    // row is authoritative for the figures, so it wins where they overlap.
    res.json({ ok: true, ...result, ...settlementView(rid, technicianId) });
  })
);

// Status for each typed failure from the wallet service. Anything not listed here
// stays a 500 on purpose — see serviceError().
const WALLET_ERROR_STATUS = {
  MIN_WITHDRAWAL: 400,
  INSUFFICIENT_BALANCE: 400,
  INVALID_AMOUNT: 400,
  INVALID_STATUS: 400,
  PENDING_EXISTS: 409,
  ALREADY_PAID: 409,
  NOT_FOUND: 404,
};

// GET /api/technician/profile
router.get(
  '/profile',
  wrap(async (req, res) => {
    const technician = db
      .prepare('SELECT * FROM technicians WHERE id = ?')
      .get(req.user.technicianId);
    if (!technician) throw new AppError('حساب الفني غير موجود', 404);
    res.json({ technician });
  })
);

// --- Profile details, portfolio gallery and review replies (Phase 1) ---

// Public URL prefixes. These must stay in sync with the express.static mounts
// in app.js so the stored paths can be used directly by <img src>.
const PROFILE_DIR = path.join(UPLOAD_ROOT, 'profiles');
const PROFILE_URL_BASE = '/uploads/technicians/profiles';
const PORTFOLIO_DIR = path.join(UPLOAD_ROOT, 'portfolio');
const PORTFOLIO_URL_BASE = '/uploads/technicians/portfolio';

const MAX_PORTFOLIO_IMAGES = 5;
const MAX_BIO_LENGTH = 1000;
const MAX_REPLY_LENGTH = 1000;

// The profile picture and the portfolio gallery are images only. The type is
// confirmed from the file's magic bytes, not the client-supplied Content-Type.
const imagesOnlyGuard = magicBytesGuard({
  allowedMimes: ['image/jpeg', 'image/png', 'image/webp'],
});

function techFolder(baseDir, technicianId) {
  const dir = path.join(baseDir, 'tech-' + technicianId);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

// Moves a staged upload into the technician folder and returns its public path.
function publishUpload(file, baseDir, urlBase, technicianId) {
  const dest = path.join(techFolder(baseDir, technicianId), path.basename(file.path));
  fs.renameSync(file.path, dest);
  return urlBase + '/tech-' + technicianId + '/' + path.basename(dest);
}

// Deletes a stored image. The stored value is a public URL, so it is matched
// against the expected urlBase + technician folder prefix and the filename is
// re-derived with path.basename. A tampered DB row therefore cannot make this
// unlink anything outside the technician's own folder.
function deleteStoredImage(storedPath, baseDir, urlBase, technicianId) {
  if (typeof storedPath !== 'string' || !storedPath) return;
  const prefix = urlBase + '/tech-' + technicianId + '/';
  if (!storedPath.startsWith(prefix)) return;
  const dir = path.join(baseDir, 'tech-' + technicianId);
  const target = path.join(dir, path.basename(storedPath));
  if (path.dirname(target) !== dir) return;
  try {
    fs.unlinkSync(target);
  } catch (err) {
    // A missing file must not fail the request; the DB row is still removed.
    if (err.code !== 'ENOENT')
      logger.info('tech', `[technician] failed to delete image:`, err.message);
  }
}

// Aggregated rating / workload numbers shown on the profile screens.
function profileStats(technicianId) {
  const reviews = db
    .prepare(
      `SELECT COUNT(*) AS review_count, AVG(rating) AS avg_rating
       FROM reviews WHERE technician_id = ?`
    )
    .get(technicianId);
  const completed = db
    .prepare("SELECT COUNT(*) AS n FROM requests WHERE technician_id = ? AND status = 'completed'")
    .get(technicianId);
  return {
    review_count: reviews.review_count || 0,
    avg_rating: reviews.avg_rating ? Math.round(reviews.avg_rating * 10) / 10 : null,
    completed_count: completed.n || 0,
  };
}

const REVIEWS_SELECT = `
  SELECT rv.*, c.name AS customer_name
  FROM reviews rv
  JOIN customers c ON c.id = rv.customer_id
`;

// GET /api/technician/profile-details
// Everything the technician profile screen needs in a single round trip.
router.get(
  '/profile-details',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);

    // Explicit column list: document paths and internal approval notes are not
    // part of the profile payload.
    const technician = db
      .prepare(
        `SELECT id, name, phone, specialty, location_text, latitude, longitude,
                availability, approval_status, approved_at, created_at
         FROM technicians WHERE id = ?`
      )
      .get(technicianId);
    if (!technician) throw new AppError('حساب الفني غير موجود', 404);

    const profile =
      db.prepare('SELECT * FROM technician_profiles WHERE technician_id = ?').get(technicianId) ||
      null;

    const portfolio = db
      .prepare(
        'SELECT * FROM technician_portfolio WHERE technician_id = ? ORDER BY created_at DESC, id DESC'
      )
      .all(technicianId);

    const recentReviews = db
      .prepare(
        `${REVIEWS_SELECT} WHERE rv.technician_id = ? ORDER BY rv.created_at DESC, rv.id DESC LIMIT 20`
      )
      .all(technicianId);

    res.json({
      technician,
      profile,
      portfolio,
      stats: profileStats(technicianId),
      recentReviews,
    });
  })
);

// PUT /api/technician/profile-details   { bio?, years_experience? }
// Partial update: omitted fields keep their current value.
router.put(
  '/profile-details',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const { bio, years_experience } = req.body || {};

    if (bio !== undefined && !isNonEmptyString(bio, MAX_BIO_LENGTH)) {
      throw badRequest('النبذة التعريفية غير صالحة');
    }
    let years = null;
    if (years_experience !== undefined) {
      years = Number(years_experience);
      if (!Number.isInteger(years) || years < 0 || years > 60) {
        throw badRequest('عدد سنوات الخبرة غير صالح');
      }
    }
    if (bio === undefined && years === undefined) {
      throw badRequest('لا يوجد بيانات للتحديث');
    }

    db.prepare(
      `INSERT INTO technician_profiles (technician_id, bio, years_experience, updated_at)
       VALUES (?, ?, ?, datetime('now'))
       ON CONFLICT(technician_id) DO UPDATE SET
         bio              = COALESCE(excluded.bio, technician_profiles.bio),
         years_experience = COALESCE(excluded.years_experience, technician_profiles.years_experience),
         updated_at       = datetime('now')`
    ).run(technicianId, bio === undefined ? null : bio.trim(), years);

    const profile = db
      .prepare('SELECT * FROM technician_profiles WHERE technician_id = ?')
      .get(technicianId);
    res.json({ message: 'تم تحديث البيانات', profile });
  })
);

// POST /api/technician/profile-image   (multipart: profile_image)
router.post(
  '/profile-image',
  profileImageUpload,
  // Runs after multer has written the file, before anything touches the DB.
  imagesOnlyGuard,
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    if (!req.file) throw badRequest('لم يتم إرفاق صورة');

    // Read the current image before overwriting the row, so the file this
    // upload replaces can be removed afterwards.
    const previous = db
      .prepare('SELECT profile_image_path FROM technician_profiles WHERE technician_id = ?')
      .get(technicianId);

    const imagePath = publishUpload(req.file, PROFILE_DIR, PROFILE_URL_BASE, technicianId);

    db.prepare(
      `INSERT INTO technician_profiles (technician_id, profile_image_path, updated_at)
       VALUES (?, ?, datetime('now'))
       ON CONFLICT(technician_id) DO UPDATE SET
         profile_image_path = excluded.profile_image_path,
         updated_at         = datetime('now')`
    ).run(technicianId, imagePath);

    // A profile picture is replaced, not accumulated: without this every upload
    // would leave the previous file on disk forever.
    if (previous && previous.profile_image_path && previous.profile_image_path !== imagePath) {
      deleteStoredImage(previous.profile_image_path, PROFILE_DIR, PROFILE_URL_BASE, technicianId);
    }

    const profile = db
      .prepare('SELECT * FROM technician_profiles WHERE technician_id = ?')
      .get(technicianId);
    res.json({ message: 'تم تحديث الصورة الشخصية', profile });
  })
);

// POST /api/technician/portfolio   (multipart: portfolio_images[], caption?)
// At most MAX_PORTFOLIO_IMAGES images are kept per technician.
router.post(
  '/portfolio',
  portfolioUpload,
  // Every image in the batch is checked before the first one is published.
  imagesOnlyGuard,
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const files = req.files || [];
    if (files.length === 0) throw badRequest('لم يتم إرفاق أي صور');

    const caption =
      req.body && req.body.caption !== undefined ? String(req.body.caption).trim() : null;
    if (caption && caption.length > 300) throw badRequest('الوصف طويل جداً');

    const existing = db
      .prepare('SELECT COUNT(*) AS n FROM technician_portfolio WHERE technician_id = ?')
      .get(technicianId).n;

    // Drop the oldest images that would push the gallery over the limit.
    const overflow = existing + files.length - MAX_PORTFOLIO_IMAGES;
    if (overflow > 0) {
      const oldest = db
        .prepare(
          'SELECT * FROM technician_portfolio WHERE technician_id = ? ORDER BY created_at ASC, id ASC LIMIT ?'
        )
        .all(technicianId, overflow);
      const remove = db.prepare('DELETE FROM technician_portfolio WHERE id = ?');
      oldest.forEach((row) => {
        deleteStoredImage(row.image_path, PORTFOLIO_DIR, PORTFOLIO_URL_BASE, technicianId);
        remove.run(row.id);
      });
    }

    const insert = db.prepare(
      'INSERT INTO technician_portfolio (technician_id, image_path, caption) VALUES (?, ?, ?)'
    );
    const added = files.map((file) => {
      const imagePath = publishUpload(file, PORTFOLIO_DIR, PORTFOLIO_URL_BASE, technicianId);
      const result = insert.run(technicianId, imagePath, caption || null);
      return { id: result.lastInsertRowid, image_path: imagePath, caption: caption || null };
    });

    const portfolio = db
      .prepare(
        'SELECT * FROM technician_portfolio WHERE technician_id = ? ORDER BY created_at DESC, id DESC'
      )
      .all(technicianId);

    res.status(201).json({ message: 'تم إضافة الصور إلى معرض الأعمال', added, portfolio });
  })
);

// DELETE /api/technician/portfolio/:id
router.delete(
  '/portfolio/:id',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const row = db
      .prepare('SELECT * FROM technician_portfolio WHERE id = ? AND technician_id = ?')
      .get(Number(req.params.id), technicianId);
    if (!row) throw new AppError('الصورة غير موجودة', 404);

    deleteStoredImage(row.image_path, PORTFOLIO_DIR, PORTFOLIO_URL_BASE, technicianId);
    db.prepare('DELETE FROM technician_portfolio WHERE id = ?').run(row.id);

    res.json({ message: 'تم حذف الصورة' });
  })
);

// POST /api/technician/reviews/:id/reply   { reply }
// Only the reviewed technician can reply, and only once.
router.post(
  '/reviews/:id/reply',
  wrap(async (req, res) => {
    const technicianId = ensureTechnician(req.user);
    const { reply } = req.body || {};
    if (!isNonEmptyString(reply, MAX_REPLY_LENGTH)) {
      throw badRequest('الرد غير صالح');
    }

    const review = db
      .prepare('SELECT * FROM reviews WHERE id = ? AND technician_id = ?')
      .get(Number(req.params.id), technicianId);
    if (!review) throw new AppError('التقييم غير موجود', 404);
    if (review.technician_reply) throw badRequest('تم الرد على هذا التقييم بالفعل');

    db.prepare(
      `UPDATE reviews SET technician_reply = ?, replied_at = datetime('now') WHERE id = ?`
    ).run(reply.trim(), review.id);

    const updated = db.prepare('SELECT * FROM reviews WHERE id = ?').get(review.id);
    res.json({ message: 'تم إضافة الرد', review: updated });
  })
);

module.exports = router;
