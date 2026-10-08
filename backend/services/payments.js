/**
 * @file   Cash payment confirmation service.
 * @module payments
 *
 * Two-sided confirmation of cash payments.
 *
 * A cash job is the one case where the platform never sees the money: the customer
 * hands it to the technician directly. The amount the technician reports is
 * therefore self-reported, and an under-report quietly undercuts the commission
 * (20% of whatever is hidden), so the customer reports the figure they paid as
 * well and the two are compared:
 *
 *   match     -> commission booked against the technician's debt, request settled
 *   mismatch  -> frozen as 'disputed' for an admin to resolve
 *   timeout   -> settled on the technician's figure (CASH_CONFIRM_TIMEOUT_HOURS)
 *
 * InstaPay needs none of this: the customer paid the platform, so the amount is
 * already known and verified.
 *
 * @author Eltokhey
 * @since  2026-10
 */

const db = require('../db');
const config = require('../config');

const round2 = (n) => Math.round(Number(n) * 100) / 100;

// Tolerance for calling two reports a match. Anything larger than a rounding
// artefact (a piastre counted on one side and not the other) is a real difference
// in what was handed over, and that is a dispute, not noise.
const MATCH_TOLERANCE = 0.01;

// amount_status values that mean the money question is closed. Once a request is
// here the amount is final, so neither side may report again — see customerReport.
const SETTLED = ['matched', 'resolved'];

// The two states an ADMIN may still decide, as opposed to the two that are final.
// Kept as its own list rather than the inverse of SETTLED so that a future state
// has to be classified deliberately instead of silently becoming admin-decidable.
const ADMIN_DECIDABLE = ['pending', 'disputed'];

function isSettled(request) {
  return SETTLED.includes(request.amount_status);
}

// A typed failure, so the routes can map it to a status without this service
// knowing anything about HTTP. Same convention as pricing.js's walletError.
function paymentError(code, message) {
  const err = new Error(message);
  err.code = code;
  return err;
}

/**
 * @function technicianReport
 * @param {number} requestId
 * @param {number} technicianId
 * @param {number} amount Cash the technician says they received.
 * @returns {{ok: true, status: string, [final_amount]?: number, [commission]?: number}}
 * @throws {Error} code NOT_FOUND | INVALID_STATUS | ALREADY_REPORTED
 */
function technicianReport(requestId, technicianId, amount) {
  const request = db
    .prepare('SELECT * FROM requests WHERE id = ? AND technician_id = ?')
    .get(requestId, technicianId);
  if (!request) throw paymentError('NOT_FOUND', 'الطلب غير موجود');

  if (request.status !== 'completed') {
    throw paymentError('INVALID_STATUS', 'الطلب لم يُكتمل بعد');
  }
  if (isSettled(request)) {
    throw paymentError('ALREADY_REPORTED', 'تم تسجيل المبلغ مسبقاً');
  }
  if (request.technician_reported_amount != null) {
    throw paymentError('ALREADY_REPORTED', 'تم تسجيل المبلغ مسبقاً');
  }

  db.prepare(
    `UPDATE requests
        SET technician_reported_amount = ?, technician_reported_at = datetime('now'),
            -- Stamped here so the request is identifiable as a cash job even
            -- while it waits for the customer's figure. COALESCE so an InstaPay
            -- request can never be flipped to cash by a late report.
            payment_method = COALESCE(payment_method, 'cash'),
            amount_status = 'pending',
            updated_at = datetime('now')
      WHERE id = ?`
  ).run(round2(amount), requestId);

  return tryMatch(requestId);
}

/**
 * @function customerReport
 * @param {number} requestId
 * @param {number} customerId
 * @param {number} amount Cash the customer says they paid.
 * @returns {{ok: true, status: string, [customer_amount]?: number, [technician_amount]?: number}}
 * @throws {Error} code NOT_FOUND | INVALID_STATUS | NOT_CASH | ALREADY_REPORTED
 */
function customerReport(requestId, customerId, amount) {
  const request = db
    .prepare('SELECT * FROM requests WHERE id = ? AND customer_id = ?')
    .get(requestId, customerId);
  if (!request) throw paymentError('NOT_FOUND', 'الطلب غير موجود');

  if (request.status !== 'completed') {
    throw paymentError('INVALID_STATUS', 'الطلب لم يُكتمل بعد');
  }
  if (request.payment_method && request.payment_method !== 'cash') {
    throw paymentError('NOT_CASH', 'هذا الطلب لم يكن دفعاً نقدياً');
  }
  // A settled request is closed to reports. This also covers the timeout case:
  // auto-resolve settles on the technician's figure, and without this guard a
  // customer reporting afterwards would drive tryMatch to flip the already-settled
  // request back to 'disputed' — a request both paid and awaiting an admin.
  if (isSettled(request)) {
    throw paymentError('ALREADY_REPORTED', 'تم تسجيل المبلغ مسبقاً');
  }
  if (request.customer_reported_amount != null) {
    throw paymentError('ALREADY_REPORTED', 'تم تسجيل المبلغ مسبقاً');
  }

  db.prepare(
    `UPDATE requests
        SET customer_reported_amount = ?, customer_reported_at = datetime('now'),
            amount_status = 'pending',
            updated_at = datetime('now')
      WHERE id = ?`
  ).run(round2(amount), requestId);

  return tryMatch(requestId);
}

/**
 * @function tryMatch
 * @param {number} requestId
 * @returns {{ok: true, status: 'pending'|'matched'|'disputed', [customer_amount]?: number, [technician_amount]?: number, [final_amount]?: number, [commission]?: number}}
 *
 * Compares whatever has been reported so far. Safe to call after either report:
 * the "waiting" branch is the normal one until both sides are in.
 */
function tryMatch(requestId) {
  const r = db.prepare('SELECT * FROM requests WHERE id = ?').get(requestId);
  if (!r) throw paymentError('NOT_FOUND', 'الطلب غير موجود');

  if (r.customer_reported_amount != null && r.technician_reported_amount != null) {
    const cust = Number(r.customer_reported_amount);
    const tech = Number(r.technician_reported_amount);

    if (Math.abs(cust - tech) < MATCH_TOLERANCE) {
      return finalize(requestId, cust, 'matched');
    }

    db.prepare(
      `UPDATE requests
          SET amount_status = 'disputed', updated_at = datetime('now')
        WHERE id = ?`
    ).run(requestId);
    return {
      ok: true,
      status: 'disputed',
      message: 'يوجد اختلاف في المبلغ — تم إحالة الطلب للمراجعة',
      customer_amount: cust,
      technician_amount: tech,
    };
  }

  return {
    ok: true,
    status: 'pending',
    message: 'في انتظار تأكيد الطرف الآخر',
    waiting_on: r.technician_reported_amount != null ? 'customer' : 'technician',
  };
}

/**
 * @function settle
 * @param {number} requestId
 * @param {number} finalAmount The agreed amount; the commission is computed from it.
 * @param {'matched'|'resolved'} status
 * @param {{note?: string, adminUserId?: number}} [opts]
 * @returns {{ok: true, status: string, [final_amount]: number, [commission]: number}}
 * @throws {Error} code NOT_FOUND
 *
 * The single place a request becomes settled. Books the commission exactly once
 * and writes the request state in one transaction, so a request can never end up
 * settled with no commission, or charged with no settled state.
 *
 * Idempotent: the commission lands in the wallet as a 'debt' row keyed to this
 * request, so its presence IS the proof it was already booked. That is what makes a
 * retry, a second report, and the timeout sweep all safe against the same request.
 * Scoped to the technician because that is how the row was written — a wallet entry
 * for someone else must not satisfy our guard.
 */
function settle(requestId, finalAmount, status, opts) {
  const options = opts || {};
  const pricing = require('./pricing');
  const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(requestId);
  if (!request) throw paymentError('NOT_FOUND', 'الطلب غير موجود');

  const technicianId = request.technician_id;
  if (technicianId) {
    const existing = db
      .prepare(
        "SELECT id FROM wallet_transactions WHERE request_id = ? AND technician_id = ? AND type = 'debt'"
      )
      .get(requestId, technicianId);
    if (existing) {
      return {
        ok: true,
        status: 'already_finalized',
        final_amount: request.final_amount,
        request_id: requestId,
      };
    }
  }

  const amount = round2(finalAmount);
  const commission = round2((amount * config.getCommissionPercent()) / 100);

  const run = db.transaction(() => {
    db.prepare(
      `UPDATE requests
          SET final_amount = ?, amount_status = ?, payment_status = 'confirmed',
              payment_method = 'cash',
              payment_reference = COALESCE(payment_reference, 'cash-' + ?),
              payment_confirmed_at = datetime('now'),
              -- The admin's decision, when there is one. Set in the same
              -- transaction as the commission so the audit trail cannot exist
              -- without the booking it justifies.
              dispute_resolved_at = CASE WHEN ? IS NULL THEN dispute_resolved_at
                                         ELSE datetime('now') END,
              dispute_resolved_by = COALESCE(?, dispute_resolved_by),
              dispute_note = COALESCE(?, dispute_note),
              updated_at = datetime('now')
        WHERE id = ?`
    ).run(
      amount,
      status,
      amount,
      options.adminUserId || null,
      options.adminUserId || null,
      options.note || null,
      requestId
    );

    if (technicianId) {
      // Positive, like every other 'debt' entry: the ledger records magnitudes and
      // `type` is what says which pot moved. total_debt is the pot that grows.
      pricing.addWalletTransaction(technicianId, {
        requestId,
        type: 'debt',
        amount: commission,
        note: 'عمولة المنصة على طلب #' + requestId + ' (كاش ' + amount + ' ج.م)',
      });
    }
  });
  run();

  return { ok: true, status, final_amount: amount, commission, request_id: requestId };
}

/**
 * @function finalize
 * @param {number} requestId
 * @param {number} finalAmount
 * @param {'matched'|'resolved'} status
 * @param {string} [note]
 * @returns {{ok: true, status: string, [final_amount]: number, [commission]: number}}
 * @throws {Error} code NOT_FOUND
 */
function finalize(requestId, finalAmount, status, note) {
  return settle(requestId, finalAmount, status, { note });
}

/**
 * @function resolveDispute
 * @param {number} requestId
 * @param {number} finalAmount Amount the admin decides is correct.
 * @param {number} adminUserId
 * @param {string} [note] Why.
 * @returns {{ok: true, status: string, [final_amount]: number, [commission]: number}}
 * @throws {Error} code NOT_FOUND | INVALID_STATUS
 *
 * An admin's decision on a cash confirmation, for either of the two states that
 * need one:
 *
 *   'pending'   the technician reported and the customer never answered. The admin
 *               is standing in for the confirmation the customer did not give.
 *   'disputed'  the two sides reported different figures and the request is frozen.
 *
 * 'matched' and 'resolved' are refused: both are already settled, the commission is
 * booked, and re-deciding them would either double-charge or silently rewrite a
 * number the technician has already been shown. settle() would refuse the second
 * booking anyway; refusing here means the caller gets the reason rather than a
 * confusing no-op.
 */
function resolveDispute(requestId, finalAmount, adminUserId, note) {
  const r = db.prepare('SELECT * FROM requests WHERE id = ?').get(requestId);
  if (!r) throw paymentError('NOT_FOUND', 'الطلب غير موجود');
  if (!ADMIN_DECIDABLE.includes(r.amount_status)) {
    throw paymentError('INVALID_STATUS', 'لا يمكن تأكيد المبلغ في هذه الحالة');
  }

  return settle(requestId, finalAmount, 'resolved', { adminUserId, note });
}

/**
 * @function autoResolveStale
 * @param {number} [withinHours] Defaults to CASH_CONFIRM_TIMEOUT_HOURS.
 * @returns {{processed: number, results: Array}}
 *
 * Settles confirmations the customer never answered, on the technician's figure.
 * Run from the queue sweep.
 *
 * The cutoff is built as a SQLite modifier ('-24 hours') rather than a JS Date so
 * the comparison happens in one clock and one string format — the same reasoning
 * queue.js applies to its own expiry sweep. Handing SQLite an ISO string with 'T'
 * and 'Z' next to the 'YYYY-MM-DD HH:MM:SS' that datetime('now') produces is the
 * kind of mismatch that compares as text and quietly resolves nothing.
 */
function autoResolveStale(withinHours) {
  const hours = withinHours || config.CASH_CONFIRM_TIMEOUT_HOURS || 24;
  const modifier = '-' + hours + ' hours';

  const stale = db
    .prepare(
      `SELECT id, technician_reported_amount FROM requests
        WHERE amount_status = 'pending'
          AND technician_reported_amount IS NOT NULL
          AND customer_reported_amount IS NULL
          AND datetime(technician_reported_at) < datetime('now', ?)`
    )
    .all(modifier);

  const results = stale.map((r) => ({
    request_id: r.id,
    ...finalize(r.id, r.technician_reported_amount, 'resolved', 'تلقائي — لم يرد العميل'),
  }));

  return { processed: results.length, results };
}

module.exports = {
  technicianReport,
  customerReport,
  resolveDispute,
  autoResolveStale,
  // Exported for the admin dispute route and for tests. Not wired to any route
  // that could settle a request which is not a match/dispute outcome.
  finalize,
};
