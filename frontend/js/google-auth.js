/**
 * @file   Google Sign-In button
 *
 * Renders Google's official button and hands the resulting ID token to a
 * callback, which is expected to post it to the server. Exposes
 * window.GoogleAuth with mount() and getConfig(); both degrade to a silent no-op
 * when the feature is not configured on the server.
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Google Sign-In wiring for the login and register pages.
//
// This file does no authentication. It renders a button, waits for Google to
// return an ID token, and hands that token over. Whether the person is signed
// in, which account they get and whether that account may act at all are
// decided entirely by the server, which verifies Google's signature over the
// token. Everything the browser can see is a string an attacker could have
// written themselves, so nothing here is trusted — not even the email the
// button hands back, which this file never reads.
(function () {
  const GIS_SRC = 'https://accounts.google.com/gsi/client';

  let configPromise = null;
  let gisPromise = null;

  // Cached for the lifetime of the page: /api/auth/google/config is fixed at
  // boot, and both login.html and register.html ask for it on load.
  function getConfig() {
    if (!configPromise) {
      configPromise = api('/api/auth/google/config')
        .then((data) => ({
          enabled: Boolean(data && data.enabled && data.client_id),
          clientId: (data && data.client_id) || '',
        }))
        // A page that cannot reach its own API has bigger problems than this
        // button; the catch keeps a failed config lookup from surfacing as an
        // unhandled rejection on every login page load.
        .catch(() => ({ enabled: false, clientId: '' }));
    }
    return configPromise;
  }

  // Google's Identity Services library. Loaded once per page; a second mount()
  // reuses the same promise instead of injecting a second <script>.
  function loadGis() {
    if (window.google && window.google.accounts && window.google.accounts.id) {
      return Promise.resolve(window.google);
    }
    if (!gisPromise) {
      gisPromise = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = GIS_SRC;
        script.async = true;
        script.defer = true;
        script.onload = () => {
          if (window.google && window.google.accounts && window.google.accounts.id) {
            resolve(window.google);
          } else {
            // The file loaded but the global is missing — usually a network
            // filter or an ad blocker stripping the script. Rejected rather
            // than swallowed, because "the button does nothing" would
            // otherwise be indistinguishable from "Google sign-in is broken".
            reject(new Error('تعذر تحميل خدمة تسجيل الدخول من جوجل'));
          }
        };
        script.onerror = () => reject(new Error('تعذر تحميل خدمة تسجيل الدخول من جوجل'));
        document.head.appendChild(script);
      }).catch((err) => {
        // Clear the memo so a later attempt retries instead of being stuck
        // with this rejected promise for the life of the page.
        gisPromise = null;
        throw err;
      });
    }
    return gisPromise;
  }

  /**
   * Renders the Google button into `container` and calls `onCredential` with
   * each ID token Google issues.
   *
   * Deliberately not modelled as a promise-returning click handler. Google's
   * button lives in a cross-origin iframe, so a page cannot observe the click,
   * and dismissing the account chooser calls no callback at all — any promise
   * wrapping it would hang with no way to settle. The flow is therefore
   * callback-driven: silence from Google means the user closed the chooser,
   * which is not an error and must not raise a toast.
   *
   * @param {HTMLElement} container      empty element to render into
   * @param {(credential: string) => void} onCredential
   * @returns {Promise<boolean>} whether the button is now on the page
   */
  async function mount(container, onCredential) {
    if (!container) return false;

    const cfg = await getConfig();
    // Left empty on purpose: a deployment with no Google credentials shows no
    // button rather than one that fails when pressed.
    if (!cfg.enabled) {
      container.innerHTML = '';
      container.style.display = 'none';
      return false;
    }

    try {
      const google = await loadGis();
      container.style.display = '';
      container.innerHTML = '';
      google.accounts.id.initialize({
        client_id: cfg.clientId,
        // ux_mode 'popup' rather than 'redirect': this app serves its pages from
        // the same origin as its API, and a redirect would navigate away from
        // a form the customer may have already filled in.
        ux_mode: 'popup',
        callback: (response) => {
          if (response && response.credential && typeof onCredential === 'function') {
            onCredential(response.credential);
          }
        },
      });
      google.accounts.id.renderButton(container, {
        type: 'standard',
        theme: 'outline',
        size: 'large',
        shape: 'rectangular',
        text: 'continue_with',
        locale: 'ar',
        // Google's fixed 240px default clips a longer localized label, so the
        // button is allowed to fill the container. The container's own CSS
        // keeps it inside the form on narrow screens.
        width: Math.min(400, Math.max(240, container.offsetWidth || 300)),
      });
      return true;
    } catch (err) {
      container.style.display = 'none';
      console.warn('[google-auth]', err);
      return false;
    }
  }

  /**
   * Exchanges a credential for a session. Split out from mount() so a page can
   * post the token itself and own the error toasts.
   *
   * @param {string} credential
   * @returns {Promise<object>} the server's response (token, user, profile_complete…)
   */
  function exchange(credential) {
    return api('/api/auth/google', { method: 'POST', body: { id_token: credential } });
  }

  window.GoogleAuth = { mount, exchange, getConfig };
})();
