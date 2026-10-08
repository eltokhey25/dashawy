/**
 * @file   Public unauthenticated API
 * @module public.routes
 *
 * The intentionally open reads used by the marketing site: site settings, published
 * specialties and featured technician profiles. No personal data is exposed — the
 * settings table holds branding/contact values only, never operational toggles or
 * anything user-specific.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Public technician directory and profiles (Phase 1).
// Mounted at /api/public BEFORE the global JWT guard, so no token is required.
// Only technicians approved by an admin are ever exposed here.
const express = require('express');
const db = require('../db');
const config = require('../config');
const refs = require('../services/refs');
const { wrap, AppError, badRequest } = require('../utils/errors');
const { isValidSpecialty } = require('../utils/validation');

const router = express.Router();

// Columns safe to expose publicly. Deliberately excludes: user_id, phone,
// criminal_record_path, national_id_path, documents_verified, approval_note,
// approved_by. Contact details stay behind the assignment flow.
const PUBLIC_TECHNICIAN_COLUMNS = `
  t.id, t.name, t.specialty, t.location_text, t.latitude, t.longitude,
  t.availability, t.approved_at, t.created_at
`;

const MAX_PUBLIC_REVIEWS = 50;

// Rating / workload numbers. The three expressions are kept separate from their
// aliases so each one can be projected on its own (e.g. rounded avg) without
// nesting an "AS" inside a function call. LEFT JOIN reviews keeps technicians
// with no reviews in the list (review_count 0, avg_rating 0 -> null in JSON).
// The list query MUST group by technician: an aggregate query with no GROUP BY
// collapses to a single row, which would make the whole directory render as
// one card. The single-profile query below deliberately has no GROUP BY — there
// the id filter is what decides "not found".
const AVG_RATING_SQL = 'ROUND(COALESCE(AVG(rv.rating), 0), 1)';
const REVIEW_COUNT_SQL = 'COALESCE(COUNT(rv.id), 0)';
const COMPLETED_COUNT_SQL =
  "(SELECT COUNT(*) FROM requests rq WHERE rq.technician_id = t.id AND rq.status = 'completed')";
// Moderated reviews (hidden = 1 by an admin) are invisible to the public: they
// must not appear in the profile, must not be listed, and must not count towards
// the average rating or the review count. Filtering in the JOIN condition (not in
// a WHERE clause) keeps technicians with zero visible reviews in the list.
const REVIEWS_JOIN = 'LEFT JOIN reviews rv ON rv.technician_id = t.id AND rv.hidden = 0';
const STATS_SELECT = `
  ${AVG_RATING_SQL}      AS avg_rating,
  ${REVIEW_COUNT_SQL}    AS review_count,
  ${COMPLETED_COUNT_SQL} AS completed_count
`;

const PUBLIC_REVIEWS_SELECT = `
  SELECT rv.id, rv.rating, rv.comment, rv.technician_reply, rv.replied_at, rv.created_at
  FROM reviews rv
`;

// Keys safe to expose to an anonymous visitor. Deliberately excludes the
// operational settings that /api/admin/settings owns (auto_assign_*), matching the
// subset the app-level /api/settings endpoint already publishes.
const PUBLIC_SETTING_KEYS = [
  'site_name',
  'site_tagline',
  'contact_phone',
  'contact_email',
  'contact_address',
  'whatsapp',
  'facebook_url',
  'instagram_url',
  'working_hours',
  'hero_title',
  'hero_subtitle',
];

// GET /api/public/settings
// Backs the dynamic footer: site name, tagline, contact details and the social
// links. Values are plain strings so the client can write them straight into the DOM.
router.get(
  '/settings',
  wrap(async (req, res) => {
    const all = refs.getSettings();
    const settings = {};
    PUBLIC_SETTING_KEYS.forEach((key) => {
      settings[key] = all[key] || '';
    });
    res.json({ settings });
  })
);

// GET /api/public/pricing
// The active tariff, as a flat key -> price map. Only service_key and price are
// returned; is_active/updated_at are admin bookkeeping and do not belong in a
// public payload.
router.get(
  '/pricing',
  wrap(async (req, res) => {
    const rows = db
      .prepare('SELECT service_key, price FROM service_pricing WHERE is_active = 1')
      .all();
    const pricing = {};
    rows.forEach((r) => {
      pricing[r.service_key] = Number(r.price);
    });
    res.json({ pricing });
  })
);

// GET /api/public/technicians
// Optional filters: ?area=KEY (Alexandria area) and ?specialty=KEY.
router.get(
  '/technicians',
  wrap(async (req, res) => {
    const { area, specialty } = req.query;

    const where = ["t.approval_status = 'approved'"];
    const params = [];

    if (area !== undefined && area !== '') {
      const areaRow = config.ALEXANDRIA_AREAS.find((a) => a.key === area);
      if (!areaRow) throw badRequest('المنطقة غير صالحة');
      // Technicians store the human-readable area label in location_text.
      where.push('t.location_text = ?');
      params.push(areaRow.label);
    }

    if (specialty !== undefined && specialty !== '') {
      if (!isValidSpecialty(specialty)) throw badRequest('نوع الخدمة غير صالح');
      where.push('t.specialty = ?');
      params.push(specialty);
    }

    const technicians = db
      .prepare(
        `SELECT ${PUBLIC_TECHNICIAN_COLUMNS},
                p.bio, p.years_experience, p.profile_image_path,
                ${STATS_SELECT}
         FROM technicians t
         LEFT JOIN technician_profiles p ON p.technician_id = t.id
         ${REVIEWS_JOIN}
         WHERE ${where.join(' AND ')}
         GROUP BY t.id
         ORDER BY avg_rating DESC, review_count DESC, t.name ASC`
      )
      .all(...params);

    res.json({ technicians });
  })
);

// GET /api/public/technicians/:id
router.get(
  '/technicians/:id',
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    if (!Number.isInteger(id)) throw badRequest('معرف الفني غير صالح');

    const technician = db
      .prepare(
        `SELECT ${PUBLIC_TECHNICIAN_COLUMNS}, ${STATS_SELECT}
         FROM technicians t
         ${REVIEWS_JOIN}
         WHERE t.id = ? AND t.approval_status = 'approved'`
      )
      .get(id);
    // This query aggregates without a GROUP BY, so SQLite always returns exactly
    // one row: for a non-matching id that row is all-NULL rather than absent.
    // Testing the id (not the row) is therefore what actually detects "not found".
    if (!technician || technician.id === null) {
      throw new AppError('الفني غير موجود', 404);
    }

    const profile =
      db
        .prepare(
          `SELECT bio, years_experience, profile_image_path, updated_at
         FROM technician_profiles WHERE technician_id = ?`
        )
        .get(id) || null;

    const portfolio = db
      .prepare(
        `SELECT id, image_path, caption, created_at
         FROM technician_portfolio WHERE technician_id = ? ORDER BY created_at DESC, id DESC`
      )
      .all(id);

    const reviews = db
      .prepare(
        `${PUBLIC_REVIEWS_SELECT}
         WHERE rv.technician_id = ? AND rv.hidden = 0
         ORDER BY rv.created_at DESC, rv.id DESC LIMIT ?`
      )
      .all(id, MAX_PUBLIC_REVIEWS);

    res.json({
      technician,
      profile,
      portfolio,
      stats: {
        // No reviews yet: surface null rather than a misleading 0.0 average.
        avg_rating: technician.avg_rating ? technician.avg_rating : null,
        review_count: technician.review_count || 0,
        completed_count: technician.completed_count || 0,
      },
      reviews,
    });
  })
);

module.exports = router;
