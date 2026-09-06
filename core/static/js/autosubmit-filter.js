/*
 * Текстовое поле фильтра отправляет форму само, без кнопки — с v2.116.0.
 *
 * Разметке нужен только `<input data-autosubmit>` внутри `<form method="get">`
 * — своего скрипта заводить не надо, как и у сортировки по шапке выше.
 * Задержка (пока набирают) не даёт отправлять запрос на каждую нажатую
 * клавишу: без неё поиск по имени плательщика слал бы страницу за каждой
 * буквой и обгонял бы сам себя. `page` в новый запрос не попадает сама —
 * поле формы GET отправляет только то, что в ней есть, а `page` там нет.
 *
 * Курсор — с v2.118.0. Отправка перезагружает страницу, а перезагрузка
 * снимает фокус с поля вместе с местом, где остановился человек: не
 * заметив этого, он продолжает печатать вникуда и обнаруживает пропажу
 * через несколько букв. Курсор запоминается в sessionStorage **перед**
 * отправкой (обычная переменная JS до конца перезагрузки не доживает)
 * и возвращается на той же странице после её загрузки — по имени поля,
 * а не по месту в разметке, потому что до и после перезагрузки это два
 * разных DOM-узла. Путь страницы в записи — чтобы случайный переход
 * на другую страницу с одноимённым полем не подхватил чужой курсор.
 */
(function () {
    'use strict';

    var DELAY_MS = 500;
    var STORAGE_KEY = 'liftteam:autosubmit-cursor';

    function rememberCursor(input) {
        var hasSelection = typeof input.selectionStart === 'number';
        try {
            sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
                path: window.location.pathname,
                name: input.name,
                start: hasSelection ? input.selectionStart : null,
                end: hasSelection ? input.selectionEnd : null
            }));
        } catch (e) {
            // Приватный режим браузера может запретить sessionStorage —
            // тогда курсор просто не восстановится, страница всё равно рабочая
        }
    }

    function restoreCursor() {
        var raw;
        try {
            raw = sessionStorage.getItem(STORAGE_KEY);
        } catch (e) {
            return;
        }
        if (!raw) return;
        sessionStorage.removeItem(STORAGE_KEY);

        var saved;
        try {
            saved = JSON.parse(raw);
        } catch (e) {
            return;
        }
        if (!saved || saved.path !== window.location.pathname || !saved.name) return;

        var input = document.querySelector('[data-autosubmit][name="' + CSS.escape(saved.name) + '"]');
        if (!input) return;
        input.focus();
        if (saved.start != null && typeof input.setSelectionRange === 'function') {
            try {
                input.setSelectionRange(saved.start, saved.end);
            } catch (e) {
                // У некоторых типов полей (number, date, ...) выделения не бывает
            }
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        restoreCursor();
        document.querySelectorAll('input[data-autosubmit]').forEach(function (input) {
            var timer = null;
            input.addEventListener('input', function () {
                clearTimeout(timer);
                timer = setTimeout(function () {
                    var form = input.form;
                    if (!form) return;
                    rememberCursor(input);
                    form.requestSubmit ? form.requestSubmit() : form.submit();
                }, DELAY_MS);
            });
        });
    });
})();
