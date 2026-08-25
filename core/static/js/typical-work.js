/*
 * «Подставить типовые работы».
 *
 * У типовой неисправности есть текст «что при этом делают». Кнопка
 * переносит его в поле работ — и на странице единицы (акт выполненных
 * работ), и в форме предложения (предлагаемые работы): текст один и тот
 * же, разница только в том, что предложение делают до ремонта.
 *
 * Подставляет **кнопка, а не программа сама**. Неисправности выбирают
 * при дефектации, до ремонта: собери программа акт автоматически,
 * он оказался бы готов по неразобранному прибору, и чек-лист показал бы
 * «работы записаны». То же правило, что у цены из прайса — предлагается,
 * а не подставляется: документ подписывает мастер.
 *
 * Как подключить: кнопке дать
 *   data-typical-work
 *   data-target="<id поля>"
 *   data-lines='["строка", "строка"]'
 * Рядом можно положить <span class="typical-work-note"> — туда уйдёт
 * ответ. Строки приходят в разметке: отдельный запрос ради подстановки —
 * ещё одна вещь, которая не придёт, когда со связью плохо.
 */
(function () {
    'use strict';

    /* Схлопнутые пробелы: та же строка, набранная с другим переносом, —
       это та же строка, и подставлять её второй раз незачем. */
    function flatten(text) {
        return String(text).replace(/\s+/g, ' ').trim();
    }

    function note(button, text) {
        var target = button.parentNode.querySelector('.typical-work-note');
        if (target) target.textContent = text;
    }

    document.addEventListener('click', function (event) {
        var button = event.target.closest('[data-typical-work]');
        if (!button) return;

        var field = document.getElementById(button.dataset.target);
        if (!field) return;

        var lines;
        try {
            lines = JSON.parse(button.dataset.lines || '[]');
        } catch (e) {
            lines = [];
        }

        if (!lines.length) {
            note(button, 'У выбранных неисправностей типовых работ не заведено.');
            return;
        }

        var current = field.value;
        var flat = flatten(current);
        var added = lines.filter(function (line) {
            return flat.indexOf(flatten(line)) === -1;
        });

        if (!added.length) {
            note(button, 'Все типовые работы уже вписаны.');
            return;
        }

        // Набранное руками не затирается — строки дописываются
        var prefix = current.trim() ? current.replace(/\s+$/, '') + '\n' : '';
        field.value = prefix + added.join('\n');
        field.focus();
        note(button, 'Подставлено строк: ' + added.length + '. Поправьте и сохраните.');
    });
})();
