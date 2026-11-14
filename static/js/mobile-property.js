/* The property page on a phone: tap a section to open or close it, "More" holds everything else, a code has a Copy button, and a link to a card that lives under "More" opens it.
   Nothing here changes data. On a computer none of these controls is shown, so nothing happens. */
(function () {
    'use strict';

    function setSection(sec, open) {
        sec.classList.toggle('pp-open', !!open);
        var head = sec.querySelector('[data-pp-toggle]');
        if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    function setMore(more, open) {
        more.classList.toggle('pp-open', !!open);
        var head = more.querySelector('[data-pp-more-toggle]');
        if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    /* open whatever holds this element (the "More" group, and the card inside it), then bring it into view */
    function reveal(target, scroll) {
        if (!target) return;
        var more = target.closest('[data-pp-more]');
        if (more) setMore(more, true);
        var sec = target.closest('[data-pp-sec]');
        if (sec) setSection(sec, true);
        var card = target.closest('.m-acc');
        if (card) {
            card.classList.add('m-open');
            var head = card.querySelector('.m-acc-head');
            if (head) head.setAttribute('aria-expanded', 'true');
        }
        if (scroll) setTimeout(function () { target.scrollIntoView({ block: 'start', behavior: 'smooth' }); }, 40);
    }

    function copyText(text, button) {
        function done() {
            var label = button.querySelector('span');
            button.classList.add('pp-copied');
            if (label) { var was = label.textContent; label.textContent = 'Copied'; setTimeout(function () { label.textContent = was; button.classList.remove('pp-copied'); }, 1500); }
            else setTimeout(function () { button.classList.remove('pp-copied'); }, 1500);
        }
        function fallback() {
            var box = document.createElement('textarea');
            box.value = text; box.setAttribute('readonly', ''); box.style.position = 'fixed'; box.style.top = '-1000px';
            document.body.appendChild(box); box.select();
            try { document.execCommand('copy'); done(); } catch (e) { /* the code stays selectable on screen */ }
            document.body.removeChild(box);
        }
        if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
    }

    document.addEventListener('click', function (e) {
        var toggle = e.target.closest('[data-pp-toggle]');
        if (toggle) { var sec = toggle.closest('[data-pp-sec]'); setSection(sec, !sec.classList.contains('pp-open')); return; }
        var moreToggle = e.target.closest('[data-pp-more-toggle]');
        if (moreToggle) { var more = moreToggle.closest('[data-pp-more]'); setMore(more, !more.classList.contains('pp-open')); return; }
        var copy = e.target.closest('[data-pp-copy]');
        if (copy) { e.preventDefault(); copyText(copy.getAttribute('data-pp-copy'), copy); return; }
        var jump = e.target.closest('[data-pp-jump]');
        if (jump) {
            var id = (jump.getAttribute('href') || '').replace('#', '');
            var target = id && document.getElementById(id);
            if (target) { e.preventDefault(); reveal(target, true); try { history.replaceState(null, '', '#' + id); } catch (err) { /* fine */ } }
        }
    });

    function fromHash() {
        var id = decodeURIComponent((window.location.hash || '').slice(1));
        var target = id && document.getElementById(id);
        if (target) reveal(target, false);
    }
    window.addEventListener('hashchange', fromHash);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fromHash); else fromHash();
})();
