/*
 * Shared, declarative "Duolingo-style" bubble picker + ghost-text contact
 * filter. Loaded globally (see templates/base.html) — a safe no-op on any
 * page with no [data-bubble-picker]/[data-contact-filter] elements, exactly
 * like the existing lucide.createIcons() call.
 *
 * Bubble picker: a hidden <input data-bubble-input> holds the real form
 * value. Clicking a .bubble inside a [data-bubble-pool] "locks" it — flies
 * it (FLIP animation) into [data-bubble-slot] and sets the hidden input.
 * Clicking the locked bubble flies it back to its original pool position
 * and clears the input. data-mode="drilldown" (the Property field) adds one
 * or two intermediate tiers that only control which pool is visible.
 *
 * Contact filter: a plain text input that live-filters an embedded JSON
 * list of {id, label} contacts client-side (no AJAX in this app), with an
 * "add new" row pinned first that reveals a small inline sub-form.
 */
(function () {
  'use strict';

  // ---- FLIP fly-to-slot animation ------------------------------------

  function flyTo(el, newParent, beforeNode, duration) {
    if (duration === undefined) duration = 320;
    const first = el.getBoundingClientRect();
    newParent.insertBefore(el, beforeNode || null);
    if (duration === 0) return; // instant — used for page-load rehydration
    const last = el.getBoundingClientRect();
    const dx = first.left - last.left;
    const dy = first.top - last.top;
    const sx = first.width / last.width;
    const sy = first.height / last.height;
    el.style.transformOrigin = 'top left';
    el.style.transition = 'none';
    el.style.transform = 'translate(' + dx + 'px, ' + dy + 'px) scale(' + sx + ', ' + sy + ')';
    el.getBoundingClientRect(); // force reflow so the "Invert" state commits
    el.style.transition = 'transform 320ms cubic-bezier(0.22, 1, 0.36, 1)';
    el.style.transform = 'none';
    el.addEventListener('transitionend', function done() {
      el.style.transition = '';
      el.style.transform = '';
      el.style.transformOrigin = '';
      el.removeEventListener('transitionend', done);
    });
  }

  const homes = new WeakMap(); // bubble el -> {parent, next} at the moment it locked

  function rememberHome(bubble) {
    homes.set(bubble, { parent: bubble.parentElement, next: bubble.nextElementSibling });
  }

  // ---- Single-tier picker (Priority / Department / Staff / Due date) --

  function initSinglePicker(root) {
    const input = root.querySelector('[data-bubble-input]');
    const slot = root.querySelector('[data-bubble-slot]');
    const pools = Array.from(root.querySelectorAll('[data-bubble-pool]'));

    function lock(bubble, duration) {
      const current = slot.querySelector('.bubble');
      if (current && current !== bubble) unlock(current, 0);
      rememberHome(bubble);
      flyTo(bubble, slot, null, duration);
      bubble.classList.add('bubble-locked');
      input.value = bubble.dataset.value;
      slot.classList.add('has-value');
      root.dispatchEvent(new CustomEvent('bubble:lock', {
        detail: { value: bubble.dataset.value, label: bubble.dataset.label },
      }));
    }

    function unlock(bubble, duration) {
      const home = homes.get(bubble);
      bubble.classList.remove('bubble-locked');
      if (home) flyTo(bubble, home.parent, home.next, duration);
      input.value = '';
      slot.classList.remove('has-value');
      root.dispatchEvent(new CustomEvent('bubble:unlock'));
    }

    pools.forEach(function (pool) {
      pool.addEventListener('click', function (e) {
        const bubble = e.target.closest('.bubble');
        if (!bubble || bubble.classList.contains('bubble-locked')) return;
        // Bubbles like the due-date "Custom" trigger their own reveal-a-picker
        // behavior on click (page-local script) and lock in later via the
        // exposed _bubbleApi once a real value exists — they opt out of the
        // pool's own immediate auto-lock-on-click.
        if (bubble.hasAttribute('data-no-autolock')) return;
        lock(bubble);
      });
    });

    slot.addEventListener('click', function (e) {
      const bubble = e.target.closest('.bubble');
      if (bubble) unlock(bubble);
    });

    root._bubbleApi = { lock: lock, unlock: unlock };

    const initial = input.value;
    if (initial) {
      const match = pools.reduce(function (found, pool) {
        return found || pool.querySelector('.bubble[data-value="' + cssEscape(initial) + '"]');
      }, null);
      if (match) lock(match, 0);
    }
  }

  // ---- Multi picker (Follow-Up: any number of simultaneous locks) -----
  // Unlike single/drilldown, there's no one hidden input carrying "the"
  // value — each locked bubble gets its own dynamically-created hidden
  // input (name=data-name on the root), so plain request.POST.getlist()
  // works server-side with zero custom serialization. No auto-unlock of
  // siblings: any number of bubbles can be locked in the slot at once.

  const multiHiddenInputs = new WeakMap(); // bubble el -> its own hidden <input>

  function initMultiPicker(root) {
    const name = root.dataset.name;
    const slot = root.querySelector('[data-bubble-slot]');
    const pools = Array.from(root.querySelectorAll('[data-bubble-pool]'));

    function lock(bubble, duration) {
      rememberHome(bubble);
      flyTo(bubble, slot, null, duration);
      bubble.classList.add('bubble-locked');
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = name;
      hidden.value = bubble.dataset.value;
      root.appendChild(hidden);
      multiHiddenInputs.set(bubble, hidden);
      root.dispatchEvent(new CustomEvent('bubble:lock', {
        detail: { value: bubble.dataset.value, label: bubble.dataset.label },
      }));
    }

    function unlock(bubble, duration) {
      const home = homes.get(bubble);
      bubble.classList.remove('bubble-locked');
      if (home) {
        // Unlike single/drilldown (never more than one locked bubble at a
        // time), several siblings can be locked simultaneously here — the
        // remembered "next sibling" may have *also* flown to the slot by
        // now, which would make it an invalid insertBefore() reference
        // (throwing, and aborting the rest of this function). Fall back to
        // appending at the end of the pool when that's happened.
        const beforeNode = home.next && home.next.parentElement === home.parent ? home.next : null;
        flyTo(bubble, home.parent, beforeNode, duration);
      }
      const hidden = multiHiddenInputs.get(bubble);
      if (hidden) {
        hidden.remove();
        multiHiddenInputs.delete(bubble);
      }
      root.dispatchEvent(new CustomEvent('bubble:unlock'));
    }

    pools.forEach(function (pool) {
      pool.addEventListener('click', function (e) {
        const bubble = e.target.closest('.bubble');
        if (!bubble) return;
        lock(bubble);
      });
    });

    slot.addEventListener('click', function (e) {
      const bubble = e.target.closest('.bubble');
      if (bubble) unlock(bubble);
    });

    root._bubbleApi = {
      lock: lock,
      unlock: unlock,
      // Finds-or-creates a bubble for a value not necessarily pre-rendered
      // in any pool (e.g. a contact found via a ghost-text search box
      // rather than one of the suggested bubbles) and locks it. Synthesized
      // bubbles go in the first pool so they have a "home" to fly back to
      // if unlocked.
      lockValue: function (value, label) {
        let bubble = pools.reduce(function (found, pool) {
          return found || pool.querySelector('.bubble[data-value="' + cssEscape(value) + '"]');
        }, null);
        if (!bubble) {
          bubble = document.createElement('button');
          bubble.type = 'button';
          bubble.className = 'bubble';
          bubble.dataset.value = value;
          bubble.dataset.label = label;
          bubble.textContent = label;
          pools[0].appendChild(bubble);
        }
        if (!bubble.classList.contains('bubble-locked')) lock(bubble);
      },
    };

    // Rehydration: data-initial-values is a comma-separated list of
    // already-selected values (e.g. a Contact's current property ids on
    // the edit form) — every matching bubble locks instantly on load. The
    // Follow-Up modal never sets this attribute (it always starts empty),
    // so this is a no-op there.
    const initialValues = (root.dataset.initialValues || '').split(',').map(function (v) { return v.trim(); }).filter(Boolean);
    initialValues.forEach(function (value) {
      const match = pools.reduce(function (found, pool) {
        return found || pool.querySelector('.bubble[data-value="' + cssEscape(value) + '"]');
      }, null);
      if (match) lock(match, 0);
    });
  }

  // ---- Drilldown picker (Property: type -> [city ->] property) --------

  function initDrilldownPicker(root) {
    const input = root.querySelector('[data-bubble-input]');
    const slot = root.querySelector('[data-bubble-slot]');
    const tier1 = root.querySelector('[data-bubble-pool][data-tier="1"]');
    const tier15Pools = Array.from(root.querySelectorAll('[data-bubble-pool][data-tier="1.5"]'));
    const tier2Pools = Array.from(root.querySelectorAll('[data-bubble-pool][data-tier="2"]'));
    const allPools = [tier1].concat(tier15Pools, tier2Pools);

    function showOnly(poolToShow) {
      allPools.forEach(function (p) {
        if (p) p.hidden = p !== poolToShow;
      });
    }

    function lock(bubble, duration) {
      // The tier-2 (or 1.5) pool stays visible after a lock (so its
      // sibling bubbles remain reachable without unlocking first) — so a
      // second click there must retire the first locked bubble itself,
      // exactly like the single-tier picker already does.
      const current = slot.querySelector('.bubble');
      if (current && current !== bubble) unlockBubble(current, 0);
      rememberHome(bubble);
      flyTo(bubble, slot, null, duration);
      bubble.classList.add('bubble-locked');
      input.value = bubble.dataset.value;
      slot.classList.add('has-value');
      root.dispatchEvent(new CustomEvent('bubble:lock', {
        detail: { value: bubble.dataset.value, label: bubble.dataset.label },
      }));
    }

    function unlockBubble(bubble, duration) {
      const home = homes.get(bubble);
      bubble.classList.remove('bubble-locked');
      if (home) flyTo(bubble, home.parent, home.next, duration);
    }

    function unlock(bubble) {
      unlockBubble(bubble);
      input.value = '';
      slot.classList.remove('has-value');
      showOnly(tier1);
      root.dispatchEvent(new CustomEvent('bubble:unlock'));
    }

    if (tier1) {
      tier1.addEventListener('click', function (e) {
        const bubble = e.target.closest('.bubble');
        if (!bubble) return;
        const typeKey = bubble.dataset.value;
        const city = root.querySelector('[data-tier="1.5"][data-parent="' + cssEscape(typeKey) + '"]');
        const flat = root.querySelector('[data-tier="2"][data-parent="' + cssEscape(typeKey) + '"]');
        showOnly(city || flat);
      });
    }

    tier15Pools.forEach(function (cityPool) {
      cityPool.addEventListener('click', function (e) {
        const bubble = e.target.closest('.bubble');
        if (!bubble) return;
        const tier2 = root.querySelector('[data-tier="2"][data-city="' + cssEscape(bubble.dataset.value) + '"]');
        if (tier2) showOnly(tier2);
      });
    });

    tier2Pools.forEach(function (pool) {
      pool.addEventListener('click', function (e) {
        const bubble = e.target.closest('.bubble');
        if (!bubble) return;
        lock(bubble);
      });
    });

    slot.addEventListener('click', function (e) {
      const bubble = e.target.closest('.bubble');
      if (bubble) unlock(bubble);
    });

    root._bubbleApi = { lock: lock, unlock: unlock };

    const initial = input.value;
    if (initial) {
      const match = root.querySelector('.bubble[data-value="' + cssEscape(initial) + '"]');
      if (match) {
        lock(match, 0);
      } else if (tier1) {
        showOnly(tier1);
      }
    } else if (tier1) {
      showOnly(tier1);
    }
  }

  // ---- Drilldown-multi picker (Contact's Properties: type -> property, ---
  // any number of locks, possibly spanning more than one type — e.g. a
  // board member on boards at two different associations) -------------
  // Combines drilldown's type-first navigation with multi's "each locked
  // bubble gets its own hidden input, no auto-unlock of siblings" behavior.
  // Since only one tier-2 pool is shown at a time, a bubble tagged
  // data-back in that pool (rendered first, styled distinctly in CSS)
  // jumps back to tier-1 instead of locking, so a second/third property
  // from a *different* type is reachable without unlocking anything.

  function initDrilldownMultiPicker(root) {
    const name = root.dataset.name;
    const slot = root.querySelector('[data-bubble-slot]');
    const tier1 = root.querySelector('[data-bubble-pool][data-tier="1"]');
    const tier15Pools = Array.from(root.querySelectorAll('[data-bubble-pool][data-tier="1.5"]'));
    const tier2Pools = Array.from(root.querySelectorAll('[data-bubble-pool][data-tier="2"]'));
    const allPools = [tier1].concat(tier15Pools, tier2Pools);

    function showOnly(poolToShow) {
      allPools.forEach(function (p) {
        if (p) p.hidden = p !== poolToShow;
      });
    }

    function lock(bubble, duration) {
      rememberHome(bubble);
      flyTo(bubble, slot, null, duration);
      bubble.classList.add('bubble-locked');
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = name;
      hidden.value = bubble.dataset.value;
      root.appendChild(hidden);
      multiHiddenInputs.set(bubble, hidden);
      root.dispatchEvent(new CustomEvent('bubble:lock', {
        detail: { value: bubble.dataset.value, label: bubble.dataset.label },
      }));
    }

    function unlock(bubble, duration) {
      const home = homes.get(bubble);
      bubble.classList.remove('bubble-locked');
      if (home) {
        const beforeNode = home.next && home.next.parentElement === home.parent ? home.next : null;
        flyTo(bubble, home.parent, beforeNode, duration);
      }
      const hidden = multiHiddenInputs.get(bubble);
      if (hidden) {
        hidden.remove();
        multiHiddenInputs.delete(bubble);
      }
      root.dispatchEvent(new CustomEvent('bubble:unlock'));
    }

    if (tier1) {
      tier1.addEventListener('click', function (e) {
        const bubble = e.target.closest('.bubble');
        if (!bubble) return;
        const typeKey = bubble.dataset.value;
        const city = root.querySelector('[data-tier="1.5"][data-parent="' + cssEscape(typeKey) + '"]');
        const flat = root.querySelector('[data-tier="2"][data-parent="' + cssEscape(typeKey) + '"]');
        showOnly(city || flat);
      });
    }

    tier15Pools.forEach(function (cityPool) {
      cityPool.addEventListener('click', function (e) {
        const bubble = e.target.closest('.bubble');
        if (!bubble) return;
        if (bubble.hasAttribute('data-back')) { showOnly(tier1); return; }
        const tier2 = root.querySelector('[data-tier="2"][data-city="' + cssEscape(bubble.dataset.value) + '"]');
        if (tier2) showOnly(tier2);
      });
    });

    tier2Pools.forEach(function (pool) {
      pool.addEventListener('click', function (e) {
        const bubble = e.target.closest('.bubble');
        if (!bubble || bubble.classList.contains('bubble-locked')) return;
        if (bubble.hasAttribute('data-back')) { showOnly(tier1); return; }
        lock(bubble);
      });
    });

    slot.addEventListener('click', function (e) {
      const bubble = e.target.closest('.bubble');
      if (bubble) unlock(bubble);
    });

    root._bubbleApi = { lock: lock, unlock: unlock };

    const initialValues = (root.dataset.initialValues || '').split(',').map(function (v) { return v.trim(); }).filter(Boolean);
    initialValues.forEach(function (value) {
      const match = tier2Pools.reduce(function (found, pool) {
        return found || pool.querySelector('.bubble[data-value="' + cssEscape(value) + '"]');
      }, null);
      if (match) lock(match, 0);
    });

    if (tier1) showOnly(tier1);
  }

  // ---- Ghost-text contact filter (Assigned Contractor / Reporter) -----

  function initContactFilter(root) {
    const hidden = root.querySelector('[data-contact-filter-input]');
    const text = root.querySelector('[data-contact-filter-text]');
    const list = root.querySelector('[data-contact-filter-list]');
    const addNewRow = root.querySelector('[data-contact-filter-addnew]');
    const sourceEl = root.querySelector('[data-contact-filter-source]');
    const contacts = JSON.parse((sourceEl && sourceEl.textContent) || '[]');

    function renderList(query) {
      list.innerHTML = '';
      const addBtn = document.createElement('button');
      addBtn.type = 'button';
      addBtn.className = 'contact-filter-item contact-filter-addnew-btn';
      addBtn.textContent = '+ Add new';
      addBtn.addEventListener('click', function () {
        list.hidden = true;
        addNewRow.hidden = false;
        const firstInput = addNewRow.querySelector('input');
        if (firstInput) firstInput.focus();
      });
      list.appendChild(addBtn);

      const q = (query || '').trim().toLowerCase();
      contacts
        .filter(function (c) { return !q || c.label.toLowerCase().indexOf(q) !== -1; })
        .slice(0, 25)
        .forEach(function (c) {
          const item = document.createElement('button');
          item.type = 'button';
          item.className = 'contact-filter-item';
          item.textContent = c.label;
          item.addEventListener('click', function () {
            hidden.value = c.id;
            text.value = c.label;
            list.hidden = true;
            addNewRow.hidden = true;
            root.dispatchEvent(new CustomEvent('contact:choose', { detail: { id: c.id, label: c.label } }));
          });
          list.appendChild(item);
        });
      list.hidden = false;
    }

    text.addEventListener('focus', function () { renderList(text.value); });
    text.addEventListener('input', function () {
      hidden.value = '';
      addNewRow.hidden = true;
      renderList(text.value);
    });
    document.addEventListener('click', function (e) {
      if (!root.contains(e.target)) list.hidden = true;
    });

    root._contactFilterApi = {
      clear: function () {
        hidden.value = '';
        text.value = '';
        addNewRow.hidden = true;
        list.hidden = true;
      },
    };

    if (hidden.value && text.value) {
      // Already has a server-rendered selection (validation-error re-render) — nothing to do,
      // the text/hidden values are already populated by the template.
    }
  }

  // ---- Optional text filter for a (currently dormant) >50-item tier ---
  // Rendered as the first child inside its own [data-bubble-pool], so it
  // shows/hides for free whenever the drilldown picker toggles that pool.

  function initBubbleFilter(input) {
    const pool = input.closest('[data-bubble-pool]');
    if (!pool) return;
    input.addEventListener('input', function () {
      const q = input.value.trim().toLowerCase();
      pool.querySelectorAll('.bubble').forEach(function (b) {
        if (b.hasAttribute('data-back')) return; // always reachable, never a search match
        b.hidden = Boolean(q) && b.dataset.label.toLowerCase().indexOf(q) === -1;
      });
    });
  }

  function cssEscape(value) {
    return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, '\\$&');
  }

  function init() {
    document.querySelectorAll('[data-bubble-picker]').forEach(function (root) {
      if (root.dataset.mode === 'drilldown') initDrilldownPicker(root);
      else if (root.dataset.mode === 'drilldown-multi') initDrilldownMultiPicker(root);
      else if (root.dataset.mode === 'multi') initMultiPicker(root);
      else initSinglePicker(root);
    });
    document.querySelectorAll('[data-contact-filter]').forEach(initContactFilter);
    document.querySelectorAll('[data-bubble-filter]').forEach(initBubbleFilter);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.BubblePicker = {
    unlock: function (root) {
      const slot = root.querySelector('[data-bubble-slot]');
      const bubble = slot && slot.querySelector('.bubble');
      if (bubble && root._bubbleApi) root._bubbleApi.unlock(bubble);
    },
  };
  window.ContactFilter = {
    clear: function (root) {
      if (root._contactFilterApi) root._contactFilterApi.clear();
    },
  };
})();
