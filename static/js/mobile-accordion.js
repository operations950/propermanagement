/* The property page on a phone: its many cards become big tap-to-open sections (the first one open, the rest closed), with an Open all / Close all button.
   Nothing is removed or changed: a closed section is only hidden, so every form and link keeps working once it is opened. On wide screens this does nothing.
   Marks: the page's phone header carries data-m-acc-root; a section's heading is the first .card-title / h5 / h6 inside it. */
(function () {
    var mq = window.matchMedia('(max-width: 767.98px)');
    var sections = [];
    var ready = false;

    function setup() {
        if (ready) return;
        var root = document.querySelector('[data-m-acc-root]');
        if (!root || !root.parentElement) return;
        var container = root.parentElement;
        container.querySelectorAll('.card, section').forEach(function (el) {
            var outer = el.parentElement && el.parentElement.closest('.card, section, details');
            if (outer && container.contains(outer)) return;                 // only the top-level ones
            if (el.closest('[data-m-acc-root]') || el.classList.contains('alert')) return;
            var h = el.querySelector('.card-title, h5, h6');
            if (!h || h.hasAttribute('data-bs-toggle')) return;           // the message-history card has its own toggle
            var wrap = h.parentElement;
            while (wrap && wrap !== el && !wrap.classList.contains('card-body')) wrap = wrap.parentElement;
            if (!wrap) wrap = el;
            var head = h;
            while (head.parentElement !== wrap) head = head.parentElement;
            el.classList.add('m-acc');
            wrap.classList.add('m-acc-wrap');
            head.classList.add('m-acc-head');
            head.setAttribute('role', 'button');
            head.setAttribute('tabindex', '0');
            head.addEventListener('click', function (e) {
                var t = e.target.closest('a, button, input, select, textarea, label, summary');
                if (t && head.contains(t) && t !== head) return;           // a link or button inside the heading does its own thing
                toggle(el);
            });
            head.addEventListener('keydown', function (e) {
                if ((e.key === 'Enter' || e.key === ' ') && e.target === head) { e.preventDefault(); toggle(el); }
            });
            sections.push(el);
        });
        sections.forEach(function (el, i) { setOpen(el, i === 0); });
        ready = true;
        openFromHash(false);
    }

    function setOpen(el, open) {
        el.classList.toggle('m-open', !!open);
        var head = el.querySelector('.m-acc-head');
        if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    function toggle(el) { setOpen(el, !el.classList.contains('m-open')); syncButton(); }

    function allOpen() { return sections.length && sections.every(function (el) { return el.classList.contains('m-open'); }); }

    function syncButton() {
        document.querySelectorAll('[data-m-acc-all]').forEach(function (b) { b.textContent = allOpen() ? 'Close all sections' : 'Open all sections'; });
    }

    function openFromHash(scroll) {
        var id = decodeURIComponent((window.location.hash || '').slice(1));
        var target = id && document.getElementById(id);
        if (!target) return;
        var sec = target.closest('.m-acc');
        if (sec) {
            setOpen(sec, true);
            syncButton();
            if (scroll) setTimeout(function () { target.scrollIntoView({ block: 'start' }); }, 30);
        }
    }

    function apply() {
        document.body.classList.toggle('m-acc-on', mq.matches);
        if (mq.matches) { setup(); syncButton(); }
    }

    document.addEventListener('click', function (e) {
        var all = e.target.closest('[data-m-acc-all]');
        if (all) {
            var open = !allOpen();
            sections.forEach(function (el) { setOpen(el, open); });
            syncButton();
            return;
        }
        var link = e.target.closest('[data-m-acc-link]');
        if (link) {
            var id = (link.getAttribute('href') || '').replace('#', '');
            var target = id && document.getElementById(id);
            if (target && target.closest('.m-acc')) {
                e.preventDefault();
                setOpen(target.closest('.m-acc'), true);
                syncButton();
                history.replaceState(null, '', '#' + id);
                setTimeout(function () { target.scrollIntoView({ block: 'start', behavior: 'smooth' }); }, 30);
            }
        }
    });
    window.addEventListener('hashchange', function () { if (mq.matches) openFromHash(true); });
    if (mq.addEventListener) mq.addEventListener('change', apply); else if (mq.addListener) mq.addListener(apply);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply); else apply();
})();
