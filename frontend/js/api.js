/**
 * @file   fetch wrapper and endpoint helpers
 *
 * Single place where the frontend talks to /api: attaches the Bearer token, parses JSON, and
 * throws on error responses. Every other script calls through here.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Small fetch wrapper that attaches the JWT and normalises errors.
(function () {
  window.api = async function api(path, options) {
    options = options || {};
    const method = options.method || 'GET';
    const body = options.body;

    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const token = Auth.getToken();
    if (token) headers.Authorization = 'Bearer ' + token;

    let res;
    try {
      res = await fetch(path, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      const err = new Error('تعذر الاتصال بالخادم');
      err.status = 0;
      throw err;
    }

    let data = {};
    try {
      data = await res.json();
    } catch (e) {
      data = {};
    }

    if (!res.ok) {
      // A 401 from the login endpoint means the credentials were rejected, not
      // that a session expired: there is no session to clear and nothing to
      // redirect away from. Exempting it lets the login form render the server's
      // error in place; every other 401 still goes through the global guard.
      const isLoginAttempt = String(path).indexOf('/api/auth/login') !== -1;
      if (res.status === 401 && !isLoginAttempt && TokenGuard) TokenGuard(data.error);
      const err = new Error(data.error || 'حدث خطأ غير متوقع');
      err.status = res.status;
      // Machine-readable tag from the server (e.g. 'profile_incomplete'), so a
      // page can branch on behaviour without matching an Arabic sentence that a
      // copy edit may reword.
      if (data.code) err.code = data.code;
      throw err;
    }
    return data;
  };

  // Global handler that fires on any 401 response.
  window.TokenGuard = function TokenGuard() {};
  window.setTokenGuard = function setTokenGuard(fn) {
    window.TokenGuard = fn;
  };

  // Authenticated blob fetch for binary resources (e.g. technician documents).
  // Does NOT go through api() because we need the raw Blob, not JSON.
  window.apiBlob = async function apiBlob(requestPath) {
    const token = Auth.getToken();
    const headers = {};
    if (token) headers.Authorization = 'Bearer ' + token;
    const res = await fetch(requestPath, { headers });
    if (!res.ok) {
      throw new Error('فشل تحميل المستند');
    }
    return res.blob();
  };

  // ============================================================
  // Service Worker update detection
  // Shows a small banner when a new version is ready.
  // ============================================================
  if ('serviceWorker' in navigator) {
    // Captured before registering: on a first-ever visit there is no controller
    // yet, and clients.claim() in the SW's activate step would fire
    // controllerchange. Reloading there would be a pointless second load of a
    // page the user is already looking at.
    const hadControllerAtLoad = Boolean(navigator.serviceWorker.controller);

    window.addEventListener('load', () => {
      navigator.serviceWorker
        .register('/sw.js')
        .then((reg) => {
          // Check for updates every 30 seconds
          setInterval(() => reg.update(), 30000);

          // A new SW is waiting to take over
          reg.addEventListener('updatefound', () => {
            const newWorker = reg.installing;
            if (!newWorker) return;
            newWorker.addEventListener('statechange', () => {
              if (newWorker.state === 'installed' && navigator.serviceWorker.controller) {
                showUpdateBanner(reg);
              }
            });
          });
        })
        .catch(() => {
          /* SW disabled in this browser */
        });

      // If the SW controller changes, the page was reloaded with a fresh SW.
      // Skipped on a first-ever activation (nothing was being served by an old
      // worker, so there is nothing stale to replace).
      let refreshing = false;
      navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (refreshing || !hadControllerAtLoad) return;
        refreshing = true;
        window.location.reload();
      });
    });
  }

  function showUpdateBanner(registration) {
    if (document.getElementById('sw-update-banner')) return;

    const banner = document.createElement('div');
    banner.id = 'sw-update-banner';
    banner.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;flex:1">
        <i class="fa-solid fa-rotate" style="font-size:1.1rem;color:#4f46e5"></i>
        <div>
          <b style="display:block;font-size:0.9rem">تحديث جديد متاح</b>
          <span style="font-size:0.78rem;color:#64748b">اضغط لإعادة التحميل وتجربة آخر إصدار</span>
        </div>
      </div>
      <button id="sw-update-btn" style="background:#4f46e5;color:#fff;border:none;padding:8px 16px;border-radius:8px;font-family:inherit;font-weight:700;font-size:0.82rem;cursor:pointer">تحديث</button>
      <button id="sw-dismiss-btn" style="background:transparent;border:none;color:#94a3b8;font-size:1rem;cursor:pointer;padding:4px 8px" aria-label="إغلاق">✕</button>
    `;

    banner.style.cssText = `
      position: fixed;
      bottom: 20px;
      inset-inline: 20px;
      z-index: 99999;
      background: #fff;
      border: 1px solid #e2e8f0;
      border-radius: 14px;
      box-shadow: 0 20px 45px rgba(15, 23, 42, 0.18);
      padding: 12px 14px;
      display: flex;
      align-items: center;
      gap: 10px;
      max-width: 520px;
      margin-inline: auto;
      animation: slideUp 0.3s ease;
    `;

    document.body.appendChild(banner);

    document.getElementById('sw-update-btn').addEventListener('click', () => {
      if (registration.waiting) {
        registration.waiting.postMessage({ type: 'SKIP_WAITING' });
      }
      banner.remove();
    });

    document.getElementById('sw-dismiss-btn').addEventListener('click', () => {
      banner.remove();
    });
  }
})();
