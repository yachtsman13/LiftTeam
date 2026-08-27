/*
 * Многострочные поля ростом по содержимому.
 *
 * Раньше каждое поле объявляло себе высоту в разметке — две строки,
 * три. В итоге под однострочный ответ («Промыт корпус») отводилось три
 * строки пустоты, а под настоящий абзац всё равно не хватало, и его
 * читали через прокрутку в четыре строки высотой.
 *
 * Теперь высота равна содержимому: пусто — одна строка, набрали абзац —
 * поле выросло. Выше MAX_ROWS не растёт: страница не должна уезжать
 * из-за одного поля, дальше прокрутка внутри него.
 *
 * Ни одну форму править не надо: скрипт находит поля сам и работает
 * на любой странице. Поля, добавленные позже (строки рецепта копируются
 * из готовой строки), подхватываются при первом же наборе.
 */
(function () {
    'use strict';

    var MAX_ROWS = 12;

    function lineHeight(field) {
        var computed = window.getComputedStyle(field);
        var value = parseFloat(computed.lineHeight);
        // normal — браузер не назвал число; берём кегль с обычным множителем
        if (!value) value = parseFloat(computed.fontSize) * 1.4;
        return value || 20;
    }

    function grow(field) {
        // Скрытое поле не измерить: у него нулевая высота содержимого,
        // и попытка подогнать высоту схлопнула бы его в ничто
        if (!field.offsetParent && field.offsetHeight === 0) return;

        var computed = window.getComputedStyle(field);
        var extra = parseFloat(computed.borderTopWidth) +
                    parseFloat(computed.borderBottomWidth);
        // Ноль, а не auto: при height:auto текстовое поле принимает
        // высоту из атрибута rows, и scrollHeight никогда не окажется
        // меньше него — поле с rows=3 так и осталось бы в три строки
        // при одной строке текста. От нуля scrollHeight равен ровно
        // содержимому.
        field.style.overflowY = 'hidden';
        field.style.height = '0px';

        var line = lineHeight(field);
        var max = line * MAX_ROWS;
        var wanted = Math.max(field.scrollHeight, line) + extra;
        if (wanted > max) {
            wanted = max;
            field.style.overflowY = 'auto';
        }
        field.style.height = wanted + 'px';
    }

    function growAll(root) {
        (root || document).querySelectorAll('textarea').forEach(grow);
    }

    document.addEventListener('input', function (event) {
        if (event.target.tagName === 'TEXTAREA') grow(event.target);
    });

    // Поле могло появиться уже с текстом — например, скопированной
    // строкой рецепта или подстановкой типовых работ
    document.addEventListener('focusin', function (event) {
        if (event.target.tagName === 'TEXTAREA') grow(event.target);
    });

    document.addEventListener('DOMContentLoaded', function () { growAll(); });

    // Ширина изменилась — тот же текст занимает другое число строк
    window.addEventListener('resize', function () { growAll(); });

    window.LiftTeamAutogrow = {grow: grow, growAll: growAll};
})();
