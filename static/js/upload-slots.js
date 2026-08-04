/*
 * Daily upload slots (onsite/booking_import_upload.html) — each
 * [data-upload-slot] wraps its own tiny <form> with a hidden file input.
 * Dropping a file onto the slot, or clicking it to browse, sets the
 * input's files and submits that slot's own form — no shared/parent form
 * involved, so there's nothing to coordinate across slots.
 */
(function () {
  'use strict';

  function initSlot(root) {
    var form = root.querySelector('form');
    var input = root.querySelector('input[type="file"]');
    if (!form || !input) return;

    root.addEventListener('dragover', function (e) {
      e.preventDefault();
      root.classList.add('upload-slot-hover');
    });
    root.addEventListener('dragleave', function () {
      root.classList.remove('upload-slot-hover');
    });
    root.addEventListener('drop', function (e) {
      e.preventDefault();
      root.classList.remove('upload-slot-hover');
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        form.submit();
      }
    });
    root.addEventListener('click', function (e) {
      if (e.target.closest('a')) return; // let a real link inside the slot behave normally
      input.click();
    });
    input.addEventListener('change', function () {
      if (input.files && input.files.length) form.submit();
    });
  }

  function init() {
    document.querySelectorAll('[data-upload-slot]').forEach(initSlot);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
