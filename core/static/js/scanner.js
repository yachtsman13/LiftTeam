/*
 * Сканер штрихкодов: один слой на всю программу.
 *
 * Сканер — это клавиатура. Он «набирает» содержимое кода и жмёт Enter,
 * никаких драйверов и никакого доступа к устройству у страницы нет.
 * Отличить его от человека можно только по скорости: сканер выдаёт
 * символы за считанные миллисекунды, человек так не умеет. На этом
 * и держится всё распознавание.
 *
 * Камера телефона здесь ни при чём и подключена быть не может: браузер
 * отдаёт камеру только в защищённом окружении (HTTPS), а программа
 * открывается по обычному http на имени в Tailscale. Появится HTTPS —
 * появится и такая возможность; до тех пор её нет вовсе.
 *
 * Что делает слой:
 *   1. ловит быстрый набор, заканчивающийся Enter, и не мешает обычному
 *      набору руками — в том числе когда курсор стоит в поле ввода:
 *      содержимое поля восстанавливается, обрывков кода в нём не остаётся;
 *   2. разбирает содержимое кода в {kind, id} — по виду пути, а не по
 *      адресу сервера (наклейка могла быть напечатана с другой основы);
 *   3. считает повтор одного и того же кода в течение секунды за один
 *      скан: сканеры нередко срабатывают дважды на одном поднесении;
 *   4. **говорит о каждом скане** — звуком, вспышкой и строкой текста.
 *      Работа идёт с занятыми руками и взглядом на полке, а не на экране:
 *      скан, молча не сработавший, — это способ завести неверные данные;
 *   5. отдаёт скан экрану, который на него подписался, а если такого нет —
 *      открывает отсканированный объект.
 *
 * Звук синтезируется здесь же, файл не грузится: статика приезжает
 * с Raspberry Pi, и лишний запрос — это ещё одна вещь, которая не придёт.
 * Отключение звука хранится в этом браузере (localStorage).
 *
 * Никакого Bootstrap JS: он приходит из интернета и не приходил уже дважды.
 * Разметка своя, прячется атрибутом `hidden`, вид — `css/scanner.css`.
 *
 * ------------------------------------------------------------------
 * ЧТО ЭТИМ ПОЛЬЗУЕТСЯ (интерфейс для экранов)
 * ------------------------------------------------------------------
 *
 * Экран объявляет, какие виды кодов он принимает, и получает разобранный
 * скан вместо перехода по ссылке:
 *
 *   var handle = LiftTeamScanner.register({
 *       kinds: ['part'],                  // part, cell, equipment, order, order_equipment
 *       name: 'Инвентаризация',           // как назвать экран в сообщении
 *       onScan: function (scan) {
 *           // scan = {kind, id, url, label, payload}
 *           return 'Деталь ' + scan.id + ' добавлена';   // строка — успех
 *       }
 *   });
 *   handle.remove();                      // экран закрылся
 *
 * Что можно вернуть из onScan:
 *   строка                      — успех, строка станет сообщением;
 *   undefined / true            — успех, сообщение соберётся само;
 *   {ok: false, text: '...'}    — неудача с объяснением;
 *   {ok: true, text, actions}   — успех с кнопками ([{label, url}]);
 *   false                       — экран скан не берёт, слой откроет объект;
 *   Promise любого из этого     — слой дождётся и скажет по результату.
 *
 * Отсканирован вид, которого экран не ждёт (на складской сверке поднесли
 * заказ) — слой скажет об этом и **предложит обычный переход кнопкой**.
 * Молчаливого «ничего не произошло» быть не должно: человек решит, что
 * не сработал сканер, и поднесёт код ещё раз.
 *
 * Подписок может быть несколько, но отвечает **последняя**, и только она:
 * скан достаётся верхнему экрану, а не разыскивается по нижним. Экрану,
 * которому нужны два вида, надо перечислить оба в одной подписке,
 * а не подписываться дважды.
 *
 * Остальное наружу:
 *   LiftTeamScanner.decode(text)          — {kind, id, url, label} или null;
 *   LiftTeamScanner.submit(text)          — обработать код, будто он со сканера
 *                                           (ручной ввод, проверка);
 *   LiftTeamScanner.report(ok, text, actions) — сказать своими словами;
 *   LiftTeamScanner.isMuted() / setMuted(flag) — звук;
 *   событие 'scanner:mute' на документе — detail: {muted}.
 */
(function () {
    'use strict';

    /* ---- распознавание набора ---- */

    /* Разрыв, после которого набранное считается брошенным и начинается
       заново. Это не признак сканера, а склейка: держать в одной строке
       то, что набрано с паузой в треть секунды, незачем. */
    var MAX_GAP_MS = 300;

    /* Средняя скорость, по которой сканер и опознаётся: миллисекунды
       на символ. Быстрее 30 мс на знак — это 2000 знаков в минуту,
       руками так не набирают.

       Считается именно средняя, а не каждый промежуток по отдельности:
       браузер посреди кода может замереть на кадр-другой (перерисовка,
       уборка мусора), и требование «каждый символ быстрее X» разрывало бы
       код пополам. Проверено в браузере: половина ссылки терялась,
       и вместо детали получалось «это не наш код». Средняя такую заминку
       переживает, а человеческий набор от неё не проходит всё равно —
       у него на знак больше сотни миллисекунд. */
    var MAX_AVERAGE_MS = 30;

    /* Короче этого код не бывает: даже голый путь — это /p/1. */
    var MIN_LENGTH = 3;

    /* Набор начался, но Enter не пришёл — забыть. Иначе первые буквы
       медленного набора остались бы висеть и склеились со следующими. */
    var IDLE_MS = 400;

    /* Тот же код, пришедший в течение этого времени, — тот же скан.
       Сканеры срабатывают дважды на одном поднесении чаще, чем хотелось бы. */
    var REPEAT_MS = 1000;

    /* Сколько держать сообщение на экране. Неудача висит дольше: её
       читают, а успех подтверждает и без чтения — по звуку. */
    var SHOW_OK_MS = 3500;
    var SHOW_FAIL_MS = 9000;

    var MUTE_KEY = 'lifteam.scanner.muted';

    // Те же виды и в том же написании, что в core/scanning.py: разбор идёт
    // и здесь, и на сервере, и расходиться им нельзя — за этим следит
    // ScanLayerWiringTests.
    var KINDS = {p: 'part', c: 'cell', e: 'equipment', o: 'order', u: 'order_equipment'};
    var KIND_LABELS = {
        part: 'Радиодеталь',
        cell: 'Ячейка хранения',
        equipment: 'Оборудование',
        order: 'Заказ на ремонт',
        order_equipment: 'Оборудование в заказе'
    };
    // Обратное к KINDS, и выводится из него, а не пишется вторым списком.
    // Пока список был отдельным, при добавлении вида его забыли обновить:
    // разбор кода `u/20` работал, а переход вёл на /undefined/20/ —
    // «страница не найдена» вместо принятой единицы. Две таблицы одного
    // и того же расходятся молча, и заметно это становится со сканером
    // в руках у стеллажа.
    var KIND_PREFIX = {};
    Object.keys(KINDS).forEach(function (letter) {
        KIND_PREFIX[KINDS[letter]] = letter;
    });

    var buffer = '';
    var startedAt = 0;
    var lastKeyAt = 0;
    var idleTimer = null;
    var snapshot = null;

    var lastPayload = '';
    var lastPayloadAt = 0;

    var handlers = [];
    var audio = null;
    var muted = readMuted();

    /* ---- разбор содержимого кода ----
       Тот же разбор, что на сервере (core/scanning.py): вид пути свой,
       адрес сервера к делу не относится — наклейку могли напечатать
       с другой основы, и она обязана читаться. */

    var ORIGIN = /^[a-z][a-z0-9+.\-]*:\/\/[^/]*/i;
    var PATH = /^\/?([pceouPCEOU])\/(\d+)\/?$/;

    /* Пробелы и невидимое по краям: сканеры добавляют их сами, а на глаз
       строка выглядит обычной. Внутри строки не убираем ничего — там такой
       символ означает, что код не наш. */
    var EDGES = /^[\s\u200b\u200e\u200f\ufeff\x00]+|[\s\u200b\u200e\u200f\ufeff\x00]+$/g;

    function decode(payload) {
        if (!payload) return null;
        var text = String(payload).replace(EDGES, '');
        if (!text) return null;
        text = text.replace(ORIGIN, '');
        text = text.split('?')[0].split('#')[0].replace(EDGES, '');
        var match = PATH.exec(text);
        if (!match) return null;
        var kind = KINDS[match[1].toLowerCase()];
        var id = parseInt(match[2], 10);
        return {
            kind: kind,
            id: id,
            label: KIND_LABELS[kind],
            url: '/' + KIND_PREFIX[kind] + '/' + id + '/'
        };
    }

    /* ---- звук ----
       Синтез, а не файл: файл — это запрос к Raspberry Pi, который может
       не дойти ровно тогда, когда со связью плохо. Успех и неудача обязаны
       различаться на слух, не глядя на экран: успех — две коротких ноты
       вверх, неудача — низкое гудение. */

    function context() {
        var Ctor = window.AudioContext || window.webkitAudioContext;
        if (!Ctor) return null;
        if (!audio) {
            try {
                audio = new Ctor();
            } catch (e) {
                return null;
            }
        }
        // Браузер приглушает звук до первого действия человека; скан — это
        // нажатие клавиш, то есть действие, и здесь звук уже разрешён
        if (audio.state === 'suspended' && audio.resume) audio.resume();
        return audio;
    }

    function tone(ctx, frequency, startAt, duration, shape, volume) {
        var oscillator = ctx.createOscillator();
        var gain = ctx.createGain();
        oscillator.type = shape || 'sine';
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0.0001, startAt);
        gain.gain.exponentialRampToValueAtTime(volume || 0.2, startAt + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, startAt + duration);
        oscillator.connect(gain);
        gain.connect(ctx.destination);
        oscillator.start(startAt);
        oscillator.stop(startAt + duration + 0.02);
    }

    function beep(ok) {
        if (muted) return;
        var ctx = context();
        if (!ctx) return;
        var now = ctx.currentTime;
        try {
            if (ok) {
                tone(ctx, 1180, now, 0.06);
                tone(ctx, 1620, now + 0.07, 0.09);
            } else {
                tone(ctx, 240, now, 0.18, 'square', 0.15);
                tone(ctx, 170, now + 0.2, 0.28, 'square', 0.15);
            }
        } catch (e) {
            // Звук — не повод ронять обработку скана
        }
    }

    function readMuted() {
        try {
            return window.localStorage.getItem(MUTE_KEY) === '1';
        } catch (e) {
            return false;
        }
    }

    function setMuted(flag) {
        muted = !!flag;
        try {
            window.localStorage.setItem(MUTE_KEY, muted ? '1' : '0');
        } catch (e) {
            // Приватный режим: звук просто не запомнится до перезагрузки
        }
        updateMuteButton();
        document.dispatchEvent(new CustomEvent('scanner:mute', {detail: {muted: muted}}));
    }

    /* ---- сообщение на экране ----
       Вспышка на всё окно и полоса с текстом. Разметка своя и прячется
       атрибутом hidden: Bootstrap приходит из интернета, и полагаться
       на его окна нельзя. */

    var panel = null;
    var panelText = null;
    var panelIcon = null;
    var panelActions = null;
    var panelMute = null;
    var hideTimer = null;

    function build() {
        if (panel) return;

        panel = document.createElement('div');
        panel.className = 'scan-flash no-print';
        panel.id = 'scanFlash';
        panel.setAttribute('role', 'status');
        panel.setAttribute('aria-live', 'assertive');
        panel.hidden = true;

        panelIcon = document.createElement('span');
        panelIcon.className = 'scan-flash-icon';

        panelText = document.createElement('span');
        panelText.className = 'scan-flash-text';

        panelActions = document.createElement('span');
        panelActions.className = 'scan-flash-actions';

        panelMute = document.createElement('button');
        panelMute.type = 'button';
        panelMute.className = 'scan-flash-mute';
        panelMute.addEventListener('click', function () { setMuted(!muted); });

        var line = document.createElement('div');
        line.className = 'scan-flash-line';
        line.appendChild(panelIcon);
        line.appendChild(panelText);
        panel.appendChild(line);
        panel.appendChild(panelActions);
        panel.appendChild(panelMute);

        document.body.appendChild(panel);
        updateMuteButton();
    }

    function updateMuteButton() {
        if (!panelMute) return;
        panelMute.textContent = muted ? 'Звук выключен' : 'Звук включён';
        panelMute.title = muted ? 'Включить звук сканера' : 'Выключить звук сканера';
        panelMute.classList.toggle('scan-flash-muted', muted);
    }

    function veil(ok) {
        var flash = document.createElement('div');
        flash.className = 'scan-veil no-print ' + (ok ? 'scan-veil-ok' : 'scan-veil-fail');
        document.body.appendChild(flash);
        setTimeout(function () {
            if (flash.parentNode) flash.parentNode.removeChild(flash);
        }, 600);
    }

    function report(ok, text, actions) {
        build();
        panel.hidden = false;
        panel.classList.toggle('scan-flash-ok', !!ok);
        panel.classList.toggle('scan-flash-fail', !ok);
        panelIcon.textContent = ok ? '✓' : '✕';
        panelText.textContent = text;

        panelActions.innerHTML = '';
        (actions || []).forEach(function (action) {
            var link = document.createElement('a');
            link.className = 'scan-flash-action';
            link.href = action.url;
            link.textContent = action.label;
            panelActions.appendChild(link);
        });

        // Перезапуск анимации: два скана подряд должны мигнуть дважды
        panel.classList.remove('scan-flash-shown');
        void panel.offsetWidth;
        panel.classList.add('scan-flash-shown');

        veil(ok);
        beep(ok);

        clearTimeout(hideTimer);
        hideTimer = setTimeout(function () {
            panel.hidden = true;
        }, ok ? SHOW_OK_MS : SHOW_FAIL_MS);
    }

    /* ---- подписка экранов ---- */

    function register(options) {
        var entry = {
            kinds: options.kinds && options.kinds.length ? options.kinds.slice() : null,
            name: options.name || 'этот экран',
            onScan: options.onScan
        };
        handlers.push(entry);
        return {
            remove: function () {
                var index = handlers.indexOf(entry);
                if (index >= 0) handlers.splice(index, 1);
            }
        };
    }

    function takes(entry, kind) {
        return !entry.kinds || entry.kinds.indexOf(kind) >= 0;
    }

    function kindsText(entry) {
        if (!entry.kinds) return 'любые коды';
        return entry.kinds.map(function (kind) {
            return (KIND_LABELS[kind] || kind).toLowerCase();
        }).join(', ');
    }

    function navigate(scan) {
        report(true, scan.label + ' №' + scan.id + ' — открываю');
        window.location.href = scan.url;
    }

    function finish(scan, outcome) {
        if (outcome === false) {
            navigate(scan);
            return;
        }
        if (typeof outcome === 'string') {
            report(true, outcome);
            return;
        }
        if (outcome && typeof outcome === 'object') {
            report(outcome.ok !== false,
                   outcome.text || (scan.label + ' №' + scan.id),
                   outcome.actions);
            return;
        }
        report(true, scan.label + ' №' + scan.id + ' — принято');
    }

    function dispatch(scan) {
        // Отвечает **последний** подписавшийся, и только он. Правило одно
        // и то же при любом числе подписок: скан достаётся верхнему экрану,
        // а не разыскивается по нижним. Иначе экран, открытый поверх другого,
        // отдавал бы часть сканов тому, что под ним, — и человек не понимал бы,
        // куда ушла деталь.
        if (!handlers.length) {
            navigate(scan);
            return;
        }

        var screen = handlers[handlers.length - 1];

        if (!takes(screen, scan.kind)) {
            // Вид не тот. Молчать нельзя, и запирать тоже: человек видит,
            // что именно он отсканировал, и может открыть объект обычным
            // переходом
            report(false,
                'Отсканирован не тот код: ' + scan.label.toLowerCase() + ' №' + scan.id +
                '. Экран «' + screen.name + '» принимает ' + kindsText(screen) + '.',
                [{label: 'Всё равно открыть', url: scan.url}]);
            return;
        }

        var outcome;
        try {
            outcome = screen.onScan(scan);
        } catch (e) {
            report(false, 'Скан не обработан: ошибка на странице. Обновите страницу.');
            return;
        }

        if (outcome && typeof outcome.then === 'function') {
            outcome.then(function (value) {
                finish(scan, value);
            }, function () {
                report(false, 'Скан не обработан — попробуйте ещё раз.');
            });
            return;
        }

        finish(scan, outcome);
    }

    /* ---- обработка одного скана ---- */

    function shorten(text) {
        return text.length > 40 ? text.slice(0, 40) + '…' : text;
    }

    function submit(payload) {
        var text = String(payload == null ? '' : payload).replace(EDGES, '');
        if (!text) return false;

        var now = Date.now();
        if (text === lastPayload && (now - lastPayloadAt) < REPEAT_MS) {
            // Сканер сработал дважды на одном поднесении — это один скан
            lastPayloadAt = now;
            return true;
        }
        lastPayload = text;
        lastPayloadAt = now;

        var scan = decode(text);
        if (!scan) {
            report(false, 'Это не код LiftTeam: ' + shorten(text) +
                          '. Подойдут наклейки программы — деталь, ячейка, оборудование, заказ.');
            return true;
        }

        // Без связи скан выполнить нельзя, и вид, будто он прошёл, хуже
        // отказа: человек уйдёт от полки, считая деталь списанной
        if (window.LiftTeamWS && LiftTeamWS.isOffline()) {
            report(false, 'Нет связи с сервером — скан не выполнен. ' + scan.label +
                          ' №' + scan.id + '; поднесите код ещё раз, когда связь вернётся.');
            return true;
        }

        scan.payload = text;
        dispatch(scan);
        return true;
    }

    /* ---- клавиатура ----
       Всё держится на скорости набора. Пока набор не закончился Enter,
       мы не знаем, сканер это или человек, поэтому символы идут в поле
       как обычно, а перед самой обработкой поле возвращается в то
       состояние, в котором было до первого символа. Обрывков кода
       в поле не остаётся. */

    function editable(element) {
        if (!element) return null;
        var tag = element.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA') return element;
        return null;
    }

    function takeSnapshot() {
        var field = editable(document.activeElement);
        if (!field) {
            snapshot = null;
            return;
        }
        snapshot = {
            field: field,
            value: field.value,
            start: field.selectionStart,
            end: field.selectionEnd
        };
    }

    function restoreSnapshot() {
        if (!snapshot) return;
        var field = snapshot.field;
        // Поле могло смениться, пока летел код, — тогда трогать его нельзя
        if (field === document.activeElement && field.value !== snapshot.value) {
            field.value = snapshot.value;
            try {
                if (snapshot.start != null) field.setSelectionRange(snapshot.start, snapshot.end);
            } catch (e) {
                // У полей вроде input[type=number] выделения нет — не страшно
            }
        }
        snapshot = null;
    }

    function forget() {
        buffer = '';
        snapshot = null;
        clearTimeout(idleTimer);
    }

    function onKeyDown(event) {
        if (event.ctrlKey || event.metaKey || event.altKey) return;

        // Часы одни на все клавиши: смешивать event.timeStamp (он считается
        // от загрузки страницы) с Date.now() нельзя — промежутки вышли бы
        // бессмысленными, и человеческий набор сошёл бы за сканер
        var now = Date.now();
        var gap = now - lastKeyAt;

        if (event.key === 'Enter') {
            var payload = buffer;
            var fast = payload.length >= MIN_LENGTH &&
                       gap < MAX_GAP_MS &&
                       (now - startedAt) <= payload.length * MAX_AVERAGE_MS;
            lastKeyAt = now;
            if (!fast) {
                forget();
                return;
            }
            // Это сканер: Enter не должен ни отправить форму, ни дойти
            // до чужих обработчиков на документе
            event.preventDefault();
            event.stopPropagation();
            // Поле возвращается в прежний вид **до** очистки состояния:
            // снимок хранится там же, и forget() стёр бы его вместе
            // с набором — обрывки кода остались бы в поле
            restoreSnapshot();
            forget();
            submit(payload);
            return;
        }

        if (event.key && event.key.length === 1) {
            if (!buffer.length || gap >= MAX_GAP_MS) {
                buffer = event.key;
                startedAt = now;
                takeSnapshot();
            } else {
                buffer += event.key;
            }
            lastKeyAt = now;
            clearTimeout(idleTimer);
            idleTimer = setTimeout(forget, IDLE_MS);
            return;
        }

        // Любая другая клавиша (Tab, стрелки, Backspace) — набор не машинный
        if (event.key !== 'Shift' && event.key !== 'CapsLock') forget();
        lastKeyAt = now;
    }

    // Перехват на погружении: обработчики страниц (выбор детали, формы)
    // висят на документе, и Enter от сканера не должен до них добираться
    document.addEventListener('keydown', onKeyDown, true);

    window.LiftTeamScanner = {
        register: register,
        decode: decode,
        submit: submit,
        report: report,
        isMuted: function () { return muted; },
        setMuted: setMuted,
        kindLabel: function (kind) { return KIND_LABELS[kind] || kind; }
    };
})();
