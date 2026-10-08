/**
 * @file   Waiting queue — keeps unaccepted requests in front of technicians
 * @module queue
 *
 * A request created when nobody was available is not dead: it waits here. Every
 * QUEUE_RECHECK_MINUTES the queue re-offers it to technicians who have become
 * available since the last pass — inside the broadcast radius and clear of the
 * debt ceiling, which is broadcast.eligibleTechnicians()'s job — and when it
 * reaches QUEUE_MAX_HOURS with nobody interested it is cancelled with a reason
 * rather than left pending forever.
 *
 * Division of labour with services/broadcast.js:
 *   - broadcast.js owns the per-offer lifecycle (accept/decline/expire) and the
 *     24h offer window. It deliberately does NOT touch queued requests.
 *   - this service owns the queued request's lifecycle end to end, and delegates
 *     the actual offering to broadcast.rebroadcastQueued().
 * One owner per row keeps the two from racing over the same deadline.
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Waiting queue: re-broadcast unaccepted requests to newly-available technicians.
const db = require('../db');
const config = require('../config');
const broadcast = require('./broadcast');
const logger = require('../utils/logger');

// How many queued requests one pass will touch. The recheck runs every few
// minutes on a single-node SQLite app, so this only matters as a brake on a large
// backlog: re-offering a thousand requests in one transaction would hold the write
// lock long enough for a technician's accept to time out behind it.
const MAX_REQUESTS_PER_PASS = 100;

// ---------------------------------------------------------------------------
// Areas
// ---------------------------------------------------------------------------

// Resolves free text to an area key.
//
// Neither requests nor technicians store an area key: both keep `location_text`,
// which customer.routes.js builds as "<area label> — <free text>". Matching on the
// label prefix is what makes the area map usable at all; a request whose location
// is typed by hand simply resolves to null, and null means "no area filter".
function areaKeyFromLabel(label) {
  if (!label) return null;
  const value = String(label).trim();
  if (!value) return null;
  const area = config.ALEXANDRIA_AREAS.find(
    (a) =>
      a.key === value || a.label === value || value.startsWith(a.label + ' ') || value === a.label
  );
  return area ? area.key : null;
}

// The area itself plus its neighbours, or null when the area is unknown.
//
// Returning null (rather than "every area") is the safe direction: an unrecognised
// location must not widen the pool to the whole city.
function qualifiedAreas(areaKey) {
  if (!areaKey) return null;
  const neighbors = config.AREA_NEIGHBORS[areaKey];
  if (neighbors && neighbors.length) return neighbors;
  return [areaKey];
}

// ---------------------------------------------------------------------------
// Eligibility
// ---------------------------------------------------------------------------

// Technicians a queued request may be offered to right now.
//
// This starts from broadcast.eligibleTechnicians() — the same predicate the
// creation-time broadcast uses — and then narrows by area. The area step is
// deliberately applied only on top: a technician whose stored location does not
// resolve to an area is kept (they may well be across town, but excluding every
// unrecognised address would silently shrink the pool), whereas a technician who
// DID state an area outside the request's neighbourhood is excluded.
function eligibleForQueuedRequest(request) {
  const candidates = broadcast.eligibleTechnicians(request);
  const areas = qualifiedAreas(areaKeyFromLabel(request.location_text));
  if (!areas) return candidates;

  return candidates.filter((t) => {
    const techArea = areaKeyFromLabel(t.location_text);
    return techArea ? areas.includes(techArea) : true;
  });
}

// ---------------------------------------------------------------------------
// Queue bookkeeping
// ---------------------------------------------------------------------------

// Puts a request on the queue and starts its clock.
//
// Idempotent, and deliberately not re-stamping queued_at: markQueued() is called
// when a request is created, but anything that re-checks a request must not be
// able to extend its life by touching it.
function markQueued(requestId) {
  const maxHours = config.QUEUE_MAX_HOURS;
  const expiresAt = new Date(Date.now() + maxHours * 3600 * 1000).toISOString();

  const result = db
    .prepare(
      `UPDATE requests
          SET is_queued = 1,
              queued_at = datetime('now'),
              queue_expires_at = COALESCE(queue_expires_at, ?)
        WHERE id = ? AND is_queued = 0 AND status = 'pending'`
    )
    .run(expiresAt, requestId);

  if (result.changes) {
    logger.info('queue', `[queue] request #` + requestId + ' queued for up to ' + maxHours + 'h.');
  }
  return result.changes > 0;
}

// Removes a request from the queue without touching its status — used when it is
// accepted, so a queued request stops being re-offered even though the row keeps
// is_queued = 1 as a record that it once waited.
function clearQueued(requestId) {
  return db
    .prepare('UPDATE requests SET is_queued = 0 WHERE id = ? AND is_queued = 1')
    .run(requestId).changes;
}

// ---------------------------------------------------------------------------
// The pass
// ---------------------------------------------------------------------------

// One full pass: expire the dead, then re-offer the living.
//
// Order matters. Expiry runs first so a request that just timed out is cancelled
// and never re-offered in the same pass; if it ran second, a request could be
// offered to a technician moments before being cancelled.
function processQueue() {
  const maxHours = config.QUEUE_MAX_HOURS;
  // A SQLite modifier string rather than a JS Date, so the comparison happens in
  // one clock and one format. Passing an ISO string here would make SQLite parse
  // two different timestamp formats and silently compare them as text.
  const expiryModifier = '-' + maxHours + ' hours';

  const cancelled = cancelExpired(expiryModifier);
  const queued = db
    .prepare(
      `SELECT id FROM requests
        WHERE is_queued = 1 AND status = 'pending'
        ORDER BY queued_at ASC, id ASC
        LIMIT ${MAX_REQUESTS_PER_PASS}`
    )
    .all();

  // Cash confirmations the customer never answered settle on the technician's
  // figure. This rides the existing sweep rather than getting its own timer: both
  // are "the other side went quiet, act on it" jobs, and the queue already runs
  // every QUEUE_RECHECK_MINUTES.
  //
  // Wrapped because this sweep is about broadcasting, and a failure to settle a
  // stale confirmation must not stop the re-offering above from happening.
  let cashResolved = 0;
  try {
    const resolved = require('./payments').autoResolveStale();
    cashResolved = resolved.processed;
    if (cashResolved > 0) {
      logger.info('queue', `[queue] auto-resolved cash confirmations:`, cashResolved);
    }
  } catch (e) {
    logger.info('queue', `[queue] auto-resolve error:`, e.message);
  }

  const results = queued.map((row) => ({
    request_id: row.id,
    ...broadcast.rebroadcastQueued(row.id),
  }));
  const reoffered = results.filter((r) => r.sent > 0);

  const summary = {
    cancelled: cancelled.cancelled,
    queued: queued.length,
    reoffered: reoffered.length,
    offers: reoffered.reduce((sum, r) => sum + r.sent, 0),
    cash_resolved: cashResolved,
  };

  if (summary.cancelled || summary.offers) {
    logger.info('queue', `[queue] ` + JSON.stringify(summary));
  }
  return summary;
}

// Cancels queued requests that have outlived QUEUE_MAX_HOURS, and closes their
// open offers in the same transaction.
function cancelExpired(expiryModifier) {
  return db.transaction(() => {
    // The ids are collected BEFORE the update. Selecting them afterwards would
    // also catch requests an admin cancelled in the same second, and closing their
    // offers from here would put this service in charge of someone else's action.
    const doomed = db
      .prepare(
        `SELECT id FROM requests
          WHERE is_queued = 1
            AND status = 'pending'
            AND datetime(queued_at) <= datetime('now', ?)`
      )
      .all(expiryModifier);
    if (!doomed.length) return { cancelled: 0, offers_closed: 0 };

    const cancel = db.prepare(
      `UPDATE requests
          SET status = 'cancelled',
              updated_at = datetime('now'),
              cancel_reason = ?,
              is_queued = 0
        WHERE id = ? AND is_queued = 1 AND status = 'pending'`
    );
    const closeOffers = db.prepare(
      `UPDATE request_broadcasts
          SET status = 'expired', responded_at = datetime('now')
        WHERE request_id = ? AND status = 'sent'`
    );

    let cancelled = 0;
    let closed = 0;
    doomed.forEach((row) => {
      // Guarded on the same conditions as the SELECT, so an accept that lands
      // between the two statements wins and the row is left alone.
      const result = cancel.run(
        'لم يتم العثور على فني متاح خلال ' + config.QUEUE_MAX_HOURS + ' ساعة من إنشاء الطلب',
        row.id
      );
      if (!result.changes) return;
      cancelled += result.changes;
      closed += closeOffers.run(row.id).changes;
    });

    if (cancelled) {
      logger.info(
        'queue',
        `[queue] cancelled ` + cancelled + ' request(s) after ' + config.QUEUE_MAX_HOURS + 'h.'
      );
    }
    return { cancelled, offers_closed: closed };
  })();
}

// ---------------------------------------------------------------------------
// Timer
// ---------------------------------------------------------------------------

let started = false;
let timer = null;

// Starts the recheck loop. Safe to call more than once — server.js and a test can
// both reach for it without double-scheduling the work.
function start() {
  if (started) return timer;
  started = true;

  const minutes = config.QUEUE_RECHECK_MINUTES;
  timer = setInterval(
    () => {
      try {
        processQueue();
      } catch (e) {
        // A failed pass must not kill the interval: without this the queue would stop
        // re-offering for the rest of the process' life and nothing would log it.
        logger.info('queue', `[queue] pass failed:`, e.message);
      }
    },
    minutes * 60 * 1000
  );

  // unref() so the timer does not keep the event loop alive on its own; otherwise
  // the process would ignore SIGTERM and a test that requires this file would
  // hang instead of exiting.
  timer.unref();

  logger.info(
    'queue',
    `[queue] service started — recheck every ` +
      minutes +
      ' min, max wait ' +
      config.QUEUE_MAX_HOURS +
      'h'
  );
  return timer;
}

function stop() {
  if (timer) clearInterval(timer);
  timer = null;
  started = false;
}

module.exports = {
  start,
  stop,
  processQueue,
  markQueued,
  clearQueued,
  eligibleForQueuedRequest,
  qualifiedAreas,
  areaKeyFromLabel,
};
