/*
 * Общая связь с сервером: WebSocket с самовосстановлением и общее знание
 * о том, есть ли связь вообще.
 *
 * Логика переподключения одна и та же для остатков склада и присутствия
 * сотрудников, и держать её в двух скриптах значило бы чинить обрывы связи
 * дважды. Здесь только связь: что делать с сообщениями, решает вызывающий.
 *
 * Пауза перед повтором растёт с секунды до полуминуты: связь по Tailscale
 * рвётся, когда телефон переходит между сетями, и переподключаться нужно
 * самим, но долбить выключенный сервер каждую секунду не следует.
 *
 * Здесь же — единственный на всю программу ответ на вопрос «есть ли сейчас
 * связь». Его слушает полоса-предупреждение (connection-status.js), а знают
 * о нём два источника:
 *   1. сокеты, открытые через open() — присутствие держит такой сокет
 *      на каждой странице, поэтому сигнал есть всегда и отдельный опрос
 *      сервера ради него заводить не нужно;
 *   2. неудачные запросы fetch (requestFailed) — они замечают обрыв раньше,
 *      чем браузер успевает закрыть сокет.
 *
 * Использование:
 *   var link = LiftTeamWS.open({
 *       path: '/ws/stock/',
 *       onMessage: function (message) { ... },
 *       onOpen: function () { ... },
 *       onOffline: function (isOffline) { ... }
 *   });
 *   link.send({action: 'ping'});
 *
 *   LiftTeamWS.watch(function (isOffline) { ... });   // общее состояние
 *   LiftTeamWS.fetch(url, options)                    // fetch, который его учитывает
 */
(function () {
    'use strict';

    var RECONNECT_START_MS = 1000;
    var RECONNECT_MAX_MS = 30000;

    /* Сколько ждать, прежде чем забыть об одном неудачном запросе.
       Запрос падает мгновенно, а браузер закрывает сокет не всегда сразу,
       поэтому полосу поднимает первый же сбой запроса. Но если сокет всё
       это время открыт, значит связь есть, а запрос не прошёл по другой
       причине — держать предупреждение дальше было бы враньём. */
    var REQUEST_FAILURE_MS = 15000;

    /* Сколько неудачных попыток подряд считать обрывом, если сокет
       не открывался ни разу. Одна попытка — это ещё и обычная гонка при
       загрузке страницы; две подряд означают, что сервера действительно
       нет. */
    var FAILED_ATTEMPTS_FOR_OFFLINE = 2;

    var sockets = [];
    var watchers = [];
    var requestFailedAt = 0;
    var requestFailureTimer = null;
    var lastReported = null;

    function socketsSayOffline() {
        var meaningful = sockets.filter(function (state) {
            return state.everOpen || state.failedAttempts >= FAILED_ATTEMPTS_FOR_OFFLINE;
        });
        if (!meaningful.length) return false;
        return meaningful.every(function (state) { return !state.isOpen; });
    }

    function requestsSayOffline() {
        return requestFailedAt > 0 && (Date.now() - requestFailedAt) < REQUEST_FAILURE_MS;
    }

    /* Сам браузер знает, что сети нет вовсе (Wi-Fi выключили, кабель
       выдернули), и говорит об этом мгновенно — раньше, чем закроется
       сокет. Обратное неверно: navigator.onLine === true означает только
       наличие сети, а не доступность сервера, поэтому «есть связь» отсюда
       не берётся. */
    function browserSaysOffline() {
        return typeof navigator !== 'undefined' && navigator.onLine === false;
    }

    function isOffline() {
        return browserSaysOffline() || socketsSayOffline() || requestsSayOffline();
    }

    function publish() {
        var offline = isOffline();
        if (offline === lastReported) return;
        lastReported = offline;
        watchers.forEach(function (watcher) {
            try {
                watcher(offline);
            } catch (e) {
                // Один сломавшийся слушатель не должен ронять остальных
            }
        });
    }

    function watch(callback) {
        watchers.push(callback);
        callback(isOffline());
    }

    /* Запрос не дошёл до сервера. Отличать это от ответа с ошибкой обязан
       вызывающий: 500 от сервера — связь есть, и предупреждать о её потере
       было бы неверно. */
    function requestFailed() {
        requestFailedAt = Date.now();
        clearTimeout(requestFailureTimer);
        requestFailureTimer = setTimeout(publish, REQUEST_FAILURE_MS + 100);
        publish();
    }

    function requestSucceeded() {
        if (!requestFailedAt) return;
        requestFailedAt = 0;
        clearTimeout(requestFailureTimer);
        publish();
    }

    /* Сорванный fetch отличается от ответа с ошибкой типом исключения:
       браузер бросает TypeError, когда запрос не ушёл или ответа не было.
       Отказ сервера сюда не попадает — он приходит обычным ответом. */
    function isNetworkError(error) {
        return error instanceof TypeError || browserSaysOffline();
    }

    /* Текст для человека вместо технического сообщения об ошибке. */
    function errorText(error, fallback) {
        if (isNetworkError(error)) {
            requestFailed();
            return 'Нет связи с сервером — действие не выполнено. Попробуйте ещё раз, когда связь восстановится.';
        }
        return fallback || 'Не удалось выполнить действие. Попробуйте ещё раз.';
    }

    /* Обёртка над fetch: сама отмечает, дошёл запрос или нет. Ответ
       возвращается как есть, вместе с кодом состояния — разбирать его
       по-прежнему делo вызывающего. */
    function request(url, options) {
        return window.fetch(url, options).then(function (response) {
            requestSucceeded();
            return response;
        }, function (error) {
            if (isNetworkError(error)) requestFailed();
            throw error;
        });
    }

    function open(options) {
        var socket = null;
        var reconnectDelay = RECONNECT_START_MS;
        var reconnectTimer = null;
        var state = {isOpen: false, everOpen: false, failedAttempts: 0};

        sockets.push(state);

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
                state.isOpen = true;
                state.everOpen = true;
                state.failedAttempts = 0;
                // Сокет открылся — значит связь есть, и памяти о неудачном
                // запросе верить больше незачем
                requestSucceeded();
                publish();
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
                state.isOpen = false;
                if (!state.everOpen) state.failedAttempts += 1;
                publish();
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

    window.addEventListener('offline', publish);
    window.addEventListener('online', publish);

    window.LiftTeamWS = {
        open: open,
        watch: watch,
        isOffline: isOffline,
        fetch: request,
        errorText: errorText,
        isNetworkError: isNetworkError,
        requestFailed: requestFailed,
        requestSucceeded: requestSucceeded
    };
})();
