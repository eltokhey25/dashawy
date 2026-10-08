/**
 * @file   Automatic job assignment service
 * @module assignment
 *
 * Picks the best technician for a new request using specialty, distance and availability, with
 * the matching logic kept as small pure functions.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Auto-assignment service.
// The matching + assignment logic lives here as a small set of pure-ish
// functions so it can easily be extended (optimised routes, priority tiers, etc.)
const db = require('../db');
const { AppError } = require('../utils/errors');
const startPinService = require('./startPin');

// Haversine distance between two coordinates in kilometres.
function haversineKm(lat1, lng1, lat2, lng2) {
  const R = 6371;
  const toRad = (deg) => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// Simple text-proximity score based on shared tokens in location descriptions.
// Used as a fallback when coordinates are missing.
function areaMatchScore(a, b) {
  const tokens = (s) =>
    new Set(
      String(s || '')
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s]/gu, ' ')
        .split(/\s+/)
        .filter(Boolean)
    );
  const ta = tokens(a);
  const tb = tokens(b);
  let overlap = 0;
  ta.forEach((tok) => {
    if (tb.has(tok)) overlap += 1;
  });
  return overlap;
}

// Reads the list of technicians that already declined this request.
function getDeclinedTechnicians(declinedJson) {
  try {
    const arr = JSON.parse(declinedJson || '[]');
    return Array.isArray(arr) ? arr.map(Number) : [];
  } catch (err) {
    return [];
  }
}

function setDeclinedTechnicians(requestId, ids) {
  db.prepare(
    "UPDATE requests SET declined_technicians = ?, updated_at = datetime('now') WHERE id = ?"
  ).run(JSON.stringify(ids), requestId);
}

// All approved + available technicians. Since the site now handles dish
// services only, every technician serves all dish service types, so the
// `specialty` parameter is intentionally ignored. The signature stays
// unchanged to avoid breaking existing callers.
function candidateTechnicians(specialty, excludeIds) {
  const rows = db
    .prepare(
      `SELECT id, latitude, longitude, location_text, availability, created_at
       FROM technicians
       WHERE availability = 'available' AND approval_status = 'approved'`
    )
    .all();

  const exclude = new Set(excludeIds.filter((id) => Number.isFinite(id)));
  return rows.filter((t) => !exclude.has(t.id));
}

// Orders candidates best-first using distance, falling back to location-text
// proximity. Specialty is no longer a factor — all technicians handle all
// dish service types.
function rankTechnicians(candidates, request) {
  const lat = request.latitude;
  const lng = request.longitude;

  return candidates
    .map((t) => {
      let distance = null;
      if (lat != null && lng != null && t.latitude != null && t.longitude != null) {
        distance = haversineKm(lat, lng, t.latitude, t.longitude);
      }
      const areaScore =
        distance !== null ? 0 : areaMatchScore(t.location_text, request.location_text);
      return { ...t, distance, areaScore };
    })
    .sort((a, b) => {
      // Candidates with usable coordinates are ranked by distance.
      if (a.distance !== null && b.distance !== null) return a.distance - b.distance;
      if (a.distance !== null) return -1;
      if (b.distance !== null) return 1;
      // Otherwise prefer a better location-text match, then oldest registrations.
      if (a.areaScore !== b.areaScore) return b.areaScore - a.areaScore;
      return a.id - b.id;
    });
}

// Core assignment function.
// Tries to assign the best available technician to the request:
//   1. closest distance (or location-text match).
// All technicians handle all dish service types, so specialty is irrelevant.
// Returns the assigned technician object, or null if no candidate is available.
function assignTechnician(requestId) {
  const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(requestId);
  if (!request) throw new AppError('Request not found', 404);

  // Only pending requests can be assigned.
  if (request.status !== 'pending') return null;

  const declined = getDeclinedTechnicians(request.declined_technicians);
  const candidates = candidateTechnicians(request.specialty, declined);
  const ranked = rankTechnicians(candidates, request);

  if (ranked.length === 0) return null;

  const best = ranked[0];

  const run = db.transaction(() => {
    db.prepare(
      `UPDATE requests
       SET technician_id = ?, status = 'assigned', assigned_at = datetime('now'),
           updated_at = datetime('now'),
           start_pin = ?, start_pin_attempts = 0, start_pin_locked_until = NULL,
           started_at = NULL
       WHERE id = ? AND status = 'pending'`
    ).run(best.id, startPinService.mint(), requestId);

    db.prepare("UPDATE technicians SET availability = 'busy' WHERE id = ?").run(best.id);
  });

  run();
  return db.prepare('SELECT * FROM technicians WHERE id = ?').get(best.id);
}

// Marks a technician as available again (after reject, complete, or cancel).
function releaseTechnician(technicianId) {
  if (!technicianId) return;
  db.prepare("UPDATE technicians SET availability = 'available' WHERE id = ?").run(technicianId);
}

// Attempts to reassign a request that was just rejected by a technician.
// Returns the new technician or null.
function reassignAfterRejection(requestId, rejectedTechnicianId) {
  const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(requestId);
  if (!request) throw new AppError('Request not found', 404);

  const declined = getDeclinedTechnicians(request.declined_technicians);
  if (rejectedTechnicianId && !declined.includes(rejectedTechnicianId)) {
    declined.push(rejectedTechnicianId);
  }
  // Frees the technician that rejected the job.
  releaseTechnician(rejectedTechnicianId);
  setDeclinedTechnicians(requestId, declined);

  return assignTechnician(requestId);
}

module.exports = {
  haversineKm,
  areaMatchScore,
  assignTechnician,
  releaseTechnician,
  reassignAfterRejection,
};
