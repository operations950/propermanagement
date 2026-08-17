// Local Weather box on the Owner Dashboard — zero-credential, entirely
// client-side. Uses the browser's own geolocation so the weather shown is
// wherever the visitor actually is, but asks for permission at most ONCE
// per browser: the result (device coordinates + reverse-geocoded place
// name, or a flag that it fell back to the office) is cached in
// localStorage, so every later page load reads the cache instead of
// calling navigator.geolocation.getCurrentPosition() again. That's the
// fix for "it always asks for location" — it used to call
// getCurrentPosition() unconditionally on every single page load; now it
// only ever does that once per browser, and after a denial/error it
// falls back to the office's fixed coordinates (data-office-lat/lon/name,
// see tickets/owner_dashboard.html) and remembers that too, so it won't
// keep retrying. A small "Update location" link lets the visitor re-ask
// at any time (e.g. after moving, or after fixing a prior denial).
//
// Calls two zero-credential public APIs directly from the browser, no
// server round-trip: Open-Meteo (api.open-meteo.com) for the forecast,
// and BigDataCloud's free reverse-geocode-client endpoint
// (api.bigdatacloud.net — no key required, built for exactly this kind
// of client-side lookup) to turn device coordinates into a "City, ST"
// label. The office fallback already has its name from the server
// (OFFICE_LOCATION_NAME), so it never needs reverse geocoding.
(function () {
    var CACHE_KEY = 'proptasks_weather_location_v1';

    // WMO weather codes (https://open-meteo.com/en/docs, "Weather variable
    // documentation") -> one of this app's existing Lucide icons, so the
    // widget matches the rest of the site's icon set instead of introducing
    // emoji or a new dependency.
    var WMO_ICONS = {
        0: 'sun', 1: 'sun', 2: 'cloud-sun', 3: 'cloud',
        45: 'cloud-fog', 48: 'cloud-fog',
        51: 'cloud-drizzle', 53: 'cloud-drizzle', 55: 'cloud-drizzle',
        56: 'cloud-drizzle', 57: 'cloud-drizzle',
        61: 'cloud-rain', 63: 'cloud-rain', 65: 'cloud-rain',
        66: 'cloud-rain', 67: 'cloud-rain',
        71: 'snowflake', 73: 'snowflake', 75: 'snowflake', 77: 'snowflake',
        80: 'cloud-rain', 81: 'cloud-rain', 82: 'cloud-rain',
        85: 'snowflake', 86: 'snowflake',
        95: 'cloud-lightning', 96: 'cloud-lightning', 99: 'cloud-lightning',
    };

    var WMO_LABELS = {
        0: 'Clear sky', 1: 'Mostly clear', 2: 'Partly cloudy', 3: 'Overcast',
        45: 'Fog', 48: 'Fog',
        51: 'Light drizzle', 53: 'Drizzle', 55: 'Heavy drizzle', 56: 'Freezing drizzle', 57: 'Freezing drizzle',
        61: 'Light rain', 63: 'Rain', 65: 'Heavy rain', 66: 'Freezing rain', 67: 'Freezing rain',
        71: 'Light snow', 73: 'Snow', 75: 'Heavy snow', 77: 'Snow grains',
        80: 'Rain showers', 81: 'Rain showers', 82: 'Heavy rain showers',
        85: 'Snow showers', 86: 'Snow showers',
        95: 'Thunderstorm', 96: 'Thunderstorm', 99: 'Thunderstorm',
    };

    function iconFor(code) {
        return WMO_ICONS[code] || 'cloud';
    }

    function labelFor(code) {
        return WMO_LABELS[code] || 'Unknown';
    }

    function readCache() {
        try {
            return JSON.parse(localStorage.getItem(CACHE_KEY));
        } catch (e) {
            return null;
        }
    }

    function writeCache(value) {
        try {
            localStorage.setItem(CACHE_KEY, JSON.stringify(value));
        } catch (e) {
            // Private browsing / storage disabled — just re-asks next load.
        }
    }

    function reverseGeocode(lat, lon, done) {
        var url = 'https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=' + lat
            + '&longitude=' + lon + '&localityLanguage=en';
        fetch(url)
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                var city = data.city || data.locality || '';
                var stateCode = data.principalSubdivisionCode
                    ? data.principalSubdivisionCode.split('-').pop()
                    : (data.principalSubdivision || '');
                done([city, stateCode].filter(Boolean).join(', ') || null);
            })
            .catch(function () { done(null); });
    }

    function render(widget, lat, lon, placeName, showUpdateLink) {
        var url = 'https://api.open-meteo.com/v1/forecast?latitude=' + lat + '&longitude=' + lon
            + '&current=temperature_2m,weather_code&daily=temperature_2m_max,temperature_2m_min,weather_code'
            + '&temperature_unit=fahrenheit&timezone=auto';

        fetch(url)
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                var current = data.current;
                var daily = data.daily;
                if (!current || !daily) throw new Error('Unexpected response shape');

                var currentIcon = iconFor(current.weather_code);
                var currentLabel = labelFor(current.weather_code);
                var hi = Math.round(daily.temperature_2m_max[0]);
                var lo = Math.round(daily.temperature_2m_min[0]);
                var placeLine = placeName
                    ? '<div class="small" style="color: var(--ink-muted);">' + placeName + '</div>'
                    : '';

                widget.innerHTML =
                    '<div class="d-flex align-items-center gap-3 mb-2">' +
                    '<i data-lucide="' + currentIcon + '" class="icon-lg" style="width: 2.5rem; height: 2.5rem; color: var(--brand-primary);"></i>' +
                    '<div>' +
                    placeLine +
                    '<div class="fw-bold" style="font-size: 1.5rem; color: var(--ink-primary);">' + Math.round(current.temperature_2m) + '°F</div>' +
                    '<div class="small" style="color: var(--ink-muted);">' + currentLabel + '</div>' +
                    '</div>' +
                    '</div>' +
                    '<p class="small mb-0" style="color: var(--ink-secondary);">Today: H ' + hi + '° / L ' + lo + '°</p>' +
                    (showUpdateLink
                        ? '<button type="button" data-weather-update class="btn btn-link btn-sm p-0 mt-1" style="font-size: 0.75rem;">Not your location? Update</button>'
                        : '');

                if (window.lucide) lucide.createIcons();

                var updateLink = widget.querySelector('[data-weather-update]');
                if (updateLink) {
                    updateLink.addEventListener('click', function () {
                        writeCache(null);
                        locate(widget, true);
                    });
                }
            })
            .catch(function () {
                var status = widget.querySelector('[data-weather-status]');
                if (status) status.textContent = 'Weather unavailable right now.';
            });
    }

    function locate(widget, forcePrompt) {
        var officeLat = widget.dataset.officeLat;
        var officeLon = widget.dataset.officeLon;
        var officeName = widget.dataset.officeName;

        function useOffice() {
            writeCache({ source: 'office' });
            render(widget, officeLat, officeLon, officeName, !!navigator.geolocation);
        }

        if (!forcePrompt) {
            var cached = readCache();
            if (cached && cached.source === 'device' && cached.lat && cached.lon) {
                render(widget, cached.lat, cached.lon, cached.name, true);
                return;
            }
            if (cached && cached.source === 'office') {
                render(widget, officeLat, officeLon, officeName, !!navigator.geolocation);
                return;
            }
        }

        if (!navigator.geolocation) {
            useOffice();
            return;
        }

        navigator.geolocation.getCurrentPosition(
            function (position) {
                var lat = position.coords.latitude.toFixed(4);
                var lon = position.coords.longitude.toFixed(4);
                reverseGeocode(lat, lon, function (name) {
                    writeCache({ source: 'device', lat: lat, lon: lon, name: name });
                    render(widget, lat, lon, name, true);
                });
            },
            function () { useOffice(); },
            { timeout: 8000, maximumAge: 15 * 60 * 1000 },
        );
    }

    document.querySelectorAll('[data-weather-widget]').forEach(function (widget) {
        locate(widget, false);
    });
})();
