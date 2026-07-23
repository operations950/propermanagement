/*
 * Whole-row click-to-edit — one pencil per row (not one per field), which
 * reveals every editable field for that row at once in a panel with its
 * own single Save button. Distinct from inline-edit.js's per-field pencil
 * pattern (auto-submit on a single bubble lock): here several bubble
 * pickers/plain inputs share one row, so nothing should auto-submit until
 * Save is clicked. Global single-open, same as inline-edit.js: opening
 * one row's panel closes whichever other row was open.
 *
 * Markup contract (works for a <tbody> wrapping two <tr>s, or a plain
 * <div> — anything that can hold the toggle/panel as descendants):
 *   <ANY data-row-edit>
 *     ...read-only summary... <button data-row-edit-toggle>
 *     <ANY data-row-edit-panel hidden>...fields + Save + data-row-edit-cancel...</ANY>
 *   </ANY>
 */
(function () {
  'use strict';

  function closeOthers(except) {
    document.querySelectorAll('[data-row-edit-panel]').forEach(function (panel) {
      if (panel === except || panel.hidden) return;
      panel.hidden = true;
    });
  }

  function initRow(row) {
    var toggle = row.querySelector('[data-row-edit-toggle]');
    var panel = row.querySelector('[data-row-edit-panel]');
    if (!toggle || !panel) return;

    toggle.addEventListener('click', function () {
      var opening = panel.hidden;
      closeOthers(panel);
      panel.hidden = !opening;
    });

    var cancel = panel.querySelector('[data-row-edit-cancel]');
    if (cancel) {
      cancel.addEventListener('click', function () { panel.hidden = true; });
    }
  }

  function init() {
    document.querySelectorAll('[data-row-edit]').forEach(initRow);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
