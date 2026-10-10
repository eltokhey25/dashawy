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
const { unauthorized, forbidden, AppError } = require('../utils/errors');

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
    // Re-derived from the database row on every request rather than read off
    // the token, for the same reason as the role above: the token was minted
    // before the customer finished their profile and stays valid for days
    // afterwards, so a claim would freeze the answer at sign-in time.
    //
    // Only meaningful for customers — a technician or admin has no customer
    // profile to complete, so they are always complete.
    profile_complete: user.role !== config.LEVELS.customer || Boolean(user.phone),
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

// Blocks a customer whose profile is not finished — currently, one who has not
// claimed a phone number yet.
//
// Mounted over the whole customer router rather than on the individual routes
// that happen to need a phone. The alternative is a rule that lives in the
// create-request handler and is silently absent from every route added later,
// which is how an incomplete account ends up able to do the one thing it was
// gated out of. Here the gate is a property of the account, so it applies to
// every customer endpoint by construction.
//
// 403 rather than 401: the token is valid and the session is real — the
// account is simply not allowed to do this until it finishes onboarding, and
// answering 401 would send the client's 401 handler into a logout loop.
function requireCompleteProfile(req, res, next) {
  if (!req.user) return next(unauthorized('Authentication required'));
  if (req.user.role !== config.LEVELS.customer) return next();
  if (req.user.profile_complete) return next();
  return next(
    new AppError('أكمل بياناتك (رقم الهاتف والمنطقة) للمتابعة', 403, 'profile_incomplete')
  );
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

module.exports = {
  authenticate,
  requireRole,
  requireSuperAdmin,
  requirePermission,
  requireCompleteProfile,
};
