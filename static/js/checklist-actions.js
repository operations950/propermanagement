/*
 * Drives onsite's cleaner-facing checklist (visit_public.html): every
 * per-item action (done/skip/undo/note/photo), the issue-report form, and
 * the supply-reading buttons all submit via fetch and patch the DOM in
 * place instead of a normal form POST + redirect. A plain POST/redirect
 * flow means every single tap reloads the whole page and resets scroll to
 * the top — brutal on a 40+ item checklist, and it's also what made the
 * per-item photo upload need an explicit "Upload" button instead of
 * attaching as soon as a file is picked. This file is loaded only by
 * visit_public.html (see its own <script src> tag), not globally.
 *
 * Photo capture buttons (Take Photo / Choose File) are still wired by the
 * shared static/js/photo-capture.js, loaded globally in
 * vendorportal/base.html — this file only adds the auto-upload-on-select
 * behavior on top of that, it doesn't replace it.
 */
(function () {
  'use strict';

  function getCsrfToken() {
    var el = document.querySelector('[name=csrfmiddlewaretoken]');
    return el ? el.value : '';
  }

  function escapeHtml(s) {
    var div = document.createElement('div');
    div.textContent = s == null ? '' : s;
    return div.innerHTML;
  }

  function truncate(s, n) {
    return s && s.length > n ? s.slice(0, n) + '…' : (s || '');
  }

  function refreshIcons() {
    if (window.lucide) window.lucide.createIcons();
  }

  // Small, non-blocking inline error — appended right inside whatever
  // container is passed (an item, a form, a supply row), auto-clears
  // itself so a transient network hiccup doesn't leave a permanent
  // red message behind.
  function showError(container, message) {
    if (!container) return;
    var el = container.querySelector('[data-inline-error]');
    if (!el) {
      el = document.createElement('div');
      el.className = 'small mt-1';
      el.style.color = 'var(--status-critical)';
      el.setAttribute('data-inline-error', '');
      container.appendChild(el);
    }
    el.textContent = message || 'Something went wrong — try again.';
    clearTimeout(el._clearTimer);
    el._clearTimer = setTimeout(function () { el.remove(); }, 6000);
  }

  function postAction(fields) {
    var fd = new FormData();
    fd.append('csrfmiddlewaretoken', getCsrfToken());
    Object.keys(fields).forEach(function (key) { fd.append(key, fields[key]); });
    return fetch(window.location.pathname, {
      method: 'POST', body: fd, headers: { 'X-Requested-With': 'XMLHttpRequest' },
    })
      .then(function (r) { return r.json(); })
      .catch(function () { return { success: false, error: 'Network error — check your connection and try again.' }; });
  }

  function updateProgress(done, total) {
    var fill = document.getElementById('checklist-progress-fill');
    var label = document.getElementById('checklist-progress-label');
    if (fill && total) fill.style.width = Math.round((done / total) * 100) + '%';
    if (label && typeof total === 'number') label.textContent = done + '/' + total;
  }

  // Recomputed from the DOM rather than sent back from the server — every
  // item row already carries its own resolved/unresolved state visually,
  // so counting what's actually on screen can never drift from it.
  function recomputeSectionCount(itemEl) {
    var section = itemEl.closest('[data-section]');
    if (!section) return;
    var items = section.querySelectorAll('.checklist-item');
    var done = 0;
    items.forEach(function (el) {
      if (el.querySelector('.chip-btn-solid-done, .chip-btn-solid-skip')) done++;
    });
    var countEl = section.querySelector('[data-section-count]');
    if (!countEl) return;
    var allDone = done === items.length;
    countEl.classList.toggle('checklist-section-count-done', allDone);
    countEl.innerHTML = (allDone ? '<i data-lucide="check" class="icon"></i> ' : '') + done + '/' + items.length;
    refreshIcons();
  }

  function renderItemState(stateEl, item) {
    if (item.is_completed) {
      stateEl.innerHTML =
        '<button type="button" class="chip-btn chip-btn-solid-done" data-action="reopen_item">' +
        '<i data-lucide="check" class="icon"></i> Done — tap to undo</button>';
    } else if (item.skip_reason) {
      stateEl.innerHTML =
        '<button type="button" class="chip-btn chip-btn-solid-skip" data-action="reopen_item">' +
        '<i data-lucide="skip-forward" class="icon"></i> <span data-skip-summary>Skipped: ' +
        escapeHtml(truncate(item.skip_reason, 40)) + '</span> — tap to undo</button>';
    } else {
      stateEl.innerHTML =
        '<div class="chip-row">' +
        '<button type="button" class="chip-btn chip-btn-done" data-action="mark_item_done"><i data-lucide="check" class="icon"></i> Done</button>' +
        '<button type="button" class="chip-btn chip-btn-skip" data-action="open_skip"><i data-lucide="skip-forward" class="icon"></i> Skip</button>' +
        '</div>';
    }
    refreshIcons();
  }

  // Reads a File into a data: URL for an instant local preview — used for
  // photos directly, and as the fallback if a video's frame grab (below)
  // fails for any reason (so a thumbnail still shows something rather than
  // nothing while the real upload is in flight).
  function readAsDataURL(file) {
    return new Promise(function (resolve) {
      var reader = new FileReader();
      reader.onload = function () { resolve(reader.result); };
      reader.onerror = function () { resolve(null); };
      reader.readAsDataURL(file);
    });
  }

  // Videos have no server-independent thumbnail of their own — a bare
  // <video> element often just renders black/blank until a frame has
  // actually been decoded, and there's no guarantee of that happening
  // before someone glances at the checklist. Grabbing one frame locally
  // (from the file the cleaner just picked, before it's even uploaded)
  // gives a real thumbnail image instantly, works the same regardless of
  // storage backend, and needs no server support.
  function generateVideoPoster(file) {
    return new Promise(function (resolve) {
      var objectUrl = URL.createObjectURL(file);
      var video = document.createElement('video');
      video.preload = 'metadata';
      video.muted = true;
      video.playsInline = true;
      video.src = objectUrl;
      var settled = false;

      function finish(dataUrl) {
        if (settled) return;
        settled = true;
        URL.revokeObjectURL(objectUrl);
        resolve(dataUrl);
      }
      function drawFrame() {
        try {
          var canvas = document.createElement('canvas');
          canvas.width = video.videoWidth || 160;
          canvas.height = video.videoHeight || 160;
          canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
          finish(canvas.toDataURL('image/jpeg', 0.75));
        } catch (e) {
          finish(null);
        }
      }
      video.addEventListener('loadeddata', function () {
        try {
          video.currentTime = Math.min(0.15, (video.duration || 1) / 2);
        } catch (e) {
          drawFrame();
        }
      });
      video.addEventListener('seeked', drawFrame);
      video.addEventListener('error', function () { finish(null); });
      setTimeout(function () { finish(null); }, 4000); // never block on a stuck decode
      video.load();
    });
  }

  function updatePhotoStatus(itemEl) {
    var thumbsEl = itemEl.querySelector('[data-photo-thumbs]');
    var statusEl = itemEl.querySelector('[data-photo-status]');
    if (!thumbsEl || !statusEl) return;
    var settled = thumbsEl.querySelectorAll(
      '.checklist-photo-thumb:not(.checklist-photo-thumb-pending):not(.checklist-photo-thumb-failed)',
    ).length;
    if (settled > 0) {
      statusEl.style.color = 'var(--status-good)';
      statusEl.innerHTML = '<i data-lucide="check" class="icon"></i> ' + settled + ' photo(s) attached';
      refreshIcons();
    }
  }

  // Keeps the "Photo required" note in sync with skip/undo — mirrors
  // visit_public.html's own server-rendered logic (skipped items never
  // need a photo, see onsite/services/checklist.py::submit_visit) so a
  // skip doesn't leave a stale red "Photo required" showing for something
  // that no longer actually blocks Submit. Runs after every done/skip/undo
  // response since any of those can flip whether this item still requires
  // a photo, and also clears the "was blocking a failed Submit" highlight —
  // no reason to leave that showing once the cleaner has actually acted on it.
  function updatePhotoRequiredState(itemEl, item) {
    var statusEl = itemEl.querySelector('[data-photo-status]');
    itemEl.classList.remove('checklist-item-error');
    if (!statusEl) return;
    var hasSettledPhoto = itemEl.querySelectorAll(
      '.checklist-photo-thumb:not(.checklist-photo-thumb-pending):not(.checklist-photo-thumb-failed)',
    ).length > 0;
    if (item.skip_reason) {
      statusEl.style.color = 'var(--ink-muted)';
      statusEl.textContent = 'Skipped — photo not required';
    } else if (hasSettledPhoto) {
      updatePhotoStatus(itemEl);
    } else {
      statusEl.style.color = 'var(--status-critical)';
      statusEl.textContent = 'Photo required';
    }
  }

  function uploadOnePhoto(itemEl, itemId, file, thumbEl) {
    var fd = new FormData();
    fd.append('action', 'upload_item_photo');
    fd.append('csrfmiddlewaretoken', getCsrfToken());
    fd.append('item_id', itemId);
    fd.append('photo', file);
    fetch(window.location.pathname, {
      method: 'POST', body: fd, headers: { 'X-Requested-With': 'XMLHttpRequest' },
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.success || !data.media || !data.media.length) throw new Error(data.error || 'Upload failed — try again.');
        thumbEl.href = data.media[0].url;
        thumbEl.classList.remove('checklist-photo-thumb-pending');
        updatePhotoStatus(itemEl);
        itemEl.classList.remove('checklist-item-error');
      })
      .catch(function (err) {
        thumbEl.classList.remove('checklist-photo-thumb-pending');
        thumbEl.classList.add('checklist-photo-thumb-failed');
        // Show the ACTUAL reason (too large, wrong file type, server error,
        // network) right on the item — previously this only set a hover
        // title with no message on screen, so a failed upload looked
        // identical no matter why it failed, giving nothing to act on.
        var message = (err && err.message) || 'Upload failed — try again.';
        thumbEl.title = message + ' (tap to retry)';
        showError(itemEl, message);
        thumbEl.onclick = function (e) {
          e.preventDefault();
          thumbEl.classList.remove('checklist-photo-thumb-failed');
          thumbEl.classList.add('checklist-photo-thumb-pending');
          uploadOnePhoto(itemEl, itemId, file, thumbEl);
        };
      });
  }

  // --- Checklist item actions (done/undo/skip/note), delegated so newly
  // rendered buttons (e.g. after an undo) work without re-binding. ---
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action]');
    if (!btn) return;
    var action = btn.dataset.action;
    var itemEl = btn.closest('.checklist-item');

    if (action === 'mark_item_done' || action === 'reopen_item') {
      if (!itemEl) return;
      var itemId = itemEl.dataset.itemId;
      btn.disabled = true;
      postAction({ action: action, item_id: itemId }).then(function (data) {
        btn.disabled = false;
        if (!data.success) return showError(itemEl, data.error);
        renderItemState(itemEl.querySelector('[data-item-state]'), data.item);
        updatePhotoRequiredState(itemEl, data.item);
        updateProgress(data.done, data.total);
        recomputeSectionCount(itemEl);
      });
      return;
    }

    if (action === 'open_skip') {
      if (!itemEl) return;
      var panel = itemEl.querySelector('[data-skip-panel]');
      panel.hidden = false;
      panel.querySelector('[data-skip-input]').focus();
      return;
    }

    if (action === 'cancel_skip') {
      if (!itemEl) return;
      var cancelPanel = itemEl.querySelector('[data-skip-panel]');
      cancelPanel.hidden = true;
      var input = cancelPanel.querySelector('[data-skip-input]');
      input.value = '';
      input.classList.remove('is-invalid');
      return;
    }

    if (action === 'confirm_skip') {
      if (!itemEl) return;
      var confirmPanel = itemEl.querySelector('[data-skip-panel]');
      var reasonInput = confirmPanel.querySelector('[data-skip-input]');
      var reason = reasonInput.value.trim();
      if (!reason) {
        reasonInput.classList.add('is-invalid');
        reasonInput.focus();
        return;
      }
      reasonInput.classList.remove('is-invalid');
      var skipItemId = itemEl.dataset.itemId;
      btn.disabled = true;
      postAction({ action: 'skip_item', item_id: skipItemId, skip_reason: reason }).then(function (data) {
        btn.disabled = false;
        if (!data.success) return showError(confirmPanel, data.error);
        confirmPanel.hidden = true;
        reasonInput.value = '';
        renderItemState(itemEl.querySelector('[data-item-state]'), data.item);
        updatePhotoRequiredState(itemEl, data.item);
        updateProgress(data.done, data.total);
        recomputeSectionCount(itemEl);
      });
      return;
    }

    if (action === 'save_note') {
      if (!itemEl) return;
      var noteInput = itemEl.querySelector('[data-note-input]');
      var noteItemId = itemEl.dataset.itemId;
      btn.disabled = true;
      postAction({ action: 'note_item', item_id: noteItemId, note: noteInput.value.trim() }).then(function (data) {
        btn.disabled = false;
        if (!data.success) return showError(itemEl, data.error);
        var summary = itemEl.querySelector('[data-note-summary]');
        if (summary) summary.textContent = data.item.note ? ('Note: ' + truncate(data.item.note, 40)) : '+ Add a note';
      });
      return;
    }

    if (action === 'record_supply_reading') {
      var row = btn.closest('[data-supply-row]');
      if (!row) return;
      var propertySupplyId = row.dataset.propertySupplyId;
      var level = btn.dataset.level;
      btn.disabled = true;
      postAction({ action: 'record_supply_reading', property_supply_id: propertySupplyId, level: level }).then(function (data) {
        btn.disabled = false;
        if (!data.success) return showError(row, data.error);
        var body = row.querySelector('[data-supply-body]');
        var badgeClass = data.level === 'high' ? 'bg-success' : data.level === 'mid' ? 'bg-warning text-dark' : 'bg-danger';
        body.innerHTML =
          '<div class="small mt-1">You said: <span class="badge rounded-pill ' + badgeClass + '">' +
          escapeHtml(data.level_display) + '</span></div>';
      });
      return;
    }
  });

  // --- Photo upload: auto-fires on file selection (Take Photo / Choose
  // File are wired by photo-capture.js), no separate Upload tap needed.
  // `multiple` on the input means one selection can carry several files.
  //
  // Each file gets its own optimistic thumbnail — a locally-generated
  // preview (instant, no network round trip) shown right away with a
  // "pending" spinner overlay, while the real upload happens in the
  // background. This is what makes attaching feel instant instead of
  // "select a photo, then wait": the cleaner sees their photo attached
  // immediately, they just don't yet know the server has it. Uploading
  // one file per request (rather than the whole batch together) means
  // one slow/failed file never blocks or fails the others, and each gets
  // its own tap-to-retry if it does fail.
  document.querySelectorAll('[data-photo-input]').forEach(function (input) {
    input.addEventListener('change', function () {
      var files = Array.prototype.slice.call(input.files || []);
      if (!files.length) return;
      var itemEl = input.closest('.checklist-item');
      if (!itemEl) return;
      var thumbsEl = itemEl.querySelector('[data-photo-thumbs]');
      var itemId = itemEl.dataset.itemId;

      files.forEach(function (file) {
        var isVideo = file.type.indexOf('video') === 0;
        var previewPromise = isVideo ? generateVideoPoster(file).then(function (poster) {
          return poster || readAsDataURL(file); // last-resort fallback if the frame grab failed
        }) : readAsDataURL(file);

        previewPromise.then(function (previewSrc) {
          var thumb = document.createElement('a');
          thumb.href = '#';
          thumb.target = '_blank';
          thumb.rel = 'noopener';
          thumb.className = 'checklist-photo-thumb checklist-photo-thumb-pending';
          var img = document.createElement('img');
          img.src = previewSrc || '';
          img.alt = '';
          thumb.appendChild(img);
          if (isVideo) {
            var badge = document.createElement('span');
            badge.className = 'checklist-photo-thumb-video-badge';
            badge.innerHTML = '<i data-lucide="play" class="icon"></i>';
            thumb.appendChild(badge);
          }
          thumbsEl.appendChild(thumb);
          refreshIcons();
          uploadOnePhoto(itemEl, itemId, file, thumb);
        });
      });

      input.value = ''; // lets the same file be picked again later if needed
    });
  });

  // --- Report an issue ---
  var issueForm = document.getElementById('issue-form');
  if (issueForm) {
    issueForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var description = issueForm.querySelector('[name="description"]');
      var photos = issueForm.querySelector('[name="photos"]');
      if (!description.value.trim()) {
        description.focus();
        return;
      }
      var fd = new FormData();
      fd.append('action', 'add_issue');
      fd.append('csrfmiddlewaretoken', getCsrfToken());
      fd.append('description', description.value.trim());
      if (photos && photos.files) {
        for (var i = 0; i < photos.files.length; i++) fd.append('photos', photos.files[i]);
      }
      var submitBtn = issueForm.querySelector('button[type="submit"]');
      submitBtn.disabled = true;
      fetch(window.location.pathname, {
        method: 'POST', body: fd, headers: { 'X-Requested-With': 'XMLHttpRequest' },
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          submitBtn.disabled = false;
          if (!data.success) return showError(issueForm, data.error);
          var row = document.createElement('div');
          row.className = 'small border-top py-1';
          row.textContent = data.description;
          var list = document.getElementById('issue-list');
          if (list) list.prepend(row);
          description.value = '';
          if (photos) photos.value = '';
        })
        .catch(function () {
          submitBtn.disabled = false;
          showError(issueForm, 'Could not send — check your connection and try again.');
        });
    });
  }

  // --- Signature overlay: hidden until "Submit visit" is tapped, then
  // locked in place over the rest of the page instead of sitting inline
  // in the normal scroll flow (a long checklist above it was fighting the
  // signature card for scroll position). Signing successfully is what
  // actually fires the real submit — see static/js/signature-pad.js's
  // data-signature-no-reload branch, which dispatches 'signature:saved'
  // here instead of reloading on its own. ---
  var openSignatureBtn = document.getElementById('open-signature-btn');
  var signatureOverlay = document.getElementById('signature-overlay');
  var closeSignatureBtn = document.getElementById('close-signature-btn');

  function openOverlay() {
    signatureOverlay.hidden = false;
    document.body.style.overflow = 'hidden';
    refreshIcons();
  }
  function closeOverlay() {
    signatureOverlay.hidden = true;
    document.body.style.overflow = '';
  }

  if (openSignatureBtn && signatureOverlay) {
    openSignatureBtn.addEventListener('click', openOverlay);
  }
  if (closeSignatureBtn && signatureOverlay) {
    closeSignatureBtn.addEventListener('click', closeOverlay);
  }
  if (signatureOverlay) {
    // Tapping the dimmed backdrop (not the card itself) closes it too —
    // the usual modal convention.
    signatureOverlay.addEventListener('click', function (e) {
      if (e.target === signatureOverlay) closeOverlay();
    });
    signatureOverlay.addEventListener('signature:saved', function (e) {
      if (!e.detail || !e.detail.ok) {
        showError(signatureOverlay.querySelector('.signature-overlay-card'), 'Could not save signature — try again.');
        return;
      }
      var submitForm = document.getElementById('submit-visit-form');
      if (submitForm) submitForm.submit();
    });
  }

  // A failed Submit re-renders this same page (see onsite/views.py's
  // VisitSubmitBlocked handling) with the blocking rows already marked —
  // their containing sections are also forced open (see the `has_blocking`
  // context) so this always finds something real if it fires at all.
  var firstBlockingItem = document.querySelector('.checklist-item-error');
  if (firstBlockingItem) {
    firstBlockingItem.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
})();
