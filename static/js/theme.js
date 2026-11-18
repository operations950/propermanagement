/* The Light / Dark / Automatic switch. The page's <head> script (templates/base.html) has already set data-theme before first paint; this file wires the menu choices:
   it applies the choice at once, remembers it in this browser (localStorage) and, for a signed-in person, saves it on their profile (POST /theme/).
   The save needs the CSRF token, and the site's CSRF cookie is HttpOnly (a script cannot read it), so the token is printed on the <html> tag (data-csrf).
   Until the server confirms the save the choice is kept as "theme-pending", and the head script obeys it, so clicking and moving on at once can never fall back to the old saved value. */
(function () {
    'use strict';
    var LABELS = { light: 'Light', dark: 'Dark', auto: 'Automatic' };
    var root = document.documentElement;

    function csrf() {
        var t = root.getAttribute('data-csrf');
        if (t) return t;
        var el = document.querySelector('input[name=csrfmiddlewaretoken]');
        if (el && el.value) return el.value;
        var m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    function current() {
        try { return localStorage.getItem('theme-pending') || localStorage.getItem('theme-pref') || root.getAttribute('data-theme-pref') || 'auto'; } catch (e) { return 'auto'; }
    }

    function mark() {
        var pref = current();
        document.querySelectorAll('[data-theme-choice]').forEach(function (el) {
            var on = el.getAttribute('data-theme-choice') === pref;
            el.classList.toggle('active', on);
            el.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        document.querySelectorAll('[data-theme-label]').forEach(function (el) { el.textContent = LABELS[pref] || 'Automatic'; });
    }

    function save(pref) {
        var url = root.getAttribute('data-theme-url');
        if (!url) return;                                   // signed out: this browser only
        var body = new URLSearchParams();
        body.set('theme', pref);
        fetch(url, { method: 'POST', credentials: 'same-origin', keepalive: true, headers: { 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest' }, body: body })
            .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok && j.ok !== false, j: j }; }); })
            .then(function (res) {
                if (!res.ok) return;                         // keep it pending: tried again on the next page
                try { if (localStorage.getItem('theme-pending') === pref) localStorage.removeItem('theme-pending'); } catch (e) {}
                root.setAttribute('data-theme-pref', pref);
            })
            .catch(function () {});
    }

    function choose(pref) {
        if (!LABELS[pref]) return;
        try { localStorage.setItem('theme-pending', pref); } catch (e) {}
        if (window.__themeApply) window.__themeApply(pref);
        mark();
        save(pref);
    }

    document.addEventListener('click', function (e) {
        var el = e.target.closest('[data-theme-choice]');
        if (!el) return;
        e.preventDefault();
        choose(el.getAttribute('data-theme-choice'));
    });

    // a choice the server never confirmed (a dropped connection, a page closed too soon): settle it now
    try {
        var pending = localStorage.getItem('theme-pending');
        if (pending) {
            if (pending === root.getAttribute('data-theme-pref') || !root.getAttribute('data-theme-url')) localStorage.removeItem('theme-pending');
            else save(pending);
        }
    } catch (e) {}
    mark();
})();
