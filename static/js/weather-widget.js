// Local Weather box on the Owner Dashboard — zero-credential, entirely
// client-side. Always renders the office's fixed coordinates/name
// (data-office-lat/lon/name, see tickets/owner_dashboard.html) — it used
// to ask the browser for the visitor's own location first via
// navigator.geolocation.getCurrentPosition(), which is what triggered a
// location-permission prompt on every single page load on mobile.
// Nothing on this dashboard actually varies by visitor location, so that
// prompt was pure friction; dropped in favor of just using the office
// directly. Calls Open-Meteo's public forecast API directly
// (api.open-meteo.com, no API key required) rather than round-tripping
// through our own server.
(function () {
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

    function render(widget, lat, lon, placeName) {
        var status = widget.querySelector('[data-weather-status]');
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
                    '<p class="small mb-0" style="color: var(--ink-secondary);">Today: H ' + hi + '° / L ' + lo + '°</p>';

                if (window.lucide) lucide.createIcons();
            })
            .catch(function () {
                if (status) status.textContent = 'Weather unavailable right now.';
            });
    }

    document.querySelectorAll('[data-weather-widget]').forEach(function (widget) {
        render(widget, widget.dataset.officeLat, widget.dataset.officeLon, widget.dataset.officeName);
    });
})();
