/**
 * @file   Auto-pickup — reaches stranded requests when a technician frees up
 * @module auto-pickup
 *
 * A request created while no technician was available is not dead: it waits on the
 * waiting queue (services/queue.js) and is re-offered every QUEUE_RECHECK_MINUTES.
 * That tick is a safety net, not the response a customer is waiting for. When a
 * technician finishes a job and genuinely has nothing else to do, the oldest waiting
 * request should reach them NOW — not up to five minutes from now, and not after they
 * have closed the app.
 *
 * So there are two entry points here, both funnelling into the same offer:
 *   - forTechnician(id) — called the moment a technician becomes available.
 *   - sweep()           — a periodic pass, for the case where nobody pressed anything.
 *
 * WHAT THIS DOES NOT DO: it does not assign. Jobs are offered, not taken —
 * services/broadcast.js owns the accept, and the first acceptance still wins.
 * Auto-assigning a job somebody never tapped is worse than a short wait: they are
 * marked busy with work they did not agree to, and it disappears from the inbox of
 * the technicians who were in the middle of deciding.
 *
 * Division of labour with the other two services:
 *   - queue.js owns the lifetime of a stranded request (when it gives up and cancels
 *     it). This file only offers, and never cancels.
 *   - broadcast.js owns the offer rows and the accept. This file decides WHO to offer
 *     to and WHEN, and nothing else.
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Auto-pickup: offer waiting requests to technicians the moment they free up.
const db = require('../db');
const config = require('../config');
const broadcast = require('./broadcast');
const queue = require('./queue');
const notifications = require('./notifications');
const logger = require('../utils/logger');

// How often the background sweep runs.
//
// Shorter than QUEUE_RECHECK_MINUTES on purpose: the queue tick exists to expire and
// re-offer on a schedule, this one exists so a request does not sit unseen between two
// technician shifts. Repeat offers are de-duplicated by the offers table, so a tick
// that finds nobody new writes nothing.
const SWEEP_INTERVAL_MS = positiveIntEnvMs('AUTO_PICKUP_INTERVAL_MS', 2 * 60 * 1000);

// Requests one sweep may touch. A brake, not a policy: this is a single-node SQLite
// app, and a tick that re-offered a thousand rows would hold the write lock long
// enough for a technician's accept to time out behind it. Oldest first, which is also
// the fair order — the customer who has waited longest gets first refusal.
const MAX_REQUESTS_PER_SWEEP = 100;

function positiveIntEnvMs(name, fallback) {
  const raw = parseInt(process.env[name], 10);
  return Number.isInteger(raw) && raw > 0 ? raw : fallback;
}

// A request is stranded when it is pending, unclaimed, and was put on the waiting
// queue at creation because nobody was eligible then. Those are exactly the requests
// with no live offer, so offering them is reach rather than noise.
//
// Deliberately NOT all pending requests: a pending request that already has 'sent'
// offers is on its way to whoever received them, and re-running it every two minutes
// would do nothing but repeat work.
function strandedRequests(limit) {
  return db
    .prepare(
      `SELECT * FROM requests
        WHERE status = 'pending'
          AND is_queued = 1
          AND technician_id IS NULL
        ORDER BY created_at ASC, id ASC
        LIMIT ?`
    )
    .all(limit);
}

// Tells technicians about offers this pass just created.
//
// Only the technicians a call actually OFFERED to are notified — not the eligible
// list, which is longer once the ones who already saw the request are filtered out.
// Keying off the offer ids is what stops a notification going to somebody whose inbox
// was never touched.
function notifyOffered(technicianIds, request) {
  if (!technicianIds.length) return;

  const areaKey = queue.areaKeyFromLabel(request.location_text);
  const area = areaKey ? config.ALEXANDRIA_AREAS.find((a) => a.key === areaKey) : null;

  const payload = notifications.newRequestForTechnician({
    specialty: request.specialty,
    customText: request.custom_specialty_text,
    areaLabel: area ? area.label : null,
  });

  technicianIds.forEach((id) => {
    const tech = db.prepare('SELECT user_id FROM technicians WHERE id = ?').get(id);
    if (tech) notifications.notify(tech.user_id, payload);
  });
}

// ---------------------------------------------------------------------------
// Offering
// ---------------------------------------------------------------------------

// Offers one request to one technician, and says so in the log.
//
// Eligibility is borrowed rather than reimplemented, so this cannot drift from the
// rules the creation-time broadcast uses: queue.eligibleForQueuedRequest() is
// broadcast.eligibleTechnicians() (approved, available, inside the radius, clear of
// the debt ceiling) narrowed to the request's neighbourhood.
function offerTo(technicianId, request) {
  const eligible = queue.eligibleForQueuedRequest(request).some((t) => t.id === technicianId);
  if (!eligible) return false;

  const result = broadcast.rebroadcastQueued(request.id);
  if (!(result.technician_ids || []).includes(technicianId)) return false;

  // 'offered', not 'assigned': the job is now in their inbox, and it becomes theirs
  // only once they accept it.
  logger.info(
    'auto-pickup',
    `[AUTO-PICKUP] Offered pending request #${request.id} to technician #${technicianId}`
  );

  notifyOffered([technicianId], request);
  return true;
}

// The instant half: a technician has just become available, hand them the oldest
// waiting request they can actually take.
//
// Called from the endpoint that freed them, so it must not be able to fail that
// endpoint — a stranded request nobody picked up is recoverable by the next sweep,
// but a completion that reports an error because of one is not.
//
// Returns { offered, request_id } so the caller can tell the technician something is
// waiting for them.
function forTechnician(technicianId) {
  const none = { offered: 0, request_id: null };
  if (!technicianId) return none;

  try {
    const tech = db.prepare('SELECT availability FROM technicians WHERE id = ?').get(technicianId);
    // Not available means not eligible, and offering anyway would put a job in the
    // inbox of somebody who cannot start it.
    if (!tech || tech.availability !== 'available') return none;

    // The oldest matching request, and only that one: an offer list is a decision
    // list, and a technician handed fifteen jobs at once stops reading any of them.
    // Whatever is left behind is picked up by the next sweep or the next free
    // technician.
    //
    // The scan stops at the same cap the sweep uses, so one technician going free
    // cannot walk a thousand rows on the request that freed them.
    const candidate = strandedRequests(MAX_REQUESTS_PER_SWEEP).find((request) =>
      offerTo(technicianId, request)
    );
    if (!candidate) return none;

    return { offered: 1, request_id: candidate.id };
  } catch (e) {
    logger.info(
      'auto-pickup',
      `[AUTO-PICKUP] pickup for technician #${technicianId} failed:`,
      e.message
    );
    return none;
  }
}

// The periodic half: nobody pressed anything, so sweep the waiting requests and offer
// each one to whoever has become available since the last pass.
//
// Returns a summary, and logs nothing when there was nothing to do: a line every two
// minutes reporting zeroes is how the line that matters gets scrolled past.
function sweep() {
  const pending = strandedRequests(MAX_REQUESTS_PER_SWEEP);
  if (!pending.length) return { requests: 0, offers: 0, technicians: 0 };

  let offers = 0;
  const reached = new Set();

  pending.forEach((request) => {
    const result = broadcast.rebroadcastQueued(request.id);
    const sent = result.technician_ids || [];
    if (!sent.length) return;

    offers += sent.length;
    sent.forEach((id) => reached.add(id));
    notifyOffered(sent, request);

    logger.info(
      'auto-pickup',
      `[AUTO-PICKUP] Request #${request.id} is live with ` +
        sent.length +
        ' technician(s): ' +
        sent.map((id) => '#' + id).join(', ')
    );
  });

  const summary = { requests: pending.length, offers, technicians: reached.size };
  logger.info('auto-pickup', '[AUTO-PICKUP] sweep ' + JSON.stringify(summary));
  return summary;
}

// ---------------------------------------------------------------------------
// Timer
// ---------------------------------------------------------------------------

let started = false;
let timer = null;

// Safe to call more than once — server.js and a test can both reach for it without
// scheduling the work twice.
function start() {
  if (started) return timer;
  started = true;

  timer = setInterval(() => {
    try {
      sweep();
    } catch (e) {
      // A failed pass must not kill the interval: without this the sweep would stop
      // for the rest of the process' life and nothing would log it.
      logger.info('auto-pickup', '[AUTO-PICKUP] sweep failed:', e.message);
    }
  }, SWEEP_INTERVAL_MS);

  // unref() so the timer never keeps the event loop alive on its own; otherwise the
  // process would ignore SIGTERM, and a test that requires this file would hang
  // instead of exiting.
  timer.unref();

  logger.info(
    'auto-pickup',
    '[AUTO-PICKUP] started — sweeping waiting requests every ' +
      Math.round(SWEEP_INTERVAL_MS / 1000) +
      's'
  );
  return timer;
}

function stop() {
  if (timer) clearInterval(timer);
  timer = null;
  started = false;
}

module.exports = {
  SWEEP_INTERVAL_MS,
  MAX_REQUESTS_PER_SWEEP,
  start,
  stop,
  sweep,
  forTechnician,
  strandedRequests,
};
