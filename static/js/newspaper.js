/* The little script the newspaper screens share: the Filters button opens the filter box, the search box fills the boxes live (answering with JSON), the Sort choice reloads. */
(function () {
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-np-filters-toggle]');
        if (!btn) return;
        var box = document.getElementById(btn.dataset.npFiltersToggle);
        if (!box) return;
        box.classList.toggle('open');
        btn.setAttribute('aria-expanded', box.classList.contains('open') ? 'true' : 'false');
    });

    document.querySelectorAll('[data-np-sort]').forEach(function (select) {
        select.addEventListener('change', function () {
            var params = new URLSearchParams(window.location.search);
            if (select.value) params.set('sort', select.value); else params.delete('sort');
            params.delete('page');
            window.location.search = params.toString();
        });
    });

    document.querySelectorAll('[data-np-search]').forEach(function (input) {
        var map = {};
        try { map = JSON.parse(input.dataset.npMap || '{}'); } catch (err) { map = {}; }
        var timer = null;
        function run() {
            var params = new URLSearchParams(window.location.search);
            if (input.value) params.set('q', input.value); else params.delete('q');
            params.delete('page');
            fetch(window.location.pathname + '?' + params.toString(), { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
                .then(function (resp) { if (!resp.ok) throw new Error('bad response'); return resp.json(); })
                .then(function (data) {
                    Object.keys(map).forEach(function (key) {
                        var el = document.getElementById(map[key]);
                        if (el && data[key] !== undefined) el.innerHTML = data[key];
                    });
                    if (window.lucide) lucide.createIcons();
                    history.replaceState(null, '', '?' + params.toString());
                })
                .catch(function () { console.error('Search failed to load.'); });
        }
        input.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(run, 250); });
        var form = input.closest('form');
        if (form) form.addEventListener('submit', function (e) { if (window.fetch) { e.preventDefault(); clearTimeout(timer); run(); } });
    });
})();
