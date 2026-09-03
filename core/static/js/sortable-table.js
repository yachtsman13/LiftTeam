/*
 * Сортировка по клику на шапку таблицы — одна логика на всю программу
 * (с v2.106.0), под серверный `views.sorted_by_request`.
 *
 * Разметке нужен только `<th data-sort="имя">` — своего скрипта или ссылок
 * заводить не надо, как и у `.clickable-row` в base.html. Клик читает
 * текущие `?sort=&dir=` из адреса, меняет их и загружает страницу заново:
 * сортирует сервер, а не браузер. Список почти всегда постраничный,
 * и пересортировка в браузере отсортировала бы только открытую страницу,
 * а остальные остались бы в прежнем порядке.
 *
 * Повторный клик по тому же столбцу переключает возрастание/убывание,
 * клик по другому столбцу сбрасывает на возрастание — так же, как
 * это устроено везде, где есть сортировка по клику на шапку.
 */
(function () {
    'use strict';

    function currentSort() {
        var params = new URLSearchParams(window.location.search);
        var dir = params.get('dir') === 'desc' ? 'desc' : 'asc';
        return { field: params.get('sort') || '', dir: dir };
    }

    function markActiveHeader() {
        var active = currentSort();
        if (!active.field) return;
        document.querySelectorAll('th[data-sort]').forEach(function (th) {
            if (th.dataset.sort === active.field) {
                th.classList.add('sortable-active', 'sort-' + active.dir);
            }
        });
    }

    function handleClick(event) {
        var th = event.target.closest('th[data-sort]');
        if (!th) return;

        var active = currentSort();
        var field = th.dataset.sort;
        var dir = (active.field === field && active.dir === 'asc') ? 'desc' : 'asc';

        var params = new URLSearchParams(window.location.search);
        params.set('sort', field);
        params.set('dir', dir);
        // Пересортировка — с первой страницы: вторая страница по-старому
        // порядку почти наверняка не та же самая часть списка
        params.delete('page');
        window.location.href = window.location.pathname + '?' + params.toString();
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!document.querySelector('th[data-sort]')) return;
        document.addEventListener('click', handleClick);
        markActiveHeader();
    });
})();
