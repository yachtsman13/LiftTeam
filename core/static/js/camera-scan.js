/*
 * Камера как второй сканер.
 *
 * Всё, что она делает, — читает QR-код и отдаёт его содержимое
 * в LiftTeamScanner.submit(). Дальше работает тот же слой, что и с USB-
 * сканером: те же виды кодов, те же подписки экранов, те же сообщения.
 * Ни один экран из-за камеры не менялся и меняться не должен — иначе
 * у одного и того же скана появилось бы два разных поведения.
 *
 * ------------------------------------------------------------------
 * ПОЧЕМУ ЭТОГО НЕ БЫЛО РАНЬШЕ И ЧТО ТРЕБУЕТСЯ
 * ------------------------------------------------------------------
 * Браузер отдаёт камеру только в защищённом окружении: HTTPS либо
 * localhost. Программа открывалась по обычному http на имени в Tailscale,
 * и получить камеру было нельзя вовсе — никакими обходными путями,
 * это свойство браузера, а не настройка. Сертификат ставится на Pi
 * командой `tailscale cert` (DEPLOY.md, раздел «HTTPS и камера»).
 *
 * Распознаёт код сам браузер — BarcodeDetector. Своей библиотеки
 * здесь нет и не будет: всё стороннее в этом проекте приезжает
 * из интернета, а интернет в лаборатории пропадает — ровно тогда,
 * когда со сканером стоят у стеллажа. Браузера без BarcodeDetector
 * (Safari, iOS) страница не обходит молча: она говорит, чего не хватает,
 * и напоминает про USB-сканер, который работает всегда.
 *
 * ------------------------------------------------------------------
 * КАК ДОБАВИТЬ КНОПКУ НА ЭКРАН
 * ------------------------------------------------------------------
 *   {% include 'core/_camera_button.html' %}
 *
 * Больше ничего. Скрипт находит кнопку по атрибуту data-camera-scan,
 * а результат уходит в общий слой — подписка экрана, если она есть,
 * сработает сама.
 */
(function () {
    'use strict';

    /* Как часто смотреть в кадр. Чаще нет смысла: распознавание занимает
       единицы миллисекунд, а расход батареи растёт линейно. */
    var LOOK_MS = 200;

    /* Один и тот же код, оставшийся в кадре, — это один скан. Слой сканера
       считает повтором секунду, но камера видит наклейку непрерывно,
       и секунды мало: она бы пищала раз в секунду, пока код в кадре. */
    var SAME_CODE_MS = 2500;

    var video = null;
    var panel = null;
    var status = null;
    var stream = null;
    var timer = null;
    var detector = null;
    var lastValue = '';
    var lastAt = 0;

    function supported() {
        return typeof window.BarcodeDetector === 'function';
    }

    function secure() {
        // localhost браузер считает защищённым и без сертификата —
        // на нём проверить камеру можно до всякого HTTPS
        return window.isSecureContext;
    }

    /* ---- разметка ----
       Своя и прячется атрибутом hidden: Bootstrap приходит из интернета,
       и его окна недоступны ровно тогда, когда со связью плохо. */

    function build() {
        if (panel) return;

        panel = document.createElement('div');
        panel.className = 'camera-scan no-print';
        panel.id = 'cameraScan';
        panel.hidden = true;

        video = document.createElement('video');
        video.className = 'camera-scan-video';
        // Оба обязательны: без playsinline телефон открывает видео
        // на весь экран своим проигрывателем, и кадры до нас не доходят
        video.setAttribute('playsinline', '');
        video.setAttribute('muted', '');
        video.muted = true;

        var frame = document.createElement('div');
        frame.className = 'camera-scan-frame';

        status = document.createElement('div');
        status.className = 'camera-scan-status';
        status.setAttribute('role', 'status');

        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'camera-scan-close';
        close.textContent = 'Закрыть';
        close.addEventListener('click', stop);

        var box = document.createElement('div');
        box.className = 'camera-scan-box';
        box.appendChild(video);
        box.appendChild(frame);

        panel.appendChild(box);
        panel.appendChild(status);
        panel.appendChild(close);
        document.body.appendChild(panel);
    }

    function say(text) {
        build();
        status.textContent = text;
    }

    /* ---- работа камеры ---- */

    function start() {
        build();

        if (!secure()) {
            LiftTeamScanner.report(false,
                'Камера доступна только по защищённому соединению (HTTPS). ' +
                'Программа открыта по http, и браузер камеру не отдаст. ' +
                'USB-сканер работает как обычно.');
            return;
        }
        if (!supported()) {
            LiftTeamScanner.report(false,
                'Этот браузер не умеет читать коды камерой (нет BarcodeDetector). ' +
                'Так ведут себя Safari и браузеры на iPhone. ' +
                'Подойдёт Chrome на Android — или USB-сканер, он работает всегда.');
            return;
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            LiftTeamScanner.report(false, 'Этот браузер не даёт доступ к камере.');
            return;
        }

        panel.hidden = false;
        say('Наведите камеру на код…');

        try {
            detector = new window.BarcodeDetector({formats: ['qr_code']});
        } catch (e) {
            LiftTeamScanner.report(false, 'Браузер не умеет читать QR-коды камерой.');
            stop();
            return;
        }

        // facingMode задаётся пожеланием, а не требованием: на ноутбуке
        // задней камеры нет вовсе, и строгое требование обернулось бы
        // отказом вместо работы с той камерой, что есть
        navigator.mediaDevices.getUserMedia({
            video: {facingMode: {ideal: 'environment'}},
            audio: false
        }).then(function (media) {
            stream = media;
            video.srcObject = media;
            return video.play();
        }).then(function () {
            timer = setInterval(look, LOOK_MS);
        }).catch(function (err) {
            // Отказ в доступе и отсутствие камеры — разные беды, и чинят
            // их по-разному; молчаливое «не получилось» не чинит ни одну
            var name = err && err.name;
            if (name === 'NotAllowedError' || name === 'SecurityError') {
                LiftTeamScanner.report(false,
                    'Доступ к камере запрещён. Разрешите его в браузере ' +
                    '(значок замка рядом с адресом) и нажмите ещё раз.');
            } else if (name === 'NotFoundError' || name === 'OverconstrainedError') {
                LiftTeamScanner.report(false, 'Камера на этом устройстве не найдена.');
            } else {
                LiftTeamScanner.report(false, 'Камеру открыть не удалось.');
            }
            stop();
        });
    }

    function look() {
        if (!detector || !video || video.readyState < 2) return;

        detector.detect(video).then(function (codes) {
            if (!codes || !codes.length) return;
            var value = codes[0].rawValue;
            if (!value) return;

            var now = Date.now();
            // Наклейка остаётся в кадре, и без этого камера пищала бы
            // без остановки, пока её не уберут
            if (value === lastValue && (now - lastAt) < SAME_CODE_MS) return;
            lastValue = value;
            lastAt = now;

            say('Прочитано: ' + value);
            // Дальше — общий слой: тот же разбор, та же подписка экрана,
            // те же сообщения, что и у USB-сканера
            LiftTeamScanner.submit(value);
        }).catch(function () {
            // Кадр не разобрался — обычное дело, следующий разберётся
        });
    }

    function stop() {
        clearInterval(timer);
        timer = null;
        detector = null;
        if (stream) {
            // Камеру надо отпустить руками: без этого индикатор на телефоне
            // горит и после закрытия панели, и человек справедливо решает,
            // что программа за ним подглядывает
            stream.getTracks().forEach(function (track) { track.stop(); });
            stream = null;
        }
        if (video) video.srcObject = null;
        if (panel) panel.hidden = true;
        lastValue = '';
    }

    document.addEventListener('click', function (event) {
        var button = event.target.closest('[data-camera-scan]');
        if (!button) return;
        event.preventDefault();
        start();
    });

    // Ушли со страницы — камера должна погаснуть вместе с ней
    window.addEventListener('pagehide', stop);

    window.LiftTeamCamera = {
        start: start,
        stop: stop,
        supported: supported,
        available: function () { return secure() && supported(); }
    };
})();
