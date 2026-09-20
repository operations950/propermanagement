/* Small dependency-free charts (bar, stacked bar, line) drawn as SVG.

   <div class="mini-chart" data-src="some-json-script-id"></div>
   {{ config|json_script:"some-json-script-id" }}

   config: {type: 'bar'|'stacked'|'line', format: 'pct'|'money'|'int'|'dec1',
            max: optional fixed top of the scale,
            labels: ['Oct', ...], full: ['October 2025', ...] (tooltip titles),
            series: [{name, color, values: [number|null, ...]}]}

   Drawn at the container's real pixel width (so text stays readable on a
   phone) and redrawn when it changes. Hover or touch a column for its numbers;
   the page also lists the same numbers in a table. */
(function () {
    var NS = 'http://www.w3.org/2000/svg';

    function fmt(kind, v, compact) {
        if (v === null || v === undefined) { return '—'; }
        if (kind === 'pct') { return Math.round(v) + '%'; }
        if (kind === 'money') {
            if (compact && Math.abs(v) >= 1000) { return '$' + (v / 1000).toFixed(v >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k'; }
            return '$' + Math.round(v).toLocaleString('en-US');
        }
        if (kind === 'dec1') { return v.toFixed(1); }
        return String(Math.round(v));
    }

    // Axis with four even, round steps: [0, step, 2*step, 3*step, 4*step].
    function niceStep(v) {
        if (v <= 0) { return 1; }
        var exp = Math.pow(10, Math.floor(Math.log10(v)));
        var f = v / exp;
        return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * exp;
    }

    function el(name, attrs, parent) {
        var node = document.createElementNS(NS, name);
        for (var k in attrs) { node.setAttribute(k, attrs[k]); }
        if (parent) { parent.appendChild(node); }
        return node;
    }

    function draw(host) {
        var cfg = JSON.parse(document.getElementById(host.dataset.src).textContent);
        var series = cfg.series, n = cfg.labels.length;
        var W = Math.max(host.clientWidth, 240), H = 210;
        host.innerHTML = '';
        host.style.position = 'relative';

        if (series.length > 1) {
            var legend = document.createElement('div');
            legend.className = 'mini-chart-legend';
            series.forEach(function (s) {
                var item = document.createElement('span');
                item.innerHTML = '<i style="background:' + s.color + '"></i>' + s.name;
                legend.appendChild(item);
            });
            host.appendChild(legend);
        }

        var totals = cfg.labels.map(function (_, i) {
            return series.reduce(function (sum, s) { return sum + (s.values[i] || 0); }, 0);
        });
        var peak = cfg.type === 'stacked' ? Math.max.apply(null, totals) : Math.max.apply(null, series.reduce(function (all, s) {
            return all.concat(s.values.filter(function (v) { return v !== null; }));
        }, [0]));
        var top = cfg.max || niceStep(peak * 1.05 / 4) * 4;
        var ticks = [0, 1, 2, 3, 4].map(function (i) { return top * i / 4; });
        var left = Math.max.apply(null, ticks.map(function (t) { return fmt(cfg.format, t, true).length; })) * 6.4 + 12;
        var m = {l: left, r: 8, t: 10, b: 24};
        var pw = W - m.l - m.r, ph = H - m.t - m.b, slot = pw / n;
        function y(v) { return m.t + ph - (v / top) * ph; }
        function xc(i) { return m.l + slot * (i + 0.5); }

        var svg = el('svg', {width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': (series[0] && series[0].name) || 'chart'}, host);
        svg.style.display = 'block';
        ticks.forEach(function (t) {
            el('line', {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: 'rgba(43,43,46,0.10)', 'stroke-width': 1}, svg);
            var label = el('text', {x: m.l - 6, y: y(t) + 4, 'text-anchor': 'end', 'font-size': 11, fill: '#6b6b6b'}, svg);
            label.textContent = fmt(cfg.format, t, true);
        });
        var every = slot < 26 ? 2 : 1;
        cfg.labels.forEach(function (l, i) {
            if (i % every && i !== n - 1) { return; }
            var t = el('text', {x: xc(i), y: H - 7, 'text-anchor': 'middle', 'font-size': 11, fill: '#6b6b6b'}, svg);
            t.textContent = l;
        });

        var hi = el('rect', {y: m.t, height: ph, width: slot, fill: 'rgba(61,97,120,0.08)', visibility: 'hidden'}, svg);

        if (cfg.type === 'line') {
            series.forEach(function (s) {
                var path = '', pen = false;
                s.values.forEach(function (v, i) {
                    if (v === null) { pen = false; return; }
                    path += (pen ? 'L' : 'M') + xc(i).toFixed(1) + ' ' + y(v).toFixed(1) + ' ';
                    pen = true;
                });
                el('path', {d: path, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}, svg);
                s.values.forEach(function (v, i) {
                    if (v !== null) { el('circle', {cx: xc(i), cy: y(v), r: 4, fill: s.color, stroke: '#fff', 'stroke-width': 2}, svg); }
                });
            });
        } else {
            var bw = Math.min(slot * 0.62, 34);
            cfg.labels.forEach(function (_, i) {
                var base = 0;
                series.forEach(function (s, si) {
                    var v = s.values[i];
                    if (v === null || v === undefined) {
                        if (si === 0) { el('line', {x1: xc(i) - 5, x2: xc(i) + 5, y1: y(0) - 1, y2: y(0) - 1, stroke: '#c9c7c2', 'stroke-width': 2}, svg); }
                        return;
                    }
                    if (v <= 0) { return; }
                    var y1 = y(base + v), y0 = y(base) - (cfg.type === 'stacked' && base > 0 ? 2 : 0);
                    var h = Math.max(y0 - y1, 1), r = Math.min(4, h, bw / 2);
                    var last = cfg.type !== 'stacked' || si === series.length - 1 || series.slice(si + 1).every(function (o) { return !o.values[i]; });
                    var x = xc(i) - bw / 2, d;
                    if (last) {
                        d = 'M' + x + ' ' + (y1 + h) + 'V' + (y1 + r) + 'Q' + x + ' ' + y1 + ' ' + (x + r) + ' ' + y1 + 'H' + (x + bw - r) +
                            'Q' + (x + bw) + ' ' + y1 + ' ' + (x + bw) + ' ' + (y1 + r) + 'V' + (y1 + h) + 'Z';
                    } else {
                        d = 'M' + x + ' ' + y1 + 'H' + (x + bw) + 'V' + (y1 + h) + 'H' + x + 'Z';
                    }
                    el('path', {d: d, fill: s.color}, svg);
                    base += v;
                });
            });
        }

        var tip = document.createElement('div');
        tip.className = 'mini-chart-tip';
        tip.style.display = 'none';
        host.appendChild(tip);
        function show(evt) {
            var box = svg.getBoundingClientRect();
            var point = evt.touches ? evt.touches[0] : evt;
            var i = Math.floor((point.clientX - box.left - m.l) / slot);
            if (i < 0 || i >= n) { hide(); return; }
            hi.setAttribute('x', m.l + slot * i);
            hi.setAttribute('visibility', 'visible');
            var rows = series.map(function (s) {
                return '<div><i style="background:' + s.color + '"></i>' + s.name + ': <strong>' + fmt(cfg.format, s.values[i]) + '</strong></div>';
            }).join('');
            if (cfg.type === 'stacked') { rows += '<div class="mini-chart-total">Total: <strong>' + fmt(cfg.format, totals[i]) + '</strong></div>'; }
            tip.innerHTML = '<div class="mini-chart-tip-title">' + cfg.full[i] + '</div>' + rows;
            tip.style.display = 'block';
            var tw = tip.offsetWidth, cx = xc(i) - tw / 2;
            tip.style.left = Math.max(0, Math.min(W - tw, cx)) + 'px';
            tip.style.top = (series.length > 1 ? 30 : 0) + 'px';
        }
        function hide() { hi.setAttribute('visibility', 'hidden'); tip.style.display = 'none'; }
        svg.addEventListener('mousemove', show);
        svg.addEventListener('mouseleave', hide);
        svg.addEventListener('touchstart', show, {passive: true});
        svg.addEventListener('touchmove', show, {passive: true});
        host._miniWidth = W;
    }

    function drawAll() {
        document.querySelectorAll('.mini-chart').forEach(function (host) {
            if (host.clientWidth !== host._miniWidth) { draw(host); }
        });
    }
    document.addEventListener('DOMContentLoaded', drawAll);
    window.addEventListener('resize', drawAll);
    window.addEventListener('load', drawAll);
})();
