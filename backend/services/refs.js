/**
 * @file   Reference data lookups
 * @module refs
 *
 * Shared read helpers for the reference tables (specialties, settings, cities) so routes do
 * not each hand-roll the same lookup.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Reference data service: DB-backed specialties + key/value settings.
const db = require('../db');
const config = require('../config');

function getSpecialties(includeDisabled) {
  const rows = db
    .prepare(
      `SELECT * FROM specialties
       ORDER BY sort_order ASC, id ASC`
    )
    .all();
  const available = rows.length > 0;

  // Never return empty: fall back to config if the table was not seeded yet.
  const base = available
    ? rows
    : config.SPECIALTIES.map((s, i) => ({
        key: s.key,
        label: s.label,
        icon: s.icon || config.DEFAULT_SPECIALTY_ICON,
        enabled: 1,
        sort_order: i,
      }));

  return includeDisabled ? base : base.filter((s) => s.enabled);
}

function getSpecialtyKeys() {
  return getSpecialties(true).map((s) => s.key);
}

function getSettings() {
  const rows = db.prepare('SELECT key, value FROM settings').all();
  const out = {};
  rows.forEach((r) => {
    out[r.key] = r.value;
  });
  return out;
}

function getSetting(key, fallback) {
  const row = db.prepare('SELECT value FROM settings WHERE key = ?').get(key);
  return row ? row.value : fallback;
}

function setSetting(key, value) {
  db.prepare(
    `INSERT INTO settings (key, value) VALUES (?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value`
  ).run(key, value);
}

module.exports = {
  getSpecialties,
  getSpecialtyKeys,
  getSettings,
  getSetting,
  setSetting,
};
