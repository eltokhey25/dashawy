/**
 * @file   Geo helpers — Haversine distance, ETA and staleness
 * @module geo
 *
 * Shared by the live-tracking endpoints (routes/customer.routes.js and
 * routes/technician.routes.js) so both sides of a job compute the same numbers
 * from the same pair of points. Deliberately free of I/O — no database, no
 * config, no request — which is what makes it safe to call from a route handler
 * on every poll.
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Great-circle geometry and travel-time estimates.
const EARTH_RADIUS_KM = 6371;

// Distance in km between two lat/lng pairs, on a spherical earth.
function haversineKm(lat1, lng1, lat2, lng2) {
  // A missing point is NaN distance, not 0: the caller must be able to tell
  // "nobody has reported a position" apart from "they are already here".
  // Number(null) is 0 and Number('') is 0, so the hasOwn/empty cases are
  // rejected BEFORE coercion — otherwise an absent latitude would be read as a
  // point off the coast of Africa and produce a confident wrong distance.
  const raw = [lat1, lng1, lat2, lng2];
  if (raw.some((v) => v === null || v === undefined || v === '')) return NaN;
  const nums = raw.map(Number);
  if (nums.some((n) => !Number.isFinite(n))) return NaN;

  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(nums[2] - nums[0]);
  const dLng = toRad(nums[3] - nums[1]);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(nums[0])) * Math.cos(toRad(nums[2])) * Math.sin(dLng / 2) ** 2;
  // The min(1, …) clamp matters: for two identical points floating point can
  // push `a` a hair above 1, and Math.asin(>1) is NaN — which would turn "they
  // are standing on the same spot" into "distance unknown".
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(Math.min(1, a)));
}

// Rough driving time in minutes.
//
// A flat city average, deliberately: this is an ETA hint for a map screen, not a
// routing promise. Returns NaN for an unknown distance rather than a fabricated
// 1-minute arrival, so a missing point can never render as "arriving now".
const AVERAGE_CITY_SPEED_KMH = 30;

function estimateEtaMinutes(distanceKm) {
  const d =
    distanceKm === null || distanceKm === undefined || distanceKm === '' ? NaN : Number(distanceKm);
  if (!Number.isFinite(d)) return NaN;
  if (d <= 0) return 0;
  return Math.max(1, Math.round((d / AVERAGE_CITY_SPEED_KMH) * 60));
}

// Whether a reported point is older than the staleness budget.
//
// Timestamps are stored by SQLite's datetime('now'), i.e. "YYYY-MM-DD HH:MM:SS"
// in UTC with no zone marker, so the space is normalised to a 'T' and 'Z' is
// appended before parsing. A missing or unparseable value counts as stale: the
// only safe reading of an unreadable timestamp is "do not present this as live".
function isStale(updatedAt, staleSeconds, nowMs) {
  if (!updatedAt) return true;
  const parsed = Date.parse(String(updatedAt).trim().replace(' ', 'T') + 'Z');
  if (!Number.isFinite(parsed)) return true;
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  const budget = Number(staleSeconds) * 1000;
  if (!Number.isFinite(budget) || budget <= 0) return false;
  return now - parsed > budget;
}

module.exports = {
  haversineKm,
  estimateEtaMinutes,
  isStale,
  EARTH_RADIUS_KM,
  AVERAGE_CITY_SPEED_KMH,
};
