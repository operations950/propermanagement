/* Call and Text buttons that open the Quo app first, and fall back to the phone's own app when Quo is not there.

   A button is an ordinary link (<a href="tel:+15615550100" data-quo="openphone://dial?number=%2B15615550100&action=call">), so it works with no script and on a computer.
   On a phone this script tries the Quo link first. If the page goes into the background (the app opened) nothing more happens; if the page is still in front after 1.5 seconds, Quo
   is not installed, so it follows the ordinary tel: / sms: link instead, and remembers that for two weeks so the next tap goes straight to the phone's app. (iPhone Safari may show
   its own "cannot open the page" note the first time when Quo is missing; that is Safari, not the page.) */
(function () {
    'use strict';
    var KEY = 'quoMissingSince', WAIT = 1500, TWO_WEEKS = 14 * 24 * 3600 * 1000;

    function onAPhone() {
        try { return window.matchMedia('(max-width: 768px)').matches || window.matchMedia('(pointer: coarse)').matches; } catch (e) { return false; }
    }
    function quoMissing() {
        try {
            var since = parseInt(window.localStorage.getItem(KEY) || '0', 10);
            return since > 0 && (Date.now() - since) < TWO_WEEKS;
        } catch (e) { return false; }
    }
    function rememberMissing() {
        try { window.localStorage.setItem(KEY, String(Date.now())); } catch (e) { /* private mode: it just asks again next time */ }
    }

    document.addEventListener('click', function (event) {
        if (event.defaultPrevented || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        var link = event.target.closest ? event.target.closest('a[data-quo]') : null;
        if (!link || !onAPhone() || quoMissing()) return;
        var quo = link.getAttribute('data-quo'), plain = link.getAttribute('href');
        if (!quo || !plain) return;
        event.preventDefault();
        var left = false;
        function gone() { left = true; }
        function visibility() { if (document.hidden) left = true; }
        document.addEventListener('visibilitychange', visibility);
        window.addEventListener('pagehide', gone);
        window.addEventListener('blur', gone);
        window.setTimeout(function () {
            document.removeEventListener('visibilitychange', visibility);
            window.removeEventListener('pagehide', gone);
            window.removeEventListener('blur', gone);
            if (left || document.hidden) return;              // Quo opened (the page went to the background): done
            rememberMissing();
            window.location.href = plain;                     // Quo is not on this phone: the phone's own app
        }, WAIT);
        window.location.href = quo;
    });
})();
