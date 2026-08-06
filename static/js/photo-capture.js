/*
 * Shared "Take Photo / Choose File" capture widget — the pattern vendor
 * portal's ticket_detail.html built inline for its single upload form
 * (task #160), generalized here so a page with many independent capture
 * spots (onsite's per-checklist-item photo requirement) doesn't need to
 * hand-roll the same three-line script once per item. Loaded globally
 * (see templates/base.html) — a safe no-op on any page with no
 * [data-photo-capture] elements, exactly like bubble-picker.js.
 *
 * Markup contract, per instance:
 *   <div data-photo-capture>
 *     <input type="file" data-photo-input hidden ...>
 *     <button type="button" data-photo-take-btn>Take Photo</button>
 *     <button type="button" data-photo-choose-btn>Choose File</button>
 *     <span data-photo-filename></span>
 *   </div>
 * All four data-photo-* elements must live inside the same
 * [data-photo-capture] container; data-photo-filename is optional.
 */
(function () {
  'use strict';

  function init(root) {
    var input = root.querySelector('[data-photo-input]');
    var takeBtn = root.querySelector('[data-photo-take-btn]');
    var chooseBtn = root.querySelector('[data-photo-choose-btn]');
    var filenameEl = root.querySelector('[data-photo-filename]');
    if (!input) return;

    if (takeBtn) {
      takeBtn.addEventListener('click', function () {
        input.setAttribute('capture', 'environment');
        input.click();
      });
    }
    if (chooseBtn) {
      chooseBtn.addEventListener('click', function () {
        input.removeAttribute('capture');
        input.click();
      });
    }
    input.addEventListener('change', function () {
      if (filenameEl) filenameEl.textContent = input.files[0] ? input.files[0].name : '';
    });
  }

  function initAll() {
    document.querySelectorAll('[data-photo-capture]').forEach(init);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();
