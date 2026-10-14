/**
 * @file   Broadcast service — Uber-style request distribution
 * @module broadcast
 *
 * When a customer creates a request it is offered to EVERY eligible technician at
 * once instead of being auto-assigned to the single nearest one. The first
 * technician to accept wins; every other live offer for that request is closed.
 *
 * Lifecycle of one request_broadcasts row:
 *   sent ──accept──▶ accepted        (exactly one row per request can do this)
 *        ──decline─▶ declined
 *        ──timeout──▶ expired ──▶ the request becomes 'no_technician'
 *        ──another winner / request closed──▶ expired
 *
 * The legacy auto-assign service (services/assignment.js) is deliberately left in
 * place as a fallback; it is simply no longer invoked on request creation.
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Uber-style broadcast distribution: offer to all, first acceptance wins.
const db = require('../db');
const config = require('../config');
const pricing = require('./pricing');
const { haversineKm } = require('./assignment');
const startPinService = require('./startPin');
const logger = require('../utils/logger');

// The live state of an offer. Kept as a constant because a typo in a status string
// would silently produce a row the inbox query never returns.
const SENT = 'sent';
const ACCEPTED = 'accepted';
const DECLINED = 'declined';
const EXPIRED = 'expired';

// ---------------------------------------------------------------------------
// Eligibility
// ---------------------------------------------------------------------------

// Technicians who may be offered a request.
//
// Filters, in order:
//   - approved and currently available (a busy technician is already on a job)
//   - distance: when the request has coordinates AND a radius is configured,
//     technicians that are positioned are limited to that radius. A technician
//     without coordinates is always included — they cannot be placed, and
//     dropping them would shrink the pool for no good reason.
//   - wallet eligibility (debt ceiling)
//
// On specialty: deliberately NOT a filter. Every technician on this platform
// handles every dish service, and `technicians.specialty` is a placeholder that
// predates that rule — in practice it holds values like 'dish_general' that
// match none of the real service keys. Filtering on it therefore excluded
// valid, available, approved technicians from every broadcast and pushed
// perfectly serviceable requests into the waiting queue. The service the
// customer asked for is still carried on the request itself, so nothing is lost
// by offering the job broadly and letting the first available technician take it.
function eligibleTechnicians(request) {
  const rows = db
    .prepare(
      `SELECT id, name, phone, specialty, latitude, longitude, location_text, availability
         FROM technicians
        WHERE approval_status = 'approved'
          AND availability = 'available'`
    )
    .all();

  const hasCoords = request.latitude != null && request.longitude != null;
  const radiusKm = broadcastRadiusKm();

  return rows.filter((t) => {
    if (hasCoords && radiusKm > 0 && t.latitude != null && t.longitude != null) {
      if (haversineKm(request.latitude, request.longitude, t.latitude, t.longitude) > radiusKm) {
        return false;
      }
    }
    return pricing.canReceiveRequests(t.id);
  });
}

// Broadcast radius in km.
//
// The `auto_assign_radius_km` settings row is the runtime override: an admin can
// change it from the settings screen without a redeploy, and an explicit 0 there
// means "no limit", which is honoured as-is. Only when that row does not exist
// (a fresh install that never seeded it) does the deployment default from
// config.BROADCAST_RADIUS_KM apply.
function broadcastRadiusKm() {
  const row = db.prepare('SELECT value FROM settings WHERE key = ?').get('auto_assign_radius_km');
  if (row) {
    const n = parseFloat(row.value);
    if (Number.isFinite(n)) return n > 0 ? n : 0;
  }
  return Number(config.BROADCAST_RADIUS_KM) > 0 ? Number(config.BROADCAST_RADIUS_KM) : 0;
}

// ---------------------------------------------------------------------------
// Broadcast
// ---------------------------------------------------------------------------

// Offers a request to every eligible technician, and re-offers it to a stranded
// one. Safe to call twice: the UNIQUE(request_id, technician_id) index means each
// technician can hold only one row per request, so a repeat call refreshes the
// existing rows instead of duplicating them, and a request that is already
// assigned/in progress/completed is refused outright.
//
// A technician who previously DECLINED this request is never re-offered it —
// that refusal is honoured. A technician whose offer merely expired is offered it
// again, because reaching them a second time is the whole point of re-broadcasting.
// Statuses a request can be offered out from. 'pending' is the normal case;
// 'no_technician' is the state the expiry sweep leaves behind, and re-offering
// from it is how an admin puts a stranded request back in front of technicians.
const BROADCASTABLE_STATUSES = ['pending', 'no_technician'];

function broadcastRequest(requestId) {
  const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(requestId);
  if (!request) return { count: 0, reason: 'request not found' };

  if (!BROADCASTABLE_STATUSES.includes(request.status)) {
    return { count: 0, reason: 'request not broadcastable' };
  }

  // Re-open a stranded request before offering it, so the offers are made against
  // a request that is genuinely 'pending' (otherwise the sweep would flag it
  // again on the next pass and the accept guard would refuse every claim).
  if (request.status === 'no_technician') {
    db.prepare(
      "UPDATE requests SET status = 'pending', updated_at = datetime('now') WHERE id = ?"
    ).run(requestId);
    request.status = 'pending';
  }

  const candidates = eligibleTechnicians(request);
  if (candidates.length === 0) {
    return { count: 0, reason: 'no eligible technicians' };
  }

  // One row per (request, technician) is enforced by a UNIQUE index, so a
  // technician who already has a row for this request cannot simply be offered
  // again with a plain INSERT. Each candidate is therefore handled by what their
  // existing row already says:
  //
  //   expired -> re-opened with a fresh sent_at, so the timeout restarts. This is
  //              the re-broadcast case, and skipping them would make re-broadcast
  //              useless: the technicians who let a request time out are exactly
  //              the ones it should reach a second time.
  //   sent    -> refreshed in place for the same reason (admin re-broadcast while
  //              the request is still open, so nobody is left on the old clock).
  //   declined-> left alone. They said no to this request; re-offering would
  //              ignore an explicit refusal.
  //   accepted-> impossible here: a request with an accepted offer is assigned,
  //              and an assigned request is not broadcastable.
  const reopen = db.prepare(
    `UPDATE request_broadcasts
        SET status = '${SENT}', sent_at = datetime('now'), responded_at = NULL
      WHERE request_id = ? AND technician_id = ? AND status = '${EXPIRED}'`
  );
  const refresh = db.prepare(
    `UPDATE request_broadcasts
        SET sent_at = datetime('now')
      WHERE request_id = ? AND technician_id = ? AND status = '${SENT}'`
  );
  const insert = db.prepare(
    `INSERT OR IGNORE INTO request_broadcasts (request_id, technician_id, status)
     VALUES (?, ?, '${SENT}')`
  );

  const run = db.transaction(() => {
    candidates.forEach((t) => {
      if (reopen.run(requestId, t.id).changes > 0) return;
      if (refresh.run(requestId, t.id).changes > 0) return;
      insert.run(requestId, t.id);
    });
  });
  run();

  // Only the rows actually live after this call. Reporting this length rather
  // than candidates.length keeps the count honest: a candidate who had declined
  // is in candidates but was deliberately not re-offered.
  const notified = db
    .prepare(
      `SELECT technician_id FROM request_broadcasts
        WHERE request_id = ? AND status = '${SENT}'`
    )
    .all(requestId)
    .map((r) => r.technician_id);

  return { count: notified.length, technician_ids: notified };
}

// ---------------------------------------------------------------------------
// Accept / decline
// ---------------------------------------------------------------------------

// First technician to accept wins the request.
//
// Concurrency note: the eligibility reads below happen OUTSIDE the transaction,
// so two technicians accepting the same moment would both see status='sent'
// before either writes. The guarded UPDATE inside the transaction is therefore
// the real arbiter — its `changes` count is checked, and if it came back 0 the
// request was taken in the meantime and this call reports the 409. Trusting the
// earlier read instead (the obvious implementation) would let a late accepter
// overwrite a valid assignment and close the true winner's offer.
function acceptBroadcast(broadcastId, technicianId) {
  const row = db
    .prepare('SELECT * FROM request_broadcasts WHERE id = ? AND technician_id = ?')
    .get(broadcastId, technicianId);

  if (!row) {
    return { ok: false, error: 'عرض الطلب غير موجود', code: 404 };
  }
  if (row.status !== SENT) {
    // The offer is closed. What to say depends on WHY, and getting this wrong is
    // actively misleading: the common case by far is losing the race, which must
    // read as "another technician got it", not "your offer timed out".
    const req = db.prepare('SELECT status FROM requests WHERE id = ?').get(row.request_id);
    const requestStatus = req ? req.status : null;

    if (row.status === EXPIRED) {
      // A still-pending request means this row was closed on its own timeout.
      // Anything else means the request moved on without this technician.
      if (requestStatus === 'pending' || requestStatus === 'no_technician') {
        return { ok: false, error: 'انتهت صلاحية عرض هذا الطلب', code: 409 };
      }
      if (requestStatus === 'cancelled') {
        return { ok: false, error: 'تم إلغاء الطلب', code: 409 };
      }
      return { ok: false, error: 'تم قبض الطلب من فني آخر', code: 409 };
    }
    if (row.status === DECLINED) {
      return { ok: false, error: 'تم التعامل مع هذا الطلب بالفعل', code: 409 };
    }
    return { ok: false, error: 'تم التعامل مع هذا الطلب بالفعل', code: 409 };
  }

  const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(row.request_id);
  if (!request) {
    return { ok: false, error: 'الطلب غير موجود', code: 404 };
  }
  if (request.status !== 'pending') {
    return { ok: false, error: 'تم قبض الطلب من فني آخر', code: 409 };
  }

  if (!pricing.canReceiveRequests(technicianId)) {
    return {
      ok: false,
      error: 'حسابك موقوف مؤقتاً بسبب تجاوز حد المديونية. يرجى سداد المستحقات.',
      code: 403,
    };
  }

  // Read BEFORE the claim: the claim clears is_queued, and that flag is the whole
  // evidence that this request sat with nobody available. A request that was queued
  // and is now being won is the auto-pickup path finishing, and it is the one that
  // deserves the line in the log — "request #41 finally went somewhere after
  // waiting" is exactly what is invisible when a request has no technician for hours.
  const wasStranded = Number(request.is_queued) === 1;

  let conflict = false;
  const startPin = startPinService.mint();
  const run = db.transaction(() => {
    // 1. Claim the request. The status guard makes this atomic: only the first
    //    transaction to move it out of 'pending' reports a change.
    //    Clearing is_queued in the same statement retires the request from the
    //    waiting queue the moment it is won, so a queued request never sits in
    //    both places at once.
    //    The PIN is written in the same statement as the claim, so a claimed
    //    request can never exist without one, and the attempt counters start
    //    clean for this technician's shift on this job.
    const claimed = db
      .prepare(
        `UPDATE requests
            SET technician_id = ?, status = 'assigned', is_queued = 0,
                assigned_at = datetime('now'), updated_at = datetime('now'),
                start_pin = ?, start_pin_attempts = 0, start_pin_locked_until = NULL,
                started_at = NULL
          WHERE id = ? AND status = 'pending'`
      )
      .run(technicianId, startPin, request.id);

    if (claimed.changes === 0) {
      conflict = true;
      return;
    }

    // 2. This technician won.
    db.prepare(
      `UPDATE request_broadcasts
          SET status = '${ACCEPTED}', responded_at = datetime('now')
        WHERE id = ?`
    ).run(broadcastId);

    // 3. Everyone else's offer is now closed. This is the "taken by another
    //    technician" signal the other technicians' UI reads.
    db.prepare(
      `UPDATE request_broadcasts
          SET status = '${EXPIRED}', responded_at = datetime('now')
        WHERE request_id = ? AND id != ? AND status = '${SENT}'`
    ).run(request.id, broadcastId);

    // 4. The winner is now occupied.
    db.prepare("UPDATE technicians SET availability = 'busy' WHERE id = ?").run(technicianId);

    // 5. Re-stamp pricing in case the tariff changed since the request was made.
    pricing.applyPricingToRequest(request.id);
  });
  run();

  if (conflict) {
    return { ok: false, error: 'تم قبض الطلب من فني آخر', code: 409 };
  }
  // The winning technician's name and the customer are returned so the caller
  // can notify them without re-reading the request: this service deliberately
  // sends nothing itself.
  const winner = db.prepare('SELECT name FROM technicians WHERE id = ?').get(technicianId);

  if (wasStranded) {
    logger.info(
      'auto-pickup',
      `[AUTO-PICKUP] Assigned pending request #${request.id} to technician #${technicianId}`
    );
  }

  return {
    ok: true,
    request_id: request.id,
    customer_id: request.customer_id,
    technician_name: winner ? winner.name : null,
  };
}

// Technician declines one offer. The request stays open for everyone else.
function declineBroadcast(broadcastId, technicianId) {
  const row = db
    .prepare('SELECT * FROM request_broadcasts WHERE id = ? AND technician_id = ?')
    .get(broadcastId, technicianId);

  if (!row) {
    return { ok: false, error: 'عرض الطلب غير موجود', code: 404 };
  }
  if (row.status !== SENT) {
    return { ok: false, error: 'تم التعامل مع هذا الطلب', code: 409 };
  }

  db.prepare(
    `UPDATE request_broadcasts
        SET status = '${DECLINED}', responded_at = datetime('now')
      WHERE id = ? AND status = '${SENT}'`
  ).run(broadcastId);

  return { ok: true };
}

// ---------------------------------------------------------------------------
// Inbox
// ---------------------------------------------------------------------------

// Live offers for one technician: their 'sent' broadcasts whose request is still
// unassigned. The join on requests.status is what keeps a request that was won
// by somebody else from lingering in the list.
function incomingForTechnician(technicianId) {
  return db
    .prepare(
      `SELECT
         rb.id AS broadcast_id,
         rb.sent_at,
         r.id AS request_id,
         r.specialty,
         r.custom_specialty_text,
         r.description,
         r.location_text,
         r.latitude,
         r.longitude,
         r.status,
         r.created_at,
         r.service_price,
         c.name AS customer_name,
         c.phone AS customer_phone,
         c.location_text AS customer_area
       FROM request_broadcasts rb
       JOIN requests r ON r.id = rb.request_id
       JOIN customers c ON c.id = r.customer_id
       WHERE rb.technician_id = ?
         AND rb.status = '${SENT}'
         AND r.status = 'pending'
       ORDER BY rb.sent_at DESC, rb.id DESC
       LIMIT 20`
    )
    .all(technicianId);
}

// ---------------------------------------------------------------------------
// Waiting-queue re-offer
// ---------------------------------------------------------------------------

// Offers a queued request to technicians who have NOT seen it before.
//
// Called by services/queue.js on every recheck tick. The point is reach, not
// persistence: a queued request is one that nobody was available for at creation
// time, so the useful thing to do is notify whoever has become available since —
// which is the opposite of broadcastRequest(), where an admin explicitly pressing
// "re-broadcast" should also reach the technicians who already declined to answer
// or let the previous offer lapse.
//
// Eligibility is delegated to eligibleTechnicians() rather than re-implemented, so
// the queue cannot drift from the rules the initial broadcast uses: approved,
// available, inside the radius, and clear of the debt ceiling.
function rebroadcastQueued(requestId) {
  const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(requestId);
  if (!request) return { sent: 0, reason: 'not_found' };
  if (request.status !== 'pending') return { sent: 0, reason: 'not_pending' };

  const candidates = eligibleTechnicians(request);

  // Anyone already holding a row for this request is filtered out. Re-notifying
  // them would be noise, and for a technician who already declined it would be
  // ignoring an answer they gave.
  const seen = new Set(
    db
      .prepare('SELECT technician_id FROM request_broadcasts WHERE request_id = ?')
      .all(requestId)
      .map((r) => r.technician_id)
  );
  const fresh = candidates.filter((t) => !seen.has(t.id));
  if (!fresh.length) return { sent: 0, technician_ids: [], reason: 'no_new_candidates' };

  // 'sent' is the live status. The older vocabulary used 'pending' here, which the
  // CHECK constraint now rejects — and because the insert is INSERT OR IGNORE the
  // rejection would be swallowed, reporting offers that were never written.
  const insert = db.prepare(
    `INSERT OR IGNORE INTO request_broadcasts (request_id, technician_id, status)
     VALUES (?, ?, '${SENT}')`
  );
  const run = db.transaction(() => {
    fresh.forEach((t) => insert.run(requestId, t.id));
  });
  run();

  // Counted from the rows that are actually live, not from `fresh`, so a rejected
  // insert can never be reported as an offer that exists.
  const sent = db
    .prepare(
      `SELECT technician_id FROM request_broadcasts
        WHERE request_id = ? AND status = '${SENT}'`
    )
    .all(requestId)
    .map((r) => r.technician_id);

  return { sent: sent.length, technician_ids: sent };
}

// ---------------------------------------------------------------------------
// Expiry
// ---------------------------------------------------------------------------

// Hours an offer stays open. Read through the config module so a deployment (or a
// test) can change the window without touching this file.
function timeoutHours() {
  const n = Number(config.BROADCAST_TIMEOUT_HOURS);
  return Number.isFinite(n) && n > 0 ? n : 24;
}

// The same window in seconds, for the callers that only need a number to schedule
// against (the sweep interval in server.js, the value the technician inbox
// reports back to the client).
function timeoutSeconds() {
  return Math.round(timeoutHours() * 3600);
}

// Closes offers nobody answered in time and hands the request to the admin queue.
//
// A request becomes 'no_technician' only when it HAD at least one live offer that
// expired. A request that was created with no eligible technician never had an
// offer to expire, so it deliberately stays 'pending' — that is the pre-existing
// behaviour, and flipping those too would empty the "waiting for a technician"
// queue that admins use to see demand nobody can serve.
//
// QUEUED REQUESTS ARE SKIPPED. A request on the waiting queue is owned by
// services/queue.js end to end: it is re-offered every QUEUE_RECHECK_MINUTES and
// cancelled at QUEUE_MAX_HOURS with a reason. If this sweep also flipped it to
// 'no_technician' at BROADCAST_TIMEOUT_HOURS, the two services would race over the
// same row at the same deadline (both default to 24h) and the request could land
// in either state depending on which tick ran first.
//
// The sweep is a single pass over the 'sent' rows, which the
// idx_broadcast_tech_status index keeps small, and it is throttled per process:
// the endpoint that triggers it is polled by every technician every few seconds.
function expireTimedOutBroadcasts() {
  const hours = timeoutHours();

  // Requests that are still open and have at least one offer older than the
  // window. Grouped per request so the UPDATE below cannot race the request
  // being accepted between the SELECT and the write.
  const stale = db
    .prepare(
      `SELECT rb.request_id, MIN(rb.sent_at) AS oldest
         FROM request_broadcasts rb
         JOIN requests r ON r.id = rb.request_id
        WHERE rb.status = '${SENT}'
          AND r.status = 'pending'
          AND r.is_queued = 0
          AND rb.sent_at <= datetime('now', ?)
        GROUP BY rb.request_id`
    )
    .all('-' + hours + ' hours');

  if (!stale.length) return { expired: 0, requests: 0 };

  let expired = 0;
  let requests = 0;

  const run = db.transaction(() => {
    const closeOffers = db.prepare(
      `UPDATE request_broadcasts
          SET status = '${EXPIRED}', responded_at = datetime('now')
        WHERE request_id = ? AND status = '${SENT}'`
    );
    // Guarded on the request still being open, so an accept that lands first wins
    // and this sweep leaves the request alone instead of overwriting 'assigned'.
    const flagRequest = db.prepare(
      `UPDATE requests
          SET status = 'no_technician', updated_at = datetime('now')
        WHERE id = ? AND status = 'pending' AND is_queued = 0`
    );

    stale.forEach((row) => {
      const result = closeOffers.run(row.request_id);
      expired += result.changes;
      requests += flagRequest.run(row.request_id).changes;
    });
  });
  run();

  if (requests) {
    logger.info(
      'broadcast',
      `[broadcast] ` + requests + ' request(s) marked no_technician after timeout.'
    );
  }
  return { expired, requests };
}

// Throttle for the lazy sweep. Every technician's poll would otherwise trigger a
// database pass, so it runs at most once per this many seconds; the periodic timer
// in server.js covers the idle case.
const SWEEP_MIN_INTERVAL_SECONDS = 5;
let lastSweepAt = 0;

function sweepIfDue(force) {
  const now = Date.now();
  if (!force && now - lastSweepAt < SWEEP_MIN_INTERVAL_SECONDS * 1000) return;
  lastSweepAt = now;
  try {
    expireTimedOutBroadcasts();
  } catch (e) {
    // A sweep failure must never break the request that triggered it; the periodic
    // sweep will retry.
    logger.info('broadcast', `[broadcast] expiry sweep failed:`, e.message);
  }
}

// Closes any offer still marked 'sent' for a request that is no longer pending
// (cancelled, completed, or flagged no_technician). Without this the rows stay
// 'sent' forever and inflate the audit trail an admin sees.
//
// Scoped to one technician so it can be called on the poll path without turning
// every inbox refresh into a table-wide write.
function expireForTechnician(technicianId) {
  return db
    .prepare(
      `UPDATE request_broadcasts
          SET status = '${EXPIRED}', responded_at = datetime('now')
        WHERE technician_id = ?
          AND status = '${SENT}'
          AND request_id IN (SELECT id FROM requests WHERE status != 'pending')`
    )
    .run(technicianId).changes;
}

// Scoped expiry sweep for a single request. Called after an admin cancels a
// request so the offered technicians drop it from their inbox immediately.
function expireForRequest(requestId) {
  return db
    .prepare(
      `UPDATE request_broadcasts
          SET status = '${EXPIRED}', responded_at = datetime('now')
        WHERE request_id = ? AND status = '${SENT}'`
    )
    .run(requestId).changes;
}

module.exports = {
  SENT,
  ACCEPTED,
  DECLINED,
  EXPIRED,
  broadcastRequest,
  rebroadcastQueued,
  acceptBroadcast,
  declineBroadcast,
  incomingForTechnician,
  expireTimedOutBroadcasts,
  sweepIfDue,
  expireForTechnician,
  expireForRequest,
  eligibleTechnicians,
  timeoutHours,
  timeoutSeconds,
};
