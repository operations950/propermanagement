/*
 * Click-to-reveal inline bubble edit — for dense, per-row table/card
 * fields (ticket_list.html's 4 columns, property_recurring_tasks.html's
 * per-row overrides) where an always-visible bubble pool would blow up
 * row height. Each field shows its current value as plain text plus a
 * pencil icon; clicking the pencil swaps that one field over to its
 * bubble picker (or ghost-text contact filter) inline. Opening any
 * picker closes whichever one was already open elsewhere on the page,
 * so at most one field is ever mid-edit at a time.
 *
 * By default, locking a bubble submits the picker's enclosing <form>
 * right away (replacing the onchange="this.form.submit()" convention
 * these fields used as plain <select>s — this is ticket_list.html's
 * case, one independent auto-submitting field per <form>). Add
 * data-inline-edit-no-autosubmit when several of these share one form
 * with its own manual submit button instead (property_recurring_tasks
 * .html's per-row overrides) — locking a bubble there just collapses
 * the picker back to its display (now showing the newly-picked label)
 * without submitting, so the user can set the other fields in the same
 * row before clicking that form's own Save button.
 *
 * No changes to bubble-picker.js itself — this only listens for the
 * bubble:lock / contact:choose CustomEvents it already dispatches. A
 * safe no-op on any page without [data-inline-edit].
 */
(function () {
  'use strict';

  function closeOthers(except) {
    document.querySelectorAll('[data-inline-edit-picker]').forEach(function (picker) {
      if (picker === except || picker.hidden) return;
      picker.hidden = true;
      var wrapper = picker.closest('[data-inline-edit]');
      var display = wrapper && wrapper.querySelector('[data-inline-edit-display]');
      if (display) display.hidden = false;
    });
  }

  function initInlineEdit(root) {
    var toggle = root.querySelector('[data-inline-edit-toggle]');
    var display = root.querySelector('[data-inline-edit-display]');
    var picker = root.querySelector('[data-inline-edit-picker]');
    if (!toggle || !picker) return;
    var autosubmit = !('inlineEditNoAutosubmit' in root.dataset);

    toggle.addEventListener('click', function () {
      var opening = picker.hidden;
      closeOthers(picker);
      picker.hidden = !opening;
      if (display) display.hidden = opening;
      // The popover normally hangs off the left edge of the trigger — for
      // a trigger near the right edge of the page/table (e.g. ticket_list's
      // rightmost Assignee column), that pushes it off-screen, so flip it
      // to hang off the right edge instead whenever it would overflow.
      if (opening) {
        picker.classList.remove('inline-edit-picker-align-right');
        if (picker.getBoundingClientRect().right > window.innerWidth) {
          picker.classList.add('inline-edit-picker-align-right');
        }
      }
    });

    function collapseWithLabel(label) {
      if (display && label) display.textContent = label;
      picker.hidden = true;
      if (display) display.hidden = false;
    }

    function handleChosen(label) {
      if (autosubmit) {
        var form = root.closest('form');
        if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
      } else {
        collapseWithLabel(label);
      }
    }

    var bubblePickerRoot = picker.querySelector('[data-bubble-picker]');
    if (bubblePickerRoot) {
      bubblePickerRoot.addEventListener('bubble:lock', function (e) { handleChosen(e.detail && e.detail.label); });
      // Clicking a locked bubble in its slot clears it back to empty — a
      // real inline action (e.g. clearing a ticket's Property back to
      // "none" moves it back into the pending-triage queue), so it
      // submits/collapses exactly like a fresh lock does.
      bubblePickerRoot.addEventListener('bubble:unlock', function () {
        var slotEl = bubblePickerRoot.querySelector('[data-bubble-slot]');
        handleChosen((slotEl && slotEl.dataset.emptyText) || '');
      });
    }
    var contactFilterRoot = picker.querySelector('[data-contact-filter]');
    if (contactFilterRoot) {
      contactFilterRoot.addEventListener('contact:choose', function (e) { handleChosen(e.detail && e.detail.label); });
    }
  }

  function init() {
    document.querySelectorAll('[data-inline-edit]').forEach(initInlineEdit);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
