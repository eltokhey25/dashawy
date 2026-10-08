/**
 * @file   Authentication and role-based access middleware
 * @module auth
 *
 * Verifies the Bearer JWT (HS256 pinned), reloads role and blocked status from the database,
 * and exports the requireAuth / requireRole / requirePermission / requireSuperAdmin guards
 * that protect the API.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// JWT authentication middleware + role guards.
const jwt = require('jsonwebtoken');
const db = require('../db');
const config = require('../config');
const { unauthorized, forbidden } = require('../utils/errors');

// Verifies the JWT and attaches req.user = { id, email, role, customerId?, technicianId? }.
function authenticate(req, res, next) {
  const header = req.headers.authorization || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : null;
  if (!token) return next(unauthorized('Authentication required'));

  let payload;
  try {
    // Pinning the algorithm list is what stops an `alg: none` token (or a
    // downgrade to a weaker family) from being accepted.
    payload = jwt.verify(token, config.JWT_SECRET, { algorithms: ['HS256'] });
  } catch (err) {
    return next(unauthorized('Invalid or expired token'));
  }

  // The token's claims are never trusted for authorization on their own: the
  // role, the super-admin flag and the permission list are all re-read from
  // the database on every request, so a revoked or downgraded account loses
  // access immediately rather than at token expiry.
  const user = db
    .prepare(
      'SELECT id, email, phone, role, is_super_admin, blocked, permissions FROM users WHERE id = ?'
    )
    .get(payload.userId);
  if (!user) return next(unauthorized('Account no longer exists'));
  if (user.blocked) return next(forbidden('تم إيقاف حسابك. تواصل مع الإدارة.'));

  req.user = {
    id: user.id,
    email: user.email,
    // Null for admins (email-only) and for legacy accounts whose phone could
    // not be claimed during migration.
    phone: user.phone || null,
    role: user.role,
    is_super_admin: Boolean(user.is_super_admin),
    permissions: (() => {
      try {
        return JSON.parse(user.permissions || '[]');
      } catch (e) {
        return [];
      }
    })(),
  };

  if (user.role === config.LEVELS.customer) {
    const c = db.prepare('SELECT id FROM customers WHERE user_id = ?').get(user.id);
    req.user.customerId = c ? c.id : null;
  }
  if (user.role === config.LEVELS.technician) {
    const t = db.prepare('SELECT id FROM technicians WHERE user_id = ?').get(user.id);
    req.user.technicianId = t ? t.id : null;
  }
  return next();
}

// Restricts a route to one or more roles.
function requireRole(...roles) {
  return (req, res, next) => {
    if (!req.user) return next(unauthorized('Authentication required'));
    if (!roles.includes(req.user.role))
      return next(forbidden('You do not have permission to do this'));
    return next();
  };
}

// Restricts a route to the super admin only (admin management).
function requireSuperAdmin(req, res, next) {
  if (!req.user) return next(unauthorized('Authentication required'));
  if (req.user.role !== 'admin' || !req.user.is_super_admin) {
    return next(forbidden('هذه الصفحة متاحة فقط للمدير العام'));
  }
  return next();
}

// Restricts a route to admins holding every listed permission. The super admin
// implicitly holds all of them.
function requirePermission(...requiredKeys) {
  return (req, res, next) => {
    if (!req.user) return next(unauthorized('Authentication required'));
    if (req.user.role !== 'admin') return next(forbidden('غير مصرح'));

    // Super admin bypasses all permission checks
    if (req.user.is_super_admin) return next();

    const userPerms = req.user.permissions || [];
    const hasAll = requiredKeys.every((k) => userPerms.includes(k));
    if (!hasAll) {
      return next(forbidden('ليس لديك صلاحية للوصول إلى هذا القسم'));
    }
    return next();
  };
}

module.exports = { authenticate, requireRole, requireSuperAdmin, requirePermission };
