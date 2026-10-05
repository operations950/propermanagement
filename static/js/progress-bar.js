/* A very thin bar across the top of the screen while the app is working, so a slow page (a QuickBooks pull, a big import)
 * never looks frozen. It appears only if the wait lasts: 150ms for a link or form navigation, 700ms for a background request
 * (so quick page loads and autocomplete never flash it). Same-origin requests only - a slow third-party widget (the weather)
 * is not the app working. window.ProgressBar.start()/done() are there for any script that wants to show it by hand. */
(function () {
  if (window.ProgressBar) return;
  var bar = document.createElement('div');
  bar.id = 'progress-bar';
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-label', 'Working');
  bar.setAttribute('aria-hidden', 'true');
  document.body.appendChild(bar);

  var showTimer = null, capTimer = null, manual = 0, pending = 0, navigating = false;

  function busy() { return navigating || manual > 0 || pending > 0; }
  function show() {
    showTimer = null;
    bar.classList.add('on');
    bar.setAttribute('aria-hidden', 'false');
    clearTimeout(capTimer);
    capTimer = setTimeout(function () { navigating = false; manual = 0; pending = 0; hide(); }, 120000); // never leave it running if a page never finishes
  }
  function hide() {
    clearTimeout(showTimer); showTimer = null;
    clearTimeout(capTimer);
    bar.classList.remove('on');
    bar.setAttribute('aria-hidden', 'true');
  }
  function after(delay) {
    if (!showTimer && !bar.classList.contains('on')) showTimer = setTimeout(show, delay);
  }
  function release() { if (!busy()) hide(); }

  // a click or submit that goes on to load a page. Another handler may cancel it after this one runs (a link a script
  // takes over), so look again once the event has finished.
  function navigation(e) {
    navigating = true;
    after(150);
    setTimeout(function () { if (e.defaultPrevented) { navigating = false; release(); } }, 0);
  }

  window.ProgressBar = {
    start: function () { manual += 1; after(0); },
    done: function () { manual = Math.max(0, manual - 1); release(); },
  };

  // a page restored from the back/forward cache comes back mid-state: never show a bar for a page that is already here
  window.addEventListener('pageshow', function () { manual = 0; pending = 0; navigating = false; hide(); });

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.hasAttribute('download') || a.hasAttribute('data-bs-toggle') || 'noProgress' in a.dataset) return;
    if (a.target && a.target !== '_self') return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || /^(javascript|mailto|tel):/i.test(href)) return;
    if (a.origin !== window.location.origin) return;
    if (a.hash && a.pathname === window.location.pathname && a.search === window.location.search) return; // same-page anchor
    navigation(e);
  });

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form || (form.target && form.target !== '_self') || ('noProgress' in form.dataset)) return;
    navigation(e);
  });

  if (window.fetch) {
    var nativeFetch = window.fetch;
    window.fetch = function (input) {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var sameOrigin = false;
      try { sameOrigin = new URL(url, window.location.href).origin === window.location.origin; } catch (err) { sameOrigin = false; }
      if (!sameOrigin) return nativeFetch.apply(this, arguments);
      pending += 1;
      after(700);
      var finish = function () { pending = Math.max(0, pending - 1); release(); };
      var promise = nativeFetch.apply(this, arguments);
      promise.then(finish, finish);
      return promise;
    };
  }
})();
