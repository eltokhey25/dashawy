/**
 * @file   Customer API
 * @module customer.routes
 *
 * Everything a signed-in customer can do: create and track requests, browse technicians,
 * manage their profile, and review completed work.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Customer routes: create requests, list/track own requests, cancel.
const express = require('express');
const db = require('../db');
const config = require('../config');
const { wrap, AppError, badRequest, forbidden, serviceError } = require('../utils/errors');
const { requireRole, requireCompleteProfile } = require('../middleware/auth');
const { isNonEmptyString, isValidSpecialty, parseLatLng } = require('../utils/validation');
const { reassignAfterRejection } = require('../services/assignment');
const broadcast = require('../services/broadcast');
const pricing = require('../services/pricing');

const router = express.Router();

// Every customer route requires a customer role.
router.use(requireRole('customer'));

// ...and a finished profile. Mounted over the whole router rather than on the
// routes that happen to need a phone, so a customer who signed in with Google
// and has not claimed a number yet is refused everywhere by construction —
// including any route added later. Without a phone there is nobody to call
// about an urgent repair, which is the entire point of the request.
router.use(requireCompleteProfile);

// Status for each typed failure from the payments service. Unlisted codes stay 500
// on purpose rather than being flattened into a misleading 400.
const PAYMENT_ERROR_STATUS = {
  NOT_FOUND: 404,
  INVALID_STATUS: 400,
  NOT_CASH: 400,
  ALREADY_REPORTED: 409,
  NOT_DISPUTED: 400,
};

const REQUEST_SELECT = `
  SELECT r.*,
         c.name AS customer_name,
         c.phone AS customer_phone,
         c.location_text AS customer_location,
         c.latitude AS customer_latitude,
         c.longitude AS customer_longitude,
         t.name  AS technician_name,
         t.phone AS technician_phone,
         t.specialty AS technician_specialty,
         t.location_text AS technician_location
  FROM requests r
  JOIN customers c ON c.id = r.customer_id
  LEFT JOIN technicians t ON t.id = r.technician_id
`;

function ensureCustomerHasProfile(user) {
  if (!user.customerId) throw forbidden('حساب العميل غير مكتمل');
}

// POST /api/customer/requests  (create a request, then broadcast it to technicians)
router.post(
  '/requests',
  wrap(async (req, res) => {
    ensureCustomerHasProfile(req.user);
    const { area_key, location_text } = req.body || {};

    let resolvedLocation = (location_text || '').trim();
    if (area_key !== undefined && area_key !== '') {
      const area = config.ALEXANDRIA_AREAS.find((a) => a.key === area_key);
      if (!area) throw badRequest('المنطقة غير صالحة');
      // Keep the customer's free-text address: the area label is a prefix, not
      // a replacement, so the technician still gets street/building/floor.
      resolvedLocation = resolvedLocation ? area.label + ' — ' + resolvedLocation : area.label;
    }

    const { specialty, custom_specialty_text, description, latitude, longitude } = req.body || {};

    if (!isValidSpecialty(specialty)) throw badRequest('نوع الخدمة غير صالح');
    if (specialty === 'other') {
      if (!isNonEmptyString(custom_specialty_text, 200)) {
        throw badRequest('يرجى كتابة اسم الخدمة المطلوبة');
      }
    }
    if (!isNonEmptyString(description, 2000)) {
      throw badRequest('يرجى وصف المشكلة (حرف واحد على الأقل)');
    }
    if (location_text !== undefined && !isNonEmptyString(location_text, 300)) {
      throw badRequest('وصف الموقع غير صالح');
    }
    const coords = parseLatLng(latitude, longitude);
    if (
      (latitude !== undefined && latitude !== '') ||
      (longitude !== undefined && longitude !== '')
    ) {
      if (!coords) throw badRequest('إحداثيات الموقع غير صالحة');
    }

    // NOTE: the legacy "customer picked a technician" path (technician_id in the
    // body) was removed with the auto-assign flow. Preferred-technician requests
    // are no longer honoured — every request now goes to the broadcast pool.

    const insert = db.prepare(
      `INSERT INTO requests (customer_id, specialty, custom_specialty_text, description, location_text, latitude, longitude)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    );
    const customText = specialty === 'other' ? custom_specialty_text.trim() : null;
    const result = insert.run(
      req.user.customerId,
      specialty,
      customText,
      description.trim(),
      resolvedLocation,
      coords ? coords.latitude : null,
      coords ? coords.longitude : null
    );

    const requestId = result.lastInsertRowid;

    // Price the request, then offer it to every eligible technician at once.
    // Nothing is assigned here: the request stays 'pending' with a NULL
    // technician_id until somebody accepts, which is what makes the first
    // acceptance the winner.
    pricing.applyPricingToRequest(requestId);
    const broadcastResult = broadcast.broadcastRequest(requestId);

    // Nobody was available, so the request waits instead of dying: the queue
    // re-offers it every QUEUE_RECHECK_MINUTES to technicians who become available,
    // and cancels it with a reason at QUEUE_MAX_HOURS.
    let queued = false;
    if (broadcastResult.count === 0) {
      queued = require('../services/queue').markQueued(requestId);
    }

    const request = db.prepare(`${REQUEST_SELECT} WHERE r.id = ?`).get(requestId);

    // Tell every technician who was actually offered this job. Keyed off the
    // broadcast's own technician_ids rather than the eligible list, so a
    // notification is never sent to somebody the offer never reached.
    if (broadcastResult.count > 0 && broadcastResult.technician_ids.length > 0) {
      const placeholders = broadcastResult.technician_ids.map(() => '?').join(',');
      const notified = db
        .prepare(`SELECT user_id FROM technicians WHERE id IN (${placeholders})`)
        .all(...broadcastResult.technician_ids);
      const push = require('../services/notifications');
      const payload = push.newRequestForTechnician({
        specialty,
        customText: customText,
        areaLabel: area_key
          ? (config.ALEXANDRIA_AREAS.find((a) => a.key === area_key) || {}).label
          : null,
      });
      notified.forEach((t) => push.notify(t.user_id, payload));
    }

    // The customer-facing answer to "did somebody take this yet?".
    //
    // Read from the row rather than assumed, so it cannot claim an assignment that
    // did not happen. `assigned: false` is NOT a failure: the request is created,
    // priced, on the waiting queue and reachable by services/auto-pickup.js the
    // moment a technician frees up. The wording is deliberately "received, we will
    // be in touch" rather than "nobody is available", because that is the truth from
    // the customer's side and the second reads as a failure to something that has
    // only just been submitted.
    const assigned = request.status === 'assigned' && Boolean(request.technician_id);
    const assignment = assigned
      ? {
          assigned: true,
          technician_id: request.technician_id,
          technician_name: request.technician_name,
          message: 'تم تعيين فني لطلبك، هيوصلك قريب!',
        }
      : {
          assigned: false,
          reason: queued ? 'no_technician_available' : 'awaiting_technician',
          technician_id: null,
          message: 'تم استلام طلبك بنجاح وسيتم التواصل معاك في أقرب وقت',
        };

    res.status(201).json({
      message: assigned
        ? assignment.message
        : broadcastResult.count > 0
          ? 'تم إرسال طلبك إلى ' + broadcastResult.count + ' فني. في انتظار القبول...'
          : queued
            ? 'تم استلام طلبك بنجاح وسيتم التواصل معاك في أقرب وقت'
            : 'تم استلام طلبك. سيتم إخطار الفنيين المتاحين.',
      request,
      assignment,
      // Kept as a top-level flag: the cached client bundle from before `assignment`
      // existed still reads this one, and a removed field would read as `undefined`
      // (falsy) — i.e. as "not assigned", which happens to be right today but for the
      // wrong reason.
      assigned,
      queued,
      broadcast: {
        sent_to: broadcastResult.count,
        technician_ids: broadcastResult.technician_ids || [],
        reason: broadcastResult.reason || null,
      },
    });
  })
);

// GET /api/customer/requests  (all requests for the current customer)
router.get(
  '/requests',
  wrap(async (req, res) => {
    ensureCustomerHasProfile(req.user);
    const requests = db
      .prepare(`${REQUEST_SELECT} WHERE r.customer_id = ? ORDER BY r.created_at DESC`)
      .all(req.user.customerId);
    res.json({ requests });
  })
);

// GET /api/customer/requests/pending-actions
//
// The gate that backs the mandatory review modal: every completed request this
// customer has not rated yet. Deliberately keyed on "no review row" rather than
// on any review state field, so it cannot drift out of step with the one-review-
// per-request rule the POST endpoint enforces — the same query both the modal and
// the can-review check would have to agree on.
//
// Read by force-review.js on load, on focus and every 30s, so it must stay cheap
// and it must NEVER be allowed to fail loudly: a customer cannot be blocked by
// an error here.
router.get(
  '/requests/pending-actions',
  wrap(async (req, res) => {
    ensureCustomerHasProfile(req.user);

    const rows = db
      .prepare(
        `SELECT r.id, r.specialty, r.custom_specialty_text, r.status, r.amount_status,
                r.technician_reported_amount, r.customer_reported_amount, r.final_amount,
                r.work_notes, r.completed_at, r.location_text,
                t.name AS technician_name, t.phone AS technician_phone,
                (SELECT COUNT(*) FROM reviews WHERE request_id = r.id) AS has_review
           FROM requests r
           LEFT JOIN technicians t ON t.id = r.technician_id
          WHERE r.customer_id = ?
            AND r.status = 'completed'
            AND r.technician_id IS NOT NULL
            AND (SELECT COUNT(*) FROM reviews WHERE request_id = r.id) = 0
          ORDER BY r.completed_at DESC, r.id DESC`
      )
      .all(req.user.customerId);

    // The technician's phone is only used to let the customer reach them; the
    // modal does not render it, so leave it out of the response rather than
    // hand the frontend a field nothing reads.
    res.json({
      requests: rows.map((r) => {
        // Send the human label, not the key: the modal renders this straight into
        // the DOM, and a customer must never be shown "dish_repair". 'other'
        // carries the customer's own wording instead.
        const specialtyMeta = config.SPECIALTIES.find((s) => s.key === r.specialty);
        const specialtyLabel =
          r.specialty === 'other' && r.custom_specialty_text
            ? r.custom_specialty_text
            : specialtyMeta
              ? specialtyMeta.label
              : r.specialty;

        return {
          id: r.id,
          specialty: r.specialty,
          specialty_label: specialtyLabel,
          status: r.status,
          amount_status: r.amount_status,
          technician_reported_amount: r.technician_reported_amount,
          customer_reported_amount: r.customer_reported_amount,
          final_amount: r.final_amount,
          work_notes: r.work_notes,
          completed_at: r.completed_at,
          location_text: r.location_text,
          technician_name: r.technician_name,
          has_review: r.has_review,
        };
      }),
      count: rows.length,
    });
  })
);

// GET /api/customer/requests/:id  (a single request, owned by the customer)
router.get(
  '/requests/:id',
  wrap(async (req, res) => {
    const request = db
      .prepare(`${REQUEST_SELECT} WHERE r.id = ? AND r.customer_id = ?`)
      .get(Number(req.params.id), req.user.customerId);
    if (!request) throw new AppError('الطلب غير موجود', 404);

    // r.* in REQUEST_SELECT already carries start_pin / start_pin_attempts /
    // start_pin_locked_until / started_at, which is correct: this response goes
    // only to the customer who owns the request, and the PIN is the whole point.
    // The technician-facing endpoints deliberately do NOT select it.
    //
    // The profile link is built here rather than in the frontend so the path can
    // change in one place, and so the customer is handed a link instead of an id
    // it has to assemble.
    request.technician_profile_url = request.technician_id
      ? '/technician-profile.html?id=' + request.technician_id
      : null;

    res.json({ request });
  })
);

// GET /api/customer/requests/:id/payment-info
// Everything the customer needs to know about paying: what the service costs, the
// split between platform and technician, and who is doing the work. Scoped to
// r.customer_id so one customer cannot read another's payment details.
//
// CASH ONLY. The platform has no payment gateway yet, so the InstaPay destination
// this endpoint used to expose (instapay_number / instapay_name) is gone — there
// is nowhere to send money, so advertising a number would only strand customers.
// A cash job is settled by the two-sided confirmation in services/payments.js: the
// technician reports what they took, the customer confirms what they paid, and the
// platform's commission is taken from the agreed figure.
//
// A request whose payment_method is still 'instapay' is a pre-gateway record; the
// fields are left in the response so old rows still render rather than going blank.
router.get(
  '/requests/:id/payment-info',
  wrap(async (req, res) => {
    const rid = Number(req.params.id);
    const r = db
      .prepare(
        `SELECT r.id, r.specialty, r.service_price, r.commission_percent,
                r.commission_amount, r.technician_earning, r.payment_method,
                r.payment_status, r.payment_reference, r.work_notes,
                r.completed_at, r.technician_id, r.amount_status,
                r.technician_reported_amount, r.customer_reported_amount,
                r.final_amount,
                t.name AS technician_name
         FROM requests r
         LEFT JOIN technicians t ON t.id = r.technician_id
         WHERE r.id = ? AND r.customer_id = ?`
      )
      .get(rid, req.user.customerId);
    if (!r) throw new AppError('الطلب غير موجود', 404);

    res.json({
      request: r,
      paymentSettings: {
        payment_methods: 'cash',
        cash_instructions: 'يُدفع المبلغ نقداً للفني عند إتمام الخدمة.',
      },
    });
  })
);

// POST /api/customer/requests/:id/pay — body: { method, reference? }
//
// The customer declaring "I have paid". It only records the CLAIM: the row moves
// to 'pending_verification' and lands in the admin settlement queue, which is what
// actually moves money in the wallet. A customer must therefore never be able to
// mark their own request 'confirmed' — that would credit their technician without
// anyone checking that the money arrived.
//
// Cash needs no reference (the technician holds it and the admin reconciles by
// hand), so an InstaPay transfer number is only meaningful for that method.
router.post(
  '/requests/:id/pay',
  wrap(async (req, res) => {
    ensureCustomerHasProfile(req.user);
    const rid = Number(req.params.id);
    const { method, reference } = req.body || {};

    const request = db
      .prepare('SELECT * FROM requests WHERE id = ? AND customer_id = ?')
      .get(rid, req.user.customerId);
    if (!request) throw new AppError('الطلب غير موجود', 404);
    if (request.status !== 'completed') {
      throw badRequest('الدفع متاح بعد إكمال الطلب فقط');
    }
    if (request.payment_status === 'confirmed') throw badRequest('تم تأكيد دفع هذا الطلب بالفعل');
    if (request.payment_status === 'refunded') throw badRequest('تم استرداد هذا الطلب');

    // CASH ONLY. The platform has no payment gateway, so instapay is not a method a
    // customer can declare: accepting it would let them mark a request as paid to a
    // destination that cannot receive anything, and the admin settlement queue would
    // then credit a technician against money that never arrived. A cash job is
    // settled through the two-sided confirmation instead (POST /report-payment).
    //
    // A legacy request already marked instapay can still be settled by an admin, so
    // this refuses the declaration rather than orphaning records that exist.
    const enabled = String(
      db.prepare('SELECT value FROM settings WHERE key = ?').get('payment_methods')?.value || ''
    )
      .split(',')
      .map((m) => m.trim())
      .filter((m) => m && m !== 'instapay');
    const allowed = enabled.length ? enabled : ['cash'];
    if (!allowed.includes(method)) throw badRequest('طريقة الدفع غير متاحة');

    const ref = reference === undefined || reference === null ? '' : String(reference).trim();
    if (ref.length > 120) throw badRequest('رقم التحويل طويل جداً');

    // Re-submitting while already pending simply updates the reference, which is
    // what a customer does when they mistyped it.
    db.prepare(
      `UPDATE requests
          SET payment_status = 'pending_verification', payment_method = ?,
              payment_reference = ?, updated_at = datetime('now')
        WHERE id = ?`
    ).run(method, ref || null, rid);

    res.json({ message: 'تم تسجيل الدفع — بنأكده خلال وقت قصير' });
  })
);

// POST /api/customer/requests/:id/report-payment  (body: { amount })
//
// The customer's half of the two-sided cash confirmation. They state what they
// actually handed the technician, and the service compares it against the
// technician's figure: agreement settles the commission, disagreement freezes the
// request for an admin. Reporting here does not pay anything — it only supplies
// the missing half of the evidence.
router.post(
  '/requests/:id/report-payment',
  wrap(async (req, res) => {
    const rid = Number(req.params.id);
    const amt = Number((req.body || {}).amount);
    if (isNaN(amt) || amt <= 0 || amt > 100000) throw badRequest('المبلغ غير صالح');

    let result;
    try {
      result = require('../services/payments').customerReport(rid, req.user.customerId, amt);
    } catch (e) {
      throw serviceError(e, PAYMENT_ERROR_STATUS);
    }

    const r = db
      .prepare(
        `SELECT amount_status, final_amount, customer_reported_amount,
                technician_reported_amount, technician_id
           FROM requests WHERE id = ?`
      )
      .get(rid);
    const finalAmount = r.final_amount != null ? Number(r.final_amount) : null;

    // Only a MATCH settles the commission, so that is the only state worth
    // interrupting the technician for. A dispute is handled by an admin and must
    // not read as "the money is yours" on the technician's phone.
    if (r.amount_status === 'matched' && r.technician_id) {
      const tech = db.prepare('SELECT user_id FROM technicians WHERE id = ?').get(r.technician_id);
      if (tech) {
        const push = require('../services/notifications');
        const commission = config.getCommissionPercent();
        const amount = finalAmount != null ? Math.round(finalAmount * commission) / 100 : null;
        push.notify(tech.user_id, push.paymentConfirmedForTechnician({ amount }));
      }
    }

    res.json({
      ok: true,
      ...result,
      status: r.amount_status,
      final_amount: finalAmount,
      commission:
        finalAmount != null ? Math.round(finalAmount * config.getCommissionPercent()) / 100 : null,
      customer_amount: r.customer_reported_amount,
      technician_amount: r.technician_reported_amount,
    });
  })
);

// GET /api/customer/requests/:id/payment-status
//
// Drives the confirmation prompt. should_report is true only once the job is done,
// it is a cash job, the technician has put a figure in, and the customer has not —
// so the customer is never asked to confirm nothing.
router.get(
  '/requests/:id/payment-status',
  wrap(async (req, res) => {
    const rid = Number(req.params.id);
    const r = db
      .prepare(
        `SELECT id, status, payment_method, payment_status, amount_status,
                customer_reported_amount, technician_reported_amount,
                final_amount, completed_at
           FROM requests
          WHERE id = ? AND customer_id = ?`
      )
      .get(rid, req.user.customerId);
    if (!r) throw new AppError('الطلب غير موجود', 404);

    const shouldReport =
      r.status === 'completed' &&
      (!r.payment_method || r.payment_method === 'cash') &&
      r.customer_reported_amount == null &&
      r.technician_reported_amount != null;

    res.json({ request: r, should_report: shouldReport });
  })
);

// GET /api/customer/requests/:id/status  (lightweight polling endpoint)
router.get(
  '/requests/:id/status',
  wrap(async (req, res) => {
    const row = db
      .prepare(
        `SELECT r.id, r.status, r.specialty, r.updated_at, r.completed_at,
                t.name AS technician_name, t.phone AS technician_phone
         FROM requests r
         LEFT JOIN technicians t ON t.id = r.technician_id
         WHERE r.id = ? AND r.customer_id = ?`
      )
      .get(Number(req.params.id), req.user.customerId);
    if (!row) throw new AppError('الطلب غير موجود', 404);
    res.json({ request: row });
  })
);

// POST /api/customer/requests/:id/cancel  (only pending / assigned requests)
router.post(
  '/requests/:id/cancel',
  wrap(async (req, res) => {
    const request = db
      .prepare('SELECT * FROM requests WHERE id = ? AND customer_id = ?')
      .get(Number(req.params.id), req.user.customerId);
    if (!request) throw new AppError('الطلب غير موجود', 404);

    if (request.status !== 'pending' && request.status !== 'assigned') {
      throw badRequest('لا يمكن إلغاء هذا الطلب في حالته الحالية');
    }

    const run = db.transaction(() => {
      db.prepare(
        `UPDATE requests
         SET status = 'cancelled', updated_at = datetime('now')
         WHERE id = ?`
      ).run(request.id);
      // Free an assigned technician if one was already matched.
      if (request.technician_id) {
        db.prepare("UPDATE technicians SET availability = 'available' WHERE id = ?").run(
          request.technician_id
        );
      }
      // Close any outstanding offer, otherwise every broadcast technician
      // keeps the request in their inbox until they happen to poll after the
      // request is no longer 'pending'.
      db.prepare(
        `UPDATE request_broadcasts
            SET status = 'expired', responded_at = datetime('now')
          WHERE request_id = ? AND status = 'sent'`
      ).run(request.id);
    });
    run();

    const updated = db.prepare(`${REQUEST_SELECT} WHERE r.id = ?`).get(request.id);
    res.json({ message: 'تم إلغاء الطلب', request: updated });
  })
);

// --- Customer reviews (Phase 1) ---
// A review can only be left on a request that the customer owns, that is
// finished, and that was actually carried out by a technician. One review per
// request, enforced by the UNIQUE constraint on reviews.request_id.

const MAX_COMMENT_LENGTH = 1000;

// GET /api/customer/requests/:id/can-review
router.get(
  '/requests/:id/can-review',
  wrap(async (req, res) => {
    ensureCustomerHasProfile(req.user);
    const request = db
      .prepare('SELECT id, status, technician_id FROM requests WHERE id = ? AND customer_id = ?')
      .get(Number(req.params.id), req.user.customerId);
    if (!request) throw new AppError('الطلب غير موجود', 404);

    const alreadyReviewed = Boolean(
      db.prepare('SELECT id FROM reviews WHERE request_id = ?').get(request.id)
    );
    const hasTechnician = Boolean(request.technician_id);
    // 'completed' with an assigned technician is the only reviewable state.
    const canReview = !alreadyReviewed && hasTechnician && request.status === 'completed';

    res.json({ canReview, alreadyReviewed, hasTechnician });
  })
);

// POST /api/customer/requests/:id/review   { rating, comment? }
router.post(
  '/requests/:id/review',
  wrap(async (req, res) => {
    ensureCustomerHasProfile(req.user);
    const { rating, comment } = req.body || {};

    const ratingValue = Number(rating);
    if (!Number.isInteger(ratingValue) || ratingValue < 1 || ratingValue > 5) {
      throw badRequest('التقييم غير صالح');
    }
    if (comment !== undefined && comment !== null && comment !== '') {
      if (!isNonEmptyString(comment, MAX_COMMENT_LENGTH)) {
        throw badRequest('التعليق غير صالح');
      }
    }

    const request = db
      .prepare('SELECT * FROM requests WHERE id = ? AND customer_id = ?')
      .get(Number(req.params.id), req.user.customerId);
    if (!request) throw new AppError('الطلب غير موجود', 404);
    if (!request.technician_id) throw badRequest('لا يوجد فني يمكن تقييمه لهذا الطلب');
    if (request.status !== 'completed') {
      throw badRequest('يمكن التقييم بعد إكمال الطلب فقط');
    }
    if (db.prepare('SELECT id FROM reviews WHERE request_id = ?').get(request.id)) {
      throw badRequest('تم تقييم هذا الطلب بالفعل');
    }

    const result = db
      .prepare(
        `INSERT INTO reviews (request_id, customer_id, technician_id, rating, comment)
         VALUES (?, ?, ?, ?, ?)`
      )
      .run(
        request.id,
        req.user.customerId,
        request.technician_id,
        ratingValue,
        comment ? String(comment).trim() : null
      );

    const review = db.prepare('SELECT * FROM reviews WHERE id = ?').get(result.lastInsertRowid);
    res.status(201).json({ message: 'شكراً لتقييمك', review });
  })
);

// --- Review flow helpers (Phase 3) ----------------------------------------
// Both endpoints are scoped to the logged-in customer by customer_id, so one
// customer can never read or count another customer's reviews.

// GET /api/customer/pending-reviews
// Returns the count and list of completed requests that have no review yet.
router.get(
  '/pending-reviews',
  wrap(async (req, res) => {
    ensureCustomerHasProfile(req.user);
    const rows = db
      .prepare(
        `SELECT r.id, r.specialty, r.completed_at,
                c.name AS customer_name,
                t.name AS technician_name, t.id AS technician_id
         FROM requests r
         JOIN customers c ON c.id = r.customer_id
         LEFT JOIN technicians t ON t.id = r.technician_id
         LEFT JOIN reviews rv ON rv.request_id = r.id
         WHERE r.customer_id = ?
           AND r.status = 'completed'
           AND r.technician_id IS NOT NULL
           AND rv.id IS NULL
         ORDER BY r.completed_at DESC`
      )
      .all(req.user.customerId);
    res.json({ count: rows.length, requests: rows });
  })
);

// GET /api/customer/my-reviews
// Returns all reviews the customer has written, keyed by request_id.
router.get(
  '/my-reviews',
  wrap(async (req, res) => {
    ensureCustomerHasProfile(req.user);
    const rows = db
      .prepare(
        `SELECT rv.*,
                t.name AS technician_name
         FROM reviews rv
         LEFT JOIN technicians t ON t.id = rv.technician_id
         WHERE rv.customer_id = ?
         ORDER BY rv.created_at DESC`
      )
      .all(req.user.customerId);
    res.json({ reviews: rows });
  })
);

module.exports = router;
