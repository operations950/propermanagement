/* The Payables page: drop invoices (one request per file, so each shows its own progress), keep every review card true by refreshing it from the server after a change,
   and send Create / Discard / Create vendor as background requests so the page never jumps. Works without this script too (plain form posts), just with page reloads. */
(function () {
    var drop = document.getElementById('payDrop');
    var cards = document.getElementById('pcards');

    function icons() { if (window.lucide && lucide.createIcons) lucide.createIcons(); }
    function csrf() { var f = document.querySelector('#payDropForm [name=csrfmiddlewaretoken]'); return f ? f.value : ''; }
    function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
    function fragment(html) { var t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; }
    function hideEmpty() { var e = document.getElementById('payEmpty'); if (e) e.classList.add('d-none'); }
    function nextCard(card) {
        var n = card.nextElementSibling;
        while (n && !n.matches('[data-card]')) n = n.nextElementSibling;
        return n;
    }

    // ---------------------------------------------------------------- dropping files
    var progress = document.getElementById('payProgress');
    var pick = document.getElementById('payPick');
    var input = document.getElementById('payFiles');
    var busy = 0;

    function addRow(name) {
        var row = document.createElement('div');
        row.className = 'pay-file';
        row.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span><span class="text-break">' + esc(name) + '</span><span class="text-muted small ms-auto">reading...</span>';
        progress.appendChild(row);
        return row;
    }
    function finish(row, ok, text) {
        row.innerHTML = '<span>' + (ok ? '&#10003;' : '&#9888;') + '</span><span class="text-break">' + row.dataset.name + '</span><span class="small ms-auto ' + (ok ? 'text-muted' : 'text-danger') + '">' + esc(text) + '</span>';
    }
    function sendFiles(files) {
        var list = Array.prototype.slice.call(files);
        if (!list.length || !drop) return;
        var chain = Promise.resolve();
        busy += list.length;
        list.forEach(function (file) {
            var row = addRow(file.name);
            row.dataset.name = esc(file.name);
            chain = chain.then(function () {
                var fd = new FormData();
                fd.append('files', file);
                fd.append('csrfmiddlewaretoken', csrf());
                return fetch(drop.dataset.uploadUrl, {method: 'POST', body: fd, headers: {'X-Requested-With': 'XMLHttpRequest'}, credentials: 'same-origin'})
                    .then(function (r) { return r.json().catch(function () { return {ok: false, error: 'The server could not read that file.'}; }); })
                    .then(function (data) {
                        if (!data.ok) { finish(row, false, data.error || 'Could not add it.'); return; }
                        finish(row, true, 'ready to check');
                        if (cards) {
                            hideEmpty();
                            data.cards.forEach(function (c) { var el = fragment(c.html); if (el) cards.insertBefore(el, cards.firstChild); });
                            icons();
                        }
                    })
                    .catch(function () { finish(row, false, 'Could not reach the server. Try again.'); });
            }).then(function () {
                busy -= 1;
                if (busy === 0 && !cards && drop.dataset.reviewUrl) window.location = drop.dataset.reviewUrl;
                if (busy === 0 && cards) { var first = cards.querySelector('[data-card]'); if (first) first.scrollIntoView({behavior: 'smooth', block: 'start'}); }
            });
        });
    }
    if (pick && input) {
        pick.addEventListener('click', function () { input.click(); });
        input.addEventListener('change', function () { sendFiles(input.files); input.value = ''; });
    }
    function hasFiles(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') !== -1; }
    ['dragenter', 'dragover'].forEach(function (n) {
        document.addEventListener(n, function (e) { if (!hasFiles(e) || !drop) return; e.preventDefault(); drop.classList.add('over'); });
    });
    document.addEventListener('dragleave', function (e) { if (drop && (e.target === document.documentElement || !e.relatedTarget)) drop.classList.remove('over'); });
    document.addEventListener('drop', function (e) {
        if (!drop || !hasFiles(e)) return;
        e.preventDefault();
        drop.classList.remove('over');
        sendFiles(e.dataTransfer.files);
    });

    // ---------------------------------------------------------------- the review cards
    function post(form, action) {
        var fd = new FormData(form);
        fd.set('action', action);
        return fetch(form.action, {method: 'POST', body: fd, headers: {'X-Requested-With': 'XMLHttpRequest'}, credentials: 'same-origin'})
            .then(function (r) { return r.json(); });
    }
    function replaceCard(card, html) {
        var el = fragment(html);
        if (!el) return card;
        card.replaceWith(el);
        icons();
        return el;
    }
    function banner(card, text, bad) {
        var old = card.querySelector('[data-card-msg]');
        if (old) old.remove();
        var d = document.createElement('div');
        d.setAttribute('data-card-msg', '');
        d.className = 'ac-strip small mb-2' + (bad ? '' : ' ac-live');
        d.textContent = text;
        var form = card.querySelector('form');
        if (form) form.insertBefore(d, form.firstChild);
    }
    function busyCard(card, on) { card.style.opacity = on ? '0.6' : ''; card.style.pointerEvents = on ? 'none' : ''; }

    document.addEventListener('change', function (e) {
        var field = e.target.closest ? e.target.closest('[data-refresh]') : null;
        if (!field) return;
        var form = field.closest('[data-card-form]');
        if (!form) return;
        var card = form.closest('[data-card]');
        busyCard(card, true);
        post(form, 'refresh').then(function (data) { if (data.html) card = replaceCard(card, data.html); busyCard(card, false); })
            .catch(function () { busyCard(card, false); });
    });

    document.addEventListener('submit', function (e) {
        var form = e.target.closest ? e.target.closest('[data-card-form]') : null;
        if (!form) return;
        e.preventDefault();
        var button = e.submitter;
        var action = button ? button.value : 'refresh';
        if (button && button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
        if (action === 'refresh') return;
        var card = form.closest('[data-card]');
        busyCard(card, true);
        post(form, action).then(function (data) {
            if (data.done) {
                var next = nextCard(card);
                var done = data.html ? fragment(data.html) : null;
                if (done) card.replaceWith(done); else card.remove();
                icons();
                if (next) next.scrollIntoView({behavior: 'smooth', block: 'start'});
                return;
            }
            if (data.html) card = replaceCard(card, data.html);
            busyCard(card, false);
            if (data.error) banner(card, data.error, true); else if (data.note) banner(card, data.note, false);
        }).catch(function () { busyCard(card, false); banner(card, 'That did not work. Try again.', true); });
    });

    document.addEventListener('click', function (e) {
        var skip = e.target.closest ? e.target.closest('[data-skip]') : null;
        if (!skip) return;
        var card = skip.closest('[data-card]');
        var next = nextCard(card);
        card.classList.add('d-none');
        if (next) next.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
})();
