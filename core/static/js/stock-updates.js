/*
 * Живое обновление остатков склада.
 *
 * Серверная часть рассылала изменения остатков в WebSocket с самого начала,
 * но подключаться к ней было некому: страница показывала цифру на момент
 * загрузки. Двое сотрудников, работающих одновременно, видели разные остатки,
 * и тот, у кого страница открыта дольше, списывал по устаревшему числу.
 *
 * Скрипт подключается только там, где остатки показаны (есть элементы
 * с data-stock-part), обновляет цифры на месте и предупреждает, когда деталь
 * уходит в дефицит.
 */
(function () {
    'use strict';

    var RECONNECT_START_MS = 1000;
    var RECONNECT_MAX_MS = 30000;

    var socket = null;
    var reconnectDelay = RECONNECT_START_MS;
    var reconnectTimer = null;
    var offlineNotice = null;

    function stockElements(partId) {
        return document.querySelectorAll('[data-stock-part="' + partId + '"]');
    }

    function watching() {
        // Либо страница показывает остатки цифрами, либо это сетка кассетниц,
        // где остаток виден цветом ячейки
        return document.querySelector('[data-stock-part]') !== null ||
               document.querySelector('.cell-item[data-cell-id]') !== null;
    }

    /* Те же три состояния, что и на сервере: дефицит, ровно на минимуме, норма. */
    function stockState(stock, minStock) {
        if (stock < minStock) return 'below';
        if (minStock > 0 && stock === minStock) return 'at_minimum';
        return 'ok';
    }

    function applyBadge(element, state) {
        element.classList.remove('bg-danger', 'bg-warning', 'bg-success', 'text-dark');
        if (!element.classList.contains('badge')) return;
        if (state === 'below') {
            element.classList.add('bg-danger');
        } else if (state === 'at_minimum') {
            element.classList.add('bg-warning', 'text-dark');
        } else {
            element.classList.add('bg-success');
        }
    }

    function applyRow(element, state) {
        var row = element.closest('tr');
        if (!row) return;
        row.classList.remove('table-danger', 'table-warning');
        if (state === 'below') {
            row.classList.add('table-danger');
        } else if (state === 'at_minimum') {
            row.classList.add('table-warning');
        }
    }

    function showToast(text, level) {
        var holder = document.getElementById('stockToasts');
        if (!holder) return;

        var toast = document.createElement('div');
        toast.className = 'alert alert-' + level + ' shadow-sm py-2 px-3 mb-2 small';
        toast.textContent = text;
        holder.appendChild(toast);
        // Убираем сами: сообщение об остатке живёт ровно до следующего дела
        setTimeout(function () { toast.remove(); }, 8000);
    }

    /* Сетка кассетниц держит содержимое ячеек в CELLS_DATA и красит ячейку
       по худшему состоянию её деталей. Обновляем и данные, и цвет: иначе
       кладовщик смотрит на зелёную ячейку, в которой уже дефицит. */
    function updateGrid(data) {
        if (typeof window.CELLS_DATA !== 'object' || !window.CELLS_DATA) return;

        Object.keys(window.CELLS_DATA).forEach(function (cellId) {
            var cell = window.CELLS_DATA[cellId];
            var touched = false;

            cell.parts.forEach(function (part) {
                if (String(part.id) === String(data.part_id)) {
                    part.stock = data.current_stock;
                    part.min_stock = data.min_stock;
                    touched = true;
                }
            });
            if (!touched) return;

            var worst = 'normal';
            cell.parts.forEach(function (part) {
                var state = stockState(part.stock, part.min_stock);
                if (state === 'below') {
                    worst = 'low_stock';
                } else if (state === 'at_minimum' && worst !== 'low_stock') {
                    worst = 'at_minimum';
                }
            });

            var element = document.querySelector('.cell-item[data-cell-id="' + cellId + '"]');
            // Ячейку в режиме перемещения не перекрашиваем: подсветка выбора
            // важнее сведений об остатке, пока деталь тащат
            if (!element || element.classList.contains('selected')) return;
            element.classList.remove('normal', 'low_stock', 'at_minimum');
            element.classList.add(cell.parts.length ? worst : 'free');
        });
    }

    function handleUpdate(data) {
        updateGrid(data);

        var elements = stockElements(data.part_id);
        if (!elements.length) return;

        var state = stockState(data.current_stock, data.min_stock);
        elements.forEach(function (element) {
            if (element.textContent.trim() === String(data.current_stock)) return;
            element.textContent = data.current_stock;
            applyBadge(element, state);
            applyRow(element, state);
            // Короткая подсветка: иначе цифра меняется незаметно
            element.classList.add('stock-just-changed');
            setTimeout(function () { element.classList.remove('stock-just-changed'); }, 2000);
        });

        if (state === 'below') {
            showToast(data.part_number + ' — остаток ' + data.current_stock +
                      ' при минимуме ' + data.min_stock, 'danger');
        } else if (state === 'at_minimum') {
            showToast(data.part_number + ' — остаток на минимуме (' + data.current_stock + ')', 'warning');
        }
    }

    function setOffline(isOffline) {
        if (isOffline && !offlineNotice) {
            offlineNotice = document.createElement('div');
            offlineNotice.className = 'alert alert-secondary py-1 px-2 mb-2 small';
            offlineNotice.textContent = 'Нет связи с сервером — остатки могут устареть';
            var holder = document.getElementById('stockToasts');
            if (holder) holder.appendChild(offlineNotice);
        } else if (!isOffline && offlineNotice) {
            offlineNotice.remove();
            offlineNotice = null;
        }
    }

    function connect() {
        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
            return;
        }
        var scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
        socket = new WebSocket(scheme + '://' + window.location.host + '/ws/stock/');

        socket.onopen = function () {
            reconnectDelay = RECONNECT_START_MS;
            setOffline(false);
        };

        socket.onmessage = function (event) {
            var message;
            try {
                message = JSON.parse(event.data);
            } catch (e) {
                return;
            }
            if (message.type === 'stock_update' && message.data) {
                handleUpdate(message.data);
            }
        };

        socket.onclose = function () {
            // Связь по Tailscale рвётся при переходе телефона между сетями,
            // поэтому переподключаемся сами, увеличивая паузу до полуминуты,
            // чтобы не долбить сервер, когда он действительно выключен.
            setOffline(true);
            clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
        };

        socket.onerror = function () {
            if (socket) socket.close();
        };
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!watching()) return;
        connect();

        // Вкладку открыли снова — не ждём очередной паузы переподключения
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') {
                reconnectDelay = RECONNECT_START_MS;
                connect();
            }
        });
    });
})();
