/**
 * @file   Client-side session state
 *
 * Stores the token and cached user in sessionStorage, exposes role and permission checks, and
 * redirects unauthenticated visitors.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Auth helpers: token + session stored in localStorage.
(function () {
  const TOKEN_KEY = 'fr_token';
  const USER_KEY = 'fr_user';
  const PROFILE_KEY = 'fr_profile';

  const Auth = {
    getToken() {
      try {
        return localStorage.getItem(TOKEN_KEY) || null;
      } catch (e) {
        return null;
      }
    },
    getUser() {
      try {
        return JSON.parse(localStorage.getItem(USER_KEY) || 'null');
      } catch (e) {
        return null;
      }
    },
    getProfile() {
      try {
        return JSON.parse(localStorage.getItem(PROFILE_KEY) || 'null');
      } catch (e) {
        return null;
      }
    },
    setSession(data) {
      localStorage.setItem(TOKEN_KEY, data.token);
      const user = { ...data.user, must_change_password: Boolean(data.must_change_password) };
      localStorage.setItem(USER_KEY, JSON.stringify(user));
      localStorage.setItem(PROFILE_KEY, JSON.stringify(data.profile || null));
    },
    clear() {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      localStorage.removeItem(PROFILE_KEY);
    },
    isAuthed() {
      return Boolean(this.getToken() && this.getUser());
    },
    role() {
      const u = this.getUser();
      return u ? u.role : null;
    },

    // Route to the role's home page; returns the path.
    homePath(role) {
      role = role || this.role();
      if (role === 'admin') return '/admin/dashboard';
      if (role === 'technician') return '/technician/dashboard.html';
      // Customers get the one-screen landing page, not the marketing site: the
      // only thing they came back for is asking for a service, and the request
      // wizard is one tap from there.
      if (role === 'customer') return '/customer/home.html';
      return '/index.html';
    },

    // Redirects a logged-in user away from index if already authed.
    redirectIfAuthed() {
      if (this.isAuthed()) {
        window.location.href = this.homePath();
      }
    },

    // Guards a protected page. If not authed -> index. If wrong role -> own home.
    requireRole(...roles) {
      if (!this.isAuthed()) {
        window.location.replace('/login.html');
        return false;
      }

      // Force password change if flagged (except on the change-password page)
      const user = this.getUser();
      if (
        user &&
        user.must_change_password &&
        !window.location.pathname.startsWith('/change-password')
      ) {
        window.location.replace('/change-password');
        return false;
      }

      const role = this.role();
      if (roles.length > 0 && !roles.includes(role)) {
        window.location.replace(this.homePath(role));
        return false;
      }
      return true;
    },

    // Refresh the cached user data from the server.
    // Returns a Promise that resolves to the updated user object (or null on failure).
    async refreshUser() {
      if (!this.isAuthed()) return null;
      try {
        const res = await fetch('/api/auth/me', {
          headers: { Authorization: 'Bearer ' + this.getToken() },
        });
        if (!res.ok) {
          if (res.status === 401) {
            this.clear();
            window.location.href = '/login.html';
          }
          return null;
        }
        const data = await res.json();
        if (data.user) {
          localStorage.setItem(USER_KEY, JSON.stringify(data.user));
          return data.user;
        }
        return null;
      } catch (e) {
        console.warn('[auth] refreshUser failed:', e);
        return null;
      }
    },

    logout(redirect) {
      this.clear();
      window.location.href = redirect || '/login.html';
    },

    // Alerts the response object for session expiry (401).
    handleUnauthorized(force) {
      if (force || (this.getToken() && this.isAuthed())) {
        this.clear();
        toast('انتهت الجلسة، يرجى تسجيل الدخول مجدداً', 'warn');
        setTimeout(() => {
          window.location.href = '/index.html';
        }, 800);
      }
    },
  };

  window.Auth = Auth;

  // Route any future 401 to session-expiry handling (auto logout).
  if (window.setTokenGuard) {
    window.setTokenGuard(function () {
      Auth.handleUnauthorized(true);
    });
  }
})();
