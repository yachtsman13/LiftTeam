/*
 * Выбор детали из каталога — везде, где нужно указать одну деталь.
 *
 * В каталоге сотни радиодеталей, и обычный список деталей означал, что
 * нужную ищут прокруткой. Здесь вместо списка — поле с выбранной деталью,
 * кнопка «Выбрать» и панель с поиском и теми же отборами, что на складе.
 * Наружу уходит всё то же скрытое поле с номером детали под прежним
 * именем, поэтому принимающая сторона ничего не знает об этой замене.
 *
 * Разметка — `core/templates/core/_part_picker.html`, отбор на сервере —
 * `views.part_search`.
 *
 * Два условия, оба вынужденные:
 *
 *   1. Никакого Bootstrap JS: ни `data-bs-toggle`, ни `modal`. Bootstrap
 *      приезжает из интернета, и когда он не приехал, выбор детали обязан
 *      работать всё равно. Панель прячется атрибутом `hidden`, за которым
 *      в base.html закреплено `display: none !important`.
 *
 *   2. Запросы — через LiftTeamWS.fetch, а не через голый fetch: обрыв
 *      связи тогда попадает в общую полосу вверху страницы, а не молча
 *      оборачивается пустым списком, из которого следует, что таких
 *      деталей нет.
 *
 * Обработчики повешены на документ, а не на каждую разметку: строки
 * рецепта неисправности размножаются копированием готовой строки, и выбор
 * в копии должен работать сам, без вызова чего бы то ни было.
 *
 * Наружу:
 *   LiftTeamPartPicker.reset(корень)   — снять выбор (нужно копиям строк);
 *   LiftTeamPartPicker.set(корень, {id, label});
 *   событие 'part-picker:change' на корне выбора — detail: {id, label, part}.
 */
(function () {
    'use strict';

    /* Пауза после нажатия клавиши. Меньше — и на каждую букву уходит
       отдельный запрос, больше — список отстаёт от набора. */
    var SEARCH_DELAY_MS = 250;

    /* Панель стоит поверх всего, включая модальные окна Bootstrap (1055),
       но ниже полосы «нет связи» (1090): полоса важнее любого выбора. */
    var state = new WeakMap();
    var openPicker = null;
    var componentTypes = null;      // общий на страницу список типов
    var componentTypesAsked = false;

    function pickerState(root) {
        var value = state.get(root);
        if (!value) {
            value = {timer: null, results: [], active: -1, request: 0};
            state.set(root, value);
        }
        return value;
    }

    function part(root, selector) {
        return root.querySelector(selector);
    }

    function escapeHtml(text) {
        var div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    /* ---- выбранное значение ---- */

    function set(root, choice) {
        var hidden = part(root, '.part-picker-value');
        var display = part(root, '.part-picker-display');
        var clear = part(root, '.part-picker-clear');
        hidden.value = choice && choice.id ? String(choice.id) : '';
        display.value = choice && choice.label ? choice.label : '';
        if (clear) clear.hidden = !hidden.value;
        root.dispatchEvent(new CustomEvent('part-picker:change', {
            bubbles: true,
            detail: {
                id: hidden.value,
                label: display.value,
                part: choice && choice.part ? choice.part : null
            }
        }));
    }

    function reset(root) {
        var roots = root && root.matches && root.matches('.part-picker')
            ? [root]
            : Array.prototype.slice.call((root || document).querySelectorAll('.part-picker'));
        roots.forEach(function (item) {
            set(item, null);
            var query = part(item, '.part-picker-query');
            if (query) query.value = '';
        });
    }

    /* ---- панель ---- */

    function place(root) {
        var panel = part(root, '.part-picker-panel');
        var control = part(root, '.part-picker-control');
        if (!panel || !control) return;
        if (window.innerWidth < 576) {
            // На телефоне панель занимает экран целиком: рядом с полем для
            // неё просто нет места, а список из пяти строк бесполезен
            panel.style.left = '';
            panel.style.top = '';
            panel.style.width = '';
            return;
        }
        var box = control.getBoundingClientRect();
        var width = Math.max(box.width, 340);
        var left = Math.min(box.left, Math.max(8, window.innerWidth - width - 8));
        var below = window.innerHeight - box.bottom;
        panel.style.width = width + 'px';
        panel.style.left = Math.max(8, left) + 'px';
        // Не помещается снизу и сверху места больше — раскрываем вверх
        if (below < 260 && box.top > below) {
            panel.style.top = Math.max(8, box.top - Math.min(420, box.top - 8)) + 'px';
        } else {
            panel.style.top = (box.bottom + 4) + 'px';
        }
    }

    function open(root) {
        if (openPicker && openPicker !== root) close(openPicker);
        openPicker = root;
        var panel = part(root, '.part-picker-panel');
        panel.hidden = false;
        place(root);
        fillTypes(root);
        var query = part(root, '.part-picker-query');
        query.focus();
        query.select();
        search(root);
    }

    function close(root) {
        var panel = part(root, '.part-picker-panel');
        if (panel) panel.hidden = true;
        if (openPicker === root) openPicker = null;
    }

    /* ---- список типов компонентов ---- */

    function fillTypes(root) {
        var select = part(root, '.part-picker-type');
        if (!select || !componentTypes) return;
        var current = select.value;
        select.innerHTML = '<option value="">Все типы</option>' +
            componentTypes.map(function (name) {
                return '<option value="' + escapeHtml(name) + '">' + escapeHtml(name) + '</option>';
            }).join('');
        select.value = current;
    }

    /* ---- поиск ---- */

    function searchUrl(root, extra) {
        var params = new URLSearchParams();
        var query = part(root, '.part-picker-query');
        var type = part(root, '.part-picker-type');
        var inStock = part(root, '.part-picker-in-stock');
        var hasQuery = query && query.value.trim();
        if (hasQuery) params.set('q', query.value.trim());
        if (type && type.value) params.set('component_type', type.value);
        if (inStock && inStock.checked) params.set('in_stock', '1');
        if (root.dataset.exclude) params.set('exclude', root.dataset.exclude);
        // default_no_cell: пока ничего не набрали — приоритет деталям без
        // ячейки, их сюда и кладут чаще всего. Начали печатать — ищем
        // по всему каталогу, ограничение снимается.
        if (!hasQuery && root.dataset.defaultNoCell) params.set('no_cell', '1');
        if (!componentTypes && !componentTypesAsked) {
            // Флаг ставится здесь, а не у вызывающего: иначе первый же
            // запрос помечал бы список типов запрошенным, сам его
            // не спросив, и отбор по типу оставался бы пустым
            componentTypesAsked = true;
            params.set('with_types', '1');
        }
        Object.keys(extra || {}).forEach(function (key) { params.set(key, extra[key]); });
        return root.dataset.searchUrl + '?' + params.toString();
    }

    function search(root) {
        var data = pickerState(root);
        var ticket = ++data.request;

        // LiftTeamWS.fetch, а не fetch: без связи это должно поднять общую
        // полосу, а не выглядеть как «ничего не найдено»
        LiftTeamWS.fetch(searchUrl(root))
            .then(function (response) {
                return response.ok ? response.json() : Promise.reject(response.status);
            })
            .then(function (payload) {
                if (ticket !== data.request) return;   // ответ на устаревший запрос
                if (payload.component_types) {
                    componentTypes = payload.component_types;
                    fillTypes(root);
                }
                data.results = payload.results || [];
                data.active = data.results.length ? 0 : -1;
                render(root, payload);
            })
            .catch(function () {
                if (ticket !== data.request) return;
                data.results = [];
                data.active = -1;
                part(root, '.part-picker-results').innerHTML =
                    '<div class="part-picker-empty">Список деталей не загрузился. Попробуйте ещё раз.</div>';
                notice(root, '');
            });
    }

    function notice(root, text) {
        var el = part(root, '.part-picker-notice');
        el.textContent = text;
        el.hidden = !text;
    }

    function render(root, payload) {
        var list = part(root, '.part-picker-results');
        var data = pickerState(root);

        if (!data.results.length) {
            list.innerHTML = '<div class="part-picker-empty">Ничего не найдено — уточните запрос</div>';
            notice(root, '');
            return;
        }

        list.innerHTML = data.results.map(function (item, index) {
            var stockClass = 'part-picker-stock';
            if (item.stock_state === 'below') stockClass += ' part-picker-stock-low';
            else if (item.stock_state === 'at_minimum') stockClass += ' part-picker-stock-edge';
            var details = [item.component_type, item.package, item.specs]
                .filter(Boolean).map(escapeHtml).join(' · ');
            return '' +
                '<div class="part-picker-result' + (index === data.active ? ' active' : '') + '"' +
                    ' role="option" data-index="' + index + '">' +
                    '<div class="part-picker-main">' +
                        '<span class="part-picker-number">' + escapeHtml(item.part_number) + '</span> ' +
                        '<span class="part-picker-name">' + escapeHtml(item.name) + '</span>' +
                    '</div>' +
                    (details ? '<div class="part-picker-specs">' + details + '</div>' : '') +
                    '<div class="part-picker-meta">' +
                        '<span class="' + stockClass + '">Остаток: ' + item.stock + ' / ' + item.min_stock + '</span>' +
                        '<span class="part-picker-cell">' +
                            (item.cell ? escapeHtml(item.cell) : 'ячейка не назначена') +
                        '</span>' +
                    '</div>' +
                '</div>';
        }).join('');

        notice(root, payload && payload.limited
            ? 'Показаны первые ' + payload.limit + ' из ' + payload.total + ' — уточните запрос'
            : '');
        scrollToActive(root);
    }

    function scrollToActive(root) {
        var active = part(root, '.part-picker-result.active');
        if (active && active.scrollIntoView) active.scrollIntoView({block: 'nearest'});
    }

    function highlight(root, index) {
        var data = pickerState(root);
        if (!data.results.length) return;
        data.active = (index + data.results.length) % data.results.length;
        var rows = root.querySelectorAll('.part-picker-result');
        Array.prototype.forEach.call(rows, function (row, position) {
            row.classList.toggle('active', position === data.active);
        });
        scrollToActive(root);
    }

    function choose(root, index) {
        var data = pickerState(root);
        var item = data.results[index];
        if (!item) return;
        set(root, {id: item.id, label: item.label, part: item});
        close(root);
        var opener = part(root, '.part-picker-open');
        if (opener) opener.focus();
    }

    /* ---- подпись к уже выбранной детали ----
       Страница вернулась с ошибкой формы: номер детали есть, а как она
       называется, разметка не знает. Спрашиваем у сервера — молчаливое
       «Деталь не выбрана» над непустым полем выглядело бы как потерянный
       выбор, и его сделали бы заново. */
    function restoreLabel(root) {
        var hidden = part(root, '.part-picker-value');
        var display = part(root, '.part-picker-display');
        if (!hidden.value || display.value) return;
        LiftTeamWS.fetch(searchUrl(root, {id: hidden.value}))
            .then(function (response) { return response.ok ? response.json() : Promise.reject(response.status); })
            .then(function (payload) {
                var item = (payload.results || [])[0];
                if (item) display.value = item.label;
            })
            .catch(function () { /* подпись не критична: номер детали на месте */ });
    }

    /* ---- события ---- */

    document.addEventListener('click', function (event) {
        var root = event.target.closest ? event.target.closest('.part-picker') : null;

        if (root) {
            if (event.target.closest('.part-picker-open') ||
                event.target.closest('.part-picker-display')) {
                event.preventDefault();
                open(root);
                return;
            }
            if (event.target.closest('.part-picker-clear')) {
                event.preventDefault();
                set(root, null);
                return;
            }
            if (event.target.closest('.part-picker-close')) {
                event.preventDefault();
                close(root);
                return;
            }
            var row = event.target.closest('.part-picker-result');
            if (row) {
                choose(root, parseInt(row.dataset.index, 10));
                return;
            }
            return;
        }

        if (openPicker) close(openPicker);
    });

    document.addEventListener('input', function (event) {
        if (!event.target.classList || !event.target.classList.contains('part-picker-query')) return;
        var root = event.target.closest('.part-picker');
        var data = pickerState(root);
        clearTimeout(data.timer);
        data.timer = setTimeout(function () { search(root); }, SEARCH_DELAY_MS);
    });

    document.addEventListener('change', function (event) {
        if (!event.target.classList) return;
        if (!event.target.classList.contains('part-picker-type') &&
            !event.target.classList.contains('part-picker-in-stock')) return;
        search(event.target.closest('.part-picker'));
    });

    /* Клавиатура: мастера работают быстро, и выбор, требующий мыши,
       раздражает больше, чем прежний длинный список. */
    document.addEventListener('keydown', function (event) {
        var root = event.target.closest ? event.target.closest('.part-picker') : null;
        if (!root) return;

        if (event.key === 'Escape') {
            if (!part(root, '.part-picker-panel').hidden) {
                event.preventDefault();
                close(root);
                var opener = part(root, '.part-picker-open');
                if (opener) opener.focus();
            }
            return;
        }

        if (part(root, '.part-picker-panel').hidden) {
            // Поле с выбранной деталью открывается с клавиатуры так же,
            // как кнопкой: Enter или стрелка вниз
            if ((event.key === 'Enter' || event.key === 'ArrowDown') &&
                event.target.classList.contains('part-picker-display')) {
                event.preventDefault();
                open(root);
            }
            return;
        }

        var data = pickerState(root);
        if (event.key === 'ArrowDown') {
            event.preventDefault();
            highlight(root, data.active + 1);
        } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            highlight(root, data.active - 1);
        } else if (event.key === 'Enter') {
            // Enter внутри панели выбирает деталь, а не отправляет форму:
            // отправка с недобранными полями теряет введённое количество
            event.preventDefault();
            choose(root, data.active);
        }
    });

    window.addEventListener('resize', function () { if (openPicker) place(openPicker); });
    window.addEventListener('scroll', function () { if (openPicker) place(openPicker); }, true);

    document.addEventListener('DOMContentLoaded', function () {
        Array.prototype.forEach.call(document.querySelectorAll('.part-picker'), restoreLabel);
    });

    window.LiftTeamPartPicker = {
        reset: reset,
        set: set,
        restoreLabel: restoreLabel
    };
})();
