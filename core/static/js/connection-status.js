/*
 * Полоса «нет связи с сервером» и защита форм от отправки в никуда.
 *
 * Сервер стоит дома, а лаборатория ходит к нему через интернет по Tailscale,
 * поэтому короткие обрывы связи здесь обычное дело. Раньше они выглядели
 * как непонятный сбой: живые остатки замирали, запросы из окон молча
 * не проходили, а отправленная форма приводила к странице ошибки браузера —
 * вместе со всем, что человек в неё вписал.
 *
 * Состояние связи берётся из общего слоя (ws-connection.js), а не из
 * отдельного опроса сервера: присутствие сотрудников держит сокет на каждой
 * странице, и его обрыв — уже готовый и мгновенный признак. Заводить ради
 * полосы вторую проверку значило бы стучаться к серверу вдвое чаще без
 * всякой пользы.
 *
 * Чего здесь намеренно нет: очереди отправки. Форма, не ушедшая при обрыве,
 * сама потом не отправляется — её отправляет человек, когда полоса погасла.
 * Автоматическая досылка означала бы, что при неясном исходе первой попытки
 * заказ или списание могут задвоиться, а разбирать это некому.
 *
 * Остаётся зазор, который закрыть нельзя: если связь оборвалась в тот
 * момент, когда форма уже ушла из браузера, страницу ошибки показывает сам
 * браузер, и перехватить это нечем. Обычно обрыв первым замечает сокет —
 * тогда работает всё описанное выше.
 */
(function () {
    'use strict';

    var OFFLINE_TEXT = 'Нет связи с сервером — часть функций может не работать.';
    var BLOCKED_TEXT = 'Нет связи с сервером — данные останутся в форме, попробуйте отправить ещё раз.';
    var RESTORED_TEXT = 'Связь восстановлена. Форма не была отправлена — отправьте её ещё раз.';
    var RESTORED_VISIBLE_MS = 20000;

    var banner = null;
    var bannerText = null;
    var retryButton = null;
    var restoredTimer = null;

    // Форма, отправку которой задержал обрыв связи, и кнопка, которой её
    // отправляли: у части форм от кнопки зависит действие (обновить, откатить),
    // и повтор без неё ушёл бы не туда.
    var pendingForm = null;
    var pendingSubmitter = null;

    /* Показ и скрытие — через hidden, а не через класс Bootstrap: сам
       Bootstrap грузится из интернета, и при неполадках со связью его может
       не быть на странице — то есть ровно тогда, когда полоса работает. */
    function show(text, restored) {
        if (!banner) return;
        bannerText.textContent = text;
        banner.hidden = false;
        banner.classList.toggle('connection-banner-restored', !!restored);
        retryButton.hidden = !restored || !pendingForm;
    }

    function hide() {
        if (!banner) return;
        banner.hidden = true;
        retryButton.hidden = true;
    }

    function render(isOffline) {
        clearTimeout(restoredTimer);
        if (isOffline) {
            show(pendingForm ? BLOCKED_TEXT : OFFLINE_TEXT, false);
            return;
        }
        if (pendingForm) {
            show(RESTORED_TEXT, true);
            // Полоса не висит вечно: данные в форме никуда не денутся,
            // а напоминание через треть минуты уже только мешает.
            restoredTimer = setTimeout(function () {
                pendingForm = null;
                pendingSubmitter = null;
                hide();
            }, RESTORED_VISIBLE_MS);
            return;
        }
        hide();
    }

    /* Отправку перехватываем на погружении и гасим событие целиком: иначе
       собственный обработчик формы (например, снятие пометки о несохранённых
       изменениях) решит, что форма ушла, хотя она осталась на месте. */
    function guardSubmit(event) {
        if (!window.LiftTeamWS || !window.LiftTeamWS.isOffline()) return;

        var form = event.target;
        if (!form || form.tagName !== 'FORM') return;
        // Обычная навигация по GET (поиск, фильтры) не меняет данные
        // и без связи просто не откроется — держать её незачем
        if ((form.method || '').toLowerCase() !== 'post') return;
        if (form.hasAttribute('data-offline-ignore')) return;

        event.preventDefault();
        event.stopPropagation();

        pendingForm = form;
        pendingSubmitter = event.submitter || null;
        render(true);
    }

    function retry() {
        var form = pendingForm;
        var submitter = pendingSubmitter;
        pendingForm = null;
        pendingSubmitter = null;
        hide();
        if (!form) return;
        // requestSubmit, а не submit: он проверяет поля и посылает событие
        // submit, поэтому форма с ошибками не уйдёт молча
        if (form.requestSubmit) {
            form.requestSubmit(submitter || undefined);
        } else {
            form.submit();
        }
    }

    function start() {
        banner = document.getElementById('connectionBanner');
        bannerText = document.getElementById('connectionBannerText');
        retryButton = document.getElementById('connectionRetry');
        if (!banner || !bannerText || !retryButton) return;

        retryButton.addEventListener('click', retry);
        document.addEventListener('submit', guardSubmit, true);

        if (window.LiftTeamWS) window.LiftTeamWS.watch(render);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
