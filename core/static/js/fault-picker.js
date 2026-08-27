/*
 * Выбор типовых неисправностей: выпадающий список и кнопка «Добавить».
 *
 * До этого стоял список с множественным выбором — тот, где надо держать
 * Ctrl. На планшете он почти неработоспособен, а при десятке
 * неисправностей не видно, что вообще выбрано.
 *
 * Наружу уходят скрытые поля с номерами под прежним именем, поэтому
 * принимающие представления и формы не менялись — тот же приём, что
 * у выбора детали.
 *
 * Уже выбранные рисует сервер: страница показывает выбранное и до того,
 * как скрипт отработал. Скрипт убирает их из выпадающего списка —
 * второй раз ту же неисправность не добавить.
 *
 * Никакого Bootstrap JS: обработчики висят на документе, разметка своя.
 */
(function () {
    'use strict';

    function badgeClass(complexity) {
        // Сложность красится одинаково всюду: сложный — красный,
        // остальное — зелёный. Значений всего два, «среднего» нет
        return complexity === 'complex' ? 'bg-danger' : 'bg-success';
    }

    function chosenIds(picker) {
        return Array.prototype.map.call(
            picker.querySelectorAll('.fault-picker-row'),
            function (row) { return row.dataset.id; }
        );
    }

    function refresh(picker) {
        var select = picker.querySelector('.fault-picker-select');
        var taken = chosenIds(picker);
        Array.prototype.forEach.call(select.options, function (option) {
            if (!option.value) return;
            // Прячем, а не удаляем: убрали строку — вариант возвращается
            // на своё место в списке, а не в конец
            option.hidden = taken.indexOf(option.value) >= 0;
        });
        select.value = '';

        var empty = picker.querySelector('.fault-picker-empty');
        if (empty) empty.hidden = taken.length > 0;
    }

    function add(picker) {
        var select = picker.querySelector('.fault-picker-select');
        var option = select.options[select.selectedIndex];
        if (!option || !option.value) return;

        var template = picker.querySelector('.fault-picker-template');
        var row = template.content.firstElementChild.cloneNode(true);
        row.dataset.id = option.value;
        row.querySelector('input').value = option.value;
        row.querySelector('.badge').textContent = option.dataset.complexityLabel || '';
        row.querySelector('.badge').classList.add(badgeClass(option.dataset.complexity));
        row.querySelector('.flex-grow-1').textContent = option.textContent.trim();

        picker.querySelector('.fault-picker-chosen').appendChild(row);
        refresh(picker);
    }

    document.addEventListener('click', function (event) {
        var addButton = event.target.closest('.fault-picker-add');
        if (addButton) {
            add(addButton.closest('.fault-picker'));
            return;
        }
        var removeButton = event.target.closest('.fault-picker-remove');
        if (removeButton) {
            var picker = removeButton.closest('.fault-picker');
            removeButton.closest('.fault-picker-row').remove();
            refresh(picker);
        }
    });

    // Выбрали из списка и нажали Enter — то же, что нажать «Добавить»,
    // а не отправить форму: отправка тут почти всегда преждевременна
    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter') return;
        var select = event.target.closest('.fault-picker-select');
        if (!select) return;
        event.preventDefault();
        add(select.closest('.fault-picker'));
    });

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.fault-picker').forEach(refresh);
    });
})();
