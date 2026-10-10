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
    // Patches the cached user/profile in place after an endpoint changes them
    // (completing a profile, for instance) without re-issuing a token. The
    // token is untouched, so there is nothing to re-validate.
    updateSession(data) {
      if (data && data.user) {
        const user = {
          ...this.getUser(),
          ...data.user,
          must_change_password: Boolean(data.must_change_password),
        };
        localStorage.setItem(USER_KEY, JSON.stringify(user));
      }
      if (data && 'profile' in data) {
        localStorage.setItem(PROFILE_KEY, JSON.stringify(data.profile || null));
      }
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

    // Path of the onboarding form a customer without a phone is sent to.
    completeProfilePath() {
      return '/complete-profile.html';
    },

    // True when the browser is already showing that form. The page is served
    // under two URLs — the .html file and the clean /complete-profile route —
    // and the redirect below must not fire on either, or the form would bounce
    // itself back to a fresh copy on every load and discard what was typed.
    onCompleteProfilePage() {
      const path = window.location.pathname;
      return path.startsWith('/complete-profile.html') || path.startsWith('/complete-profile');
    },

    // Whether the signed-in customer has claimed a phone. Absent flag means an
    // older cached payload predating the field, which is treated as complete so
    // a returning customer is never bounced out of the app by a stale value.
    isProfileComplete() {
      const user = this.getUser();
      if (!user || user.role !== 'customer') return true;
      if (typeof user.profile_complete !== 'boolean') return true;
      return user.profile_complete;
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

      // A customer who signed in with Google and has not claimed a phone yet
      // holds a valid token but may not use the product, and the API refuses
      // every /api/customer route for them. Caught here as well as on the
      // server so the form is one redirect away instead of a wall of failed
      // requests. The page itself is exempt, or the form could never load.
      // The server's answer is the real one — this only saves the round trips.
      if (role === 'customer' && !this.isProfileComplete() && !this.onCompleteProfilePage()) {
        window.location.replace(this.completeProfilePath());
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
          // Merged rather than replaced: /api/auth/me does not echo the
          // permissions a page has not re-fetched yet, and the cached copy is
          // what the admin UI reads between calls.
          localStorage.setItem(USER_KEY, JSON.stringify({ ...this.getUser(), ...data.user }));
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
