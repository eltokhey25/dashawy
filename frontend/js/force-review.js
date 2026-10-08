/**
 * @file   Force the customer to rate a completed request before using the app.
 * @module force-review
 *
 * A completed job is only really finished once the customer has rated it, so on
 * every customer-facing page this asks the server what is still un-reviewed and
 * puts a modal on screen that cannot be dismissed: no close button, no backdrop
 * click, Escape is swallowed, and if anything removes the node from the DOM a
 * MutationObserver puts it straight back.
 *
 * The rating itself is required, the comment is not. Pending reviews are shown
 * one at a time, oldest-completed last, and the queue is refreshed on load, on
 * window focus and every 30 seconds — so the prompt also appears for a customer
 * who never clicked the push notification, and reappears after a reload.
 *
 * Load order: needs Auth, api() and (for toasts) window.toast.
 */
(function () {
  'use strict';

  if (!window.Auth || !window.api) return;
  if (!Auth.isAuthed() || Auth.role() !== 'customer') return;

  // Never interrupt the request wizard or the auth pages: those need to stay
  // usable before the customer is properly onboarded.
  var path = window.location.pathname;
  if (
    path.indexOf('/request') === 0 ||
    path.indexOf('/create-request') === 0 ||
    path.indexOf('/register') === 0 ||
    path.indexOf('/login') === 0 ||
    path.indexOf('/change-password') === 0
  ) {
    return;
  }

  var CHECK_DELAY_MS = 1200; // let the page paint before the first fetch
  var REFRESH_MS = 30000;
  var PENDING_URL = '/api/customer/requests/pending-actions';

  var isModalOpen = false;
  var pendingQueue = [];
  var inFlight = false; // a check is already running
  var submittedIds = {}; // this session's successes, so the poll cannot re-open one

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function say(message, type) {
    if (typeof window.toast === 'function') window.toast(message, type);
  }

  // A push may deep-link straight to one specific job (?review=42). Put that one
  // first so the customer is rating what they tapped, and not some older job.
  function orderQueue(rows) {
    var wanted = parseInt(new URLSearchParams(window.location.search).get('review'), 10);
    if (!wanted) return rows;
    var hit = null;
    var rest = [];
    rows.forEach(function (row) {
      if (Number(row.id) === wanted) hit = row;
      else rest.push(row);
    });
    return hit ? [hit].concat(rest) : rows;
  }

  async function checkPending() {
    if (isModalOpen || inFlight) return;
    inFlight = true;
    try {
      var data = await api(PENDING_URL);
      var rows = (data && data.requests) || [];
      var fresh = rows.filter(function (row) {
        return !submittedIds[row.id];
      });
      if (fresh.length === 0) {
        pendingQueue = [];
        return;
      }
      pendingQueue = orderQueue(fresh);
      showNext();
    } catch (e) {
      // Silent by design: this runs on every page, and an expired session or a
      // flaky network must never surface an error the customer cannot act on.
      // The review itself stays enforced server-side.
    } finally {
      inFlight = false;
    }
  }

  function showNext() {
    if (pendingQueue.length === 0) {
      isModalOpen = false;
      return;
    }
    isModalOpen = true;
    showReviewModal(pendingQueue.shift());
  }

  function showReviewModal(request) {
    var selectedRating = 0;

    var overlay = document.createElement('div');
    // Re-attaching the SAME node on every childList change keeps the modal's
    // state (typed comment, chosen stars) intact if the page behind re-renders.
    overlay.setAttribute('data-force-review', '1');
    overlay.style.cssText = [
      'position:fixed',
      'inset:0',
      'z-index:99999',
      'background:rgba(15,23,42,0.85)',
      'backdrop-filter:blur(8px)',
      'display:flex',
      'align-items:center',
      'justify-content:center',
      'padding:16px',
      'overflow-y:auto',
    ].join(';');

    var box = document.createElement('div');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', 'force-review-title');
    box.style.cssText = [
      'background:#fff',
      'border-radius:20px',
      'max-width:460px',
      'width:100%',
      'padding:26px',
      'box-shadow:0 30px 70px rgba(0,0,0,0.35)',
      'text-align:center',
    ].join(';');

    var stars = '';
    for (var i = 1; i <= 5; i++) {
      stars +=
        '<button type="button" data-star="' +
        i +
        '" aria-label="' +
        i +
        ' نجوم" ' +
        'style="background:none;border:none;font-size:2.4rem;color:#cbd5e1;' +
        'cursor:pointer;padding:4px;line-height:1;">' +
        '<i class="fa-regular fa-star"></i></button>';
    }

    var amount = Number(request.technician_reported_amount || request.final_amount || 0);

    box.innerHTML = [
      '<div style="width:64px;height:64px;border-radius:50%;background:linear-gradient(135deg,#4f46e5,#a855f7);color:#fff;display:grid;place-items:center;font-size:1.6rem;margin:0 auto 16px;">',
      '<i class="fa-solid fa-star"></i>',
      '</div>',
      '<h2 id="force-review-title" style="font-size:1.4rem;font-weight:800;color:#0f172a;margin:0 0 8px;">',
      'قيّم الفني',
      '</h2>',
      '<p style="color:#64748b;font-size:0.9rem;margin:0 0 18px;line-height:1.7;">',
      '<b>' + escapeHtml(request.technician_name || 'الفني') + '</b> أنهى المهمة.',
      ' رأيك يساعد العملاء التانيين.',
      '</p>',
      '<div style="background:#f8fafc;border-radius:12px;padding:14px 16px;margin-bottom:20px;text-align:right;">',
      '<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed #e2e8f0;">',
      '<span style="color:#64748b;font-size:0.85rem;">الخدمة</span>',
      '<b style="color:#0f172a;font-size:0.9rem;">' +
        escapeHtml(request.specialty_label || request.specialty) +
        '</b>',
      '</div>',
      '<div style="display:flex;justify-content:space-between;padding:6px 0;">',
      '<span style="color:#64748b;font-size:0.85rem;">المبلغ المدفوع</span>',
      '<b style="color:#047857;font-size:1rem;">' +
        (amount > 0 ? escapeHtml(amount) + ' ج.م' : 'غير محدد') +
        '</b>',
      '</div>',
      '</div>',
      '<div id="force-stars" style="display:flex;justify-content:center;gap:6px;margin-bottom:6px;">',
      stars,
      '</div>',
      '<div style="color:#94a3b8;font-size:0.78rem;margin-bottom:16px;">اختر تقييم (1-5 نجوم)</div>',
      '<textarea id="force-comment" maxlength="1000"',
      'placeholder="اكتب تعليق (اختياري) — رأيك يساعد العملاء التانيين…"',
      'style="width:100%;min-height:70px;padding:12px 14px;border:1.5px solid #e2e8f0;border-radius:10px;font-family:inherit;font-size:0.9rem;resize:vertical;margin-bottom:14px;box-sizing:border-box;"></textarea>',
      '<button id="force-submit"',
      'style="width:100%;padding:14px;background:#4f46e5;color:#fff;border:none;border-radius:12px;font-family:inherit;font-size:1rem;font-weight:800;cursor:pointer;transition:background 0.15s;">',
      'إرسال التقييم',
      '</button>',
      '<div style="margin-top:12px;color:#94a3b8;font-size:0.75rem;line-height:1.6;">',
      'يجب إرسال التقييم قبل متابعة استخدام التطبيق',
      '</div>',
    ].join('');

    overlay.appendChild(box);
    document.body.appendChild(overlay);

    // Freeze the page behind so the modal cannot be scrolled away from.
    var previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    // --- Make it undismissable -------------------------------------------
    function blockKeys(event) {
      if (event.key === 'Escape' || event.key === 'Esc') {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      // Keep focus inside the dialog: without this, Tab walks into the page
      // behind the overlay, which reads as the modal having been dismissed.
      if (event.key === 'Tab') {
        var focusables = box.querySelectorAll('button, textarea, [href], input, select');
        if (focusables.length === 0) return;
        var first = focusables[0];
        var last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
    document.addEventListener('keydown', blockKeys, true);

    var observer = new MutationObserver(function () {
      if (!document.body.contains(overlay) && isModalOpen) {
        document.body.appendChild(overlay);
      }
    });
    observer.observe(document.body, { childList: true });

    // --- Stars ------------------------------------------------------------
    var starsWrap = box.querySelector('#force-stars');

    function paintStars(count) {
      var buttons = starsWrap.querySelectorAll('button');
      buttons.forEach(function (button, index) {
        var on = index + 1 <= count;
        button.innerHTML = on
          ? '<i class="fa-solid fa-star"></i>'
          : '<i class="fa-regular fa-star"></i>';
        button.style.color = on ? '#f59e0b' : '#cbd5e1';
      });
    }

    starsWrap.querySelectorAll('button').forEach(function (button) {
      button.addEventListener('click', function () {
        selectedRating = Number(button.dataset.star);
        paintStars(selectedRating);
      });
      button.addEventListener('mouseenter', function () {
        paintStars(Number(button.dataset.star));
      });
      button.addEventListener('mouseleave', function () {
        paintStars(selectedRating);
      });
    });

    var submitBtn = box.querySelector('#force-submit');
    submitBtn.addEventListener('click', async function () {
      if (selectedRating < 1) {
        say('اختر تقييم من 1 إلى 5 نجوم', 'error');
        submitBtn.focus();
        return;
      }

      submitBtn.disabled = true;
      submitBtn.textContent = 'جاري الإرسال…';

      try {
        var comment = box.querySelector('#force-comment').value.trim();
        var res = await api('/api/customer/requests/' + request.id + '/review', {
          method: 'POST',
          body: { rating: selectedRating, comment: comment },
        });
        if (!res) throw new Error('تعذر تسجيل التقييم');

        submittedIds[request.id] = true;
        say('شكرًا لتقييمك! ⭐', 'success');

        // Tear down in this order: stop the guards BEFORE removing the node, or
        // the observer would immediately put the overlay back.
        document.removeEventListener('keydown', blockKeys, true);
        observer.disconnect();
        document.body.style.overflow = previousOverflow;
        overlay.remove();

        isModalOpen = false;

        if (pendingQueue.length > 0) {
          setTimeout(showNext, 500);
        } else {
          // Ask once more in case another job finished while this one was open.
          setTimeout(checkPending, 1000);
        }
      } catch (err) {
        say(err.message || 'تعذر إرسال التقييم', 'error');
        submitBtn.disabled = false;
        submitBtn.textContent = 'إرسال التقييم';
      }
    });

    // Focus the first star so the dialog is usable from the keyboard at once.
    var firstStar = starsWrap.querySelector('button');
    if (firstStar) firstStar.focus();
  }

  window.addEventListener('load', function () {
    setTimeout(checkPending, CHECK_DELAY_MS);
  });

  window.addEventListener('focus', function () {
    if (!isModalOpen) setTimeout(checkPending, 500);
  });

  setInterval(function () {
    if (!isModalOpen) checkPending();
  }, REFRESH_MS);
})();
