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
   page you're already on. The trail ignores a repeat of the current page. */
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

    // Record this page: a repeat of the page on top is ignored; arriving at the
    // page just below the top means the user went back, so drop the top.
    function record() {
        if (/^\/(login|logout)\b/.test(location.pathname)) return;
        var trail = read(), current = here();
        if (trail.length && trail[trail.length - 1] === current) return;
        if (trail.length > 1 && trail[trail.length - 2] === current) trail.pop();
        else trail.push(current);
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
                if (!previous || previous === current) return;   // no trail: follow the link's own href
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
