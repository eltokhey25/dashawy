/**
 * @file PWA install prompt handler.
 * @module pwa-install
 *
 * Shows a floating "Add to Home Screen" banner when the browser fires the
 * beforeinstallprompt event (Android Chrome / desktop Chrome), and falls back to
 * written instructions on iOS Safari, which never fires that event.
 *
 * Loaded by every page. The banner it creates is guarded by its id, so the
 * hand-written handler this replaced cannot come back as a duplicate, and the
 * dismissal is stored as a timestamp with a TTL rather than a permanent flag —
 * a user who said no last week is offered it again, one who said no this
 * morning is not bothered twice in a day.
 */
(function () {
  // Already installed: the app is running in its own window, so there is nothing
  // to offer. `display-mode` covers Android/desktop and `navigator.standalone`
  // covers iOS, which has no standard equivalent of the former.
  if (window.matchMedia('(display-mode: standalone)').matches) return;
  if (window.navigator.standalone) return;

  const DISMISS_KEY = 'pwa-install-dismissed';
  const DISMISS_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

  try {
    const dismissed = localStorage.getItem(DISMISS_KEY);
    // Number('') is 0 and a legacy '1' flag (what the old inline banner wrote)
    // reads as 1 ms since the epoch, so both fall out as "not dismissed"
    // rather than pinning the banner off forever.
    if (
      dismissed &&
      Number.isFinite(Number(dismissed)) &&
      Date.now() - Number(dismissed) < DISMISS_TTL_MS
    ) {
      return;
    }
  } catch (e) {
    /* localStorage disabled (private mode): show the banner every time */
  }

  let deferredPrompt = null;

  function showBanner() {
    // Idempotent: if something already rendered the banner, leave it alone.
    if (document.getElementById('pwa-install-banner')) return;

    const banner = document.createElement('div');
    banner.id = 'pwa-install-banner';
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-label', 'تثبيت تطبيق دشاوي');
    banner.innerHTML = `
      <img src="/dashawy-brand/icon-192.png" alt="" class="pwa-icon">
      <div class="pwa-text">
        <b>ثبّت تطبيق دشاوي</b>
        <span>وصول أسرع + إشعارات فورية</span>
      </div>
      <button id="pwa-install-btn" class="pwa-btn" type="button">تثبيت</button>
      <button id="pwa-dismiss-btn" class="pwa-close" type="button" aria-label="إغلاق">✕</button>
    `;
    document.body.appendChild(banner);

    document.getElementById('pwa-install-btn').addEventListener('click', async () => {
      // iOS: no prompt event exists, so the button explains the manual route.
      if (!deferredPrompt) {
        banner.remove();
        alert(
          'لتثبيت التطبيق على iPhone:\n1. اضغط زر المشاركة (Share)\n2. اختر "إضافة إلى الشاشة الرئيسية"\n3. اضغط "إضافة"'
        );
        return;
      }
      deferredPrompt.prompt();
      const { outcome } = await deferredPrompt.userChoice;
      console.log('[pwa] install outcome:', outcome);
      // Either way the banner is done: if it was dismissed we must not keep
      // pushing, and if it was accepted the app is on the home screen.
      rememberDismissal();
      deferredPrompt = null;
      banner.remove();
    });

    document.getElementById('pwa-dismiss-btn').addEventListener('click', () => {
      rememberDismissal();
      banner.remove();
    });
  }

  function rememberDismissal() {
    try {
      localStorage.setItem(DISMISS_KEY, String(Date.now()));
    } catch (e) {
      /* nothing to persist; the banner simply returns next load */
    }
  }

  // Android Chrome / desktop Chrome.
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    // Delayed so it cannot fight the first paint or a page that is already
    // mid-interaction (the request wizard auto-advances between steps).
    setTimeout(showBanner, 3000);
  });

  // iOS Safari never fires beforeinstallprompt, so offer the manual route.
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
  if (isIOS && isSafari) {
    setTimeout(showBanner, 5000);
  }
})();
