/**
 * @file   Admin page bootstrap and shared admin logic
 *
 * Initialises every admin page: enforces auth and permissions, fetches the current admin, and
 * exposes the formatting and rendering helpers the admin screens share.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Admin-area guard + granular permission helpers.
// Every admin page starts with `if (!(await Admin.init())) throw ...`, which
// re-syncs the cached user from the server first so permission changes made by
// the super admin take effect without a re-login.
(function () {
  function parsePermissions() {
    try {
      const u = Auth.getUser();
      if (!u) return [];
      if (u.is_super_admin) {
        return [
          'stats',
          'customers',
          'technicians',
          'requests',
          'reviews',
          'specialties',
          'settings',
          'data_requests',
        ];
      }
      return Array.isArray(u.permissions) ? u.permissions : [];
    } catch (e) {
      return [];
    }
  }

  // Map of paths → required permission
  const ROUTE_PERMS = {
    '/admin/dashboard': 'stats',
    '/admin/customers': 'customers',
    '/admin/technicians': 'technicians',
    '/admin/finance': 'stats',
    '/admin/wallets': 'stats',
    '/admin/requests': 'requests',
    '/admin/reviews': 'reviews',
    '/admin/specialties': 'specialties',
    '/admin/settings': 'settings',
    '/admin/data-requests': 'data_requests',
  };

  function requiredPermForPath() {
    const path = window.location.pathname.replace(/\/$/, '');
    return ROUTE_PERMS[path] || null;
  }

  window.Admin = {
    permissions: function permissions() {
      return parsePermissions();
    },

    // Syncs user data from the server, then enforces auth + permissions.
    // MUST be awaited at the top of every admin page.
    //   if (!(await Admin.init())) throw new Error('redirect');
    init: async function init() {
      // 1. Basic auth check
      if (!Auth.isAuthed() || Auth.role() !== 'admin') {
        window.location.replace('/admin');
        return false;
      }

      // 2. Refresh user data from the server (picks up permission changes)
      await Auth.refreshUser();

      // 3. Re-check with fresh data
      const u = Auth.getUser();
      if (!u || u.role !== 'admin') {
        window.location.replace('/admin');
        return false;
      }

      // 4. /admin/admins requires super_admin
      if (window.location.pathname.startsWith('/admin/admins')) {
        if (!u.is_super_admin) {
          window.location.replace('/admin/dashboard');
          return false;
        }
        return true;
      }

      // 5. Check the permission required for the current page
      const required = requiredPermForPath();
      if (required && !this.has(required)) {
        // Redirect to the first page they DO have access to
        const fallback = this.firstAccessiblePath();
        if (fallback && fallback !== window.location.pathname) {
          window.location.replace(fallback);
        } else {
          // No accessible pages at all
          Auth.clear();
          window.location.replace('/admin');
        }
        return false;
      }

      return true;
    },

    // Returns true if the current user has the given permission.
    has: function has(permKey) {
      const u = Auth.getUser();
      if (!u) return false;
      if (u.is_super_admin) return true;
      return Array.isArray(u.permissions) && u.permissions.includes(permKey);
    },

    // Which admin page should this user land on?
    // Returns the first path they have access to, or null.
    firstAccessiblePath: function firstAccessiblePath() {
      const order = [
        { path: '/admin/dashboard', perm: 'stats' },
        { path: '/admin/requests', perm: 'requests' },
        { path: '/admin/customers', perm: 'customers' },
        { path: '/admin/technicians', perm: 'technicians' },
        { path: '/admin/finance', perm: 'stats' },
        { path: '/admin/wallets', perm: 'stats' },
        { path: '/admin/reviews', perm: 'reviews' },
        { path: '/admin/specialties', perm: 'specialties' },
        { path: '/admin/settings', perm: 'settings' },
        { path: '/admin/data-requests', perm: 'data_requests' },
      ];
      const u = Auth.getUser();
      if (u && u.is_super_admin) return '/admin/dashboard';
      for (const item of order) {
        if (this.has(item.perm)) return item.path;
      }
      return null;
    },

    // Legacy method kept for backward compatibility
    require: function require() {
      if (!Auth.isAuthed() || Auth.role() !== 'admin') {
        window.location.replace('/admin');
        return false;
      }
      if (window.location.pathname.startsWith('/admin/admins')) {
        const u = Auth.getUser();
        if (!u || !u.is_super_admin) {
          window.location.replace('/admin/dashboard');
          return false;
        }
      }
      return true;
    },

    // Legacy method
    requirePermission: function requirePermission(permKey) {
      if (!this.has(permKey)) {
        toast('ليس لديك صلاحية للوصول إلى هذا القسم', 'error');
        setTimeout(() => window.location.replace('/admin/dashboard'), 800);
        return false;
      }
      return true;
    },

    // Hide sidebar links the user doesn't have access to.
    hideUnauthorizedNav: function hideUnauthorizedNav() {
      const map = {
        'nav-stats': 'stats',
        'nav-customers': 'customers',
        'nav-technicians': 'technicians',
        'nav-finance': 'stats',
        'nav-wallets': 'stats',
        'nav-requests': 'requests',
        'nav-reviews': 'reviews',
        'nav-specialties': 'specialties',
        'nav-settings': 'settings',
        'nav-data-requests': 'data_requests',
      };

      Object.entries(map).forEach(([id, perm]) => {
        const el = document.getElementById(id);
        if (el && !this.has(perm)) el.style.display = 'none';
      });
    },

    // Refresh permissions mid-session (e.g. on window focus).
    refresh: async function refresh() {
      await Auth.refreshUser();
      this.hideUnauthorizedNav();
      const u = Auth.getUser();
      const adminsLink = document.getElementById('nav-admins');
      if (adminsLink && u && u.is_super_admin) {
        adminsLink.style.display = '';
      }
    },
  };

  // Auto-refresh permissions when the tab regains focus
  window.addEventListener('focus', () => {
    if (Auth.isAuthed() && Auth.role() === 'admin') {
      window.Admin.refresh();
    }
  });
})();
