/*
 * Текстовое поле фильтра отправляет форму само, без кнопки — с v2.116.0.
 *
 * Разметке нужен только `<input data-autosubmit>` внутри `<form method="get">`
 * — своего скрипта заводить не надо, как и у сортировки по шапке выше.
 * Задержка (пока набирают) не даёт отправлять запрос на каждую нажатую
 * клавишу: без неё поиск по имени плательщика слал бы страницу за каждой
 * буквой и обгонял бы сам себя. `page` в новый запрос не попадает сама —
 * поле формы GET отправляет только то, что в ней есть, а `page` там нет.
 */
(function () {
    'use strict';

    var DELAY_MS = 500;

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('input[data-autosubmit]').forEach(function (input) {
            var timer = null;
            input.addEventListener('input', function () {
                clearTimeout(timer);
                timer = setTimeout(function () {
                    var form = input.form;
                    if (!form) return;
                    form.requestSubmit ? form.requestSubmit() : form.submit();
                }, DELAY_MS);
            });
        });
    });
})();
