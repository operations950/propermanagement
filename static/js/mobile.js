/* The phone top bar and the full-site switch (see templates/_mobile_bar.html and static/css/mobile.css).
 *   - fills in the page's name in the bar;
 *   - decides whether this page is a main screen (no Back) or one level down (Back shows), and where Back goes: the page's own Up address when it has one (the wide screen the
 *     person came from, worked out on the server: core/nav.py, passed in #mBackUrl), else the page the person came from on this site, else the main screen of that area
 *     (Tickets, Properties, ...), never a dead end;
 *   - "Open the full site" (data-m-full) turns the phone bar off for the rest of the visit; "App menu" (data-m-app) turns it back on;
 *   - the Home calendar's day-by-day agenda (data-m-agenda).
 * Nothing here is needed for a page to work: with no script the bar is just Menu. Storage that is blocked only means the full-site switch does not stick. */
(function () {
    var KEY = 'm-full';
    var path = location.pathname;

    // Main screens: the six areas and the menu itself. Everything else is one level down.
    var TOP = ['/', '/m/', '/w/', '/w/home/', '/tickets/', '/onsite/', '/properties/', '/contacts/', '/clock/', '/owner/'];
    // Where Back falls back to when there is no earlier page on this site to return to.
    var AREAS = [
        ['/tickets', '/tickets/'], ['/properties', '/properties/'], ['/contacts', '/contacts/'], ['/w/onsite', '/onsite/'], ['/onsite', '/onsite/'], ['/clock', '/clock/'],
        ['/w/compliance', '/w/compliance/'], ['/compliance', '/w/compliance/'], ['/w/projects', '/w/projects/'], ['/processes', '/w/projects/'], ['/w/runs', '/w/projects/'],
        ['/w/financials', '/w/financials/'], ['/accounting', '/w/financials/'], ['/close', '/w/financials/'], ['/w/', '/w/']
    ];
    // A page you only pass through (a form): Back should skip it, not return to it.
    var FORMISH = /\/(edit|new|delete|add|create|upload)\/?$/;

    function isTop() { return TOP.indexOf(path) !== -1; }

    function areaRoot() {
        for (var i = 0; i < AREAS.length; i++) {
            if (path.indexOf(AREAS[i][0]) === 0) return AREAS[i][1];
        }
        return '/m/';
    }

    function cameFromHere() {
        try {
            if (!document.referrer || history.length < 2) return false;
            var r = new URL(document.referrer);
            if (r.origin !== location.origin) return false;
            if (r.pathname === path) return false;                    // a form that sent the person back to itself
            if (FORMISH.test(r.pathname)) return false;               // the form they just filled in
            return true;
        } catch (e) { return false; }
    }

    function pageTitle() {
        var t = (document.title || '').replace(/\s+[—–|-]\s*Proper.*$/i, '').trim();
        return (!t || /^Proper (Management|Realty)$/i.test(t)) ? 'Proper Realty' : t;
    }

    function setFull(on) {
        try { if (on) sessionStorage.setItem(KEY, '1'); else sessionStorage.removeItem(KEY); } catch (e) { /* the switch just does not stick */ }
    }

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('[data-m-full]').forEach(function (a) { a.addEventListener('click', function () { setFull(true); }); });
        document.querySelectorAll('[data-m-app]').forEach(function (a) { a.addEventListener('click', function () { setFull(false); }); });

        var bar = document.getElementById('mBar');
        if (bar) {
            var title = bar.querySelector('[data-m-title]');
            if (title) title.textContent = pageTitle();
            var back = bar.querySelector('[data-m-back]');
            if (back && !isTop()) {
                var hint = document.getElementById('mBackUrl');
                var explicit = hint ? hint.textContent.trim() : '';
                back.hidden = false;
                bar.classList.add('m-has-back');
                back.setAttribute('href', explicit || areaRoot());
                if (!explicit && cameFromHere()) {
                    back.addEventListener('click', function (ev) { ev.preventDefault(); history.back(); });
                }
            }
        }
        initAgenda();
    });

    /* Home: one day at a time with big previous and next buttons; the whole stretch at once; and the full calendar (add or edit an event) on request. */
    function initAgenda() {
        document.querySelectorAll('[data-m-agenda]').forEach(function (box) {
            var days = box.querySelectorAll('[data-day]');
            if (!days.length) return;
            var prev = box.querySelector('[data-prev]'), next = box.querySelector('[data-next]');
            var label = box.querySelector('[data-label]'), when = box.querySelector('[data-when]');
            var all = box.querySelector('[data-all]'), allBtn = box.querySelector('[data-toggle-all]'), editBtn = box.querySelector('[data-toggle-cal]');
            var head = box.querySelector('.m-agenda-head');
            var i = 0;
            function show(n) {
                i = Math.max(0, Math.min(days.length - 1, n));
                days.forEach(function (d, k) { d.hidden = k !== i; });
                if (label) label.textContent = days[i].getAttribute('data-label');
                if (when) when.textContent = days[i].getAttribute('data-when');
                if (prev) prev.disabled = i === 0;
                if (next) next.disabled = i === days.length - 1;
            }
            if (prev) prev.addEventListener('click', function () { show(i - 1); });
            if (next) next.addEventListener('click', function () { show(i + 1); });
            if (allBtn && all) {
                allBtn.addEventListener('click', function () {
                    var open = all.hidden;
                    all.hidden = !open;
                    days.forEach(function (d, k) { d.hidden = open || k !== i; });
                    if (head) head.hidden = open;
                    allBtn.setAttribute('aria-pressed', open ? 'true' : 'false');
                    allBtn.textContent = open ? 'Back to one day' : 'Next 2 weeks';
                });
            }
            if (editBtn) {
                editBtn.addEventListener('click', function () {
                    var home = box.closest('.m-home');
                    if (!home) return;
                    var on = home.classList.toggle('m-show-cal');
                    editBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
                    editBtn.textContent = on ? 'Hide the calendar tools' : 'Add or edit events';
                });
            }
            show(0);
        });
    }
})();
