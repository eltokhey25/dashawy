/**
 * @file   Shared UI helpers and components
 *
 * Toast, modal, spinner, escaping, pagination and scroll-reveal helpers used across the
 * public, customer and technician pages.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Shared UI helpers: metadata (icons/labels), DOM builders, pagination, modal, toasts.
(function () {
  // Service metadata: key -> { label, icon, color, bg, desc }
  const SPECIALTY_META = {
    dish_install: {
      label: 'تركيب دش',
      icon: 'fa-solid fa-satellite-dish',
      color: '#0369a1',
      bg: '#e0f2fe',
      desc: 'تركيب أطباق الدش والأقمار الصناعية بجميع الأحجام',
    },
    dish_repair: {
      label: 'صيانة دش',
      icon: 'fa-solid fa-screwdriver-wrench',
      color: '#b45309',
      bg: '#fef3c7',
      desc: 'إصلاح أعطال الدش، تغيير القطع التالفة، إعادة التركيب',
    },
    signal_tuning: {
      label: 'ضبط إشارة',
      icon: 'fa-solid fa-tower-broadcast',
      color: '#047857',
      bg: '#d1fae5',
      desc: 'ضبط الإشارة والقنوات، حل مشاكل التقطيع وعدم الاستقبال',
    },
    receiver_service: {
      label: 'تركيب / صيانة رسيفر',
      icon: 'fa-solid fa-tv',
      color: '#be185d',
      bg: '#fce7f3',
      desc: 'تركيب وبرمجة الرسيفرات، تحديث السوفت وير، إصلاح الأعطال',
    },
    other: {
      label: 'خدمة أخرى',
      icon: 'fa-solid fa-circle-question',
      color: '#475569',
      bg: '#e2e8f0',
      desc: 'مش لاقي خدمتك في القائمة؟ اكتبها بنفسك وسنوصلها لأقرب فني مناسب',
    },
  };

  const STATUS_META = {
    pending: { label: 'قيد الانتظار', icon: 'fa-regular fa-clock', badge: 'b-pending' },
    // A request the broadcast window expired without an answer. The second class
    // is deliberate: statusBadge concatenates `badge ` + this string, so the
    // `.no-tech` rule in style.css distinguishes it from a genuinely cancelled
    // request while keeping the same red base colour.
    no_technician: {
      label: 'لا يوجد فني متاح',
      icon: 'fa-solid fa-user-slash',
      badge: 'b-cancelled no-tech',
    },
    assigned: { label: 'تم تحديد فني', icon: 'fa-solid fa-user-check', badge: 'b-assigned' },
    in_progress: {
      label: 'جاري التنفيذ',
      icon: 'fa-solid fa-screwdriver-wrench',
      badge: 'b-in_progress',
    },
    completed: { label: 'مكتمل', icon: 'fa-solid fa-circle-check', badge: 'b-completed' },
    cancelled: { label: 'ملغي', icon: 'fa-solid fa-ban', badge: 'b-cancelled' },
  };

  const AVAIL_META = {
    available: { label: 'متاح', icon: 'fa-solid fa-circle-check', badge: 'b-available' },
    busy: { label: 'مشغول', icon: 'fa-solid fa-hourglass-half', badge: 'b-busy' },
    offline: { label: 'غير متاح', icon: 'fa-solid fa-user-slash', badge: 'b-offline' },
  };

  // The two-sided cash confirmation's own state machine, from
  // services/payments.js. Separate from STATUS_META because these are answers to
  // "how much money changed hands", not to "where is the job" — a request can be
  // 'completed' (work done) and still be 'pending' here (nobody has confirmed the
  // amount). Folding the two together is how a settled job keeps showing as
  // unresolved, so they stay apart.
  const AMOUNT_STATUS_META = {
    pending: { label: 'بانتظار تأكيد الدفع', icon: 'fa-solid fa-clock', badge: 'b-pending' },
    matched: { label: 'تم تأكيد الدفع', icon: 'fa-solid fa-circle-check', badge: 'b-completed' },
    disputed: {
      label: 'قيد المراجعة',
      icon: 'fa-solid fa-triangle-exclamation',
      badge: 'b-cancelled',
    },
    resolved: { label: 'تم الدفع', icon: 'fa-solid fa-circle-check', badge: 'b-completed' },
  };

  // True when a stored social value is a phone number rather than a URL.
  //
  // A stricter "digits only" test is not enough: admins type WhatsApp numbers as
  // "+20 102 533 0520" or "(010) 25330520", and those would fail the test, be
  // treated as a URL, and land in href as a broken relative link. So the rule is
  // "every character is phone-ish AND at least 6 digits are present" — which
  // still rejects real URLs (they contain letters, ':' and '/') while accepting
  // every formatting an admin is likely to paste.
  function isPhoneNumber(value) {
    const v = String(value).trim();
    if (!v) return false;
    if (!/^[\d\s()+.\-]+$/.test(v)) return false;
    return v.replace(/\D/g, '').length >= 6;
  }

  const Ui = {
    meta: (key) =>
      SPECIALTY_META[key] || {
        label: key,
        icon: 'fa-solid fa-wrench',
        color: '#334155',
        bg: '#e2e8f0',
        desc: '',
      },
    statusMeta: (s) => STATUS_META[s] || { label: s, icon: 'fa-regular fa-circle', badge: '' },
    availMeta: (a) => AVAIL_META[a] || { label: a, icon: 'fa-solid fa-circle', badge: '' },

    specialtyLabel: (k) => (SPECIALTY_META[k] ? SPECIALTY_META[k].label : k),
    statusLabel: (s) => (STATUS_META[s] ? STATUS_META[s].label : s),
    availabilityLabel: (a) => (AVAIL_META[a] ? AVAIL_META[a].label : a),
    serviceKeys: () => Object.keys(SPECIALTY_META),

    dateTime(str) {
      if (!str) return '—';
      const s = String(str).trim();
      const hasTz = s.includes('T') || /[+-]\d{2}:\d{2}$/.test(s) || s.endsWith('Z');
      const normalized = hasTz ? s : s.replace(' ', 'T') + 'Z';
      const d = new Date(normalized);
      if (isNaN(d)) return str;
      try {
        return d.toLocaleString('ar-EG-u-nu-latn', {
          year: 'numeric',
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        });
      } catch (e) {
        return d.toLocaleString();
      }
    },

    dateOnly(str) {
      if (!str) return '—';
      const d = new Date(String(str).trim().replace(' ', 'T') + 'Z');
      if (isNaN(d)) return str;
      return d.toISOString().slice(0, 10);
    },

    initials(name) {
      const p = String(name || '؟')
        .trim()
        .split(/\s+/);
      return ((p[0] || '؟')[0] || '؟') + (p[1] ? p[1][0] : '');
    },

    // Builds an icon element.
    ic(cls, extraClass) {
      const i = document.createElement('i');
      i.className = (cls || 'fa-solid fa-circle') + (extraClass ? ' ' + extraClass : '');
      return i;
    },

    iconBadge(key) {
      const m = Ui.meta(key);
      return Ui.el(
        'span',
        { class: 'rt-ic', attrs: { style: 'background:' + m.bg + ';color:' + m.color } },
        [Ui.ic(m.icon)]
      );
    },

    statusBadge(status) {
      const m = Ui.statusMeta(status);
      return Ui.el('span', { class: 'badge ' + m.badge }, [Ui.ic(m.icon), Ui.statusLabel(status)]);
    },

    // Null for a request with no amount state (InstaPay, or cash not yet reported),
    // so a caller can splice the result into a card without a guard: null children
    // are dropped by Ui.el.
    amountStatusBadge(amountStatus) {
      if (!amountStatus) return null;
      const m = AMOUNT_STATUS_META[amountStatus];
      if (!m) return null;
      return Ui.el('span', { class: 'badge ' + m.badge }, [Ui.ic(m.icon), m.label]);
    },

    amountStatusLabel(amountStatus) {
      return AMOUNT_STATUS_META[amountStatus] ? AMOUNT_STATUS_META[amountStatus].label : '';
    },

    availBadge(a) {
      const m = Ui.availMeta(a);
      return Ui.el('span', { class: 'badge ' + m.badge }, [Ui.ic(m.icon), Ui.availabilityLabel(a)]);
    },

    escapeHtml(str) {
      return String(str == null ? '' : str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    },

    el(tag, options, children) {
      options = options || {};
      const node = document.createElement(tag);
      if (options.class) node.className = options.class;
      if (options.text !== undefined) node.textContent = options.text;
      if (options.html !== undefined) node.innerHTML = options.html;
      if (options.id) node.id = options.id;
      if (options.attrs) {
        Object.entries(options.attrs).forEach(([k, v]) => node.setAttribute(k, v == null ? '' : v));
      }
      if (options.on) {
        Object.entries(options.on).forEach(([ev, fn]) => node.addEventListener(ev, fn));
      }
      (children || []).forEach((c) => {
        if (c instanceof Node) node.appendChild(c);
        else if (c != null) node.appendChild(document.createTextNode(String(c)));
      });
      return node;
    },

    clear(node) {
      while (node.firstChild) node.removeChild(node.firstChild);
    },

    // URL that goes to the create-request flow, preserving the chosen service.
    serviceUrl(key) {
      const u = Auth.getUser();
      if (u && u.role === 'customer')
        return '/customer/create-request.html?service=' + encodeURIComponent(key);
      if (u && (u.role === 'admin' || u.role === 'technician')) {
        return '/customer/create-request.html?service=' + encodeURIComponent(key) + '&preview=1';
      }
      return '/login.html?service=' + encodeURIComponent(key);
    },

    // Public landing CTA target depending on the auth state.
    // Customers go to the wizard. Currently uncalled, but it is named as the
    // customer CTA resolver, so leaving it on the deprecated form would hand the
    // legacy page to the first caller that uses it.
    ctaUrl() {
      const u = Auth.getUser();
      if (u && u.role === 'customer') return '/request';
      if (u && u.role === 'technician') return '/technician/dashboard.html';
      if (u && u.role === 'admin') return '/admin/dashboard';
      return '#services';
    },

    // Skeletons for loading states.
    skeletonList(rows, type) {
      const wrap = document.createElement('div');
      wrap.className = 'req-list';
      for (let i = 0; i < rows; i++) {
        const sk = Ui.el('div', { class: 'sk sk-card' });
        sk.appendChild(Ui.el('div', { class: 'sk sk-line w60' }));
        sk.appendChild(Ui.el('div', { class: 'sk sk-line w40' }));
        sk.appendChild(Ui.el('div', { class: 'sk sk-line' }));
        sk.appendChild(Ui.el('div', { class: 'sk sk-line w60' }));
        wrap.appendChild(sk);
      }
      return wrap;
    },

    skeletonTable(rows, cols) {
      const frag = document.createDocumentFragment();
      for (let i = 0; i < rows; i++) {
        const tds = [];
        for (let j = 0; j < cols; j++)
          tds.push(
            Ui.el('td', {}, [
              Ui.el('div', {
                class: 'sk sk-line',
                attrs: { style: 'width:' + (55 + ((j * 7) % 30)) + '%' },
              }),
            ])
          );
        frag.appendChild(Ui.el('tr', {}, tds));
      }
      return frag;
    },

    // Button loading state.
    btnLoading(btn, loadingText) {
      btn.setAttribute('data-label', btn.innerHTML);
      btn.disabled = true;
      btn.innerHTML = '<span class="spin"></span> ' + (loadingText || 'جارٍ التحميل…');
    },
    btnReset(btn) {
      btn.disabled = false;
      if (btn.getAttribute('data-label')) btn.innerHTML = btn.getAttribute('data-label');
    },

    // Generic modal. head may be a title string or { title, icon:{cls,bg,color} }.
    // Calling openModal(null, ...) closes the currently open modal.
    openModal(head, bodyNodes, footNodes, opts) {
      opts = opts || {};
      if (Ui.openModal.current) Ui.openModal.current.close();
      if (head == null) return { overlay: null, modal: null, close: function () {} };

      let title = '';
      let iconEl = null;
      if (typeof head === 'string') {
        title = head;
      } else {
        title = head.title || '';
        iconEl = head.icon
          ? Ui.el(
              'span',
              {
                class: 'mh-ic',
                attrs: {
                  style:
                    'background:' +
                    (head.icon.bg || 'var(--brand-soft)') +
                    ';color:' +
                    (head.icon.color || 'var(--brand)'),
                },
              },
              [Ui.ic(head.icon.cls)]
            )
          : null;
      }
      const overlay = Ui.el('div', { class: 'modal-overlay' });
      const footList = footNodes ? (Array.isArray(footNodes) ? footNodes : [footNodes]) : null;
      const bodyList =
        bodyNodes == null ? null : Array.isArray(bodyNodes) ? bodyNodes : [bodyNodes];
      const modal = Ui.el('div', { class: 'modal' + (opts.lg ? ' modal-lg' : '') }, [
        Ui.el('div', { class: 'modal-head' }, [
          iconEl,
          Ui.el('h3', {}, [title]),
          Ui.el('button', { class: 'modal-close', attrs: { 'aria-label': 'إغلاق' } }, [
            Ui.ic('fa-solid fa-xmark'),
          ]),
        ]),
        Ui.el('div', { class: 'modal-body' }, bodyList),
        footList && footList.length ? Ui.el('div', { class: 'modal-foot' }, footList) : null,
      ]);

      let done = false;
      function close() {
        if (done) return;
        done = true;
        if (Ui.openModal.current && Ui.openModal.current.overlay === overlay)
          Ui.openModal.current = null;
        overlay.classList.remove('open');
        document.removeEventListener('keydown', onKey);
        if (typeof opts.onClose === 'function') opts.onClose();
        setTimeout(() => overlay.remove(), 180);
      }
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) close();
      });
      modal.querySelector('.modal-close').addEventListener('click', close);
      function onKey(e) {
        if (e.key === 'Escape') close();
      }
      document.addEventListener('keydown', onKey);

      overlay.appendChild(modal);
      document.body.appendChild(overlay);
      requestAnimationFrame(() => overlay.classList.add('open'));
      Ui.openModal.current = { overlay, modal, close };
      if (typeof opts.onOpen === 'function') opts.onOpen();
      return Ui.openModal.current;
    },

    // Pagination.
    paginate(total, page, size, onGo) {
      const pages = Math.max(1, Math.ceil(total / size));
      const p = Math.min(page, pages);
      const wrap = Ui.el('div', { class: 'pager' });

      const btn = (label, targetPage, cls, disabled) =>
        Ui.el(
          'button',
          {
            class: cls || '',
            attrs: { disabled: disabled || '' },
            on: {
              click: () => {
                window.scrollTo({ top: 0, behavior: 'smooth' });
                onGo(targetPage);
              },
            },
          },
          [label]
        );

      wrap.appendChild(btn('<', p - 1, '', p <= 1));
      const start = Math.max(1, p - 2);
      const end = Math.min(pages, start + 4);
      for (let i = start; i <= end; i++) {
        wrap.appendChild(btn(String(i), i, i === p ? 'on' : ''));
      }
      wrap.appendChild(btn('>', p + 1, '', p >= pages));
      return wrap;
    },

    // Reveal-on-scroll.
    initReveal() {
      // 1. Any element with [data-reveal] but without .reveal gets it
      document.querySelectorAll('[data-reveal]:not(.reveal)').forEach((el) => {
        el.classList.add('reveal');
      });

      // 2. Auto-stagger children of grids
      document.querySelectorAll('.service-grid, .steps, .stat-grid').forEach((grid) => {
        Array.from(grid.children).forEach((child, i) => {
          if (!child.classList.contains('reveal')) child.classList.add('reveal');
          child.setAttribute('data-delay', String(Math.min(i + 1, 6)));
        });
      });

      // 3. Collect all .reveal elements not yet revealed
      const els = Array.from(document.querySelectorAll('.reveal:not(.in)'));

      if (!('IntersectionObserver' in window)) {
        els.forEach((e) => e.classList.add('in'));
        return;
      }

      // Disconnect previous observer
      if (Ui._revealObserver) {
        Ui._revealObserver.disconnect();
        Ui._revealObserver = null;
      }

      // 4. Create observer with early trigger
      const obs = new IntersectionObserver(
        (entries) => {
          entries.forEach((en) => {
            if (en.isIntersecting) {
              en.target.classList.add('in');
              obs.unobserve(en.target);
            }
          });
        },
        {
          threshold: 0.01,
          rootMargin: '0px 0px 0px 0px',
        }
      );

      els.forEach((e) => obs.observe(e));
      Ui._revealObserver = obs;
    },

    // Binds hamburger + avatar dropdowns on a page.
    initNav() {
      const toggle = document.querySelector('.nav-toggle');
      const menu = document.querySelector('.nav-menu');
      if (toggle && menu) {
        toggle.addEventListener('click', () => menu.classList.toggle('open'));
      }
      document.addEventListener('click', (e) => {
        document.querySelectorAll('.avatar-menu .dropdown.open').forEach((d) => {
          if (!d.closest('.avatar-menu') || !d.closest('.avatar-menu').contains(e.target))
            d.classList.remove('open');
        });
        const quoted = e.target.closest('.avatar-btn');
        const dropdown = quoted && quoted.parentElement.querySelector('.dropdown');
        if (dropdown) dropdown.classList.toggle('open');
      });
    },

    // Loads the public settings and writes them into any element carrying the
    // matching data attribute (footer brand, contact links, social icons).
    // Elements whose value is empty are hidden, so an admin clearing a URL
    // removes that icon from the footer instead of leaving a dead link.
    applySiteSettings: async function applySiteSettings() {
      try {
        const data = await api('/api/public/settings');
        const s = data.settings || {};

        document.querySelectorAll('[data-site-name]').forEach((el) => {
          if (s.site_name) el.textContent = s.site_name;
        });

        document.querySelectorAll('[data-site-tagline]').forEach((el) => {
          if (s.site_tagline) el.textContent = s.site_tagline;
        });

        document.querySelectorAll('[data-contact-address]').forEach((el) => {
          if (s.contact_address) el.textContent = s.contact_address;
        });

        // The contact value lives in a nested <span> so the icon beside it is not
        // wiped by textContent; the href always lands on the nearest anchor.
        document.querySelectorAll('[data-contact-phone]').forEach((el) => {
          if (!s.contact_phone) return;
          el.textContent = s.contact_phone;
          const link = el.tagName === 'A' ? el : el.closest('a');
          if (link) link.href = 'tel:' + s.contact_phone.replace(/\s/g, '');
        });

        document.querySelectorAll('[data-contact-email]').forEach((el) => {
          if (!s.contact_email) return;
          el.textContent = s.contact_email;
          const link = el.tagName === 'A' ? el : el.closest('a');
          if (link) link.href = 'mailto:' + s.contact_email;
        });

        const socialMap = {
          'data-social-whatsapp': s.whatsapp,
          'data-social-instagram': s.instagram_url,
          'data-social-facebook': s.facebook_url,
        };

        Object.entries(socialMap).forEach(([attr, url]) => {
          document.querySelectorAll('[' + attr + ']').forEach((el) => {
            if (url && url.trim() !== '') {
              // A bare phone number is expanded to a wa.me link; anything that
              // already looks like a URL is used as-is.
              let finalUrl = url.trim();
              if (attr === 'data-social-whatsapp' && isPhoneNumber(finalUrl)) {
                finalUrl = 'https://wa.me/' + finalUrl.replace(/\D/g, '');
              }
              el.href = finalUrl;
              el.style.display = '';
            } else {
              el.style.display = 'none';
            }
          });
        });
      } catch (e) {
        // Silent fail — the footer keeps its static markup.
        console.warn('[settings] failed to load', e);
      }
    },

    // Injects the user section into a public navbar (login buttons vs avatar).
    renderNavUser(navUserEl) {
      const user = Auth.getUser();
      const profile = Auth.getProfile();
      Ui.clear(navUserEl);
      if (!Auth.isAuthed()) {
        // Desktop-only buttons rendered into #nav-user
        navUserEl.appendChild(
          Ui.el(
            'a',
            {
              class: 'btn btn-ghost btn-sm nav-desktop-only',
              attrs: { href: '/login.html' },
            },
            ['تسجيل الدخول']
          )
        );

        navUserEl.appendChild(
          Ui.el(
            'a',
            {
              class: 'btn btn-brand btn-sm nav-desktop-only',
              attrs: { href: '/register.html' },
            },
            ['حساب جديد']
          )
        );

        navUserEl.appendChild(
          Ui.el(
            'a',
            {
              class: 'btn btn-outline btn-sm nav-desktop-only',
              attrs: { href: '/register-tech.html' },
            },
            [Ui.ic('fa-solid fa-screwdriver-wrench'), 'انضم كفني']
          )
        );

        // Also inject the same links into the mobile menu (if it exists)
        const mobileMenu = document.querySelector('.nav-menu');
        if (mobileMenu) {
          const guestLinks = [
            { href: '/login.html', label: 'تسجيل الدخول', cls: 'nav-mobile-only' },
            { href: '/register.html', label: 'حساب جديد', cls: 'nav-mobile-only' },
            { href: '/register-tech.html', label: 'انضم كفني', cls: 'nav-mobile-only' },
          ];
          guestLinks.forEach((g) => {
            mobileMenu.appendChild(
              Ui.el(
                'a',
                {
                  class: g.cls,
                  attrs: { href: g.href },
                },
                [g.label]
              )
            );
          });
        }
        return;
      }
      const name = (profile && profile.name) || user.email;
      const roleLabel = { customer: 'عميل', technician: 'فني', admin: 'إدارة' }[user.role] || '';
      const head = Ui.el('div', { class: 'dropdown-head' }, [
        Ui.el('b', {}, [name]),
        Ui.el('span', {}, [roleLabel + ' · ' + user.email]),
      ]);
      const links = [];
      if (user.role === 'customer') {
        // The customer navbar is deliberately two links wide (home + my requests),
        // so everything actionable lives here instead. 'اطلب الخدمة' points at the
        // wizard; the old /customer/create-request.html form and the
        // browse-technicians page are reachable by URL but no longer promoted.
        links.push(Ui.el('a', { attrs: { href: '/' } }, [Ui.ic('fa-solid fa-house'), 'الرئيسية']));
        links.push(
          Ui.el('a', { attrs: { href: '/request' } }, [Ui.ic('fa-solid fa-plus'), 'اطلب الخدمة'])
        );
        links.push(
          Ui.el('a', { attrs: { href: '/customer/my-requests.html' } }, [
            Ui.ic('fa-solid fa-list-check'),
            'طلباتي',
          ])
        );
      } else {
        links.push(
          Ui.el('a', { attrs: { href: Auth.homePath(user.role) } }, [
            Ui.ic('fa-solid fa-gauge-high'),
            'لوحة التحكم',
          ])
        );
      }
      // Technician-only: link to the self-service profile page (Phase 2).
      // Kept out of the shared branch above so admins don't get it either.
      if (user.role === 'technician') {
        links.push(
          Ui.el('a', { attrs: { href: '/technician/profile.html' } }, [
            Ui.ic('fa-solid fa-user'),
            'بروفايلي',
          ])
        );
      }
      if (user.role !== 'admin') {
        links.push(
          Ui.el('a', { attrs: { href: '/privacy.html', target: '_blank' } }, [
            Ui.ic('fa-solid fa-shield-halved'),
            'سياسة الخصوصية',
          ])
        );
        links.push(
          Ui.el(
            'button',
            {
              class: 'drop danger',
              on: {
                click: async (ev) => {
                  try {
                    if (
                      !confirm(
                        'هل أنت متأكد؟ سيتم حذف بياناتك الشخصية وطلباتك نهائياً خلال 30 يوماً وفقاً لسياسة الخصوصية.'
                      )
                    )
                      return;
                    await api('/api/data-subject/request', {
                      method: 'POST',
                      body: { request_type: 'erasure' },
                    });
                    toast('تم تسجيل طلب حذف بياناتك. سنتواصل معك خلال 30 يوماً.', 'success');
                  } catch (err) {
                    toast(err.message, 'error');
                  }
                },
              },
            },
            [Ui.ic('fa-solid fa-trash-can'), 'طلب حذف بياناتي']
          )
        );
      }
      // "Change password" is a security action every signed-in person needs, so
      // it lives in this shared dropdown rather than being repeated per role:
      // customers, technicians and admins all render the same menu.
      links.push(
        Ui.el('a', { attrs: { href: '/change-password.html' } }, [
          Ui.ic('fa-solid fa-key'),
          ' تغيير كلمة المرور',
        ])
      );
      links.push(
        Ui.el('button', { class: 'drop danger', on: { click: () => Auth.logout() } }, [
          Ui.ic('fa-solid fa-arrow-right-from-bracket'),
          'تسجيل الخروج',
        ])
      );
      const menu = Ui.el('div', { class: 'dropdown' }, [head].concat(links));
      navUserEl.appendChild(
        Ui.el('div', { class: 'avatar-menu' }, [
          Ui.el('button', { class: 'avatar-btn' }, [
            Ui.el('span', { class: 'avatar' }, [Ui.initials(name)]),
            Ui.el('span', { class: 'who' }, [
              Ui.el('b', {}, [name]),
              Ui.el('span', {}, [roleLabel]),
            ]),
            Ui.ic('fa-solid fa-chevron-down', 'chev'),
          ]),
          menu,
        ])
      );
    },
  };

  window.toast = function toast(message, type) {
    let root = document.getElementById('toast-root');
    if (!root) {
      root = Ui.el('div', { id: 'toast-root' });
      document.body.appendChild(root);
    }
    const icons = {
      success: 'fa-solid fa-circle-check',
      error: 'fa-solid fa-circle-xmark',
      warn: 'fa-solid fa-triangle-exclamation',
      '': 'fa-solid fa-circle-info',
    };
    const t = Ui.el('div', { class: 'toast ' + (type || '') }, [
      Ui.ic(icons[type || ''] || icons['']),
      message,
    ]);
    root.appendChild(t);
    setTimeout(() => {
      t.style.opacity = '0';
      t.style.transition = 'opacity 0.3s';
      setTimeout(() => t.remove(), 320);
    }, 3600);
  };

  window.setFieldError = function setFieldError(input, message) {
    const scope = input.closest('form') || input.closest('.modal') || document.body;
    clearFormErrors(scope);
    input.classList.add('invalid');
    const wrap = input.closest('.form-group') || input.parentElement;
    const err = Ui.el('div', { class: 'form-error' }, [
      Ui.ic('fa-solid fa-circle-exclamation'),
      message,
    ]);
    wrap.appendChild(err);
  };

  window.clearFormErrors = function clearFormErrors(scope) {
    (scope || document).querySelectorAll('.form-error').forEach((e) => e.remove());
    (scope || document)
      .querySelectorAll('.input.invalid')
      .forEach((e) => e.classList.remove('invalid'));
  };

  window.loadSpecialties = async function loadSpecialties(select, opts) {
    opts = opts || {};
    try {
      const data = await api('/api/specialties');
      Ui.clear(select);
      const keys = (data.specialties || []).map((s) => s.key);
      keys.forEach((k) =>
        select.appendChild(
          Ui.el('option', {
            text: SPECIALTY_META[k] ? SPECIALTY_META[k].label : k,
            attrs: { value: k },
          })
        )
      );
      return keys;
    } catch (e) {
      Ui.clear(select);
      const keys = Object.keys(SPECIALTY_META);
      keys.forEach((k) =>
        select.appendChild(Ui.el('option', { text: SPECIALTY_META[k].label, attrs: { value: k } }))
      );
      return keys;
    } finally {
      if (opts.placeholder) {
        select.insertAdjacentHTML(
          'afterbegin',
          '<option value="">' + opts.placeholder + '</option>'
        );
        if (!opts.value) select.value = '';
      } else if (opts.value) {
        select.value = opts.value;
      }
    }
  };

  window.Ui = Ui;
})();
