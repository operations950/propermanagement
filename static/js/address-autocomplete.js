/*
 * Property address picker: debounced live suggestions from Google Places
 * (via a small server-side proxy — see core/places.py, core/views.py's
 * property_address_autocomplete/property_address_lookup), with a manual
 * "can't find it" override that reveals the real street/city/state/zip
 * inputs directly.
 *
 * This is the app's first fetch()-based script — everything else in this
 * app (including bubble-picker.js) is zero-AJAX, full POST/redirect, by
 * deliberate convention. Live type-ahead can't work that way, so this one
 * file is the isolated exception; it stays out of bubble-picker.js on
 * purpose. A safe no-op on any page without [data-address-autocomplete].
 */
(function () {
  'use strict';

  const DEBOUNCE_MS = 300;

  function initAddressAutocomplete(root) {
    const searchInput = root.querySelector('[data-address-search]');
    const suggestionsEl = root.querySelector('[data-address-suggestions]');
    const manualToggle = root.querySelector('[data-address-manual-toggle]');
    const manualFields = root.querySelector('[data-address-manual-fields]');
    const streetInput = root.querySelector('input[name="street"]');
    const cityInput = root.querySelector('input[name="city"]');
    const stateInput = root.querySelector('input[name="state"]');
    const zipInput = root.querySelector('input[name="zip_code"]');
    const autocompleteUrl = root.dataset.autocompleteUrl;
    const lookupUrlTemplate = root.dataset.lookupUrl;

    if (manualToggle && manualFields) {
      manualToggle.addEventListener('click', function () {
        manualFields.hidden = !manualFields.hidden;
      });
    }

    if (!searchInput || !suggestionsEl || !autocompleteUrl) return; // Places not configured — manual fields are the whole UI

    let debounceTimer = null;

    function renderSuggestions(suggestions) {
      suggestionsEl.innerHTML = '';
      if (!suggestions.length) {
        suggestionsEl.hidden = true;
        return;
      }
      suggestions.forEach(function (s) {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'contact-filter-item';
        item.textContent = s.text;
        item.addEventListener('click', function () {
          selectSuggestion(s);
        });
        suggestionsEl.appendChild(item);
      });
      suggestionsEl.hidden = false;
    }

    function selectSuggestion(suggestion) {
      searchInput.value = suggestion.text;
      suggestionsEl.hidden = true;
      const url = lookupUrlTemplate.replace('PLACE_ID', encodeURIComponent(suggestion.place_id));
      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (streetInput) streetInput.value = data.street || '';
          if (cityInput) cityInput.value = data.city || '';
          if (stateInput) stateInput.value = data.state || '';
          if (zipInput) zipInput.value = data.zip_code || '';
        });
    }

    searchInput.addEventListener('input', function () {
      const query = searchInput.value.trim();
      clearTimeout(debounceTimer);
      if (!query) {
        suggestionsEl.hidden = true;
        return;
      }
      debounceTimer = setTimeout(function () {
        fetch(autocompleteUrl + '?q=' + encodeURIComponent(query))
          .then(function (r) { return r.json(); })
          .then(function (data) { renderSuggestions(data.suggestions || []); });
      }, DEBOUNCE_MS);
    });

    document.addEventListener('click', function (e) {
      if (!root.contains(e.target)) suggestionsEl.hidden = true;
    });
  }

  function init() {
    document.querySelectorAll('[data-address-autocomplete]').forEach(initAddressAutocomplete);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
