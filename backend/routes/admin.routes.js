/**
 * @file   Admin dashboard API
 * @module admin.routes
 *
 * All /api/admin endpoints. Each route is guarded by an explicit permission check on top of
 * the router-level admin role requirement.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Admin routes: dashboard stats, technician management, all requests & customers.
const express = require('express');
const bcrypt = require('bcryptjs');
const fs = require('fs');
const path = require('path');
const db = require('../db');
const config = require('../config');
const broadcast = require('../services/broadcast');
const { wrap, AppError, badRequest, forbidden, serviceError } = require('../utils/errors');
const { requireRole, requireSuperAdmin, requirePermission } = require('../middleware/auth');
const {
  isValidEmail,
  isValidPhone,
  isValidName,
  isNonEmptyString,
  isValidSpecialty,
  isStrongPassword,
  passwordPolicyError,
  parseLatLng,
  normalizeEmail,
} = require('../utils/validation');

const router = express.Router();

// Every admin route requires an admin role.
router.use(requireRole('admin'));

// ---------- Dashboard stats ----------

// GET /api/admin/stats
router.get(
  '/stats',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const count = (sql, ...args) => db.prepare(sql).get(...args);

    const totalRequests = count('SELECT COUNT(*) AS n FROM requests').n;
    const byStatus = {};
    for (const s of config.STATUSES) {
      byStatus[s] = db.prepare('SELECT COUNT(*) AS n FROM requests WHERE status = ?').get(s).n;
    }
    const totalCustomers = count('SELECT COUNT(*) AS n FROM customers').n;
    const totalTechnicians = count('SELECT COUNT(*) AS n FROM technicians').n;
    const pendingTechnicians = count(
      "SELECT COUNT(*) AS n FROM technicians WHERE approval_status = 'pending'"
    ).n;
    const availableTechniciansAll = count(
      "SELECT COUNT(*) AS n FROM technicians WHERE availability = 'available'"
    ).n;
    const busyTechniciansAll = count(
      "SELECT COUNT(*) AS n FROM technicians WHERE availability = 'busy'"
    ).n;
    const newestRequests = db
      .prepare(
        `SELECT r.*, c.name AS customer_name, c.phone AS customer_phone,
                c.location_text AS customer_location, t.name AS technician_name,
                t.phone AS technician_phone
         FROM requests r
         JOIN customers c ON c.id = r.customer_id
         LEFT JOIN technicians t ON t.id = r.technician_id
         ORDER BY r.created_at DESC LIMIT 8`
      )
      .all();

    // Average response time (created_at -> assigned_at) in minutes, where assigned.
    const avgResponse = db
      .prepare(
        `SELECT ROUND(AVG((julianday(assigned_at) - julianday(created_at)) * 24 * 60), 0) AS minutes
         FROM requests WHERE assigned_at IS NOT NULL`
      )
      .get().minutes;

    // Requests per day over the last 7 days (for the trend chart).
    const dayRows = db
      .prepare(
        `SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n
         FROM requests
         WHERE created_at >= datetime('now', '-6 days')
         GROUP BY day`
      )
      .all();
    const byDay = {};
    dayRows.forEach((row) => {
      byDay[row.day] = row.n;
    });

    res.json({
      totalRequests,
      byStatus,
      totalCustomers,
      totalTechnicians,
      pendingTechnicians,
      availableTechnicians: availableTechniciansAll,
      busyTechnicians: busyTechniciansAll,
      avgResponseMinutes: avgResponse == null ? null : Number(avgResponse),
      byDay,
      newestRequests,
    });
  })
);

// The full set of section permissions a sub-admin can be granted.
const VALID_PERMS = [
  'stats',
  'customers',
  'technicians',
  'requests',
  'reviews',
  'specialties',
  'settings',
  'data_requests',
];

// Sanitises a client-supplied permissions array down to known keys.
function sanitizePermissions(permissions) {
  return Array.isArray(permissions) ? permissions.filter((p) => VALID_PERMS.includes(p)) : [];
}

// ---------- Admin management (super admin only) ----------

// GET /api/admin/admins
router.get(
  '/admins',
  requireSuperAdmin,
  wrap(async (req, res) => {
    const rows = db
      .prepare(
        `SELECT id, email, is_super_admin, blocked, must_change_password, created_at, permissions
     FROM users
     WHERE role = ?
     ORDER BY is_super_admin DESC, id ASC`
      )
      .all('admin')
      .map((r) => ({
        ...r,
        permissions: (() => {
          try {
            return JSON.parse(r.permissions || '[]');
          } catch (e) {
            return [];
          }
        })(),
      }));
    res.json({ admins: rows });
  })
);

// POST /api/admin/admins  (create a new sub-admin)
router.post(
  '/admins',
  requireSuperAdmin,
  wrap(async (req, res) => {
    const { email, password, permissions, is_super_admin: superFlag } = req.body || {};
    const bcrypt = require('bcryptjs');

    if (!isValidEmail(email)) throw badRequest('البريد الإلكتروني غير صالح');
    const pwProblem = passwordPolicyError(password);
    if (pwProblem) throw badRequest(pwProblem);

    const permsArray = sanitizePermissions(permissions);

    // Accepted as true / 1 / "1" and nothing else. A checkbox that arrives
    // unchecked, absent, or as the string "false" all mean the same thing —
    // a sub-admin — so the default has to be the safe one.
    const isSuper = superFlag === true || superFlag === 1 || superFlag === '1';

    const normEmail = normalizeEmail(email);
    if (db.prepare('SELECT id FROM users WHERE email = ?').get(normEmail)) {
      throw badRequest('هذا البريد الإلكتروني مستخدم بالفعل');
    }

    const hash = bcrypt.hashSync(password, 12);
    const result = db
      .prepare(
        `INSERT INTO users (email, password_hash, role, is_super_admin, must_change_password, permissions)
     VALUES (?, ?, 'admin', ?, 0, ?)`
      )
      .run(normEmail, hash, isSuper ? 1 : 0, JSON.stringify(permsArray));

    res.status(201).json({
      message: 'تم إضافة المسؤول بنجاح',
      admin: {
        id: result.lastInsertRowid,
        email: normEmail,
        is_super_admin: isSuper,
        permissions: permsArray,
      },
    });
  })
);

// PUT /api/admin/admins/:id  (update permissions only)
router.put(
  '/admins/:id',
  requireSuperAdmin,
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    const { permissions } = req.body || {};

    const target = db
      .prepare('SELECT id, email, role, is_super_admin FROM users WHERE id = ?')
      .get(id);
    if (!target || target.role !== 'admin') throw new AppError('المسؤول غير موجود', 404);
    if (target.is_super_admin) throw forbidden('لا يمكن تعديل صلاحيات المدير العام');

    const permsArray = sanitizePermissions(permissions);
    db.prepare('UPDATE users SET permissions = ? WHERE id = ?').run(JSON.stringify(permsArray), id);

    res.json({ message: 'تم تحديث الصلاحيات', permissions: permsArray });
  })
);

// POST /api/admin/admins/:id/reset-password
// The super admin may reset any admin's password, including their own. The
// must_change_password flag is deliberately left untouched: admins are not
// forced to change the password again after signing in.
router.post(
  '/admins/:id/reset-password',
  requireSuperAdmin,
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    const { new_password } = req.body || {};
    const bcrypt = require('bcryptjs');

    const newPwProblem = passwordPolicyError(new_password);
    if (newPwProblem) throw badRequest(newPwProblem);

    const target = db.prepare('SELECT id, role FROM users WHERE id = ?').get(id);
    if (!target || target.role !== 'admin') throw new AppError('المسؤول غير موجود', 404);

    const hash = bcrypt.hashSync(new_password, 12);
    db.prepare('UPDATE users SET password_hash = ? WHERE id = ?').run(hash, id);

    res.json({ message: 'تم تغيير كلمة المرور. يمكن للمسؤول تسجيل الدخول مباشرة.' });
  })
);

// POST /api/admin/admins/:id/toggle-active  (disable/enable a sub-admin)
router.post(
  '/admins/:id/toggle-active',
  requireSuperAdmin,
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    const target = db.prepare('SELECT id, role, is_super_admin FROM users WHERE id = ?').get(id);

    if (!target || target.role !== 'admin') throw new AppError('المسؤول غير موجود', 404);
    if (target.is_super_admin) throw forbidden('لا يمكن إيقاف حساب المدير العام');
    if (id === req.user.id) throw forbidden('لا يمكنك إيقاف حسابك الشخصي');

    const current = db.prepare('SELECT blocked FROM users WHERE id = ?').get(id);
    const newVal = current.blocked ? 0 : 1;
    db.prepare('UPDATE users SET blocked = ? WHERE id = ?').run(newVal, id);

    res.json({
      message: newVal ? 'تم إيقاف حساب المسؤول' : 'تم تفعيل حساب المسؤول',
      blocked: !!newVal,
    });
  })
);

// DELETE /api/admin/admins/:id
router.delete(
  '/admins/:id',
  requireSuperAdmin,
  wrap(async (req, res) => {
    const id = Number(req.params.id);

    // The one refusal left. Deleting yourself would end the only session that
    // can reach this endpoint, and the account would have to be recreated
    // through the boot-time recovery block.
    if (id === req.user.id) throw new AppError('مش ممكن تحذف حسابك بنفسك', 400);

    const target = db.prepare('SELECT id, role, is_super_admin FROM users WHERE id = ?').get(id);
    if (!target || target.role !== 'admin') throw new AppError('المسؤول غير موجود', 404);

    // Every remaining admin-management route is gated on requireSuperAdmin, so
    // removing the last one leaves nobody able to create another — the panel
    // could only be recovered by redeploying with FORCE_RESET_ADMIN. Counted
    // across all super admins rather than just the page's visible rows, so a
    // second account cannot be hidden from the check by being blocked.
    if (target.is_super_admin) {
      const { count } = db
        .prepare("SELECT COUNT(*) AS count FROM users WHERE role = 'admin' AND is_super_admin = 1")
        .get();
      if (count <= 1) {
        throw new AppError('مش ممكن تحذف آخر مدير عام. عيّن مدير عام تاني الأول.', 400);
      }
    }

    // Any other admin can go, super admin or not. requireSuperAdmin above is
    // what keeps the endpoint restricted; a second role check here would only
    // stop one super admin from removing a rival, which is not a protection
    // worth having — the guard that matters is the self-check above.
    db.prepare('DELETE FROM users WHERE id = ?').run(id);
    res.json({ message: 'تم حذف المسؤول' });
  })
);

// ---------- Technician management ----------

// GET /api/admin/technicians/pending  (applicants awaiting approval)
router.get(
  '/technicians/pending',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const rows = db
      .prepare(
        `SELECT t.*, u.email, u.created_at AS account_created_at
       FROM technicians t JOIN users u ON u.id = t.user_id
       WHERE t.approval_status = 'pending'
       ORDER BY t.created_at DESC`
      )
      .all();
    res.json({ technicians: rows });
  })
);

// POST /api/admin/technicians/:id/approve  (activate a pending applicant)
router.post(
  '/technicians/:id/approve',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const t = db.prepare('SELECT * FROM technicians WHERE id = ?').get(Number(req.params.id));
    if (!t) throw new AppError('الفني غير موجود', 404);
    db.prepare(
      `UPDATE technicians
       SET approval_status = 'approved', availability = 'available',
           documents_verified = 1, approved_at = datetime('now'),
           approved_by = ?
       WHERE id = ?`
    ).run(req.user.id, t.id);
    res.json({ message: 'تم قبول الفني وتفعيل حسابه' });
  })
);

// POST /api/admin/technicians/:id/reject  (reject the application; optional note)
router.post(
  '/technicians/:id/reject',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const t = db.prepare('SELECT * FROM technicians WHERE id = ?').get(Number(req.params.id));
    if (!t) throw new AppError('الفني غير موجود', 404);
    const note = req.body && req.body.note ? String(req.body.note).slice(0, 500) : null;
    db.prepare(
      `UPDATE technicians
       SET approval_status = 'rejected', availability = 'offline',
           approval_note = ?, approved_at = datetime('now'), approved_by = ?
       WHERE id = ?`
    ).run(note, req.user.id, t.id);
    res.json({ message: 'تم رفض طلب الفني' });
  })
);

// GET /api/admin/technicians
router.get(
  '/technicians',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const technicians = db
      .prepare(
        `SELECT t.*, u.email, u.phone AS account_phone, u.created_at AS account_created_at
         FROM technicians t
         JOIN users u ON u.id = t.user_id
         ORDER BY t.created_at DESC`
      )
      .all();
    res.json({ technicians });
  })
);

// POST /api/admin/technicians  (admin-only creation)
router.post(
  '/technicians',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const {
      email,
      password,
      name,
      phone,
      specialty,
      area_key,
      location_text,
      latitude,
      longitude,
      availability,
    } = req.body || {};

    if (!isValidEmail(email)) throw badRequest('البريد الإلكتروني غير صالح');
    const normEmail = normalizeEmail(email);
    if (db.prepare('SELECT id FROM users WHERE email = ?').get(normEmail)) {
      throw badRequest('هذا البريد الإلكتروني مستخدم بالفعل');
    }
    const pwProblem = passwordPolicyError(password);
    if (pwProblem) throw badRequest(pwProblem);
    if (!isValidName(name)) throw badRequest('الاسم غير صالح');
    if (!isValidPhone(phone)) throw badRequest('رقم الهاتف غير صالح');
    if (specialty !== undefined && !isValidSpecialty(specialty))
      throw badRequest('التخصص غير صالح');
    const resolvedSpecialty = specialty === undefined ? 'dish_general' : specialty;
    let resolvedLocation = (location_text || '').trim();
    if (area_key !== undefined && area_key !== '') {
      const area = config.ALEXANDRIA_AREAS.find((a) => a.key === area_key);
      if (!area) throw badRequest('المنطقة غير صالحة');
      resolvedLocation = area.label;
    }
    const coords = parseLatLng(latitude, longitude);
    if (
      (latitude !== undefined && latitude !== '') ||
      (longitude !== undefined && longitude !== '')
    ) {
      if (!coords) throw badRequest('إحداثيات الموقع غير صالحة');
    }
    if (availability && !['available', 'busy', 'offline'].includes(availability)) {
      throw badRequest('حالة التوفر غير صالحة');
    }

    const passwordHash = bcrypt.hashSync(password, 12);
    const run = db.transaction(() => {
      const userRes = db
        .prepare("INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'technician')")
        .run(normEmail, passwordHash);
      db.prepare(
        `INSERT INTO technicians
           (user_id, name, phone, specialty, location_text, latitude, longitude, availability,
            documents_verified, approval_status)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'approved')`
      ).run(
        userRes.lastInsertRowid,
        name.trim(),
        phone.trim(),
        resolvedSpecialty,
        resolvedLocation,
        coords ? coords.latitude : null,
        coords ? coords.longitude : null,
        availability || 'available'
      );
      return userRes.lastInsertRowid;
    });

    const userId = run();
    const technician = db
      .prepare(
        `SELECT t.*, u.email
         FROM technicians t JOIN users u ON u.id = t.user_id
         WHERE t.user_id = ?`
      )
      .get(userId);
    res.status(201).json({ message: 'تم إضافة الفني بنجاح', technician });
  })
);

// PUT /api/admin/technicians/:id  (update profile / availability / credentials)
router.put(
  '/technicians/:id',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const technician = db
      .prepare('SELECT * FROM technicians WHERE id = ?')
      .get(Number(req.params.id));
    if (!technician) throw new AppError('الفني غير موجود', 404);

    const {
      email,
      password,
      name,
      phone,
      specialty,
      location_text,
      latitude,
      longitude,
      availability,
    } = req.body || {};

    const updates = {};
    if (name !== undefined) {
      if (!isValidName(name)) throw badRequest('الاسم غير صالح');
      updates.name = name.trim();
    }
    if (phone !== undefined) {
      if (!isValidPhone(phone)) throw badRequest('رقم الهاتف غير صالح');
      updates.phone = phone.trim();
    }
    if (specialty !== undefined) {
      if (!isValidSpecialty(specialty)) throw badRequest('التخصص غير صالح');
      updates.specialty = specialty;
    }
    if (availability !== undefined) {
      if (!['available', 'busy', 'offline'].includes(availability)) {
        throw badRequest('حالة التوفر غير صالحة');
      }
      updates.availability = availability;
    }
    if (location_text !== undefined) {
      if (!isNonEmptyString(location_text, 300)) throw badRequest('وصف الموقع غير صالح');
      updates.location_text = location_text.trim();
    }
    if (latitude !== undefined || longitude !== undefined) {
      const coords = parseLatLng(latitude, longitude);
      if (!coords) throw badRequest('إحداثيات الموقع غير صالحة');
      updates.latitude = coords.latitude;
      updates.longitude = coords.longitude;
    }

    if (email !== undefined || password !== undefined) {
      const user = db.prepare('SELECT * FROM users WHERE id = ?').get(technician.user_id);
      if (email !== undefined) {
        if (!isValidEmail(email)) throw badRequest('البريد الإلكتروني غير صالح');
        const normEmail = normalizeEmail(email);
        const clash = db
          .prepare('SELECT id FROM users WHERE email = ? AND id != ?')
          .get(normEmail, user.id);
        if (clash) throw badRequest('هذا البريد الإلكتروني مستخدم بالفعل');
        db.prepare('UPDATE users SET email = ? WHERE id = ?').run(normEmail, user.id);
      }
      if (password !== undefined) {
        const pwProblem = passwordPolicyError(password);
        if (pwProblem) throw badRequest(pwProblem);
        db.prepare('UPDATE users SET password_hash = ? WHERE id = ?').run(
          bcrypt.hashSync(password, 12),
          user.id
        );
      }
    }

    if (Object.keys(updates).length > 0) {
      const cols = Object.keys(updates)
        .map((k) => `${k} = @${k}`)
        .join(', ');
      db.prepare(`UPDATE technicians SET ${cols} WHERE id = @id`).run({
        ...updates,
        id: technician.id,
      });
    }

    const updated = db
      .prepare(
        `SELECT t.*, u.email
         FROM technicians t JOIN users u ON u.id = t.user_id
         WHERE t.id = ?`
      )
      .get(technician.id);
    res.json({ message: 'تم تحديث بيانات الفني', technician: updated });
  })
);

// DELETE /api/admin/technicians/:id
router.delete(
  '/technicians/:id',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const technician = db
      .prepare('SELECT * FROM technicians WHERE id = ?')
      .get(Number(req.params.id));
    if (!technician) throw new AppError('الفني غير موجود', 404);

    db.prepare('DELETE FROM users WHERE id = ?').run(technician.user_id);
    res.json({ message: 'تم حذف الفني' });
  })
);

// POST /api/admin/technicians/:id/reset-password
// Lets an admin set a new password for a technician's user account.
router.post(
  '/technicians/:id/reset-password',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const techId = Number(req.params.id);
    const { new_password } = req.body || {};
    const newPwProblem = passwordPolicyError(new_password);
    if (newPwProblem) throw badRequest(newPwProblem);
    const tech = db.prepare('SELECT user_id FROM technicians WHERE id = ?').get(techId);
    if (!tech) throw new AppError('الفني غير موجود', 404);
    const bcrypt = require('bcryptjs');
    const hash = bcrypt.hashSync(new_password, 12);
    db.prepare('UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?').run(
      hash,
      tech.user_id
    );
    res.json({
      message: 'تم إعادة تعيين كلمة المرور بنجاح',
      must_change_password: true,
    });
  })
);

// GET /api/admin/technicians/:id
// Full technician details including uploaded document paths, statistics,
// and account info.
router.get(
  '/technicians/:id',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    const tech = db
      .prepare(
        `SELECT t.*, u.email, u.created_at AS account_created_at
       FROM technicians t JOIN users u ON u.id = t.user_id
       WHERE t.id = ?`
      )
      .get(id);
    if (!tech) throw new AppError('الفني غير موجود', 404);

    const stats = {
      total: db.prepare('SELECT COUNT(*) AS n FROM requests WHERE technician_id = ?').get(id).n,
      completed: db
        .prepare(
          "SELECT COUNT(*) AS n FROM requests WHERE technician_id = ? AND status = 'completed'"
        )
        .get(id).n,
      in_progress: db
        .prepare(
          "SELECT COUNT(*) AS n FROM requests WHERE technician_id = ? AND status = 'in_progress'"
        )
        .get(id).n,
      cancelled: db
        .prepare(
          "SELECT COUNT(*) AS n FROM requests WHERE technician_id = ? AND status = 'cancelled'"
        )
        .get(id).n,
    };

    const recent = db
      .prepare(
        `SELECT r.*, c.name AS customer_name
       FROM requests r JOIN customers c ON c.id = r.customer_id
       WHERE r.technician_id = ? ORDER BY r.created_at DESC LIMIT 5`
      )
      .all(id);

    res.json({ technician: tech, stats, recentRequests: recent });
  })
);

// GET /api/admin/technicians/:id/document/:type
// type: 'criminal_record' | 'national_id'
router.get(
  '/technicians/:id/document/:type',
  requirePermission('technicians'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    const type = req.params.type;

    if (type !== 'criminal_record' && type !== 'national_id') {
      throw new AppError('نوع المستند غير صالح', 400);
    }

    const t = db.prepare('SELECT * FROM technicians WHERE id = ?').get(id);
    if (!t) throw new AppError('الفني غير موجود', 404);

    const relPath = type === 'criminal_record' ? t.criminal_record_path : t.national_id_path;
    if (!relPath) throw new AppError('لم يتم رفع هذا المستند', 404);

    // relPath is like "/uploads/technicians/user-43/file.png"
    // Strip the leading "/uploads/" to get a filesystem-relative path.
    const uploadsDir = path.join(__dirname, '..', '..', 'uploads');
    const rel = relPath.replace(/^\/uploads\//, '');
    const absolute = path.join(uploadsDir, rel);

    // Security: prevent path traversal
    const resolved = path.resolve(absolute);
    const uploadsResolved = path.resolve(uploadsDir);
    if (!resolved.startsWith(uploadsResolved)) {
      throw new AppError('مسار غير صالح', 400);
    }

    if (!fs.existsSync(resolved)) {
      throw new AppError('الملف غير موجود على السيرفر', 404);
    }

    const ext = path.extname(resolved).toLowerCase();
    const mime =
      {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.webp': 'image/webp',
        '.pdf': 'application/pdf',
      }[ext] || 'application/octet-stream';

    res.setHeader('Content-Type', mime);
    // Never let a browser sniff this response into a type the server did not
    // declare; documents are streamed outside express.static so the header has
    // to be set here.
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Content-Disposition', 'inline');
    res.setHeader('Cache-Control', 'private, max-age=300');
    fs.createReadStream(resolved).pipe(res);
  })
);

// ---------- Customers ----------

// GET /api/admin/customers
router.get(
  '/customers',
  requirePermission('customers'),
  wrap(async (req, res) => {
    const customers = db
      .prepare(
        `SELECT c.*, u.email, u.phone AS account_phone,
                (SELECT COUNT(*) FROM requests r WHERE r.customer_id = c.id) AS requests_count
         FROM customers c
         JOIN users u ON u.id = c.user_id
         ORDER BY c.created_at DESC`
      )
      .all();
    res.json({ customers });
  })
);

// POST /api/admin/customers/:id/reset-password
// Lets an admin set a new password for a customer's user account.
router.post(
  '/customers/:id/reset-password',
  requirePermission('customers'),
  wrap(async (req, res) => {
    const custId = Number(req.params.id);
    const { new_password } = req.body || {};
    const newPwProblem = passwordPolicyError(new_password);
    if (newPwProblem) throw badRequest(newPwProblem);
    const cust = db.prepare('SELECT user_id FROM customers WHERE id = ?').get(custId);
    if (!cust) throw new AppError('العميل غير موجود', 404);
    const bcrypt = require('bcryptjs');
    const hash = bcrypt.hashSync(new_password, 12);
    db.prepare('UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?').run(
      hash,
      cust.user_id
    );
    res.json({
      message: 'تم إعادة تعيين كلمة المرور بنجاح',
      must_change_password: true,
    });
  })
);

// GET /api/admin/customers/:id
// Full customer details including statistics and recent requests.
router.get(
  '/customers/:id',
  requirePermission('customers'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    const customer = db
      .prepare(
        `SELECT c.*, u.email, u.created_at AS account_created_at
       FROM customers c JOIN users u ON u.id = c.user_id
       WHERE c.id = ?`
      )
      .get(id);
    if (!customer) throw new AppError('العميل غير موجود', 404);

    const stats = {
      total: db.prepare('SELECT COUNT(*) AS n FROM requests WHERE customer_id = ?').get(id).n,
      completed: db
        .prepare(
          "SELECT COUNT(*) AS n FROM requests WHERE customer_id = ? AND status = 'completed'"
        )
        .get(id).n,
      cancelled: db
        .prepare(
          "SELECT COUNT(*) AS n FROM requests WHERE customer_id = ? AND status = 'cancelled'"
        )
        .get(id).n,
      active: db
        .prepare(
          "SELECT COUNT(*) AS n FROM requests WHERE customer_id = ? AND status IN ('pending','assigned','in_progress')"
        )
        .get(id).n,
    };

    const recent = db
      .prepare(
        `SELECT r.*, t.name AS technician_name
       FROM requests r LEFT JOIN technicians t ON t.id = r.technician_id
       WHERE r.customer_id = ? ORDER BY r.created_at DESC LIMIT 10`
      )
      .all(id);

    res.json({ customer, stats, recentRequests: recent });
  })
);

// DELETE /api/admin/customers/:id
// Deletes the customer, all their requests, and their user account.
router.delete(
  '/customers/:id',
  requirePermission('customers'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    const customer = db.prepare('SELECT * FROM customers WHERE id = ?').get(id);
    if (!customer) throw new AppError('العميل غير موجود', 404);

    const run = db.transaction(() => {
      // Delete requests first (in case FK is RESTRICT not CASCADE)
      db.prepare('DELETE FROM requests WHERE customer_id = ?').run(id);
      // Delete the customer row
      db.prepare('DELETE FROM customers WHERE id = ?').run(id);
      // Delete the user account
      db.prepare('DELETE FROM users WHERE id = ?').run(customer.user_id);
    });
    run();

    res.json({ message: 'تم حذف العميل وجميع طلباته' });
  })
);

// PATCH /api/admin/customers/:id/block  — blocks or unblocks a customer.
router.patch(
  '/customers/:id/block',
  requirePermission('customers'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    const customer = db.prepare('SELECT * FROM customers WHERE id = ?').get(id);
    if (!customer) throw new AppError('العميل غير موجود', 404);
    const blocked = req.body && req.body.blocked ? 1 : 0;
    db.prepare('UPDATE customers SET blocked = ? WHERE id = ?').run(blocked, id);
    res.json({ message: blocked ? 'تم إيقاف العميل' : 'تم تفعيل العميل' });
  })
);

// ---------- Specialties & settings ----------

const refs = require('../services/refs');

// GET /api/admin/specialties  (include disabled; add technician count)
router.get(
  '/specialties',
  requirePermission('specialties'),
  wrap(async (req, res) => {
    const rows = refs.getSpecialties(true).map((s) => ({
      ...s,
      technicians_count: db
        .prepare('SELECT COUNT(*) AS n FROM technicians WHERE specialty = ?')
        .get(s.key).n,
      requests_count: db
        .prepare('SELECT COUNT(*) AS n FROM requests WHERE specialty = ?')
        .get(s.key).n,
    }));
    res.json({ specialties: rows });
  })
);

// POST /api/admin/specialties
router.post(
  '/specialties',
  requirePermission('specialties'),
  wrap(async (req, res) => {
    const { key, label, icon, enabled, sort_order } = req.body || {};
    const normKey = String(key || '').trim();
    if (!/^[a-z][a-z0-9_]*$/.test(normKey))
      throw badRequest('مفتاح التخصص غير صالح (حروف لاتينية صغيرة)');
    if (!isNonEmptyString(label, 100)) throw badRequest('اسم التخصص غير صالح');
    if (db.prepare('SELECT id FROM specialties WHERE key = ?').get(normKey)) {
      throw badRequest('هذا المفتاح مستخدم بالفعل');
    }
    const maxOrder = db
      .prepare('SELECT COALESCE(MAX(sort_order), 0) AS m FROM specialties')
      .get().m;
    const result = db
      .prepare(
        `INSERT INTO specialties (key, label, icon, enabled, sort_order)
         VALUES (?, ?, ?, ?, ?)`
      )
      .run(
        normKey,
        label.trim(),
        (icon || config.DEFAULT_SPECIALTY_ICON).trim(),
        enabled === false || enabled === 0 ? 0 : 1,
        sort_order === undefined ? maxOrder + 1 : Number(sort_order)
      );
    const specialty = db
      .prepare('SELECT * FROM specialties WHERE id = ?')
      .get(result.lastInsertRowid);
    // Give the new service its tariff row straight away. Without it the tariff
    // editor would list the service with no row to save into, and requests for it
    // would be created at 0 until the next server restart (db.js backfills then).
    db.prepare('INSERT OR IGNORE INTO service_pricing (service_key, price) VALUES (?, 0)').run(
      normKey
    );
    res.status(201).json({ message: 'تم إضافة التخصص', specialty });
  })
);

// PUT /api/admin/specialties/:id
router.put(
  '/specialties/:id',
  requirePermission('specialties'),
  wrap(async (req, res) => {
    const specialty = db
      .prepare('SELECT * FROM specialties WHERE id = ?')
      .get(Number(req.params.id));
    if (!specialty) throw new AppError('التخصص غير موجود', 404);
    const { label, icon, enabled, sort_order } = req.body || {};
    const updates = {};
    if (label !== undefined) {
      if (!isNonEmptyString(label, 100)) throw badRequest('اسم التخصص غير صالح');
      updates.label = label.trim();
    }
    if (icon !== undefined) {
      if (!isNonEmptyString(icon, 80)) throw badRequest('أيقونة التخصص غير صالحة');
      updates.icon = icon.trim();
    }
    if (enabled !== undefined) updates.enabled = enabled === true || enabled === 1 ? 1 : 0;
    if (sort_order !== undefined) updates.sort_order = Number(sort_order);

    if (Object.keys(updates).length > 0) {
      const cols = Object.keys(updates)
        .map((k) => `${k} = @${k}`)
        .join(', ');
      db.prepare(`UPDATE specialties SET ${cols} WHERE id = @id`).run({
        ...updates,
        id: specialty.id,
      });
    }
    const updated = db.prepare('SELECT * FROM specialties WHERE id = ?').get(specialty.id);
    res.json({ message: 'تم تحديث التخصص', specialty: updated });
  })
);

// DELETE /api/admin/specialties/:id
router.delete(
  '/specialties/:id',
  requirePermission('specialties'),
  wrap(async (req, res) => {
    const specialty = db
      .prepare('SELECT * FROM specialties WHERE id = ?')
      .get(Number(req.params.id));
    if (!specialty) throw new AppError('التخصص غير موجود', 404);
    const used = db
      .prepare(
        'SELECT (SELECT COUNT(*) FROM technicians WHERE specialty = ?) + (SELECT COUNT(*) FROM requests WHERE specialty = ?) AS n'
      )
      .get(specialty.key, specialty.key).n;
    if (used > 0) {
      throw badRequest('لا يمكن حذف تخصص مستخدم من الفنيين أو الطلبات — عطّله بدلاً من ذلك');
    }
    db.prepare('DELETE FROM specialties WHERE id = ?').run(specialty.id);
    res.json({ message: 'تم حذف التخصص' });
  })
);

// GET /api/admin/settings
router.get(
  '/settings',
  requirePermission('settings'),
  wrap(async (req, res) => {
    res.json({ settings: refs.getSettings() });
  })
);

// PUT /api/admin/settings  — body: { settings: { key: value, ... } }
router.put(
  '/settings',
  requirePermission('settings'),
  wrap(async (req, res) => {
    const patch = (req.body || {}).settings || req.body || {};
    // Site/branding + social keys, plus the original operational keys. The social
    // entries are plain URLs/numbers and end up in the site footer, so they are
    // trimmed and length-capped like everything else below.
    const allowed = new Set([
      'site_name',
      'site_tagline',
      'contact_phone',
      'contact_email',
      'contact_address',
      'whatsapp',
      'facebook_url',
      'instagram_url',
      'instapay_number',
      'instapay_name',
      'wallet_debt_limit',
      'payment_methods',
      'commission_percent',
      'working_hours',
      'hero_title',
      'hero_subtitle',
      'support_phone',
      'support_email',
      'auto_assign_enabled',
      'auto_assign_radius_km',
    ]);
    Object.entries(patch).forEach(([key, value]) => {
      if (!allowed.has(key)) return;
      const str = String(value == null ? '' : value);
      if (str.length > 500) throw badRequest('قيمة الإعداد غير صالحة');
      refs.setSetting(key, str.trim());
    });
    res.json({ message: 'تم حفظ الإعدادات', settings: refs.getSettings() });
  })
);

// ---------- Service pricing ----------

// GET /api/admin/pricing
//
// Driven by the specialties table rather than by service_pricing, with the tariff
// LEFT JOINed on. The editor therefore lists every service the platform offers,
// including one an admin has just created and not priced yet — reading only
// service_pricing would hide it and the price field would be unreachable.
router.get(
  '/pricing',
  requirePermission('settings'),
  wrap(async (req, res) => {
    const pricing = db
      .prepare(
        `SELECT s.key AS service_key, s.label, s.icon, s.sort_order,
                COALESCE(p.price, 0)     AS price,
                COALESCE(p.is_active, 1) AS is_active,
                p.updated_at
           FROM specialties s
           LEFT JOIN service_pricing p ON p.service_key = s.key
          ORDER BY s.sort_order ASC, s.id ASC`
      )
      .all();
    res.json({ pricing });
  })
);

// PUT /api/admin/pricing/:service_key — body: { price?, is_active? }
router.put(
  '/pricing/:service_key',
  requirePermission('settings'),
  wrap(async (req, res) => {
    const { price, is_active } = req.body || {};
    const key = req.params.service_key;
    const specialty = db.prepare('SELECT key FROM specialties WHERE key = ?').get(key);
    const row = db
      .prepare('SELECT service_key FROM service_pricing WHERE service_key = ?')
      .get(key);
    if (!specialty && !row) throw new AppError('الخدمة غير موجودة', 404);

    const updates = [];
    const params = [];
    if (price !== undefined) {
      const p = Number(price);
      if (isNaN(p) || p < 0 || p > 100000) throw badRequest('سعر غير صالح');
      updates.push('price = ?');
      params.push(p);
    }
    if (is_active !== undefined) {
      updates.push('is_active = ?');
      params.push(is_active ? 1 : 0);
    }
    if (updates.length === 0) throw badRequest('لا توجد تغييرات');

    if (!row) {
      // A service priced for the first time. INSERT rather than UPDATE so a
      // specialty that predates the tariff table is not stuck at 0 forever.
      db.prepare(
        `INSERT INTO service_pricing (service_key, price, is_active, updated_at)
         VALUES (?, ?, ?, datetime('now'))`
      ).run(
        key,
        price === undefined ? 0 : Number(price),
        is_active === undefined ? 1 : is_active ? 1 : 0
      );
    } else {
      db.prepare(
        `UPDATE service_pricing SET ${updates.join(', ')}, updated_at = datetime('now')
          WHERE service_key = ?`
      ).run(...params, key);
    }
    res.json({ message: 'تم تحديث السعر' });
  })
);

// ---------- Technician wallets ----------

// Status for each typed failure from the wallet service. The same map the
// technician routes use: an unlisted code stays a 500 rather than being flattened.
const WALLET_ERROR_STATUS = {
  MIN_WITHDRAWAL: 400,
  INSUFFICIENT_BALANCE: 400,
  INVALID_AMOUNT: 400,
  INVALID_STATUS: 400,
  PENDING_EXISTS: 409,
  ALREADY_PAID: 409,
  NOT_FOUND: 404,
};

// Status for each typed failure from the payments service.
const PAYMENT_ERROR_STATUS = {
  NOT_FOUND: 404,
  INVALID_STATUS: 400,
  NOT_CASH: 400,
  ALREADY_REPORTED: 409,
  NOT_DISPUTED: 400,
};

// The amount_status values an admin is allowed to decide on. Mirrors
// ADMIN_DECIDABLE in services/payments.js, which enforces the same rule: a
// matched or resolved request is final and re-deciding it would double-charge.
const ADMIN_DECIDABLE_AMOUNT_STATUS = ['pending', 'disputed'];

// The debt ceiling a technician is blocked at, read once per request rather than
// repeated literal 300s (the setting is admin-editable).
function walletDebtLimit() {
  const row = db.prepare('SELECT value FROM settings WHERE key = ?').get('wallet_debt_limit');
  const n = parseFloat(row && row.value);
  return Number.isFinite(n) && n > 0 ? n : 300;
}

// GET /api/admin/wallets
// Sorted by balance ascending so the technicians who owe the platform money
// (negative balances) appear first — that is the list an admin works through
// when chasing commission.
router.get(
  '/wallets',
  requirePermission('stats'),
  wrap(async (req, res) => {
    // Ordered by total_debt DESC, then balance DESC: under the two-pot model
    // `balance` is never negative, so sorting by it would put every technician in
    // the same arbitrary order and the biggest debtor would be buried. The result
    // is "who owes us the most" first, which is the list an admin works through.
    const wallets = db
      .prepare(
        `SELECT w.*, t.name, t.phone, t.specialty, t.availability,
                CASE WHEN w.total_debt >= ? THEN 1 ELSE 0 END AS blocked
           FROM technician_wallet w
           JOIN technicians t ON t.id = w.technician_id
          ORDER BY w.total_debt DESC, w.balance DESC, t.name ASC`
      )
      .all(walletDebtLimit());
    res.json({ wallets, debtLimit: walletDebtLimit() });
  })
);

// GET /api/admin/wallets/:technician_id/transactions
router.get(
  '/wallets/:technician_id/transactions',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const tid = Number(req.params.technician_id);
    // debt_after is returned alongside balance_after because the two pots move
    // independently: without it a debt entry looks like "-40 next to an unchanged
    // balance", which reads as a bug rather than a charge to the debt pot.
    const rows = db
      .prepare(
        `SELECT * FROM wallet_transactions
          WHERE technician_id = ?
          ORDER BY created_at DESC, id DESC LIMIT 100`
      )
      .all(tid);
    res.json({ transactions: rows });
  })
);

// POST /api/admin/wallets/:technician_id/payout — body: { amount, note? }
//
// Records money the technician handed over to the platform, most often the cash
// commission they collected. 'payout' means exactly that — technician to platform.
// Moving money the OTHER way (platform to technician) is a withdrawal and is not
// implemented; see the note below.
//
// THE SIGN. addWalletTransaction ADDS `amount` to the balance, so the amount is
// passed through POSITIVE: a technician on -120 who hands over 50 ends up on -70,
// which is what "they paid back their debt" means.
//
// This was previously negated, which made a hand-over DEEPEN the debt (-120 -> -170)
// while total_paid_to_platform still grew — the ledger said they had paid while
// their balance said they owed more. The second-order effect was total_owed, which
// pricing.js only increments for a negative amount, so every recorded payout used
// to inflate the debt figure as well.
//
// Overdrawing is still possible on purpose: an admin may legitimately record cash
// commission that has been collected but not yet handed over. That case is
// type='debt' and is booked by /payments/:id/confirm, not here.
router.post(
  '/wallets/:technician_id/payout',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const tid = Number(req.params.technician_id);
    const { amount, note } = req.body || {};
    const amt = Number(amount);
    if (isNaN(amt) || amt <= 0) throw badRequest('مبلغ غير صالح');
    const tech = db.prepare('SELECT id FROM technicians WHERE id = ?').get(tid);
    if (!tech) throw new AppError('الفني غير موجود', 404);

    // type='payout' reduces total_debt by the amount and raises
    // total_paid_to_platform by the same figure (see pricing.addWalletTransaction).
    // No negation here: a payout is money the technician handed OVER.
    const pricing = require('../services/pricing');
    const wallet = pricing.ensureWallet(tid);
    pricing.addWalletTransaction(tid, {
      type: 'payout',
      amount: amt,
      note: note || 'تحويل من الفني للمنصة',
    });
    const after = pricing.ensureWallet(tid);
    res.json({
      message: 'تم تسجيل التحويل',
      total_debt: Number(after.total_debt || 0),
      balance: Number(after.balance || 0),
      remaining_debt: Number(wallet.total_debt || 0) - amt,
    });
  })
);

// ---------- Withdrawals ----------

// GET /api/admin/withdrawals  (?status=pending|approved|paid|rejected)
// What the platform owes technicians, oldest request first. `pending` is the work
// queue: an admin approves, transfers the money, then marks it paid.
router.get(
  '/withdrawals',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const valid = ['pending', 'approved', 'paid', 'rejected'];
    const status = req.query.status;
    let sql = `
      SELECT wr.*, t.name AS technician_name, t.phone AS technician_phone,
             u.email AS processed_by_email
        FROM withdrawal_requests wr
        JOIN technicians t ON t.id = wr.technician_id
        LEFT JOIN users u ON u.id = wr.processed_by
       WHERE 1 = 1`;
    const params = [];
    if (status) {
      if (!valid.includes(status)) throw badRequest('حالة السحب غير صالحة');
      sql += ' AND wr.status = ?';
      params.push(status);
    }
    sql += ' ORDER BY wr.requested_at ASC, wr.id ASC LIMIT 100';
    const rows = db.prepare(sql).all(...params);

    // The headline numbers an admin needs before opening the queue.
    const totals = db
      .prepare(
        `SELECT
           COUNT(*) AS count,
           COALESCE(SUM(amount), 0) AS total
         FROM withdrawal_requests
         WHERE status IN ('pending', 'approved')`
      )
      .get();

    res.json({
      withdrawals: rows,
      count: rows.length,
      awaiting_total: Number(totals.total || 0),
      awaiting_count: totals.count,
    });
  })
);

// POST /api/admin/withdrawals/:id/approve
// The admin has decided to transfer. The wallet is untouched until /paid, because
// the money has not moved yet.
router.post(
  '/withdrawals/:id/approve',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    if (!Number.isInteger(id)) throw badRequest('معرّف الطلب غير صالح');
    let request;
    try {
      request = require('../services/pricing').approveWithdrawal(id, req.user.id);
    } catch (e) {
      throw serviceError(e, WALLET_ERROR_STATUS);
    }
    res.json({ message: 'تم اعتماد الطلب. قم بتحويل المبلغ ثم اضغط "تم التحويل".', request });
  })
);

// POST /api/admin/withdrawals/:id/paid   { note? }
// The transfer has been made: this is what debits the technician's balance.
router.post(
  '/withdrawals/:id/paid',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    if (!Number.isInteger(id)) throw badRequest('معرّف الطلب غير صالح');
    const note = (req.body || {}).note;
    let request;
    try {
      request = require('../services/pricing').markWithdrawalPaid(
        id,
        req.user.id,
        note ? String(note).slice(0, 300) : null
      );
    } catch (e) {
      throw serviceError(e, WALLET_ERROR_STATUS);
    }
    res.json({ message: 'تم تسجيل الدفع. تم خصم المبلغ من رصيد الفني.', request });
  })
);

// POST /api/admin/withdrawals/:id/reject   { note? }
router.post(
  '/withdrawals/:id/reject',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    if (!Number.isInteger(id)) throw badRequest('معرّف الطلب غير صالح');
    const note = (req.body || {}).note;
    let request;
    try {
      request = require('../services/pricing').rejectWithdrawal(
        id,
        req.user.id,
        note ? String(note).slice(0, 300) : null
      );
    } catch (e) {
      throw serviceError(e, WALLET_ERROR_STATUS);
    }
    res.json({ message: 'تم رفض الطلب', request });
  })
);

// ---------- Payments ----------

// GET /api/admin/payments/pending
// Completed requests that have not been financially settled yet. NULL
// payment_status is treated as unpaid so requests completed before the payments
// feature existed still show up here rather than disappearing from the queue.
//
// Two populations share this queue:
//   1. InstaPay, and cash jobs settled by hand before two-sided confirmation
//      existed — identified by payment_status being unpaid/pending.
//   2. Cash jobs mid-confirmation — amount_status='pending', i.e. one side has
//      reported and the other has not. Those are NOT admin work: they settle
//      themselves on agreement or on the timeout, so they are surfaced for
//      visibility but flagged, and resolving one here would race the sweep.
// The dispute queue (/payments/disputed) is where an admin actually intervenes.
// GET /api/admin/payments/pending
// Cash confirmations that still need a decision, plus the pre-confirmation records.
//
// The NOT EXISTS clause is the important part. It mirrors exactly the condition
// settle() uses to decide whether the commission has already been booked, so the
// queue can never show a row that /resolve-dispute would then refuse. Without it
// this page listed three requests (payment_status='confirmed' with a stale
// amount_status='pending', left by the old single-sided flow) whose commission was
// already on the ledger — an admin clicking "تأكيد المبلغ" would be told it worked
// while no money moved.
router.get(
  '/payments/pending',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const rows = db
      .prepare(
        `SELECT r.*, c.name AS customer_name, c.phone AS customer_phone,
                t.name AS technician_name, t.phone AS technician_phone
         FROM requests r
         JOIN customers c ON c.id = r.customer_id
         LEFT JOIN technicians t ON t.id = r.technician_id
         WHERE r.status = 'completed'
           AND (
                 -- In the two-sided flow and un-booked: an admin can settle these.
                 (r.amount_status IN ('pending', 'disputed')
                  AND NOT EXISTS (
                    SELECT 1 FROM wallet_transactions wt
                     WHERE wt.request_id = r.id AND wt.type = 'debt'))
                 -- Pre-W2 records: the confirmation flow never ran on them, so
                 -- there is nothing to decide here and no amount figures to show.
                 OR (r.amount_status IS NULL
                     AND (r.payment_status IS NULL
                          OR r.payment_status IN ('unpaid', 'pending_verification')))
               )
         ORDER BY CASE r.amount_status WHEN 'disputed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                  r.completed_at DESC
         LIMIT 100`
      )
      .all();
    res.json({ payments: rows, count: rows.length });
  })
);

// GET /api/admin/payments/disputed
// Cash jobs where the technician's and the customer's figures disagree. Frozen:
// the commission has not been booked and will not be until an admin picks a number.
router.get(
  '/payments/disputed',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const rows = db
      .prepare(
        `SELECT r.*, c.name AS customer_name, c.phone AS customer_phone,
                t.name AS technician_name, t.phone AS technician_phone
         FROM requests r
         JOIN customers c ON c.id = r.customer_id
         LEFT JOIN technicians t ON t.id = r.technician_id
         WHERE r.amount_status = 'disputed'
         ORDER BY r.technician_reported_at DESC`
      )
      .all();
    res.json({ disputes: rows, count: rows.length });
  })
);

// POST /api/admin/payments/:id/resolve-dispute  (body: { final_amount, note? })
// Settles a disputed cash job on the amount the admin decides is correct, and books
// the commission from it. Who decided what is recorded on the request
// (dispute_resolved_by / dispute_resolved_at / dispute_note) in the same
// transaction as the booking, so the audit trail cannot exist without it.
// POST /api/admin/payments/:id/resolve-dispute  (body: { final_amount, note? })
// An admin's decision on a cash confirmation, for either open state:
//
//   pending   the technician reported, the customer never answered. The admin is
//             standing in for the confirmation that is not coming.
//   disputed  the two sides reported different figures.
//
// Refused for 'matched' and 'resolved': both are settled, the commission is
// already booked, and re-deciding them would double-charge or silently overwrite a
// figure the technician has already been shown.
//
// The status guard is duplicated in the service on purpose. This one is what the
// caller sees; the service's is the backstop that holds if another caller reaches
// the money without going through here.
router.post(
  '/payments/:id/resolve-dispute',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const rid = Number(req.params.id);
    const { final_amount, note } = req.body || {};
    const amt = Number(final_amount);
    if (isNaN(amt) || amt <= 0 || amt > 100000) throw badRequest('المبلغ غير صالح');

    const r = db
      .prepare('SELECT id, status, technician_id, amount_status FROM requests WHERE id = ?')
      .get(rid);
    if (!r) throw new AppError('الطلب غير موجود', 404);

    if (!ADMIN_DECIDABLE_AMOUNT_STATUS.includes(r.amount_status)) {
      throw badRequest('هذا الطلب تم تأكيده أو حله بالفعل — لا يحتاج تدخل');
    }
    if (r.status !== 'completed') throw badRequest('الطلب لم يُكتمل بعد');
    if (!r.technician_id) throw badRequest('لا يوجد فني مرتبط بالطلب');

    let result;
    try {
      result = require('../services/payments').resolveDispute(
        rid,
        amt,
        req.user.id,
        note || 'تأكيد إداري'
      );
    } catch (e) {
      throw serviceError(e, PAYMENT_ERROR_STATUS);
    }

    const after = db
      .prepare(
        'SELECT amount_status, final_amount, dispute_note, dispute_resolved_at FROM requests WHERE id = ?'
      )
      .get(rid);

    // A row can pass the status guard and still be un-bookable: requests settled
    // before the two-sided flow existed carry payment_status='confirmed' with a
    // stale amount_status, and a matching ledger row makes settle() a no-op.
    // Reporting that as a success would tell the admin their decision was applied
    // when no money moved, so it is called out explicitly.
    if (result.status === 'already_finalized') {
      res.json({
        message: 'المبلغ محسوب بالفعل لهذا الطلب — لم يتم تعديل أي رصيد',
        already_finalized: true,
        status: after.amount_status,
        final_amount: after.final_amount,
        commission:
          after.final_amount != null
            ? Math.round(after.final_amount * config.getCommissionPercent()) / 100
            : null,
      });
      return;
    }

    res.json({
      message:
        r.amount_status === 'disputed' ? 'تم حل النزاع وتسجيل المبلغ' : 'تم تأكيد المبلغ وتسجيله',
      status: after.amount_status,
      final_amount: after.final_amount,
      commission: Math.round(after.final_amount * config.getCommissionPercent()) / 100,
      dispute_note: after.dispute_note,
      dispute_resolved_at: after.dispute_resolved_at,
    });
  })
);

// POST /api/admin/payments/:request_id/confirm — body: { method, reference?, note? }
// Settles a completed request and moves the money in the technician's wallet.
//
// The two methods settle differently, and that asymmetry is deliberate:
//  - instapay: the customer paid the PLATFORM directly, so the technician's
//    share is credited to them (+earning) and the platform's cut is already banked.
//  - cash: the customer handed the money to the TECHNICIAN, so the platform's
//    commission becomes a debt the technician now owes (-commission). Nothing is
//    credited to the technician because they already hold the gross.
router.post(
  '/payments/:request_id/confirm',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const rid = Number(req.params.request_id);
    const { method, reference } = req.body || {};
    const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(rid);
    if (!request) throw new AppError('الطلب غير موجود', 404);
    if (request.status !== 'completed') throw badRequest('الطلب ليس مكتملاً');
    if (!request.technician_id) throw badRequest('لا يوجد فني مرتبط');

    // A request in the two-sided cash flow is owned by services/payments.js, not by
    // this route. Settling it here would bypass the whole point of the flow: a
    // dispute would get marked 'confirmed' while still flagged 'disputed' with no
    // audit trail, and a half-reported job would keep mutating after being settled.
    // (The wallet booking itself is separately protected against double-charging by
    // the request-keyed debt row, but that would leave the state machine lying.)
    if (request.amount_status === 'disputed') {
      throw badRequest('الطلب محل نزاع — يُحل من صفحة النزاعات');
    }
    if (request.amount_status === 'pending') {
      throw badRequest('الطلب في انتظار تأكيد الطرف الآخر');
    }

    // Idempotency. Without this guard a double-click (or a retry from a client
    // that never saw the first response) settles the same request twice and
    // charges the technician's wallet twice. A request that is already
    // 'confirmed' is treated as a no-op rather than an error so a legitimate
    // retry still gets a success response.
    if (request.payment_status === 'confirmed') {
      res.json({ message: 'تم تأكيد الدفع مسبقاً', already_confirmed: true });
      return;
    }
    if (request.payment_status === 'refunded') {
      throw badRequest('تم استرداد هذا الطلب — لا يمكن تأكيد دفعه');
    }

    // CASH ONLY. The platform has no payment gateway, so an instapay settlement
    // would credit a technician's earning (and imply the platform banked the
    // technician's cut) against money it never received. A request already marked
    // instapay is legacy; the cash path still settles it, and refund is unaffected.
    if (method === 'instapay') throw badRequest('إنستاباي غير متاح حالياً — الدفع كاش فقط');
    const validMethods = ['cash'];
    const payMethod = validMethods.includes(method) ? method : 'cash';

    const pricing = require('../services/pricing');
    const earning = request.technician_earning || 0;
    const commission = request.commission_amount || 0;

    const run = db.transaction(() => {
      db.prepare(
        `UPDATE requests
            SET payment_status = 'confirmed', payment_method = ?,
                payment_reference = ?, payment_confirmed_at = datetime('now'),
                payment_confirmed_by = ?, updated_at = datetime('now')
          WHERE id = ?`
      ).run(payMethod, reference || null, req.user.id, rid);

      if (payMethod === 'cash') {
        pricing.addWalletTransaction(request.technician_id, {
          requestId: rid,
          type: 'debt',
          amount: -commission,
          note: 'عمولة المنصة على طلب #' + rid + ' (كاش)',
        });
      } else {
        pricing.addWalletTransaction(request.technician_id, {
          requestId: rid,
          type: 'payment',
          amount: earning,
          note: 'أرباح طلب #' + rid + ' (إنستاباي)',
        });
      }
    });
    run();

    res.json({ message: 'تم تأكيد الدفع' });
  })
);

// POST /api/admin/payments/:request_id/refund
// Marks the request refunded. Deliberately ledger-neutral: reversing a wallet
// entry automatically would corrupt a balance that may already have been spent
// or paid out. An admin settles the wallet by recording an adjustment, which
// leaves the decision visible in the ledger history.
router.post(
  '/payments/:request_id/refund',
  requirePermission('stats'),
  wrap(async (req, res) => {
    const rid = Number(req.params.request_id);
    const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(rid);
    if (!request) throw new AppError('الطلب غير موجود', 404);
    db.prepare(
      "UPDATE requests SET payment_status = 'refunded', updated_at = datetime('now') WHERE id = ?"
    ).run(rid);
    res.json({ message: 'تم تسجيل الاسترداد' });
  })
);

// ---------- All requests ----------

// GET /api/admin/requests  (?status=.., ?specialty=..)
router.get(
  '/requests',
  requirePermission('requests'),
  wrap(async (req, res) => {
    const { status, specialty } = req.query;
    let sql = `
      SELECT r.*, c.name AS customer_name, c.phone AS customer_phone,
             c.location_text AS customer_location,
             t.name AS technician_name, t.phone AS technician_phone,
             t.specialty AS technician_specialty
      FROM requests r
      JOIN customers c ON c.id = r.customer_id
      LEFT JOIN technicians t ON t.id = r.technician_id
      WHERE 1 = 1`;
    const params = [];
    if (status && config.STATUSES.includes(status)) {
      sql += ' AND r.status = ?';
      params.push(status);
    }
    if (specialty && refs.getSpecialtyKeys().includes(specialty)) {
      sql += ' AND r.specialty = ?';
      params.push(specialty);
    }
    sql += ' ORDER BY r.created_at DESC';
    res.json({ requests: db.prepare(sql).all(...params) });
  })
);

// POST /api/admin/requests/:id/assign  — force-assign a technician to a pending request.
router.post(
  '/requests/:id/assign',
  requirePermission('requests'),
  wrap(async (req, res) => {
    const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(Number(req.params.id));
    if (!request) throw new AppError('الطلب غير موجود', 404);
    if (request.status !== 'pending') throw badRequest('فقط طلب بانتظار فني يمكن تعيين فني له');

    const technicianId = Number(req.body && req.body.technician_id);
    const technician = db.prepare('SELECT * FROM technicians WHERE id = ?').get(technicianId);
    if (!technician) throw new AppError('الفني غير موجود', 404);
    if (technician.specialty !== request.specialty) {
      throw badRequest('هذا الفني لا يطابق تخصص الطلب');
    }

    const run = db.transaction(() => {
      db.prepare(
        `UPDATE requests
         SET technician_id = ?, status = 'assigned', assigned_at = datetime('now'),
             updated_at = datetime('now'),
             start_pin = ?, start_pin_attempts = 0, start_pin_locked_until = NULL,
             started_at = NULL
         WHERE id = ?`
      ).run(technician.id, require('../services/startPin').mint(), request.id);
      db.prepare("UPDATE technicians SET availability = 'busy' WHERE id = ?").run(technician.id);
    });
    run();

    res.json({
      message: 'تم تعيين ' + technician.name + ' على الطلب',
      technician: { id: technician.id, name: technician.name, phone: technician.phone },
    });
  })
);

// POST /api/admin/requests/:id/unassign  — remove the technician and return to pending.
router.post(
  '/requests/:id/unassign',
  requirePermission('requests'),
  wrap(async (req, res) => {
    const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(Number(req.params.id));
    if (!request) throw new AppError('الطلب غير موجود', 404);
    if (request.status !== 'assigned' && request.status !== 'in_progress') {
      throw badRequest('لا يمكن إلغاء التعيين في هذه الحالة');
    }

    const run = db.transaction(() => {
      db.prepare(
        `UPDATE requests
         SET technician_id = NULL, status = 'pending', assigned_at = NULL,
             updated_at = datetime('now')
         WHERE id = ?`
      ).run(request.id);
      if (request.technician_id) {
        db.prepare("UPDATE technicians SET availability = 'available' WHERE id = ?").run(
          request.technician_id
        );
      }
    });
    run();

    res.json({ message: 'تم إلغاء التعيين وعاد الطلب إلى الانتظار' });
  })
);

// POST /api/admin/requests/:id/cancel  — admin cancels a pending or assigned request.
router.post(
  '/requests/:id/cancel',
  requirePermission('requests'),
  wrap(async (req, res) => {
    const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(Number(req.params.id));
    if (!request) throw new AppError('الطلب غير موجود', 404);
    if (request.status === 'completed' || request.status === 'cancelled') {
      throw badRequest('لا يمكن إلغاء طلب في حالته الحالية');
    }

    const run = db.transaction(() => {
      db.prepare(
        `UPDATE requests
         SET status = 'cancelled', updated_at = datetime('now')
         WHERE id = ?`
      ).run(request.id);
      if (request.technician_id) {
        db.prepare("UPDATE technicians SET availability = 'available' WHERE id = ?").run(
          request.technician_id
        );
      }
      // Close outstanding offers so the cancelled request stops appearing in
      // the offered technicians' inboxes.
      broadcast.expireForRequest(request.id);
    });
    run();

    res.json({ message: 'تم إلغاء الطلب' });
  })
);

// POST /api/admin/requests/:id/rebroadcast
// Puts a stranded request back in front of technicians.
//
// This exists because a request the expiry sweep flagged 'no_technician' used to
// be a dead end: both broadcastRequest and /requests/:id/assign insist on
// 'pending', so nothing could ever move it again and it sat in the admin list
// forever with nobody able to act on it.
//
// The offer bookkeeping (re-opening expired rows, refreshing live ones, leaving
// declines alone) lives in broadcastRequest rather than being duplicated here, so
// every caller gets identical behaviour and one code path owns the UNIQUE index.
router.post(
  '/requests/:id/rebroadcast',
  requirePermission('requests'),
  wrap(async (req, res) => {
    const rid = Number(req.params.id);
    if (!Number.isInteger(rid)) throw badRequest('معرّف الطلب غير صالح');

    const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(rid);
    if (!request) throw new AppError('الطلب غير موجود', 404);

    const broadcastable = ['pending', 'no_technician'];
    if (!broadcastable.includes(request.status)) {
      throw badRequest('لا يمكن إعادة بث الطلب في حالته الحالية');
    }

    // broadcastRequest re-opens a 'no_technician' request itself, so this endpoint
    // does not have to — and duplicating the UPDATE here would mean two places
    // that can disagree about the state machine.
    const result = broadcast.broadcastRequest(rid);

    res.json({
      message:
        result.count > 0
          ? 'تم إعادة بث الطلب إلى ' + result.count + ' فني'
          : 'لم يتم العثور على فنيين متاحين — تأكد من التخصص أو أضف فنيين جدد',
      sent_to: result.count,
      technician_ids: result.technician_ids || [],
      reason: result.reason || null,
    });
  })
);

// GET /api/admin/requests/queued
// Requests sitting in the waiting queue: nobody was available when they were
// created, and the queue service keeps re-offering them until one is taken or
// QUEUE_MAX_HOURS cancels them.
//
// Registered before /requests/:id/broadcasts so the intent is obvious; Express
// matches literal segments first regardless, since ':id' would not match 'queued'
// on a two-segment path anyway.
router.get(
  '/requests/queued',
  requirePermission('requests'),
  wrap(async (req, res) => {
    const requests = db
      .prepare(
        `SELECT r.*, c.name AS customer_name, c.phone AS customer_phone,
                c.location_text AS customer_location
           FROM requests r
           JOIN customers c ON c.id = r.customer_id
          WHERE r.is_queued = 1 AND r.status = 'pending'
          ORDER BY r.queued_at ASC, r.id ASC`
      )
      .all();
    res.json({ requests, count: requests.length });
  })
);

// GET /api/admin/requests/:id/broadcasts
// Who this request was offered to and how each technician responded. This is the
// audit trail for the broadcast flow: 'accepted' is the winner, 'declined' means
// that technician said no, and 'expired' means the offer ran out of time, lost the
// race to another technician, or the request was closed underneath it.
router.get(
  '/requests/:id/broadcasts',
  requirePermission('requests'),
  wrap(async (req, res) => {
    const rid = Number(req.params.id);
    if (!Number.isInteger(rid)) throw badRequest('معرّف الطلب غير صالح');

    const request = db.prepare('SELECT id FROM requests WHERE id = ?').get(rid);
    if (!request) throw new AppError('الطلب غير موجود', 404);

    const broadcasts = db
      .prepare(
        `SELECT rb.id, rb.request_id, rb.technician_id, rb.status,
                rb.sent_at, rb.responded_at,
                t.name AS technician_name, t.phone AS technician_phone
           FROM request_broadcasts rb
           JOIN technicians t ON t.id = rb.technician_id
          WHERE rb.request_id = ?
          ORDER BY rb.sent_at DESC, rb.id DESC`
      )
      .all(rid);

    res.json({ broadcasts, count: broadcasts.length });
  })
);

// DELETE /api/admin/requests/:id  — permanently remove the request.
router.delete(
  '/requests/:id',
  requirePermission('requests'),
  wrap(async (req, res) => {
    const request = db.prepare('SELECT * FROM requests WHERE id = ?').get(Number(req.params.id));
    if (!request) throw new AppError('الطلب غير موجود', 404);

    const run = db.transaction(() => {
      if (request.technician_id) {
        db.prepare("UPDATE technicians SET availability = 'available' WHERE id = ?").run(
          request.technician_id
        );
      }
      db.prepare('DELETE FROM requests WHERE id = ?').run(request.id);
    });
    run();

    res.json({ message: 'تم حذف الطلب نهائياً' });
  })
);

// ---------- Review moderation ----------
// Admins can list every review (visible or hidden) and hide / unhide / delete
// it. Hidden reviews stay in the database but are excluded from the public
// technician profile and from the public rating averages (see public.routes.js).
//
// NOTE: /reviews/stats is registered BEFORE /reviews/:id so that the literal
// "stats" is never parsed as a review id.

const REVIEWS_MAX_LIMIT = 200;
const REVIEWS_DEFAULT_LIMIT = 50;

// Clamps ?limit / ?offset so a hand-edited query string can never turn into a
// NaN bind (which better-sqlite3 rejects) or an unbounded SELECT.
function readPaging(query) {
  const rawLimit = Number(query.limit);
  const rawOffset = Number(query.offset);
  const limit =
    Number.isFinite(rawLimit) && rawLimit > 0
      ? Math.min(Math.floor(rawLimit), REVIEWS_MAX_LIMIT)
      : REVIEWS_DEFAULT_LIMIT;
  const offset = Number.isFinite(rawOffset) && rawOffset > 0 ? Math.floor(rawOffset) : 0;
  return { limit, offset };
}

// Builds the shared WHERE fragment used by both the list and the count query so
// the two can never drift apart.
function reviewsFilter(query) {
  const clauses = [];
  const params = [];

  if (query.technician_id) {
    clauses.push('rv.technician_id = ?');
    params.push(Number(query.technician_id));
  }
  if (query.rating) {
    clauses.push('rv.rating = ?');
    params.push(Number(query.rating));
  }
  if (query.hidden === '0' || query.hidden === '1') {
    clauses.push('rv.hidden = ?');
    params.push(Number(query.hidden));
  }
  if (query.search) {
    const q = '%' + String(query.search).trim() + '%';
    clauses.push('(rv.comment LIKE ? OR c.name LIKE ? OR t.name LIKE ?)');
    params.push(q, q, q);
  }
  return { where: clauses.length ? ' AND ' + clauses.join(' AND ') : '', params };
}

// GET /api/admin/reviews  (list + filters + pagination)
router.get(
  '/reviews',
  requirePermission('reviews'),
  wrap(async (req, res) => {
    const { technician_id, rating, hidden, search } = req.query;
    const { limit, offset } = readPaging(req.query);
    const { where, params } = reviewsFilter({ technician_id, rating, hidden, search });

    const rows = db
      .prepare(
        `SELECT rv.*,
            c.name AS customer_name, c.phone AS customer_phone,
            t.name AS technician_name, t.id AS technician_id,
            u.email AS customer_email
     FROM reviews rv
     JOIN customers c ON c.id = rv.customer_id
     JOIN technicians t ON t.id = rv.technician_id
     JOIN users u ON u.id = c.user_id
     WHERE 1 = 1${where}
     ORDER BY rv.created_at DESC, rv.id DESC
     LIMIT ? OFFSET ?`
      )
      .all(...params, limit, offset);

    const total = db
      .prepare(
        `SELECT COUNT(*) AS n
     FROM reviews rv
     JOIN customers c ON c.id = rv.customer_id
     JOIN technicians t ON t.id = rv.technician_id
     WHERE 1 = 1${where}`
      )
      .get(...params).n;

    res.json({ reviews: rows, total, limit, offset });
  })
);

// GET /api/admin/reviews/stats  (must stay before /reviews/:id)
router.get(
  '/reviews/stats',
  requirePermission('reviews'),
  wrap(async (req, res) => {
    const stats = {
      total: db.prepare('SELECT COUNT(*) AS n FROM reviews').get().n,
      hidden: db.prepare('SELECT COUNT(*) AS n FROM reviews WHERE hidden = 1').get().n,
      // The public average only counts visible reviews, so hidden ones are excluded.
      avgRating: db.prepare('SELECT AVG(rating) AS a FROM reviews WHERE hidden = 0').get().a,
      byRating: {},
    };
    for (let i = 1; i <= 5; i++) {
      stats.byRating[i] = db
        .prepare('SELECT COUNT(*) AS n FROM reviews WHERE rating = ? AND hidden = 0')
        .get(i).n;
    }
    stats.avgRating = stats.avgRating ? Number(stats.avgRating.toFixed(2)) : 0;
    res.json(stats);
  })
);

// POST /api/admin/reviews/:id/hide   { reason? }
router.post(
  '/reviews/:id/hide',
  requirePermission('reviews'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    if (!Number.isInteger(id)) throw badRequest('معرف المراجعة غير صالح');
    const rv = db.prepare('SELECT id FROM reviews WHERE id = ?').get(id);
    if (!rv) throw new AppError('المراجعة غير موجودة', 404);

    const reason =
      req.body && req.body.reason ? String(req.body.reason).trim().slice(0, 500) : null;
    db.prepare(
      `UPDATE reviews
     SET hidden = 1, hidden_reason = ?, hidden_at = datetime('now'), hidden_by = ?
     WHERE id = ?`
    ).run(reason || null, req.user.id, id);

    res.json({ message: 'تم إخفاء المراجعة' });
  })
);

// POST /api/admin/reviews/:id/unhide
router.post(
  '/reviews/:id/unhide',
  requirePermission('reviews'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    if (!Number.isInteger(id)) throw badRequest('معرف المراجعة غير صالح');
    const rv = db.prepare('SELECT id FROM reviews WHERE id = ?').get(id);
    if (!rv) throw new AppError('المراجعة غير موجودة', 404);

    db.prepare(
      'UPDATE reviews SET hidden = 0, hidden_reason = NULL, hidden_at = NULL, hidden_by = NULL WHERE id = ?'
    ).run(id);

    res.json({ message: 'تم إظهار المراجعة' });
  })
);

// DELETE /api/admin/reviews/:id   (permanent)
router.delete(
  '/reviews/:id',
  requirePermission('reviews'),
  wrap(async (req, res) => {
    const id = Number(req.params.id);
    if (!Number.isInteger(id)) throw badRequest('معرف المراجعة غير صالح');
    const rv = db.prepare('SELECT id FROM reviews WHERE id = ?').get(id);
    if (!rv) throw new AppError('المراجعة غير موجودة', 404);

    db.prepare('DELETE FROM reviews WHERE id = ?').run(id);
    res.json({ message: 'تم حذف المراجعة نهائياً' });
  })
);

// ---------- Legal compliance (Law 151/2020) ----------

// GET /api/admin/data-subject-requests
router.get(
  '/data-subject-requests',
  requirePermission('data_requests'),
  wrap(async (req, res) => {
    const rows = db
      .prepare(
        `SELECT r.*, u.email AS user_email
     FROM data_subject_requests r JOIN users u ON u.id = r.user_id
     ORDER BY r.requested_at DESC`
      )
      .all();
    res.json({ requests: rows });
  })
);

// PATCH /api/admin/data-subject-requests/:id
router.patch(
  '/data-subject-requests/:id',
  requirePermission('data_requests'),
  wrap(async (req, res) => {
    const { status, admin_note } = req.body || {};
    const r = db
      .prepare('SELECT * FROM data_subject_requests WHERE id = ?')
      .get(Number(req.params.id));
    if (!r) throw new AppError('الطلب غير موجود', 404);
    db.prepare(
      `UPDATE data_subject_requests
     SET status = ?, admin_note = ?, completed_at = datetime('now')
     WHERE id = ?`
    ).run(status || 'completed', admin_note || null, r.id);
    res.json({ message: 'تم تحديث الطلب' });
  })
);

// DELETE /api/admin/users/:id/erase  (right to erasure)
router.delete(
  '/users/:id/erase',
  requirePermission('data_requests'),
  wrap(async (req, res) => {
    const userId = Number(req.params.id);
    const user = db.prepare('SELECT * FROM users WHERE id = ?').get(userId);
    if (!user) throw new AppError('المستخدم غير موجود', 404);

    const run = db.transaction(() => {
      if (user.role === 'customer') {
        const c = db.prepare('SELECT id FROM customers WHERE user_id = ?').get(userId);
        if (c) db.prepare('DELETE FROM requests WHERE customer_id = ?').run(c.id);
        db.prepare('DELETE FROM customers WHERE user_id = ?').run(userId);
      }
      if (user.role === 'technician') {
        const t = db.prepare('SELECT id FROM technicians WHERE user_id = ?').get(userId);
        if (t) db.prepare('DELETE FROM requests WHERE technician_id = ?').run(t.id);
        db.prepare('DELETE FROM technicians WHERE user_id = ?').run(userId);
      }
      db.prepare('DELETE FROM consents WHERE user_id = ?').run(userId);
      db.prepare('DELETE FROM data_subject_requests WHERE user_id = ?').run(userId);
      db.prepare('DELETE FROM users WHERE id = ?').run(userId);
    });
    run();

    res.json({ message: 'تم حذف جميع بيانات المستخدم نهائياً' });
  })
);

module.exports = router;
