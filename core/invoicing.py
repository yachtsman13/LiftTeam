"""Общий интерфейс к банкам, через которые выставляются счета.

Зачем он. Бухгалтеров двое, и счета они выставляют из разных банков:
у одного счёт в Т-Банке, у другого в Точке. Это два разных юрлица,
а не одна фирма с двумя расчётными счетами, — значит, различаются
и реквизиты в самом счёте. Поэтому всё, что стоит выше банка (форма,
представление, заказ), знает только про «провайдера» и не знает,
с каким именно банком имеет дело.

Устройство. Разговор с банком остался там, где и был: `core/tbank.py`
и `core/tochka.py` — модули с функциями, каждый со своей схемой запроса
и своими настройками. Здесь — тонкие обёртки над ними, приводящие
их к одним и тем же именам методов и к одному типу ошибки
`InvoiceError`. Логику банков этот модуль не содержит и содержать
не должен: иначе особенности одного начнут протекать в другой.

Чего здесь нет. Проверки подлинности уведомлений об оплате: приём
вебхуков живёт отдельно, в `core/webhooks.py`, и устроен своим набором
классов — у него другая сторона разговора (банк обращается к нам,
а не мы к банку) и другой набор настроек. Смешивать их в одном
интерфейсе значило бы, что «провайдер настроен» начнёт означать
две разные вещи сразу.

Позиции счёта. На входе у всех провайдеров один вид, тот, что
возвращает `RepairOrder.invoice_items()`:

    {'name': …, 'price': 54000.0, 'unit': 'шт.', 'vat': 'None', 'amount': 1}

Перевод в имена полей конкретного банка — забота его модуля.
"""
from django.conf import settings

TBANK = 'tbank'
TOCHKA = 'tochka'

PROVIDER_CHOICES = [
    (TBANK, 'Т-Банк'),
    (TOCHKA, 'Точка Банк'),
]

# Провайдер, который подставляется, если у сотрудника банк не выбран.
# Т-Банк, потому что до появления второго банка он был единственным,
# и все прежние счета выставлены через него
DEFAULT_PROVIDER = TBANK


class InvoiceError(Exception):
    """Счёт выставить не удалось. Текст пригоден для показа бухгалтеру.

    Один тип на все банки: представлению незачем знать, чей именно отказ
    оно поймало, — оно всё равно покажет текст человеку и запишет его
    в заказ.
    """


class InvoiceProvider:
    """Банк, через который выставляется счёт.

    Наследники не хранят состояния: настройки читаются из `settings`
    при каждом обращении, как и во всей остальной программе. Поэтому
    экземпляр можно создавать когда угодно и сколько угодно раз,
    а `override_settings` в тестах действует сразу.
    """

    code = ''
    label = ''
    # Имя переменной .env, которой включается выставление счетов. Стоит
    # на странице счёта: без него «выключено в настройках» означает
    # «ищите сами, где именно»
    enable_setting = ''

    def __str__(self):
        return self.label

    # --- Настроен ли банк ---

    def is_configured(self):
        """Заданы ли токен и всё прочее, без чего обращаться некуда."""
        raise NotImplementedError

    def invoice_enabled(self):
        """Разрешено ли выставлять счета.

        Отдельно от `is_configured`: счёт уходит заказчику от лица фирмы,
        и включаться сам собой после обновления он не должен.
        """
        raise NotImplementedError

    def missing_settings(self):
        """Список незаполненных настроек — чтобы сказать, что чинить."""
        raise NotImplementedError

    # --- Счёт ---

    def build_invoice(self, number, items, payer=None, emails=(),
                      invoice_date=None, due_date=None):
        """Тело запроса. Без сети: его показывают человеку до отправки."""
        raise NotImplementedError

    def send_invoice(self, payload, emails=()):
        """Отправляет счёт в банк. Возвращает ответ банка как есть."""
        raise NotImplementedError

    def invoice_pdf_url(self, response):
        """Ссылка на PDF счёта. Пусто — банк её не даёт."""
        raise NotImplementedError

    def external_id(self, response):
        """Идентификатор счёта в банке. Пусто — банк его не возвращает."""
        return ''

    def payment_status(self, external_id):
        """Статус оплаты счёта по его идентификатору в банке."""
        raise NotImplementedError


class TBankProvider(InvoiceProvider):
    """Т-Банк. Обёртка над `core/tbank.py`, логику которого не трогаем.

    Всё, что уходит в банк, собирает и отправляет прежний модуль теми же
    вызовами и с тем же телом запроса, что и до появления провайдеров.
    Здесь только приведение имён и перевод `TBankError` в `InvoiceError`.

    Номер расчётного счёта берётся из TBANK_ACCOUNT, а не из карточки
    юрлица: менять состав запроса к Т-Банку в этой задаче нельзя, и счёт,
    по которому банк рисует документ, он и так знает сам.
    """

    code = TBANK
    label = 'Т-Банк'
    enable_setting = 'TBANK_INVOICE_ENABLED'

    @property
    def _api(self):
        # Ввозится внутри, а не наверху модуля: так `core/tbank.py`
        # остаётся ни от чего здесь не зависящим
        from . import tbank
        return tbank

    def is_configured(self):
        return self._api.is_configured()

    def invoice_enabled(self):
        return self._api.invoice_enabled()

    def missing_settings(self):
        return [] if self._api.is_configured() else ['TBANK_TOKEN']

    def build_invoice(self, number, items, payer=None, emails=(),
                      invoice_date=None, due_date=None):
        try:
            return self._api.build_invoice(
                number=number, items=items, payer=payer, emails=emails,
                invoice_date=invoice_date, due_date=due_date,
            )
        except self._api.TBankError as exc:
            raise InvoiceError(str(exc)) from exc

    def send_invoice(self, payload, emails=()):
        # Т-Банк рассылает счёт сам: адреса уже лежат в теле запроса
        # полем contacts, отдельного метода отправки у него нет
        try:
            return self._api.send_invoice(payload)
        except self._api.TBankError as exc:
            raise InvoiceError(str(exc)) from exc

    def invoice_pdf_url(self, response):
        return self._api.invoice_pdf_url(response)

    def external_id(self, response):
        # Нужен, чтобы уведомление банка об оплате нашло заказ: в нём
        # приезжает только идентификатор счёта. Имя поля в ответе
        # не подтверждено — подробности в core/tbank.py
        return self._api.invoice_external_id(response)

    def payment_status(self, external_id):
        raise InvoiceError(
            'Т-Банк не отдаёт статус счёта отдельным методом: оплата видна '
            'в выписке, раздел «Поступления»'
        )


class TochkaProvider(InvoiceProvider):
    """Точка Банк. Обёртка над `core/tochka.py`.

    Что в нём проверено по документации, а что нет — написано в шапке
    того модуля. Здесь важно одно: непроверенного он не додумывает,
    а отказывается с понятным текстом.
    """

    code = TOCHKA
    label = 'Точка Банк'
    enable_setting = 'TOCHKA_INVOICE_ENABLED'

    @property
    def _api(self):
        from . import tochka
        return tochka

    def is_configured(self):
        return self._api.is_configured()

    def invoice_enabled(self):
        return self._api.invoice_enabled()

    def missing_settings(self):
        return self._api.missing_settings()

    def build_invoice(self, number, items, payer=None, emails=(),
                      invoice_date=None, due_date=None):
        try:
            return self._api.build_invoice(
                number=number, items=items, payer=payer,
                invoice_date=invoice_date, due_date=due_date,
            )
        except self._api.TochkaError as exc:
            raise InvoiceError(str(exc)) from exc

    def send_invoice(self, payload, emails=()):
        """Выставляет счёт и, если есть кому, отправляет его почтой.

        У Точки это два метода: создание счёта письма не шлёт. Если письмо
        не ушло, счёт всё равно выставлен — об этом говорим отдельно,
        а не выдаём отказ за «счёт не выставлен».
        """
        api = self._api
        try:
            answer = api.send_invoice(payload)
        except api.TochkaError as exc:
            raise InvoiceError(str(exc)) from exc

        document = api.document_id(answer)
        failed = []
        for email in emails or ():
            if not email:
                continue
            try:
                api.send_invoice_to_email(document, email)
            except api.TochkaError as exc:
                failed.append(f'{email}: {exc}')
        if failed:
            answer = dict(answer)
            answer['emailErrors'] = failed
        return answer

    def invoice_pdf_url(self, response):
        return self._api.invoice_pdf_url(response)

    def external_id(self, response):
        return self._api.document_id(response)

    def payment_status(self, external_id):
        try:
            return self._api.payment_status(external_id)
        except self._api.TochkaError as exc:
            raise InvoiceError(str(exc)) from exc


PROVIDERS = {
    TBANK: TBankProvider,
    TOCHKA: TochkaProvider,
}


def get_provider(code):
    """Провайдер по коду. Неизвестный код — ошибка, а не молчаливый Т-Банк.

    Молчаливая подстановка здесь означала бы счёт, выставленный не из того
    банка и не от того юрлица, — а это чужие реквизиты в документе,
    ушедшем заказчику.
    """
    provider = PROVIDERS.get(str(code or '').strip())
    if provider is None:
        raise InvoiceError(f'Неизвестный банк: «{code}»')
    return provider()


def provider_label(code):
    """Название банка для показа. Неизвестный код показываем как есть."""
    return dict(PROVIDER_CHOICES).get(code, code or '')


def default_provider_for(employee):
    """Какой банк подставить сотруднику в форму счёта.

    Только подсказка: поле на форме остаётся видимым и доступным для
    правки, потому что бухгалтер иногда выставляет счёт из чужого банка,
    подменяя коллегу.
    """
    chosen = getattr(employee, 'default_provider', '') or ''
    if chosen in PROVIDERS:
        return chosen
    return DEFAULT_PROVIDER


def due_days(code):
    """Через сколько дней счёт считается просроченным к оплате."""
    if code == TOCHKA:
        return int(getattr(settings, 'TOCHKA_INVOICE_DUE_DAYS', 14))
    return int(getattr(settings, 'TBANK_INVOICE_DUE_DAYS', 14))
