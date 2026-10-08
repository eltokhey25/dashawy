/**
 * @file   Consent and terms API
 * @module consent.routes
 *
 * Records and serves the terms-of-service and privacy consent trail required before a customer
 * can create a request.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Consent routes — records/logs explicit consent under the Egyptian
// Personal Data Protection Law No. 151 of 2020.
const express = require('express');
const db = require('../db');
const { wrap, AppError, badRequest } = require('../utils/errors');
const { authenticate } = require('../middleware/auth');

const router = express.Router();

// POST /api/consent  (record consent for the logged-in user)
router.post(
  '/',
  authenticate,
  wrap(async (req, res) => {
    const { consent_type, policy_version } = req.body || {};
    if (!consent_type || !policy_version) {
      throw badRequest('نوع الموافقة ونسخة السياسة مطلوبان');
    }
    const ip = req.headers['x-forwarded-for'] || req.socket.remoteAddress || null;
    const ua = req.headers['user-agent'] || null;

    db.prepare(
      `INSERT INTO consents (user_id, consent_type, ip_address, user_agent, policy_version)
     VALUES (?, ?, ?, ?, ?)`
    ).run(req.user.id, consent_type, ip, ua, policy_version);

    res.status(201).json({ message: 'تم تسجيل الموافقة بنجاح' });
  })
);

// POST /api/consent/withdraw  (withdraw a previously granted consent)
router.post(
  '/withdraw',
  authenticate,
  wrap(async (req, res) => {
    const { consent_type } = req.body || {};
    if (!consent_type) throw badRequest('نوع الموافقة مطلوب');

    const result = db
      .prepare(
        `UPDATE consents SET withdrawn_at = datetime('now')
     WHERE user_id = ? AND consent_type = ? AND withdrawn_at IS NULL`
      )
      .run(req.user.id, consent_type);

    if (result.changes === 0) {
      throw new AppError('لا توجد موافقة نشطة من هذا النوع', 404);
    }

    res.json({ message: 'تم سحب الموافقة. سيتم حذف بياناتك الحساسة خلال 30 يوماً.' });
  })
);

// GET /api/consent  (list current user's consents)
router.get(
  '/',
  authenticate,
  wrap(async (req, res) => {
    const rows = db
      .prepare('SELECT * FROM consents WHERE user_id = ? ORDER BY granted_at DESC')
      .all(req.user.id);
    res.json({ consents: rows });
  })
);

module.exports = router;
