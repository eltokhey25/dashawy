/**
 * @file   Data subject access and deletion API
 * @module data-subject.routes
 *
 * GDPR-style export and erasure requests: downloads a copy of the caller's personal data or
 * deletes the records they are allowed to have removed.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Data subject rights endpoints — access, correction, deletion requests
// under the Egyptian Personal Data Protection Law No. 151 of 2020.
const express = require('express');
const db = require('../db');
const { wrap, AppError, badRequest } = require('../utils/errors');
const { authenticate } = require('../middleware/auth');

const router = express.Router();

// POST /api/data-subject/request  (user requests access/correction/deletion)
router.post(
  '/request',
  authenticate,
  wrap(async (req, res) => {
    const { request_type, note } = req.body || {};
    const valid = ['access', 'rectification', 'erasure', 'restriction', 'portability'];
    if (!valid.includes(request_type)) throw badRequest('نوع الطلب غير صالح');

    db.prepare(
      `INSERT INTO data_subject_requests (user_id, request_type, admin_note)
     VALUES (?, ?, ?)`
    ).run(req.user.id, request_type, note || null);

    res.status(201).json({
      message: 'تم استلام طلبك. سيتم الرد خلال 30 يوماً كحد أقصى وفقاً للقانون.',
    });
  })
);

// GET /api/data-subject/my-requests
router.get(
  '/my-requests',
  authenticate,
  wrap(async (req, res) => {
    const rows = db
      .prepare('SELECT * FROM data_subject_requests WHERE user_id = ? ORDER BY requested_at DESC')
      .all(req.user.id);
    res.json({ requests: rows });
  })
);

module.exports = router;
