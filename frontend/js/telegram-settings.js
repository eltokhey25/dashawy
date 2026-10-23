/**
 * @file   Telegram integration banner for signed-in dashboards
 * @module telegram-settings
 *
 * Renders a slim "Link Telegram" banner into any page that has
 * `<div id="telegram-settings"></div>`. The current state comes from
 * GET /api/auth/telegram-link, which is authenticated and reads the session
 * user — unlike the public linking page, nobody has to type an email here.
 *
 * @author Eltokhey
 * @since  2026-10
 */
(function () {
  const container = document.getElementById('telegram-settings');
  if (!container) return;
  if (!window.Auth || !window.api || !Auth.isAuthed()) return;

  const user = Auth.getUser() || {};
  const email = user.email || '';
  const linkHref = '/telegram-link.html' + (email ? '?email=' + encodeURIComponent(email) : '');

  function el(tag, style, text) {
    const node = document.createElement(tag);
    if (style) node.style.cssText = style;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function render(linked) {
    container.textContent = '';

    // One narrow alert-style row: icon, one-line message, and one compact action.
    // The wrapper is deliberately not `.card`, whose padding would turn this
    // into a full settings block and push the primary action down the page.
    const banner = el(
      'div',
      'display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:12px;' +
        'text-align:start;' +
        (linked
          ? 'background:#f0fdf4;border:1px solid #bbf7d0;color:#166534;'
          : 'background:#eff6ff;border:1px solid #bfdbfe;color:#1e3a8a;')
    );

    const icon = el(
      'span',
      'display:inline-grid;place-items:center;width:30px;height:30px;border-radius:9px;' +
        'font-size:0.95rem;flex:0 0 auto;' +
        (linked ? 'background:#dcfce7;color:#15803d;' : 'background:#dbeafe;color:#1d4ed8;')
    );
    icon.innerHTML = '<i class="fa-brands fa-telegram" aria-hidden="true"></i>';

    const text = el(
      'span',
      'flex:1 1 160px;min-width:0;font-size:0.82rem;font-weight:600;line-height:1.5;',
      linked
        ? 'حسابك مرتبط بتليجرام — أكواد الدخول توصلك عليه.'
        : 'اربط حسابك ليصلك كود الدخول على تليجرام.'
    );

    banner.appendChild(icon);
    banner.appendChild(text);

    if (linked) {
      const badge = el(
        'span',
        'display:inline-flex;align-items:center;gap:5px;flex:0 0 auto;padding:5px 9px;' +
          'border-radius:999px;background:#dcfce7;color:#15803d;font-size:0.76rem;' +
          'font-weight:800;white-space:nowrap;'
      );
      badge.innerHTML = '<i class="fa-solid fa-circle-check" aria-hidden="true"></i> مرتبط';
      banner.appendChild(badge);
    } else {
      const button = document.createElement('a');
      button.className = 'btn btn-brand btn-sm';
      button.href = linkHref;
      button.textContent = 'ربط الآن';
      button.style.cssText =
        'flex:0 0 auto;width:auto;max-width:100%;padding:6px 12px;font-size:0.78rem;' +
        'border-radius:8px;white-space:nowrap;';
      banner.appendChild(button);
    }

    container.appendChild(banner);
  }

  async function load() {
    try {
      const data = await api('/api/auth/telegram-link');
      render(Boolean(data && data.linked));
    } catch (err) {
      // The banner is an enhancement, not a gate: a failed status check must not
      // take the dashboard down. Show the unlinked action so the user still has
      // a path to the linking page.
      render(false);
    }
  }

  load();
})();
