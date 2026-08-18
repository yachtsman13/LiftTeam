/*
 * Общее подключение к WebSocket с самовосстановлением.
 *
 * Логика переподключения одна и та же для остатков склада и присутствия
 * сотрудников, и держать её в двух скриптах значило бы чинить обрывы связи
 * дважды. Здесь только связь: что делать с сообщениями, решает вызывающий.
 *
 * Пауза перед повтором растёт с секунды до полуминуты: связь по Tailscale
 * рвётся, когда телефон переходит между сетями, и переподключаться нужно
 * самим, но долбить выключенный сервер каждую секунду не следует.
 *
 * Использование:
 *   var link = LiftTeamWS.open({
 *       path: '/ws/stock/',
 *       onMessage: function (message) { ... },
 *       onOpen: function () { ... },
 *       onOffline: function (isOffline) { ... }
 *   });
 *   link.send({action: 'ping'});
 */
(function () {
    'use strict';

    var RECONNECT_START_MS = 1000;
    var RECONNECT_MAX_MS = 30000;

    function open(options) {
        var socket = null;
        var reconnectDelay = RECONNECT_START_MS;
        var reconnectTimer = null;

        var onMessage = options.onMessage || function () {};
        var onOpen = options.onOpen || function () {};
        var onOffline = options.onOffline || function () {};

        function connect() {
            if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
                return;
            }
            var scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
            socket = new WebSocket(scheme + '://' + window.location.host + options.path);

            socket.onopen = function () {
                reconnectDelay = RECONNECT_START_MS;
                onOffline(false);
                onOpen();
            };

            socket.onmessage = function (event) {
                var message;
                try {
                    message = JSON.parse(event.data);
                } catch (e) {
                    return;
                }
                onMessage(message);
            };

            socket.onclose = function () {
                onOffline(true);
                clearTimeout(reconnectTimer);
                reconnectTimer = setTimeout(connect, reconnectDelay);
                reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
            };

            socket.onerror = function () {
                if (socket) socket.close();
            };
        }

        connect();

        // Вкладку открыли снова — не ждём очередной паузы переподключения
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') {
                reconnectDelay = RECONNECT_START_MS;
                connect();
            }
        });

        return {
            send: function (payload) {
                if (socket && socket.readyState === WebSocket.OPEN) {
                    socket.send(JSON.stringify(payload));
                    return true;
                }
                return false;
            },
            isOpen: function () {
                return !!socket && socket.readyState === WebSocket.OPEN;
            }
        };
    }

    window.LiftTeamWS = {open: open};
})();
