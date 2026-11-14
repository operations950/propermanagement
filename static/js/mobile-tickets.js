/* The phone version of Tickets (see static/css/mobile-tickets.css). Nothing here runs on a wide screen. */
(function () {
  var phone = window.matchMedia('(max-width: 767.98px)');

  // The list: the "More filters" sheet is the full list's own filter box, shown full screen.
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-m-filters-open]')) { document.body.classList.add('m-tf-open'); window.scrollTo(0, 0); }
    else if (e.target.closest('[data-m-filters-close]')) { document.body.classList.remove('m-tf-open'); }
  });

  // The ticket page: sections become tap-to-open rows; the big buttons at the top open a section and scroll to it.
  function accordions() {
    if (!phone.matches) return;
    var byTitle = {};
    document.querySelectorAll('.card.mb-3').forEach(function (card) {
      var body = card.querySelector(':scope > .card-body');
      if (!body) return;
      var title = body.querySelector('h6.card-title');
      if (!title || title.hasAttribute('data-bs-toggle') || card.classList.contains('m-acc')) return;
      var text = title.textContent.replace(/\s+/g, ' ').trim();
      var head = title;
      while (head.parentElement && head.parentElement !== body) head = head.parentElement;
      var wrap = document.createElement('div');
      wrap.className = 'm-acc-body';
      Array.prototype.slice.call(body.children).forEach(function (child) { if (child !== head) wrap.appendChild(child); });
      body.appendChild(wrap);
      card.classList.add('m-acc');
      title.setAttribute('role', 'button');
      title.setAttribute('tabindex', '0');
      var open = /^Update status/.test(text);                      // the working section stays open; the rest wait to be tapped
      card.classList.toggle('is-open', open);
      title.setAttribute('aria-expanded', open ? 'true' : 'false');
      function toggle() {
        var now = !card.classList.contains('is-open');
        card.classList.toggle('is-open', now);
        title.setAttribute('aria-expanded', now ? 'true' : 'false');
      }
      title.addEventListener('click', function (ev) { if (ev.target.closest('button, a, input, select')) return; toggle(); });
      title.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(); } });
      byTitle[text] = card;
    });
    window.__mTicketSections = byTitle;
    document.querySelectorAll('[data-m-goto]').forEach(function (btn) {          // a button for a section this ticket does not have (e.g. Mark done on a finished ticket) is not shown
      var t = btn.dataset.mGoto;
      if (t !== 'note' && t !== 'status' && !section(t)) btn.hidden = true;
    });
  }

  function section(name) {
    var map = window.__mTicketSections || {};
    var key = Object.keys(map).filter(function (k) { return k.indexOf(name) !== -1; })[0];
    return key ? map[key] : null;
  }

  function goto(target) {
    var el = null;
    if (target === 'note') {
      var s = section('Update status');
      if (s) s.classList.add('is-open');
      el = document.querySelector('#status-note-form');
      var ta = el && el.querySelector('textarea');
      if (ta) setTimeout(function () { ta.focus({ preventScroll: true }); }, 350);
    } else if (target === 'status') {
      var s2 = section('Update status');
      if (s2) s2.classList.add('is-open');
      el = document.querySelector('#status-form');
    } else {
      el = section(target);
      if (el) el.classList.add('is-open');
    }
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    el.classList.remove('m-flash');
    void el.offsetWidth;
    el.classList.add('m-flash');
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-m-goto]');
    if (btn) goto(btn.dataset.mGoto);
  });

  // The New ticket form: "More options" and the quick property / "Assign to me" chips (each presses the real bubble, so the form posts what it always did).
  document.addEventListener('click', function (e) {
    var more = e.target.closest('[data-m-more]');
    if (more) {
      var box = document.getElementById('m-more-fields');
      box.classList.toggle('open');
      more.textContent = box.classList.contains('open') ? 'Fewer options' : more.dataset.label;
      return;
    }
    var prop = e.target.closest('[data-m-pick-property]');
    if (prop) {
      var picker = document.querySelector('[data-bubble-picker][data-name="property"]');
      var bubble = picker && picker.querySelector('.bubble-pool .bubble[data-value="' + prop.dataset.mPickProperty + '"]');
      if (bubble && picker._bubbleApi) { picker._bubbleApi.lock(bubble); picker.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
      return;
    }
    var me = e.target.closest('[data-m-me]');
    if (me) {
      var staff = document.getElementById('staff-picker');
      var mine = staff && staff.querySelector('.bubble[data-value="' + me.dataset.mMe + '"]');
      if (mine && staff._bubbleApi) staff._bubbleApi.lock(mine);
    }
  });

  document.addEventListener('DOMContentLoaded', accordions);
  if (document.readyState !== 'loading') accordions();
  if (phone.addEventListener) phone.addEventListener('change', accordions);
})();
