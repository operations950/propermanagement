/*
 * Drag-and-drop contact import on the Contacts screen: drop a document,
 * Claude parses it (core/contact_document_import.py), and the results
 * render here as compact rows the staff member can edit (pencil) or
 * exclude (x) before accepting — nothing is saved as a real Contact until
 * "Accept all" is clicked. No dynamically-inserted bubble-pickers/row-edit
 * panels here on purpose (those shared modules only init what's on the
 * page at load time) — this stays self-contained with a plain <select>
 * for type and event delegation on the rows container, so it works
 * regardless of how many rows get added after the initial page load.
 */
(function () {
  'use strict';

  function escapeHtml(value) {
    var div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  }

  function initContactImport(root) {
    var parseUrl = root.dataset.parseUrl;
    var commitUrl = root.dataset.commitUrl;
    var csrfToken = root.querySelector('[name=csrfmiddlewaretoken]').value;
    var typeOptionsHtml = root.querySelector('[data-import-type-options]').innerHTML;

    var dropzone = root.querySelector('[data-import-dropzone]');
    var fileInput = root.querySelector('[data-import-file-input]');
    var loading = root.querySelector('[data-import-loading]');
    var errorBox = root.querySelector('[data-import-error]');
    var resultsBox = root.querySelector('[data-import-results]');
    var rowsBox = root.querySelector('[data-import-rows]');
    var footer = root.querySelector('[data-import-footer]');
    var countEl = root.querySelector('[data-import-count]');
    var acceptButton = root.querySelector('[data-import-accept-all]');

    var contacts = [];
    var editingIdx = null;

    function reset() {
      contacts = [];
      editingIdx = null;
      dropzone.hidden = false;
      loading.hidden = true;
      errorBox.hidden = true;
      resultsBox.hidden = true;
      footer.hidden = true;
      fileInput.value = '';
    }

    function showError(message) {
      loading.hidden = true;
      dropzone.hidden = false;
      errorBox.textContent = message;
      errorBox.hidden = false;
    }

    function typeLabel(value) {
      var tmp = document.createElement('select');
      tmp.innerHTML = typeOptionsHtml;
      var match = Array.prototype.find.call(tmp.options, function (o) { return o.value === value; });
      return match ? match.textContent : value;
    }

    function renderRows() {
      countEl.textContent = String(contacts.length);
      if (!contacts.length) {
        rowsBox.innerHTML = '<div class="text-center text-muted small py-3">No contacts left — drop another document or cancel.</div>';
        return;
      }
      rowsBox.innerHTML = contacts.map(function (c, idx) {
        if (idx === editingIdx) {
          return (
            '<div class="row g-2 align-items-center py-2 px-2' + (idx ? ' border-top' : '') + '" style="border-color: var(--border-hairline) !important;" data-import-row data-idx="' + idx + '">' +
            '<div class="col-3"><input type="text" class="form-control form-control-sm" data-field="name" value="' + escapeHtml(c.name) + '" placeholder="Name"></div>' +
            '<div class="col-2"><input type="text" class="form-control form-control-sm" data-field="phone" value="' + escapeHtml(c.phone) + '" placeholder="Phone"></div>' +
            '<div class="col-3"><input type="email" class="form-control form-control-sm" data-field="email" value="' + escapeHtml(c.email) + '" placeholder="Email"></div>' +
            '<div class="col-3"><select class="form-select form-select-sm" data-field="contact_type">' + typeOptionsHtml + '</select></div>' +
            '<div class="col-1 text-end">' +
            '<button type="button" class="btn btn-link btn-sm p-0 me-1" data-import-toggle title="Done"><i data-lucide="check" class="icon"></i></button>' +
            '<button type="button" class="btn btn-link btn-sm p-0 text-danger" data-import-remove title="Exclude"><i data-lucide="x" class="icon"></i></button>' +
            '</div></div>'
          );
        }
        return (
          '<div class="d-flex align-items-center gap-2 py-2 px-2' + (idx ? ' border-top' : '') + '" style="border-color: var(--border-hairline) !important;" data-import-row data-idx="' + idx + '">' +
          '<div class="flex-grow-1 text-truncate fw-medium">' + (escapeHtml(c.name) || '<span class="text-muted">(no name)</span>') + '</div>' +
          '<div class="text-truncate text-muted small" style="width: 120px;">' + (escapeHtml(c.phone) || '—') + '</div>' +
          '<div class="text-truncate text-muted small" style="width: 170px;">' + (escapeHtml(c.email) || '—') + '</div>' +
          '<span class="badge bg-light text-dark border flex-shrink-0">' + escapeHtml(typeLabel(c.contact_type)) + '</span>' +
          '<button type="button" class="btn btn-link btn-sm p-0 flex-shrink-0" data-import-toggle title="Edit"><i data-lucide="pencil" class="icon"></i></button>' +
          '<button type="button" class="btn btn-link btn-sm p-0 text-danger flex-shrink-0" data-import-remove title="Exclude"><i data-lucide="x" class="icon"></i></button>' +
          '</div>'
        );
      }).join('');
      if (window.lucide) lucide.createIcons();
      if (editingIdx !== null) {
        var select = rowsBox.querySelector('[data-idx="' + editingIdx + '"] [data-field=contact_type]');
        if (select) select.value = contacts[editingIdx].contact_type;
      }
    }

    function parseFile(file) {
      dropzone.hidden = true;
      errorBox.hidden = true;
      loading.hidden = false;

      var formData = new FormData();
      formData.append('file', file);
      formData.append('csrfmiddlewaretoken', csrfToken);

      fetch(parseUrl, { method: 'POST', body: formData, headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (resp) { return resp.json().then(function (data) { return { ok: resp.ok, data: data }; }); })
        .then(function (result) {
          loading.hidden = true;
          if (!result.ok || !result.data.success) {
            showError((result.data && result.data.error) || 'Something went wrong reading that document.');
            return;
          }
          contacts = result.data.contacts.map(function (c) {
            return {
              name: c.name || '', phone: c.phone || '', email: c.email || '',
              contact_type: c.contact_type || 'other', trade: c.trade || '',
            };
          });
          editingIdx = null;
          resultsBox.hidden = false;
          footer.hidden = false;
          renderRows();
        })
        .catch(function () {
          showError('Something went wrong reading that document — please try again.');
        });
    }

    dropzone.addEventListener('click', function () { fileInput.click(); });
    dropzone.addEventListener('dragover', function (e) {
      e.preventDefault();
      dropzone.style.background = 'var(--surface-page)';
    });
    dropzone.addEventListener('dragleave', function () { dropzone.style.background = ''; });
    dropzone.addEventListener('drop', function (e) {
      e.preventDefault();
      dropzone.style.background = '';
      var file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) parseFile(file);
    });
    fileInput.addEventListener('change', function () {
      if (fileInput.files[0]) parseFile(fileInput.files[0]);
    });

    rowsBox.addEventListener('click', function (e) {
      var rowEl = e.target.closest('[data-import-row]');
      if (!rowEl) return;
      var idx = Number(rowEl.dataset.idx);

      if (e.target.closest('[data-import-remove]')) {
        contacts.splice(idx, 1);
        if (editingIdx === idx) editingIdx = null;
        else if (editingIdx !== null && editingIdx > idx) editingIdx -= 1;
        renderRows();
        return;
      }
      if (e.target.closest('[data-import-toggle]')) {
        editingIdx = editingIdx === idx ? null : idx;
        renderRows();
      }
    });

    rowsBox.addEventListener('input', function (e) {
      var rowEl = e.target.closest('[data-import-row]');
      var field = e.target.dataset.field;
      if (!rowEl || !field) return;
      contacts[Number(rowEl.dataset.idx)][field] = e.target.value;
    });
    rowsBox.addEventListener('change', function (e) {
      var rowEl = e.target.closest('[data-import-row]');
      var field = e.target.dataset.field;
      if (!rowEl || !field) return;
      contacts[Number(rowEl.dataset.idx)][field] = e.target.value;
    });

    acceptButton.addEventListener('click', function () {
      if (!contacts.length) return;
      acceptButton.disabled = true;
      fetch(commitUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ contacts: contacts }),
      })
        .then(function (resp) { return resp.json(); })
        .then(function (data) {
          if (data.success) {
            window.location.reload();
          } else {
            acceptButton.disabled = false;
            showError(data.error || 'Something went wrong saving these contacts.');
            resultsBox.hidden = false;
            dropzone.hidden = true;
          }
        })
        .catch(function () {
          acceptButton.disabled = false;
          showError('Something went wrong saving these contacts — please try again.');
        });
    });

    root.closest('.modal').addEventListener('hidden.bs.modal', reset);
  }

  function init() {
    document.querySelectorAll('[data-contact-import]').forEach(initContactImport);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
