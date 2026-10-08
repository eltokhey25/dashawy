/**
 * @file   Start PIN minting.
 * @module services/startPin
 *
 * A 4-digit code that the customer reads off their request screen and the
 * technician types in at the door to move a job to 'in_progress'. Its only job is
 * to prove the technician is standing in front of the customer who booked the
 * job, so a job cannot be started by whoever happens to be assigned it.
 *
 * It lives in its own module because three different places assign a request and
 * therefore all three must mint one:
 *   - broadcast.acceptBroadcast()  — the normal "technician accepted the offer"
 *   - assignment.assignTechnician() — used by reassignAfterRejection()
 *   - the admin "assign a technician by hand" action
 * Missing one of them produced a job that could never be started, because the
 * only route to 'in_progress' refuses a request with no PIN.
 *
 * It could not live in either assignment or broadcast without a require cycle:
 * broadcast already requires assignment.
 *
 * @author Eltokhey
 * @since  2026-10
 */
const crypto = require('crypto');

const MIN = 1000;
const MAX = 10000; // exclusive — 1000..9999, always exactly four digits

/**
 * Mint a fresh Start PIN.
 *
 * Uses crypto.randomInt rather than Math.random: this is the only thing standing
 * between a technician and a job they were not assigned, and Math.random's
 * output is predictable from a handful of observed values. Range and width are
 * identical to the Math.random() version it replaces — 1000..9999 inclusive.
 *
 * @returns {string} a four-digit string, e.g. '0417'
 */
function mint() {
  return String(crypto.randomInt(MIN, MAX));
}

module.exports = { mint, MIN, MAX };
