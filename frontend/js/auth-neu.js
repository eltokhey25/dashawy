// Show/hide toggle for password fields on the soft-UI auth pages.
// Any <button data-pass-toggle="<input id>"> flips that input between
// password and text, and keeps its icon and pressed state in step.
(function () {
  document.querySelectorAll('[data-pass-toggle]').forEach(function (btn) {
    var input = document.getElementById(btn.getAttribute('data-pass-toggle'));
    if (!input) return;
    var icon = btn.querySelector('i');
    btn.addEventListener('click', function () {
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      btn.setAttribute('aria-label', show ? 'إخفاء كلمة المرور' : 'إظهار كلمة المرور');
      if (icon) icon.className = show ? 'fa-solid fa-eye-slash' : 'fa-solid fa-eye';
      input.focus();
    });
  });
})();
