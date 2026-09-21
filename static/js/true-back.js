/* True "back" for the small "← Something" links at the top of detail pages.

   Those links used to point at one fixed page, so reaching a detail page from
   somewhere else (a list, a dashboard, a property page) and pressing Back took
   you to that fixed page instead of the one you were actually on. This keeps a
   small per-tab trail of the pages you've visited and sends the link to the
   page BEFORE this one. The link's own href stays as the fallback for when
   there is no trail (a bookmarked page, a fresh tab).

   The trail (not history.back()) is used on purpose: saving a form or tapping
   an in-page action POSTs and redirects back to the same page, which leaves the
   same address in history twice — history.back() would then "go back" to the
   page you're already on.

   "Back" means the last SIGNIFICANTLY DIFFERENT screen. Changing a filter, a
   search, a month or a window length on a screen you're already on (the same
   path with a different query string) is still the same screen, so it replaces
   its trail entry instead of adding one: from a calendar you've flipped through
   three months, Back goes to where you came from, not to last month. */
(function () {
    var KEY = 'proptasks.trail';
    var MAX = 40;

    function read() {
        try { return JSON.parse(sessionStorage.getItem(KEY)) || []; } catch (e) { return []; }
    }
    function write(trail) {
        try { sessionStorage.setItem(KEY, JSON.stringify(trail.slice(-MAX))); } catch (e) { /* storage blocked: links just use their fallback */ }
    }
    function here() { return location.pathname + location.search; }

    function pathOf(url) { return url.split('?')[0].split('#')[0]; }

    // Record this page. Same screen as the one on top (only the query differs):
    // just refresh that entry. Arriving at the screen just below the top means
    // the user went back, so drop the top. Anything else is a new screen.
    function record() {
        if (/^\/(login|logout)\b/.test(location.pathname)) return;
        var trail = read(), current = here();
        if (trail.length && pathOf(trail[trail.length - 1]) === location.pathname) {
            trail[trail.length - 1] = current;
        } else if (trail.length > 1 && pathOf(trail[trail.length - 2]) === location.pathname) {
            trail.pop();
            trail[trail.length - 1] = current;
        } else {
            trail.push(current);
        }
        write(trail);
    }

    function isBackLink(a) {
        if (a.hasAttribute('data-no-back')) return false;
        return a.hasAttribute('data-back') || !!a.querySelector('[data-lucide="arrow-left"], .lucide-arrow-left');
    }

    function bind() {
        document.querySelectorAll('a').forEach(function (a) {
            if (a.__trueBack || !isBackLink(a) || a.closest('nav, .navbar, .brand-bar')) return;
            a.__trueBack = true;
            a.addEventListener('click', function (event) {
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;   // open-in-new-tab keeps working
                var trail = read(), current = here();
                var previous = trail.length > 1 && trail[trail.length - 1] === current ? trail[trail.length - 2] : null;
                if (!previous || pathOf(previous) === location.pathname) return;   // no trail: follow the link's own href
                event.preventDefault();
                trail.pop();
                write(trail);
                location.assign(previous);
            });
        });
    }

    record();
    document.addEventListener('DOMContentLoaded', bind);
    window.addEventListener('load', bind);     // again after icons have been swapped in
    window.addEventListener('pageshow', function (e) { if (e.persisted) { record(); bind(); } });
})();
