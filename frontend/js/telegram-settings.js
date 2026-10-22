/**
 * @file   Telegram integration card for signed-in dashboards
 * @module telegram-settings
 *
 * Renders the "Link Telegram" card into any page that has
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

    const card = el(
      'div',
      'display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;'
    );
    card.className = 'card';

    const info = el('div', 'display:flex;align-items:center;gap:14px;min-width:0;');

    const icon = el(
      'span',
      'display:inline-grid;place-items:center;width:44px;height:44px;border-radius:12px;' +
        'background:#e0f2fe;color:#0369a1;font-size:1.25rem;flex:0 0 auto;'
    );
    icon.innerHTML = '<i class="fa-brands fa-telegram"></i>';

    const text = el('div', 'min-width:0;');
    const title = el(
      'h3',
      'margin:0 0 2px;font-size:1rem;font-weight:800;color:var(--ink);',
      'ربط تليجرام'
    );
    const sub = el(
      'p',
      'margin:0;color:var(--muted);font-size:0.82rem;line-height:1.7;',
      linked
        ? 'حسابك مرتبط — أكواد الدخول بتوصلك على تليجرام.'
        : 'اربط حسابك بتليجرام عشان يوصلك كود الدخول هناك بدل البريد.'
    );
    text.appendChild(title);
    text.appendChild(sub);

    info.appendChild(icon);
    info.appendChild(text);

    const action = el('div', 'flex:0 0 auto;');

    if (linked) {
      const badge = el(
        'span',
        'display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:999px;' +
          'background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;font-size:0.85rem;font-weight:700;'
      );
      badge.innerHTML = '<i class="fa-solid fa-circle-check"></i> مرتبط';
      action.appendChild(badge);
    } else {
      const button = document.createElement('a');
      button.className = 'btn btn-brand btn-sm';
      button.href = linkHref;
      button.innerHTML = '<i class="fa-brands fa-telegram"></i> ربط تليجرام';
      action.appendChild(button);
    }

    card.appendChild(info);
    card.appendChild(action);
    container.appendChild(card);
  }

  async function load() {
    try {
      const data = await api('/api/auth/telegram-link');
      render(Boolean(data && data.linked));
    } catch (err) {
      // The card is an enhancement, not a gate: a failed status check must not
      // take the dashboard down. Show the unlinked action so the user still has
      // a path to the linking page.
      render(false);
    }
  }

  load();
})();
