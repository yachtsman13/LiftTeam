/**
 * Подгонка шрифта на этикетках.
 *
 * Этикетка 43x25 мм — жёсткий размер, а содержимое произвольное: артикул
 * бывает и пятизначным, и с суффиксом на пол-строки, описание пишут кто как.
 * Раньше лишнее просто обрезалось многоточием, то есть с наклейки пропадала
 * та самая часть, ради которой её и читают.
 *
 * Здесь шрифт внутри блока уменьшается, пока содержимое не поместится, но
 * не мельче нижней границы: нечитаемая этикетка не лучше обрезанной. Граница
 * задаётся в разметке атрибутом data-fit долей от исходного размера, а сами
 * размеры в CSS умножены на переменную --fit.
 *
 * Что не влезло даже при минимальном шрифте, по-прежнему обрезается — но это
 * уже край, а не обычный случай.
 */
(function () {
    'use strict';

    // Шаг подбора. Мельче — дольше считается на пачке в 200 этикеток,
    // крупнее — заметен скачок размера между соседними наклейками
    var STEP = 0.05;
    // Запас в один пиксель: браузер округляет размеры, и без него блок
    // считался бы переполненным на ровном месте
    var TOLERANCE = 1;

    /* Переполнение считается по настоящим прямоугольникам строк, а не по
       scrollWidth/scrollHeight. Те округлены до целых пикселей, и при
       масштабе экрана, отличном от единицы, блок с вылезающим текстом
       отчитывался, что всё поместилось: шрифт не уменьшался, а лишнее
       молча уезжало под нижнюю строку этикетки. Прямоугольники дробные,
       и такой промах на них невозможен.

       scrollWidth/scrollHeight оставлены запасной проверкой: у строки,
       переполненной изнутри (длинное слово без переносов), собственный
       прямоугольник равен родительскому, и заметен только этот способ. */
    function overflows(element) {
        var box = element.getBoundingClientRect();
        var children = element.children;
        for (var i = 0; i < children.length; i++) {
            var line = children[i].getBoundingClientRect();
            if (line.bottom > box.bottom + TOLERANCE || line.right > box.right + TOLERANCE) {
                return true;
            }
        }
        return element.scrollWidth > element.clientWidth + TOLERANCE ||
               element.scrollHeight > element.clientHeight + TOLERANCE;
    }

    function fit(element) {
        var min = parseFloat(element.dataset.fit);
        if (!min || min <= 0 || min > 1) {
            min = 0.7;
        }

        var scale = 1;
        element.style.setProperty('--fit', scale);
        resetClamp(element);
        while (scale - STEP >= min && overflows(element)) {
            scale = Math.round((scale - STEP) * 100) / 100;
            element.style.setProperty('--fit', scale);
        }
        clampToFit(element);
    }

    /**
     * Последняя мера, когда и минимальный шрифт не помог: обрезать текст
     * так, чтобы кончался он целой строкой с многоточием, а не полоской
     * от следующей. Обрезается **хвост**: начало фразы важнее конца,
     * а на этикетке текст прижат к нижнему краю и при переполнении
     * вылезает вверх — без этой обрезки пропадало бы как раз начало.
     *
     * Число строк считается по настоящим прямоугольникам строк, а не делением
     * высоты на межстрочный интервал: интервал больше самих букв, и деление
     * промахивается — из-под многоточия выглядывают верхушки следующей строки.
     * Высота задаётся по нижнему краю последней целой строки, поэтому лишнему
     * взяться неоткуда.
     */
    function resetClamp(element) {
        // Замер должен идти по полному тексту, иначе подгонка шрифта увидит
        // уже урезанный вариант и решит, что всё поместилось
        var target = element.querySelector('[data-fit-clamp]');
        if (target) {
            target.style.webkitLineClamp = '';
            target.style.maxHeight = '';
            target.style.overflow = '';
            target.style.display = '';
        }
    }


    function clampToFit(element) {
        var target = element.querySelector('[data-fit-clamp]');
        if (!target) {
            return;
        }

        if (!overflows(element)) {
            return;
        }

        var range = document.createRange();
        range.selectNodeContents(target);
        var rects = range.getClientRects();
        if (!rects.length) {
            return;
        }

        /* Считаем по высоте, а не по нижней границе блока. Текст прижат
           к нижнему краю, и при переполнении он вылезает ВВЕРХ: нижние
           строки остаются внутри блока, и проверка «строка ниже дна»
           не срабатывала бы ни на одной — обрезка не наступала вовсе,
           а лишнее молча уезжало под верхнюю кромку. То есть у длинного
           описания пропадало начало фразы, а не хвост. */
        var room = element.getBoundingClientRect().height;
        var start = rects[0].top;

        var lines = 0;
        var bottom = 0;
        for (var i = 0; i < rects.length; i++) {
            if (rects[i].bottom - start > room + TOLERANCE) {
                break;
            }
            lines += 1;
            bottom = rects[i].bottom;
        }

        if (lines === 0) {
            // Даже одна строка не помещается — прячем текст целиком:
            // половина строки поперёк этикетки выглядит как брак печати
            target.style.display = 'none';
            return;
        }

        target.style.maxHeight = (bottom - start) + 'px';
        target.style.overflow = 'hidden';
        target.style.webkitLineClamp = String(lines);
    }

    function fitAll() {
        var blocks = document.querySelectorAll('[data-fit]');
        for (var i = 0; i < blocks.length; i++) {
            fit(blocks[i]);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', fitAll);
    } else {
        fitAll();
    }

    // Шрифт мог ещё не загрузиться к первому расчёту: с подставленным
    // шрифтом ширина строки другая, и подгонка вышла бы мимо
    if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(fitAll);
    }

    // Печать бывает и мимо кнопки — через Ctrl+P. Пересчитываем перед ней:
    // на бумагу должно уйти то же, что видно на экране
    window.addEventListener('beforeprint', fitAll);
})();
