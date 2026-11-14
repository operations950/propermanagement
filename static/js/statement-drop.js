/* Drop a bank statement anywhere on the page. It lands in the file box of the card marked data-statement-drop (any <details> around the box is opened),
   its name is shown in [data-statement-name] if the card has one, and the person presses the card's own button to read it. One script for every statement screen. */
(function () {
    var card = document.querySelector('[data-statement-drop]');
    if (!card) return;
    var input = card.querySelector('input[type=file]');
    if (!input) return;
    var style = document.createElement('style');
    style.textContent = '.stmt-drop-on { outline: 2px dashed var(--brand-primary, #2f6fed); outline-offset: -6px; background: color-mix(in srgb, var(--brand-primary, #2f6fed) 6%, white); }';
    document.head.appendChild(style);

    function hasFiles(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') !== -1; }
    function onOtherFileBox(e) { return e.target && e.target.closest && e.target.closest('input[type=file]') && e.target !== input; }

    ['dragenter', 'dragover'].forEach(function (name) {
        document.addEventListener(name, function (e) {
            if (!hasFiles(e) || onOtherFileBox(e)) return;
            e.preventDefault();
            card.classList.add('stmt-drop-on');
        });
    });
    document.addEventListener('dragleave', function (e) {
        if (e.target === document.documentElement || !e.relatedTarget) card.classList.remove('stmt-drop-on');
    });
    document.addEventListener('drop', function (e) {
        card.classList.remove('stmt-drop-on');
        if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length || onOtherFileBox(e)) return;
        e.preventDefault();
        for (var n = input.parentElement; n && n !== document.body; n = n.parentElement) {
            if (n.tagName === 'DETAILS') n.open = true;
        }
        input.files = e.dataTransfer.files;
        var name = card.querySelector('[data-statement-name]');
        if (name) name.textContent = input.files[0].name;
        input.dispatchEvent(new Event('change', {bubbles: true}));
        input.scrollIntoView({block: 'center'});
    });
})();
