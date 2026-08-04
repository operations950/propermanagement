// Small, self-contained canvas signature capture for the DIGITAL_SIGNATURE
// process step type — draws on a <canvas data-signature-pad>, uploads the
// result as a PNG via the same upload endpoint every other step-type
// attachment uses (process_run_step_upload), then reloads so the step
// picks up its new attachment/completed state.
(function () {
    function initPad(root) {
        var canvas = root.querySelector('[data-signature-canvas]');
        var clearBtn = root.querySelector('[data-signature-clear]');
        var saveBtn = root.querySelector('[data-signature-save]');
        if (!canvas) return;

        var ctx = canvas.getContext('2d');
        ctx.lineWidth = 2;
        ctx.lineCap = 'round';
        ctx.strokeStyle = '#1a1a1a';
        var drawing = false;
        var hasDrawn = false;

        function pos(e) {
            var rect = canvas.getBoundingClientRect();
            var point = e.touches ? e.touches[0] : e;
            return { x: point.clientX - rect.left, y: point.clientY - rect.top };
        }

        function start(e) {
            e.preventDefault();
            drawing = true;
            hasDrawn = true;
            var p = pos(e);
            ctx.beginPath();
            ctx.moveTo(p.x, p.y);
        }

        function move(e) {
            if (!drawing) return;
            e.preventDefault();
            var p = pos(e);
            ctx.lineTo(p.x, p.y);
            ctx.stroke();
        }

        function end() {
            drawing = false;
        }

        canvas.addEventListener('mousedown', start);
        canvas.addEventListener('mousemove', move);
        canvas.addEventListener('mouseup', end);
        canvas.addEventListener('mouseleave', end);
        canvas.addEventListener('touchstart', start, { passive: false });
        canvas.addEventListener('touchmove', move, { passive: false });
        canvas.addEventListener('touchend', end);

        if (clearBtn) {
            clearBtn.addEventListener('click', function () {
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                hasDrawn = false;
            });
        }

        if (saveBtn) {
            saveBtn.addEventListener('click', function () {
                if (!hasDrawn) {
                    alert('Draw a signature first.');
                    return;
                }
                canvas.toBlob(function (blob) {
                    var fd = new FormData();
                    fd.append('csrfmiddlewaretoken', root.dataset.csrf);
                    fd.append('file', blob, 'signature.png');
                    fd.append('caption', 'Signature');
                    // Extra fields a specific caller wants sent alongside the
                    // signature image (e.g. a typed name) — opt-in via
                    // data-signature-extra so this stays a no-op for callers
                    // that don't have any (the processes app's original use).
                    root.querySelectorAll('[data-signature-extra]').forEach(function (el) {
                        fd.append(el.name, el.value);
                    });
                    saveBtn.disabled = true;
                    fetch(root.dataset.uploadUrl, { method: 'POST', body: fd, credentials: 'same-origin' })
                        .then(function () { window.location.reload(); });
                }, 'image/png');
            });
        }
    }

    document.querySelectorAll('[data-signature-pad]').forEach(initPad);
})();
