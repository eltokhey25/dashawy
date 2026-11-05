/**
 * @file   Show the photo attached to an order.
 * @module request-photo
 *
 * Order photos are private: /api/requests/<id>/photo answers only the
 * customer who placed the order, the technician it is assigned or offered
 * to, and admins. An <img src> carries no Authorization header, so the file
 * is fetched with the JWT (apiBlob) and shown from a blob: URL, the same way
 * the admin previews identity documents.
 *
 * Load order: needs Ui and apiBlob (api.js).
 */
(function () {
  'use strict';

  var LOADING = 'جارٍ تحميل الصورة…';
  var FAILED = 'تعذر تحميل الصورة';

  async function fetchObjectUrl(url) {
    var blob = await apiBlob(url);
    return URL.createObjectURL(blob);
  }

  // Full-size view in the shared modal.
  async function open(url) {
    var note = Ui.el('div', { class: 'muted', attrs: { style: 'text-align:center;padding:24px 0' } }, [LOADING]);
    var modal = Ui.openModal('صورة الطلب', note, null, {});
    try {
      var src = await fetchObjectUrl(url);
      var img = Ui.el('img', {
        attrs: {
          src: src,
          alt: 'صورة الطلب',
          style: 'display:block;max-width:100%;max-height:70vh;margin:0 auto;border-radius:12px',
        },
      });
      img.addEventListener('load', function () { URL.revokeObjectURL(src); });
      note.replaceWith(img);
    } catch (e) {
      note.textContent = (e && e.message) || FAILED;
    }
    return modal;
  }

  // A thumbnail for a request card or detail view. Loads lazily and, when
  // clicked, opens the full-size image (in a new tab inside an existing modal,
  // since opening the shared modal would close that one).
  function inline(url, opts) {
    opts = opts || {};
    var wrap = Ui.el('div', { class: 'req-photo', attrs: { style: 'margin-top:10px' } });
    var label = Ui.el('div', { class: 'muted', attrs: { style: 'font-size:0.8rem;margin-bottom:6px' } }, [
      Ui.ic('fa-solid fa-image'),
      ' صورة من العميل',
    ]);
    var img = Ui.el('img', {
      attrs: {
        alt: 'صورة الطلب',
        title: 'اضغط للتكبير',
        style:
          'display:block;max-width:100%;max-height:180px;border-radius:10px;cursor:zoom-in;' +
          'object-fit:cover;border:1px solid #e2e8f0;background:#f8fafc;min-height:48px',
      },
    });
    wrap.appendChild(label);
    wrap.appendChild(img);
    fetchObjectUrl(url)
      .then(function (src) {
        img.src = src;
        img.addEventListener('click', function () {
          if (opts.newTab) window.open(src, '_blank', 'noopener');
          else open(url);
        });
      })
      .catch(function () {
        wrap.replaceChildren(label, Ui.el('div', { class: 'muted', attrs: { style: 'font-size:0.8rem' } }, [FAILED]));
      });
    return wrap;
  }

  window.RequestPhoto = { open: open, inline: inline };
})();
