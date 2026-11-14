/* The installable web app, in the page.
 *   - registers the service worker (only over https, or on this computer);
 *   - shows a small "Install the app" tip once on a phone (Android: an Install button; iPhone: "Share, then Add to Home Screen"), and again whenever someone taps an
 *     element marked data-pwa-install (the menu's "Install the app");
 *   - when the app is running full screen (no browser, so no Back button) puts a small Back arrow in the top bar.
 * Nothing here is needed for the site to work; everything is wrapped so a blocked storage or an unusual browser just means no tip. */
(function () {
    var host = location.hostname;
    var local = host === 'localhost' || host === '127.0.0.1' || host === '[::1]';
    var standalone = (window.matchMedia && matchMedia('(display-mode: standalone)').matches) || window.navigator.standalone === true;
    var ua = navigator.userAgent || '';
    var ios = /iPad|iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
    var phone = window.matchMedia && matchMedia('(max-width: 767.98px)').matches && (matchMedia('(pointer: coarse)').matches || /Mobi|Android/i.test(ua));
    var SEEN = 'pwa-tip-seen';
    var deferred = null;

    if (standalone) document.documentElement.classList.add('pwa-standalone');

    if ('serviceWorker' in navigator && (location.protocol === 'https:' || local)) {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function (err) {
                if (window.console) console.warn('Service worker not registered:', err && err.message);
            });
        });
    }

    function tip() { return document.getElementById('pwaTip'); }
    function links() { return document.querySelectorAll('[data-pwa-install]'); }

    function show() {
        var el = tip();
        if (!el || standalone) return;
        var kind = deferred ? 'android' : ios ? 'ios' : 'other';
        el.querySelectorAll('[data-pwa-text]').forEach(function (s) { s.hidden = s.getAttribute('data-pwa-text') !== kind; });
        var go = el.querySelector('[data-pwa-go]');
        if (go) go.hidden = !deferred;
        el.hidden = false;
    }
    function hide() { var el = tip(); if (el) el.hidden = true; }
    function markSeen() { try { localStorage.setItem(SEEN, '1'); return true; } catch (e) { return false; } }
    function seen() { try { return localStorage.getItem(SEEN) === '1'; } catch (e) { return true; } }     // storage blocked: never nag

    window.addEventListener('beforeinstallprompt', function (event) {
        event.preventDefault();
        deferred = event;
    });
    window.addEventListener('appinstalled', function () {
        deferred = null;
        hide();
        links().forEach(function (a) { a.hidden = true; });
    });

    document.addEventListener('DOMContentLoaded', function () {
        var el = tip();
        if (el) {
            el.addEventListener('click', function (ev) {
                if (ev.target.closest('[data-pwa-close]')) { hide(); markSeen(); }
                if (ev.target.closest('[data-pwa-go]') && deferred) {
                    var ask = deferred;
                    deferred = null;
                    ask.prompt();
                    if (ask.userChoice) ask.userChoice.then(function () { hide(); markSeen(); });
                }
            });
        }
        links().forEach(function (a) {
            if (standalone) return;
            a.hidden = false;
            a.addEventListener('click', function (ev) { ev.preventDefault(); show(); });
        });
        if (phone && !standalone && !seen()) {
            window.setTimeout(function () { if (markSeen()) show(); }, 2500);     // the Android prompt event has fired by now
        }
        if (standalone && history.length > 1) addBack();
    });

    function addBack() {
        var brand = document.querySelector('.brand-bar .navbar-brand') || document.querySelector('.op-top .container > a');
        if (!brand || brand.parentNode.classList.contains('pwa-brand')) return;
        var wrap = document.createElement('div');
        wrap.className = 'pwa-brand d-flex align-items-center';
        var back = document.createElement('button');
        back.type = 'button';
        back.className = 'pwa-back';
        back.setAttribute('aria-label', 'Back');
        back.innerHTML = '&lsaquo;';
        back.addEventListener('click', function () { history.back(); });
        brand.parentNode.insertBefore(wrap, brand);
        wrap.appendChild(back);
        wrap.appendChild(brand);
    }
})();
