/*
 * Auto-formats every phone <input type="tel"> in the app as XXX-XXX-XXXX —
 * type digits only, dashes are inserted automatically. Matches the format
 * core.models.phone_validator enforces server-side. Loaded globally (see
 * templates/base.html); a safe no-op on any page with no tel inputs.
 */
(function () {
  'use strict';

  function formatDigits(digits) {
    digits = digits.slice(0, 10);
    if (digits.length <= 3) return digits;
    if (digits.length <= 6) return digits.slice(0, 3) + '-' + digits.slice(3);
    return digits.slice(0, 3) + '-' + digits.slice(3, 6) + '-' + digits.slice(6);
  }

  function initPhoneInput(input) {
    // Deliberately only formats as the user types — never rewrites a
    // pre-filled value on load, since older/imported numbers that predate
    // this format (e.g. a raw "+15619723598" from Quo) would otherwise get
    // silently mangled into a wrong-looking 10-digit number instead of
    // being left for a human to actually correct.
    input.addEventListener('input', function () {
      input.value = formatDigits(input.value.replace(/\D/g, ''));
    });
  }

  function init() {
    document.querySelectorAll('input[type="tel"]').forEach(initPhoneInput);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
