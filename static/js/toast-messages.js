// Wires up the server-rendered Django-messages toast bubbles (see
// base.html's .toast-stack block): each auto-dismisses after a few
// seconds, pauses its timer while hovered/focused so a message being read
// doesn't vanish out from under someone, and can always be dismissed early
// with its close (X) button. Fully self-contained — a page with no
// messages this request just has no #toastStack element and this file is
// a no-op.
(function () {
    // Errors/warnings get more time on screen than a routine success/info
    // confirmation — worth reading, not just worth flashing.
    var AUTO_DISMISS_MS = {
        success: 5000,
        info: 5000,
        debug: 5000,
        warning: 7000,
        error: 9000,
    };

    function dismiss(toast) {
        if (toast._dismissed) return;
        toast._dismissed = true;
        clearTimeout(toast._timer);
        toast.classList.add('toast-hiding');
        toast.addEventListener('animationend', function () {
            toast.remove();
        }, { once: true });
    }

    function armTimer(toast) {
        var ms = AUTO_DISMISS_MS[toast.dataset.tag] || AUTO_DISMISS_MS.info;
        toast._timer = setTimeout(function () { dismiss(toast); }, ms);
    }

    function init() {
        var stack = document.getElementById('toastStack');
        if (!stack) return;
        stack.querySelectorAll('.toast-bubble').forEach(function (toast) {
            armTimer(toast);
            toast.addEventListener('mouseenter', function () { clearTimeout(toast._timer); });
            toast.addEventListener('mouseleave', function () { if (!toast._dismissed) armTimer(toast); });
            toast.addEventListener('focusin', function () { clearTimeout(toast._timer); });
            toast.addEventListener('focusout', function () { if (!toast._dismissed) armTimer(toast); });
            var closeBtn = toast.querySelector('[data-toast-close]');
            if (closeBtn) closeBtn.addEventListener('click', function () { dismiss(toast); });
        });
    }

    document.addEventListener('DOMContentLoaded', init);
})();
