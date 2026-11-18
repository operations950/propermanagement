/* The Light / Dark / Automatic switch. The page's <head> script (templates/base.html) has already set data-theme before first paint; this file wires the menu choices:
   it applies the choice at once, remembers it in this browser (localStorage) and, for a signed-in person, saves it on their profile (POST /theme/). */
(function () {
    'use strict';
    var LABELS = { light: 'Light', dark: 'Dark', auto: 'Automatic' };

    function getCookie(name) {
        var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
        return m ? decodeURIComponent(m[1]) : '';
    }

    function current() {
        try { return localStorage.getItem('theme-pref') || document.documentElement.getAttribute('data-theme-pref') || 'auto'; } catch (e) { return 'auto'; }
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

    function choose(pref) {
        if (!LABELS[pref]) return;
        if (window.__themeApply) window.__themeApply(pref);
        document.documentElement.setAttribute('data-theme-pref', pref);
        mark();
        var url = document.documentElement.getAttribute('data-theme-url');
        if (!url) return;                                   // signed out: this browser only
        var body = new URLSearchParams();
        body.set('theme', pref);
        fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': getCookie('csrftoken'), 'X-Requested-With': 'XMLHttpRequest' }, body: body }).catch(function () {});
    }

    document.addEventListener('click', function (e) {
        var el = e.target.closest('[data-theme-choice]');
        if (!el) return;
        e.preventDefault();
        choose(el.getAttribute('data-theme-choice'));
    });
    mark();
})();
