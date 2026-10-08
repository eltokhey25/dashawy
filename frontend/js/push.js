/**
 * @file Push notification subscription handler.
 * @module push
 *
 * Registers this browser for Web Push and hands the subscription to the server.
 * Everything is best-effort and silent: push is a bonus on top of a UI that
 * already polls, so no failure here is ever allowed to surface as an error.
 *
 * Load order matters — this file uses Auth, so /js/auth.js must come first.
 */
(function () {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  if (!('Notification' in window)) return;

  // VAPID keys are per-server and stable, so the key in the URL lets one page
  // share the subscription another page already created instead of asking for
  // permission again on every page the user visits.
  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  async function sendToServer(sub) {
    try {
      const res = await fetch('/api/notifications/subscribe', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer ' + Auth.getToken(),
        },
        body: JSON.stringify({ subscription: sub.toJSON() }),
      });
      if (!res.ok) console.warn('[push] server rejected the subscription:', res.status);
    } catch (err) {
      console.warn('[push] send to server failed:', err.message);
    }
  }

  async function subscribe() {
    // Re-checked here as well as at load: a long-lived tab can have its session
    // replaced (or the user signed out) in the meantime, and subscribing without
    // a token would store the device against nobody.
    if (!Auth.isAuthed()) return;
    if (Notification.permission === 'denied') return;

    try {
      const reg = await navigator.serviceWorker.ready;
      const existing = await reg.pushManager.getSubscription();
      if (existing) {
        // Already subscribed on this device — just make sure the server still
        // has it (a wiped DB, or a subscription made before this feature).
        await sendToServer(existing);
        return;
      }

      // Ask only after the page has settled, and never on a tab that was not
      // actually looked at (a background tab must not pop a permission prompt).
      if (Notification.permission === 'default') {
        if (document.hidden) return;
        const perm = await Notification.requestPermission();
        if (perm !== 'granted') return;
      }

      const res = await fetch('/api/notifications/vapid-public-key');
      if (!res.ok) throw new Error('vapid endpoint returned ' + res.status);
      const { publicKey, enabled } = await res.json();
      if (!publicKey || !enabled) {
        console.warn('[push] VAPID keys are not configured on the server');
        return;
      }

      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });

      await sendToServer(sub);
    } catch (err) {
      console.warn('[push] subscribe failed:', err.message);
    }
  }

  // The install banner owns the first few seconds after load; asking for
  // notifications in the same window means two competing prompts. 6s keeps them
  // apart without making the customer wait long enough to wonder.
  window.addEventListener('load', () => {
    setTimeout(subscribe, 6000);
  });
})();
