/*
 * Присутствие сотрудников: кто сейчас за терминалом.
 *
 * Открытое соединение и есть признак присутствия, поэтому скрипт грузится
 * на каждой странице — иначе человек, работающий с заказами, числился бы
 * отсутствующим. Список сотрудников перерисовывается только там, где он
 * показан (страница «Кто на связи»); на остальных страницах скрипт лишь
 * держит соединение и шлёт сигнал.
 *
 * Период сигнала приходит с сервера: там же задан и таймаут, от которого
 * он считается, и держать это число ещё и здесь значило бы рассогласовать
 * их при первой же правке настроек.
 */
(function () {
    'use strict';

    var DEFAULT_HEARTBEAT_MS = 30000;

    var heartbeatTimer = null;
    var link = null;

    function roster() {
        return document.getElementById('presenceList');
    }

    function timeText(iso) {
        if (!iso) return 'ни разу';
        var moment = new Date(iso);
        if (isNaN(moment.getTime())) return 'ни разу';
        var stamp = moment.toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
        var today = new Date();
        if (moment.toDateString() !== today.toDateString()) {
            stamp = moment.toLocaleDateString('ru-RU', {day: '2-digit', month: '2-digit'}) + ' в ' + stamp;
        } else {
            stamp = 'в ' + stamp;
        }
        return stamp;
    }

    function render(entries) {
        var holder = roster();
        if (!holder) return;

        entries.forEach(function (entry) {
            var row = holder.querySelector('[data-employee="' + entry.id + '"]');
            if (!row) return;

            var dot = row.querySelector('[data-presence-dot]');
            if (dot) {
                dot.classList.remove('presence-online', 'presence-offline');
                dot.classList.add(entry.online ? 'presence-online' : 'presence-offline');
            }

            var status = row.querySelector('[data-presence-status]');
            if (!status) return;
            if (entry.online) {
                status.textContent = 'в сети';
                status.className = 'text-success small';
            } else if (entry.last_seen) {
                status.textContent = 'был(а) в сети ' + timeText(entry.last_seen);
                status.className = 'text-muted small';
            } else {
                status.textContent = 'не в сети — ни разу не заходил(а)';
                status.className = 'text-muted small';
            }
        });
    }

    function startHeartbeat(intervalMs) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = setInterval(function () {
            if (link) link.send({action: 'ping'});
        }, intervalMs);
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!window.LiftTeamWS) return;

        link = window.LiftTeamWS.open({
            path: '/ws/presence/',
            onOpen: function () {
                // Сигнал сразу после обрыва и восстановления: ждать целый
                // период значило бы, что вернувшийся человек ещё полминуты
                // числится ушедшим.
                link.send({action: 'ping'});
            },
            onMessage: function (message) {
                if (message.type === 'connection_established') {
                    var seconds = parseInt(message.heartbeat_seconds, 10);
                    startHeartbeat(seconds > 0 ? seconds * 1000 : DEFAULT_HEARTBEAT_MS);
                } else if (message.type === 'presence_roster' && message.roster) {
                    render(message.roster);
                }
            }
        });
    });
})();
