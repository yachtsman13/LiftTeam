"""
Модели данных для LiftTeam v2.59.0.
Сущности: Client, EquipmentModel, Equipment, FaultType, FaultTypePart, RepairOrder,
          RepairOrderEquipment, RepairOrderDetail, SparePart, StorageCell, StockMovement,
          StockAllocation, OrderCost, InventorySession, InventorySessionLine, Payment,
          Employee (User extension).
"""
import calendar
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.db.models import DecimalField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MinValueValidator
from django.utils import timezone

from . import invoicing


def warranty_months():
    """Срок гарантии в месяцах. 0 — гарантия не ведётся."""
    return getattr(settings, 'WARRANTY_MONTHS', 12)


def warranty_cutoff():
    """Дата, раньше которой завершённый ремонт уже не на гарантии.

    Условие «гарантия действует» удобнее проверять не по каждой единице,
    а одним сравнением в базе: заказ завершён не раньше этой даты.
    None — гарантия отключена (`WARRANTY_MONTHS = 0`).
    """
    months = warranty_months()
    if not months:
        return None
    return add_months(timezone.now(), -months)


def debt_overdue_days():
    """Через сколько дней после счёта долг считается просроченным."""
    return getattr(settings, 'DEBT_OVERDUE_DAYS', 14)


def order_overdue_days(status):
    """Порог просрочки (в днях) для статуса заказа — сколько можно провести
    в нём без движения, прежде чем это считается «зависшим».

    None — у статуса порога нет: `shipped` и `unrepairable` — завершённые
    состояния, не «зависшие».
    """
    return getattr(settings, 'ORDER_OVERDUE_DAYS', {}).get(status)


def format_amount(value):
    """Сумма с разделителями разрядов: «54 000».

    Пробел неразрывный: и в письме, и в печатном акте перенос строки
    посреди числа превращает 54 000 в «54» на одной строке и «000»
    на другой.
    """
    return f'{value:,.0f}'.replace(',', ' ')


def format_spec(value):
    """Значение характеристики без хвостовых нулей: «0.15», а не «0.150000».

    Поля характеристик — `DecimalField(decimal_places=6)`, и Django при
    записи дописывает значение нулями до шести знаков. Само число от этого
    не меняется, но «0.150000 А» в карточке и в форме читается как точность
    до микроампера, которой ни у кого нет. Убрать нули на уровне базы нельзя —
    Django выравнивает значение при каждой записи, поэтому приводим их
    к виду при показе.
    """
    if value is None:
        return ''
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    number = number.normalize()
    # normalize() у целых даёт «1E+2»: показатель уходит вправо от точки.
    # Возвращаем такие числа к обычной записи
    if number.as_tuple().exponent > 0:
        number = number.quantize(Decimal(1))
    return f'{number:f}'


def plural_genitive(word):
    """Родительный падеж множественного числа: «резистор» → «резисторов».

    Нужно ровно в одном месте — в заголовке этикетки на ячейку, где вместо
    перечисления одинаковых деталей пишется «Набор резисторов». Тип
    компонента вводит человек, готового списка типов в программе нет,
    поэтому форма выводится правилами, а не таблицей соответствий.

    Правила покрывают то, что реально пишут в поле «Тип компонента».
    Несклоняемые слова («реле») остаются как есть — «Набор реле» звучит
    верно и так.
    """
    word = (word or '').strip()
    if not word:
        return ''
    stem, last = word[:-1], word[-1].lower()
    if last in 'ое':           # реле, ядро — не склоняем, вернее так, чем «релей»
        return word
    if last == 'ь':            # предохранитель → предохранителей
        return stem + 'ей'
    if last == 'й':            # разъёмный случай: случай → случаев
        return stem + 'ев'
    if last == 'а':            # микросхема → микросхем
        return stem
    if last == 'я':            # батарея → батарей
        return stem + 'й'
    if last in 'жчшщ':         # дроссель уже выше, а нож → ножей
        return word + 'ей'
    return word + 'ов'         # резистор → резисторов, диод → диодов


def add_months(moment, months):
    """Дата через `months` месяцев (месяцы могут быть отрицательными).

    В стандартной библиотеке сложения месяцев нет, а `timedelta` считает
    только в днях: «год» превратился бы в 365 дней и в високосный год
    гарантия заканчивалась бы на день раньше срока. Если в конечном месяце
    нужного числа нет (31 марта минус месяц), берётся последний день месяца.
    """
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


class EmployeeManager(BaseUserManager):
    def create_user(self, username, full_name, password=None, **extra_fields):
        if not username:
            raise ValueError('Логин обязателен')
        user = self.model(username=username, full_name=full_name, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, full_name, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'admin')
        extra_fields.setdefault('is_active', True)
        return self.create_user(username, full_name, password, **extra_fields)


class Employee(AbstractBaseUser, PermissionsMixin):
    """Сотрудник / пользователь системы. Авторизация по логину (username)."""
    ROLE_CHOICES = [
        ('admin', 'Администратор'),
        ('warehouse', 'Кладовщик'),
        ('repair_manager', 'Менеджер по ремонту'),
        ('accountant', 'Бухгалтер'),
    ]

    username = models.CharField('Логин', max_length=150, unique=True)
    full_name = models.CharField('ФИО', max_length=255)
    email = models.EmailField('Email', blank=True)
    # Числовой идентификатор в MAX. Не логин и не телефон: бот видит человека
    # только по этому номеру и только после того, как тот сам ему написал.
    # Узнать его помогает команда `manage.py max_updates`.
    max_user_id = models.CharField('ID в MAX', max_length=32, blank=True)
    # То же самое для Telegram. Идентификатор другой: в Telegram и человек,
    # и группа — это chat_id, приставок вроде «user:» там нет.
    telegram_chat_id = models.CharField('ID в Telegram', max_length=32, blank=True)
    # Личный выбор канала внутренних оповещений (дефицит деталей,
    # задолженности). Глобальные NOTIFY_MAX/NOTIFY_TELEGRAM в .env включают
    # канал всей роли сразу, а эти три поля дополнительно фильтруют по
    # человеку. default=True сохраняет прежнее поведение для тех, кто ничего
    # не менял: получают все, у кого канал глобально включён и ID заполнен.
    notify_by_email = models.BooleanField('Оповещения на почту', default=True)
    notify_by_max = models.BooleanField('Оповещения в MAX', default=True)
    notify_by_telegram = models.BooleanField('Оповещения в Telegram', default=True)
    # Банк, из которого этот бухгалтер обычно выставляет счета. Бухгалтеров
    # двое, и банки у них разные, но подменять друг друга они могут —
    # поэтому это только подсказка: на форме счёта банк подставляется
    # отсюда и остаётся доступным для правки, а не прибивается намертво
    default_provider = models.CharField(
        'Банк по умолчанию', max_length=20, blank=True,
        choices=invoicing.PROVIDER_CHOICES,
        help_text='Подставляется в форму счёта. Поменять можно на самой форме.'
    )
    role = models.CharField('Роль', max_length=20, choices=ROLE_CHOICES, default='repair_manager')
    is_active = models.BooleanField('Активен', default=True)
    is_staff = models.BooleanField('Сотрудник', default=False)
    date_joined = models.DateTimeField('Дата регистрации', auto_now_add=True)
    # Присутствие. Отметку обновляет живое WebSocket-соединение браузера
    # (см. core/consumers.py::PresenceConsumer), а «в сети» — это функция
    # от неё и таймаута PRESENCE_TIMEOUT_SECONDS, а не отдельный флаг.
    # Флаг пришлось бы гасить при разрыве, а разрыв неотличим от заминки
    # в сети; к тому же значение в базе переживает перезапуск сервера,
    # и страница присутствия верна уже при первой загрузке.
    #
    # Обратная сторона: при закрытии браузера или выходе отметка нарочно
    # не сбрасывается, поэтому человек считается в сети ещё до истечения
    # таймаута, а затем показывается «был(а) в сети в ЧЧ:ММ». Это не сбой,
    # а плата за то, что короткий обрыв связи не гасит индикатор.
    last_seen = models.DateTimeField('Последняя активность', null=True, blank=True, db_index=True)

    objects = EmployeeManager()

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['full_name']

    class Meta:
        verbose_name = 'Сотрудник'
        verbose_name_plural = 'Сотрудники'
        ordering = ['full_name']

    def __str__(self):
        return f"{self.full_name} ({self.get_role_display()})"

    def has_perm(self, perm, obj=None):
        return self.is_superuser

    def has_module_perms(self, app_label):
        return self.is_superuser

    @staticmethod
    def presence_cutoff():
        """Граница «в сети»: отметки старше неё считаются устаревшими."""
        return timezone.now() - timedelta(seconds=settings.PRESENCE_TIMEOUT_SECONDS)

    @property
    def is_online(self):
        """Сотрудник считается в сети, пока отметка активности свежая.

        Одно определение на всех: страница присутствия, рассылка по сокету
        и тесты спрашивают именно его, чтобы «в сети» нигде не считалось
        по-своему.
        """
        if self.last_seen is None:
            return False
        return self.last_seen >= self.presence_cutoff()

    def touch_presence(self):
        """Отметить активность.

        Обновляем запросом к набору, а не save(): это дешевле, не поднимает
        сигналы модели и не затирает поля, которые в это же время правит
        кто-то другой (например, администратор в карточке сотрудника).
        """
        now = timezone.now()
        Employee.objects.filter(pk=self.pk).update(last_seen=now)
        self.last_seen = now


class Organization(models.Model):
    """Реквизиты своего юрлица — шапка печатных документов и счетов.

    Отдельная модель, а не строки в настройках, потому что менять их
    приходится владельцу — переехали, сменился директор, — а лазить для
    этого по SSH в файл настроек он не должен.

    С v2.50.0 записей может быть несколько. Причина: бухгалтеров двое,
    и работают они от разных юрлиц с разными банками. Одна из записей
    помечена основной (`is_default`) — она и стоит в печатных актах
    и в коммерческом предложении, как стояла единственная запись раньше.
    Выбор юрлица для печатных документов по каждому заказу отдельно
    пока не сделан: это отдельное решение владельца.

    Связь с банком — поле `provider`, по одному банку на юрлицо и не
    больше одного юрлица на банк. Именно по нему счёт, выставляемый
    через Точку, получает реквизиты второго ИП, а не первого ООО.
    """
    name = models.CharField('Полное название', max_length=255,
                            help_text='ООО «Название» — как в документах')
    inn = models.CharField('ИНН', max_length=20, blank=True)
    kpp = models.CharField('КПП', max_length=20, blank=True)
    address = models.CharField('Адрес', max_length=500, blank=True)
    phone = models.CharField('Телефон', max_length=100, blank=True)
    email = models.EmailField('Email', blank=True)
    # Кто подписывает акты. Должность отдельно от имени: в подписи она
    # печатается слева от фамилии
    signatory_position = models.CharField('Должность подписанта', max_length=100,
                                          blank=True, default='Директор')
    signatory_name = models.CharField('ФИО подписанта', max_length=255, blank=True)

    # Банковские реквизиты и ОГРН нужны шапке коммерческого предложения:
    # там они стоят по требованию делового обычая, а не для красоты —
    # по ним заказчик заводит вас в своей бухгалтерии
    ogrn = models.CharField('ОГРН / ОГРНИП', max_length=20, blank=True)
    # Город подписания документа. Отдельно от адреса: в бланке он стоит
    # слева от даты одним словом, а адрес — строкой в шапке
    city = models.CharField(
        'Город подписания', max_length=100, blank=True, default='Москва',
        help_text='Печатается слева от даты в коммерческом предложении'
    )
    bank_name = models.CharField('Банк', max_length=255, blank=True)
    bank_bik = models.CharField('БИК', max_length=20, blank=True)
    bank_account = models.CharField('Расчётный счёт', max_length=30, blank=True)
    corr_account = models.CharField('Корреспондентский счёт', max_length=30, blank=True)
    # «Без НДС, применяется УСН, ПСН» — строка налогового режима. Свободный
    # текст, а не выбор из списка: режимы совмещают, и формулировка своя
    tax_note = models.CharField(
        'Налоговый режим', max_length=255, blank=True, default='Без НДС, применяется УСН',
        help_text='Печатается под итогом коммерческого предложения'
    )

    # Через какой банк это юрлицо выставляет счета. Пусто — ни через какой:
    # такое юрлицо в печатных документах участвует, а в счетах нет.
    # Один банк — одно юрлицо: иначе по выбранному на форме банку нельзя
    # было бы понять, чьи реквизиты ставить в счёт
    provider = models.CharField(
        'Банк для счетов', max_length=20, blank=True,
        choices=invoicing.PROVIDER_CHOICES,
        help_text='Через какой банк это юрлицо выставляет счета. '
                  'Один банк можно закрепить только за одним юрлицом.'
    )
    # Основное юрлицо — то, что печатается в актах и предложении. Ровно
    # одно: «основных» два — это два разных бланка на один заказ
    is_default = models.BooleanField(
        'Основное юрлицо', default=False,
        help_text='Его реквизиты стоят в актах и коммерческом предложении'
    )

    class Meta:
        verbose_name = 'Юрлицо'
        verbose_name_plural = 'Юрлица'
        ordering = ['-is_default', 'name']
        constraints = [
            # Один банк — не больше чем у одного юрлица. Пустое значение
            # под ограничение не попадает: юрлиц без банка может быть сколько
            # угодно
            models.UniqueConstraint(
                fields=['provider'], condition=~models.Q(provider=''),
                name='unique_organization_per_provider',
            ),
        ]

    def __str__(self):
        return self.name or 'Реквизиты не заполнены'

    def save(self, *args, **kwargs):
        """Следит за тем, чтобы основное юрлицо было ровно одно.

        Иначе «основное» перестаёт что-либо значить, и в акт попадает
        то юрлицо, которое первым вернула база.
        """
        super().save(*args, **kwargs)
        if self.is_default:
            type(self).objects.exclude(pk=self.pk).filter(
                is_default=True).update(is_default=False)
        elif not type(self).objects.filter(is_default=True).exists():
            # Ни одного основного не осталось — им становится эта запись.
            # Печатать документы без шапки хуже, чем печатать с чужой
            type(self).objects.filter(pk=self.pk).update(is_default=True)
            self.is_default = True

    @classmethod
    def get_solo(cls):
        """Основное юрлицо — то, чьи реквизиты идут в печатные документы.

        Имя осталось прежним: до v2.50.0 запись была одна, и все печатные
        формы зовут её так. Создаётся пустой, если её ещё нет: печатать
        документы можно и с незаполненной шапкой — хуже, чем не напечатать
        вовсе, это не будет.
        """
        organization = cls.objects.filter(is_default=True).first() or cls.objects.first()
        if organization is None:
            organization = cls.objects.create(is_default=True)
        return organization

    @classmethod
    def for_provider(cls, code):
        """Юрлицо, закреплённое за этим банком, или None.

        None здесь — не поломка, а рабочее состояние: банк настроен,
        а карточку юрлица к нему ещё не завели. Бухгалтеру об этом
        говорят словами, счёт не выставляется.
        """
        if not code:
            return None
        return cls.objects.filter(provider=code).first()

    @property
    def is_filled(self):
        return bool(self.name)

    @property
    def details_line(self):
        """Реквизиты одной строкой — для шапки документа."""
        parts = []
        if self.inn:
            parts.append(f'ИНН {self.inn}')
        if self.kpp:
            parts.append(f'КПП {self.kpp}')
        if self.address:
            parts.append(self.address)
        if self.phone:
            parts.append(f'тел. {self.phone}')
        return ', '.join(parts)

    @property
    def registration_line(self):
        """ИНН и ОГРН одной строкой — вторая строка шапки предложения."""
        parts = []
        if self.inn:
            parts.append(f'ИНН {self.inn}')
        if self.kpp:
            parts.append(f'КПП {self.kpp}')
        if self.ogrn:
            parts.append(f'ОГРН/ОГРНИП {self.ogrn}')
        return ' '.join(parts)

    @property
    def bank_lines(self):
        """Банковские реквизиты — по строке на печатную строку.

        Список, а не одна строка: в шапке они стоят двумя строками,
        и склеивать их, чтобы потом резать в шаблоне, незачем.
        """
        lines = []
        first = []
        if self.bank_name:
            first.append(self.bank_name)
        if self.bank_bik:
            first.append(f'БИК {self.bank_bik}')
        if first:
            lines.append(' '.join(first))

        second = []
        if self.bank_account:
            second.append(f'Р/с {self.bank_account}')
        if self.corr_account:
            second.append(f'К/с {self.corr_account}')
        if second:
            lines.append('  '.join(second))
        return lines


class Client(models.Model):
    """Заказчик."""
    name = models.CharField('Название', max_length=255)
    inn = models.CharField('ИНН', max_length=20, blank=True)
    kpp = models.CharField('КПП', max_length=20, blank=True)
    contact_person = models.CharField('Контактное лицо', max_length=255, blank=True)
    phone = models.CharField('Телефон', max_length=50, blank=True)
    email = models.EmailField('Email', blank=True)
    # Адрес нужен коммерческому предложению: строка «Кому:» без него
    # выглядит как записка, а не как документ, отправляемый организации
    address = models.CharField('Адрес', max_length=500, blank=True)

    class Meta:
        verbose_name = 'Заказчик'
        verbose_name_plural = 'Заказчики'
        ordering = ['name']

    def __str__(self):
        return self.name


class EquipmentType(models.Model):
    """Тип оборудования — справочник: «Привод дверей», «Преобразователь
    частоты», «Источник бесперебойного питания».

    Тип принадлежит модели, а не отдельной единице: любой EkoDrive 2.0 —
    преобразователь частоты, в какой бы коробке он ни приехал. Поэтому
    ссылка на тип стоит у `EquipmentModel`, а не у `Equipment`.

    Отдельная таблица, а не строка в модели: на тип вешается прайс
    заказчика, а по свободному тексту цену не найти — «привод дверей»
    и «Привод дверей» стали бы разными позициями.

    С родовым названием `EquipmentModel.kind` не пересекается и его
    не заменяет: `kind` — приставка к названию модели в акте дефектации,
    которую оставляют пустой, если родовое слово уже стоит в самом
    названии. Тип в документы не идёт вовсе.
    """
    name = models.CharField('Название типа', max_length=255, unique=True)
    description = models.TextField('Описание', blank=True)

    class Meta:
        verbose_name = 'Тип оборудования'
        verbose_name_plural = 'Типы оборудования'
        ordering = ['name']

    def __str__(self):
        return self.name


def available_equipment_for_order(exclude_order=None):
    """Оборудование, которое можно принять в заказ.

    Убрано то, что сейчас лежит в другом незакрытом заказе: один и тот же
    прибор не может одновременно стоять на двух верстаках, и предлагать
    его к приёму — значит звать завести путаницу.

    Отремонтированное и отгруженное в списке остаётся: оно у заказчика
    и приехать снова может. Именно ради этого история ремонтов и ведётся
    по единице — вернувшийся прибор надо выбрать из справочника, а не
    заводить заново.

    `exclude_order` — заказ, который сейчас правят: его собственные
    единицы обязаны остаться в списке, иначе форма потеряла бы то,
    что в ней уже выбрано.
    """
    busy = RepairOrderEquipment.objects.filter(
        repair_order__status__in=RepairOrder.OPEN_STATUSES
    )
    if exclude_order is not None:
        busy = busy.exclude(repair_order=exclude_order)
    return Equipment.objects.exclude(pk__in=busy.values('equipment_id'))


class PriceList(models.Model):
    """Прайс на ремонт: базовый или для одного заказчика.

    Базовый — ровно один, у него не заполнен заказчик. Он и есть ответ
    на вопрос «сколько это стоит вообще»; прайс заказчика лишь уточняет
    его там, где с этим заказчиком договорились иначе. Поэтому строк
    в прайсе заказчика обычно немного, а не полная копия базового.

    Отдельной истории цен нет намеренно: цена, о которой договорились
    по конкретному ремонту, замораживается в самом заказе
    (`RepairOrderEquipment.list_price`). Прайс — это «сколько стоит
    сегодня», а не «сколько стоило в марте»: второе нужно ровно там,
    где уже записано.
    """
    client = models.OneToOneField(
        'Client', on_delete=models.CASCADE, null=True, blank=True,
        related_name='price_list', verbose_name='Заказчик',
        help_text='Пусто — это базовый прайс, один на всю программу.'
    )
    note = models.TextField('Примечание', blank=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Прайс'
        verbose_name_plural = 'Прайсы'
        ordering = ['client__name']

    def __str__(self):
        return f'Прайс {self.client.name}' if self.client_id else 'Базовый прайс'

    @property
    def is_base(self):
        return self.client_id is None

    def save(self, *args, **kwargs):
        # Базовый ровно один: второй такой же превратил бы поиск цены
        # в лотерею — какой из двух подхватится, зависело бы от порядка
        # записей. Проверка здесь, а не ограничением в базе: SQLite
        # считает все NULL разными, и обычная уникальность тут не работает.
        if self.client_id is None:
            twin = PriceList.objects.filter(client__isnull=True)
            if self.pk:
                twin = twin.exclude(pk=self.pk)
            if twin.exists():
                raise ValidationError('Базовый прайс уже есть, он один на всю программу')
        super().save(*args, **kwargs)

    @classmethod
    def base(cls):
        """Базовый прайс; заводится сам при первом обращении."""
        found = cls.objects.filter(client__isnull=True).first()
        return found or cls.objects.create()

    @classmethod
    def line_for(cls, client, equipment_type, complexity=''):
        """Строка прайса под этот тип и сложность — или None.

        Порядок: сначала прайс заказчика, потом базовый; внутри прайса —
        строка ровно под эту сложность, а если её нет, строка без
        сложности (общая для всех). Тот же приём, что и у рецепта
        деталей: уточнение вытесняет общее, но только там, где заведено.

        Возвращается строка, а не число: мастеру надо видеть, откуда
        цена — из его прайса или из базового.
        """
        if equipment_type is None:
            return None

        orders = [Q(price_list__client=client)] if client is not None else []
        orders.append(Q(price_list__client__isnull=True))
        for scope in orders:
            lines = PriceListLine.objects.filter(
                scope, equipment_type=equipment_type
            ).select_related('price_list__client')
            exact = lines.filter(complexity=complexity).first() if complexity else None
            if exact:
                return exact
            general = lines.filter(complexity='').first()
            if general:
                return general
        return None


class PriceListLine(models.Model):
    """Строка прайса: тип оборудования, сложность и цена.

    Сложность пустая — цена на любой ремонт этого типа. Заполненная —
    уточнение, которое вытесняет общую строку. Пустую заводить не
    обязательно: у типа может быть цена только на сложный ремонт,
    а на простой — из базового прайса.
    """
    price_list = models.ForeignKey(
        PriceList, on_delete=models.CASCADE, related_name='lines',
        verbose_name='Прайс'
    )
    equipment_type = models.ForeignKey(
        EquipmentType, on_delete=models.CASCADE, related_name='price_lines',
        verbose_name='Тип оборудования'
    )
    complexity = models.CharField(
        'Сложность', max_length=20, blank=True,
        choices=[('simple', 'Простой'), ('medium', 'Средний'), ('complex', 'Сложный')],
        help_text='Пусто — цена на любой ремонт этого типа.'
    )
    price = models.DecimalField(
        'Цена', max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal('0'))]
    )

    class Meta:
        verbose_name = 'Строка прайса'
        verbose_name_plural = 'Строки прайса'
        ordering = ['equipment_type__name', 'complexity']
        constraints = [
            models.UniqueConstraint(
                fields=['price_list', 'equipment_type', 'complexity'],
                name='unique_price_per_type_and_complexity',
            )
        ]

    def __str__(self):
        if self.complexity:
            return f'{self.equipment_type} ({self.get_complexity_display().lower()}): {self.price}'
        return f'{self.equipment_type}: {self.price}'


class EquipmentModel(models.Model):
    """Модель оборудования."""
    name = models.CharField('Название модели', max_length=255, unique=True)
    # Родовое название — «Преобразователь частоты», «Привод дверей». В списках
    # и на этикетках оно только съедает место, поэтому там печатается одно
    # name; но в акте дефектации первая строка — «Тип: <родовое> <модель>»,
    # и без него документ выглядит как записка для своих, а не как акт.
    kind = models.CharField(
        'Родовое название для акта', max_length=100, blank=True,
        help_text='Приставка к названию модели в акте дефектации: '
                  '«Преобразователь частоты», «Привод дверей». Если она уже '
                  'есть в начале названия модели, поле оставьте пустым. '
                  'Это не тип из справочника: тип в документы не идёт.'
    )
    # Пусто у всех моделей, заведённых до появления справочника, и остаётся
    # пустым, пока владелец не проставит тип руками. Придумывать типы за него
    # миграцией нельзя: «Преобразователь частоты Emotron» и «ПЧ Emotron» —
    # это про один тип, а машина увидит две разные строки.
    equipment_type = models.ForeignKey(
        EquipmentType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='models', verbose_name='Тип оборудования',
        help_text='Необязательно. По типу позже подбирается цена из прайса '
                  'заказчика.'
    )

    class Meta:
        verbose_name = 'Модель оборудования'
        verbose_name_plural = 'Модели оборудования'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def full_name(self):
        """Родовое название вместе с моделью — как пишут в акте дефектации."""
        if self.kind:
            return f'{self.kind} {self.name}'
        return self.name

    def materials_for(self, version=None):
        """Материалы модели для этого исполнения: схемы, инструкции, методики.

        Общие (без версии) **плюс** помеченные этим исполнением. Вытеснения
        здесь нет — и это намеренное отличие от рецепта деталей, где строка
        с версией заменяет общую. Там вытесняется строка **по той же
        детали**: два разных конденсатора на одно место — это ошибка.
        Здесь вытеснять нечего: схема исполнения и общая инструкция
        по настройке нужны обе, и спрятать вторую значило бы отправить
        мастера искать её мимо программы.
        """
        materials = self.materials.all()
        if version is None:
            return materials.filter(version__isnull=True)
        return materials.filter(Q(version__isnull=True) | Q(version=version))


class EquipmentVersion(models.Model):
    """Версия модели оборудования: EkoDrive 2.0-1.1 и EkoDrive 2.0-0.7.

    По сути одно и то же изделие, отличающееся исполнением — алюминиевый
    корпус вместо пластикового, большие кнопки, другое число конденсаторов
    в том же месте платы. Отдельной моделью такое заводить нельзя: тогда
    у каждой версии был бы свой список типовых неисправностей, а он общий.

    Обозначение печатается везде — в актах, на этикетке и в списках, —
    слитно с названием модели: «БУАД-7-31.4». Комментарий не печатается
    нигде: «алюминиевый корпус» нужен мастеру у стола, а не заказчику.
    """
    equipment_model = models.ForeignKey(
        EquipmentModel, on_delete=models.CASCADE, related_name='versions',
        verbose_name='Модель оборудования'
    )
    name = models.CharField(
        'Версия', max_length=100,
        help_text='Приписка к названию модели вместе с разделителем — так, '
                  'как она дописана на изделии: «.4» для БУАД-7-31.4, '
                  '«-1.1» для EcoDrive-2.3-1.1. Разделитель хранится здесь же: '
                  'на изделиях он произвольный, правилом его не вывести.'
    )
    note = models.TextField(
        'Комментарий', blank=True,
        help_text='Чем эта версия отличается: алюминиевый корпус, большие '
                  'кнопки. Справочно, в документы не идёт.'
    )

    class Meta:
        verbose_name = 'Версия модели'
        verbose_name_plural = 'Версии моделей'
        ordering = ['equipment_model__name', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['equipment_model', 'name'], name='unique_version_per_model'
            )
        ]

    def __str__(self):
        # Простое сложение, без своего разделителя: он хранится внутри
        # самого обозначения («.4», «-1.1»). Пока дефис стоял здесь, к нему
        # добавлялся ещё один из обозначения, и в списке выбора получалось
        # «БУАД-7-31-.4».
        return f'{self.equipment_model.name}{self.name}'


# Виды материалов — списком на уровне модуля, а не только внутри модели:
# по этому же порядку они и сортируются, а тело `class Meta` имён своего
# класса не видит. Порядок значимый: за столом первой открывают схему.
EQUIPMENT_MATERIAL_KINDS = [
    ('scheme', 'Схема'),
    ('manual', 'Инструкция'),
    ('method', 'Методика проверки'),
    ('other', 'Прочее'),
]


class EquipmentMaterial(models.Model):
    """Ссылка на материал по модели: схема, инструкция, методика проверки.

    Хранится **ссылка, а не файл**. Материалы лежат на Яндекс.Диске, где
    их и правят: схему подрисовывают, инструкцию дополняют, — и копия
    в программе разошлась бы с оригиналом молча. Плюс к тому схема
    в приличном разрешении весит десятки мегабайт, а программа живёт
    на Raspberry Pi с картой памяти.

    Ссылка любая, не только на Диск: попадаются и страницы производителя,
    и файлы в общей папке. Проверяется только то, что это ссылка.

    Материал висит на **модели**, потому что описывает прибор. Версия —
    уточнение: схема бывает своя у исполнения, а инструкция по настройке
    общая. Пусто — годится для всех исполнений.
    """
    KIND_CHOICES = EQUIPMENT_MATERIAL_KINDS

    equipment_model = models.ForeignKey(
        EquipmentModel, on_delete=models.CASCADE, related_name='materials',
        verbose_name='Модель оборудования'
    )
    # Пусто — материал общий для всех исполнений; указано — только для этого.
    # CASCADE: исполнение удалили — уточняющий его материал больше не о чем
    version = models.ForeignKey(
        EquipmentVersion, on_delete=models.CASCADE, null=True, blank=True,
        related_name='materials', verbose_name='Только для исполнения'
    )
    kind = models.CharField(
        'Что это', max_length=20, choices=KIND_CHOICES, default='other'
    )
    title = models.CharField('Название', max_length=255)
    url = models.URLField('Ссылка', max_length=500)
    note = models.CharField('Примечание', max_length=255, blank=True)

    class Meta:
        verbose_name = 'Материал по модели'
        verbose_name_plural = 'Материалы по моделям'
        # По виду в том порядке, в каком он объявлен, а не по алфавиту кода:
        # по алфавиту схема оказывалась последней, а за столом её открывают
        # первой. Порядок берётся из самого списка видов — второй такой
        # список рядом однажды разошёлся бы с ним.
        ordering = [
            models.Case(
                *[models.When(kind=code, then=models.Value(position))
                  for position, (code, _) in enumerate(EQUIPMENT_MATERIAL_KINDS)],
                default=models.Value(len(EQUIPMENT_MATERIAL_KINDS)),
                output_field=models.IntegerField(),
            ),
            'title',
        ]

    def __str__(self):
        if self.version_id:
            return f'{self.title} ({self.version.name})'
        return self.title

    def clean(self):
        # Исполнение чужой модели означало бы материал, который не покажется
        # никогда: отбор идёт от модели единицы
        if self.version_id and self.version.equipment_model_id != self.equipment_model_id:
            raise ValidationError({
                'version': 'Это исполнение другой модели.'
            })


class Equipment(models.Model):
    """Единица оборудования."""
    model = models.ForeignKey(EquipmentModel, on_delete=models.CASCADE, verbose_name='Модель')
    # Пусто — обычное дело: у большей части оборудования версий не бывает
    # вовсе, и выдумывать их, лишь бы поле было заполнено, не надо.
    # SET_NULL, а не CASCADE: удаление версии из справочника не должно
    # уносить с собой сами единицы оборудования.
    version = models.ForeignKey(
        EquipmentVersion, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='equipments', verbose_name='Версия'
    )
    # Считывается с коробки при приёме, если её видно. Пусто — значит
    # не разобрали или на корпусе её нет
    manufacture_date = models.DateField('Дата изготовления', null=True, blank=True)

    @property
    def version_suffix(self):
        """Приписка исполнения — «.4», «-1.1» — или пусто.

        Разделитель хранится внутри самого обозначения версии, поэтому
        здесь только приписывание, без своих правил: на изделиях
        разделители произвольные («БУАД-7-31.4», но «EcoDrive-2.3-1.1»),
        и вывести из них правило нельзя. Что завели в справочнике,
        то и печатается.

        Версии нет — не печатается ничего; «исп. 1.0» вместо пустого поля
        не выдумывается.
        """
        if self.version_id:
            return self.version.name
        return ''

    @property
    def designation(self):
        """Обозначение изделия одной строкой — «БУАД-7-31.4».

        Одно место на всю программу, как и с текстом неисправности:
        заказчик не должен получить в акте приёма одно название изделия,
        а в акте выполненных работ другое. Идёт в акт приёма, акт
        выполненных работ, на этикетку и в списки выбора.
        """
        return f'{self.model.name}{self.version_suffix}'

    @property
    def full_designation(self):
        """То же обозначение с родовым словом впереди — «Привод дверей
        БУАД-7-31.4».

        Нужно только акту дефектации: там изделие называют полностью.
        На этикетке и в остальных актах родового слова нет намеренно —
        оно съело бы место, а короткое обозначение и так узнают.
        """
        return f'{self.model.full_name}{self.version_suffix}'

    serial_number = models.CharField('Серийный номер', max_length=100, unique=True)
    # Серийник без регистра, пробелов и разделителей — чтобы найти «буад 1234»,
    # когда в базе лежит «БУАД-1234». Не unique: одинаковая нормализованная
    # форма — повод предупредить сотрудника, а не запретить сохранение;
    # решение, та же это единица или другая, остаётся за человеком.
    serial_normalized = models.CharField(
        'Серийный номер (для поиска)', max_length=100, blank=True, db_index=True, editable=False
    )
    current_client = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='Текущий заказчик'
    )

    class Meta:
        verbose_name = 'Оборудование'
        verbose_name_plural = 'Оборудование'
        ordering = ['serial_number']

    def __str__(self):
        # Обозначение вместе с исполнением, а не голое название модели:
        # в списке выбора два EkoDrive разных исполнений иначе неразличимы.
        return f"{self.designation} — {self.serial_number}"

    @staticmethod
    def normalize_serial(value):
        """Приводит серийный номер к виду, пригодному для сравнения.

        Убирает всё, кроме букв и цифр, и поднимает регистр. Серийники
        вводят вручную разные люди в разное время, поэтому «БУАД-1234»,
        «буад 1234» и «BUAD_1234» встречаются как записи одной и той же
        физической единицы. Само введённое значение при этом не меняется —
        оно печатается на этикетке и должно выглядеть так, как его набрали.
        """
        return ''.join(ch for ch in (value or '') if ch.isalnum()).upper()

    @classmethod
    def find_similar(cls, serial_number, exclude_pk=None):
        """Единицы с таким же серийником по нормализованному сравнению."""
        normalized = cls.normalize_serial(serial_number)
        if not normalized:
            return cls.objects.none()
        found = cls.objects.filter(serial_normalized=normalized)
        if exclude_pk is not None:
            found = found.exclude(pk=exclude_pk)
        return found.select_related('model')

    def save(self, *args, **kwargs):
        self.serial_normalized = self.normalize_serial(self.serial_number)
        if 'update_fields' in kwargs and kwargs['update_fields'] is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'serial_normalized'}
        super().save(*args, **kwargs)

    def repair_history(self):
        """Заказы, в которых участвовала эта единица, от новых к старым."""
        return (
            RepairOrderEquipment.objects
            .filter(equipment=self)
            .select_related('repair_order', 'repair_order__client')
            .order_by('-repair_order__date_received')
        )

    def active_warranty(self, exclude_order_id=None):
        """Последний ремонт этой единицы, гарантия на который ещё действует.

        Возвращает `RepairOrderEquipment` или None. Заказ, из которого
        смотрят, исключается: на странице самого заказа интересно, была ли
        единица на гарантии по прошлому ремонту, а не по текущему.
        """
        found = Equipment.warranty_map([self], exclude_order_id=exclude_order_id)
        return found.get(self.pk)

    @staticmethod
    def warranty_map(equipments, exclude_order_id=None):
        """{id единицы: действующий гарантийный ремонт} — одним запросом.

        Для списка оборудования проверять гарантию поштучно нельзя: это
        запрос на строку таблицы. Условие «гарантия ещё действует»
        разворачивается в «заказ завершён не раньше, чем N месяцев назад»,
        и такой отбор целиком делает база.
        """
        cutoff = warranty_cutoff()
        ids = [eq.pk if hasattr(eq, 'pk') else eq for eq in equipments]
        if cutoff is None or not ids:
            return {}

        visits = (
            RepairOrderEquipment.objects
            .filter(
                equipment_id__in=ids,
                repair_order__date_completed__isnull=False,
                repair_order__date_completed__gte=cutoff,
            )
            .select_related('repair_order')
            .order_by('repair_order__date_completed')
        )
        if exclude_order_id is not None:
            visits = visits.exclude(repair_order_id=exclude_order_id)

        # Порядок по возрастанию даты: последняя запись по единице затирает
        # предыдущие, и в словаре остаётся самый свежий ремонт
        return {visit.equipment_id: visit for visit in visits}


class FaultType(models.Model):
    """Типовая неисправность конкретной модели оборудования.

    Неисправности и набор деталей для их устранения повторяются в рамках
    одной модели — вместо того чтобы каждый раз вписывать список деталей
    заново, инженер один раз заводит здесь «рецепт» (см. `FaultTypePart`)
    и потом применяет его в заказе. Применение только предлагает список —
    сам рецепт при этом не меняется, см. `FaultTypePart` и
    `RepairOrderEquipment.faults`.
    """
    equipment_model = models.ForeignKey(
        EquipmentModel, on_delete=models.CASCADE, related_name='fault_types',
        verbose_name='Модель оборудования'
    )
    # Короткое рабочее название — «высохли конденсаторы». Им пользуются
    # сотрудники в списках и при выборе. В документ оно не попадает
    # НИКОГДА и ни при каких условиях: заказчик читает не цеховой жаргон,
    # а описание. Единственный текст для документов — description.
    name = models.CharField(
        'Неисправность', max_length=255,
        help_text='Коротко и по-рабочему, для списков. В документы это '
                  'название не попадает.'
    )
    description = models.TextField(
        'Описание для документов', blank=True,
        help_text='Полная формулировка, которая печатается в акте '
                  'дефектации и в коммерческом предложении. Пусто — '
                  'неисправность в документы не попадёт вовсе: короткое '
                  'название вместо описания не подставляется.'
    )
    # Свойство самой неисправности, а не единицы: замена высохшего
    # конденсатора проста на любом приборе, а прошивка процессора сложна
    # на любом. Сложность единицы из этого выводится (см.
    # `RepairOrderEquipment.derived_complexity`).
    complexity = models.CharField(
        'Сложность ремонта', max_length=20, default='simple',
        choices=[('simple', 'Простой'), ('complex', 'Сложный')]
    )

    class Meta:
        verbose_name = 'Типовая неисправность'
        verbose_name_plural = 'Типовые неисправности'
        ordering = ['equipment_model__name', 'name']

    def __str__(self):
        return f'{self.name} ({self.equipment_model.name})'

    @property
    def document_text(self):
        """Текст этой неисправности для документа — только описание.

        Пустое описание даёт пустую строку, и в документ не попадает
        ничего: подставить сюда `name` было бы ровно тем, что запрещено.
        """
        return self.description.strip()

    def recipe_lines(self, version=None):
        """Строки рецепта для указанной версии модели.

        Строка без версии — общая, она применяется к любому исполнению.
        Строка с версией — уточнение для него же: с какой-то версии тех же
        конденсаторов надо не три, а пять. Уточнение вытесняет общую строку
        по этой детали и только по ней; остальные детали берутся общими.

        Никакого наследования между версиями нет и быть не должно:
        применимость проставляется руками, и угадывать, какая версия
        похожа на какую, программа не будет.

        `version` — объект версии или её номер; None означает «версия
        неизвестна», и тогда действует только общая часть рецепта.
        """
        version_id = getattr(version, 'pk', version)
        base = {}
        override = {}
        order_of_appearance = []

        for line in self.parts.select_related('part'):
            if line.version_id is None:
                target = base
            elif version_id is not None and line.version_id == version_id:
                target = override
            else:
                continue
            if line.part_id not in base and line.part_id not in override:
                order_of_appearance.append(line.part_id)
            target[line.part_id] = line

        return [override.get(part_id) or base[part_id] for part_id in order_of_appearance]


class FaultTypePart(models.Model):
    """Строка рецепта: деталь и её типовое количество для одной неисправности.

    Деталь связана строкой, а не парой (неисправность, деталь) в самой
    `FaultType` — неисправностей с несколькими деталями в рецепте
    большинство, отсюда и отдельная модель, по образцу `RepairOrderDetail`.
    """
    fault_type = models.ForeignKey(
        FaultType, on_delete=models.CASCADE, related_name='parts',
        verbose_name='Неисправность'
    )
    # Строкой вперёд: SparePart объявлена ниже по файлу
    part = models.ForeignKey('SparePart', on_delete=models.CASCADE, verbose_name='Деталь')
    quantity = models.IntegerField('Типовое количество', validators=[MinValueValidator(1)])
    # Пусто — строка общая для всех исполнений. Указана версия — строка
    # заменяет общую только для этого исполнения (см. `FaultType.recipe_lines`)
    version = models.ForeignKey(
        EquipmentVersion, on_delete=models.CASCADE, null=True, blank=True,
        related_name='fault_type_parts', verbose_name='Только для версии'
    )

    class Meta:
        verbose_name = 'Деталь в рецепте неисправности'
        verbose_name_plural = 'Детали в рецепте неисправности'

    def __str__(self):
        if self.version_id:
            return f'{self.part.name} x{self.quantity} ({self.version})'
        return f'{self.part.name} x{self.quantity}'


class RepairOrderQuerySet(models.QuerySet):
    """Отбор по долгам. Условия записаны здесь, а не в каждом отчёте:
    «должник» встречается на дашборде, в отчёте, в выгрузке и в напоминаниях,
    и расходиться эти четыре определения не должны."""

    def with_debt(self):
        """Заказы, по которым остались деньги.

        Неремонтопригодные исключены: по ним счёт не выставляют, и статус
        оплаты остаётся «не оплачен» просто потому, что его никто не менял —
        это не долг заказчика.
        """
        return self.filter(
            payment_status__in=['unpaid', 'partially_paid']
        ).exclude(status='unrepairable')

    def open(self):
        """Заказы в незавершённых статусах — ещё не «Отгружен» и не
        «Ремонт невозможен». Один и тот же список статусов нужен и здесь,
        и в проверке просроченных заказов, и в подсчёте текущей загрузки
        по инженерам."""
        return self.filter(status__in=RepairOrder.OPEN_STATUSES)

    def overdue(self, days=None):
        """Долги, по которым уже можно напоминать.

        Отсчёт от даты счёта, а не от приёма заказа: пока счёт не выставлен,
        требовать оплату не за что, и напоминание выглядело бы нелепо.
        """
        if days is None:
            days = debt_overdue_days()
        cutoff = timezone.localdate() - timedelta(days=days)
        return (
            self.with_debt()
            .filter(invoice_date__isnull=False, invoice_date__lte=cutoff)
            # Остаток после оплат — то, что реально можно требовать.
            # Нулевой остаток не долг: письмо «оплатите 0 ₽» позорнее, чем
            # отсутствие письма. В отчёте такой заказ остаётся видимым,
            # потому что это недозаполненная карточка, а не оплаченный ремонт.
            #
            # Суммы считаются подзапросами, а не двумя Sum по соединениям:
            # соединение с оплатами размножило бы строки стоимостей, и три
            # платежа превратили бы ремонт на 1000 в ремонт на 3000
            .annotate(
                cost_total=Coalesce(
                    Subquery(
                        RepairOrderEquipment.objects
                        .filter(repair_order=OuterRef('pk'))
                        .values('repair_order')
                        .annotate(total=Sum('repair_cost'))
                        .values('total')[:1]
                    ),
                    Value(Decimal('0')),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
                paid_total=Coalesce(
                    Subquery(
                        Payment.objects
                        .filter(repair_order=OuterRef('pk'))
                        .values('repair_order')
                        .annotate(total=Sum('amount'))
                        .values('total')[:1]
                    ),
                    Value(Decimal('0')),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
            )
            .filter(cost_total__gt=F('paid_total'))
        )

    def shipped_without_invoice(self):
        """Оборудование уехало, а счёт не выставлен.

        Это не долг заказчика, а недоделка своей же конторы, и попадает она
        в сводку именно поэтому: иначе такой заказ не виден нигде — в отчёте
        о задолженностях он есть, но ничем не выделен.
        """
        return self.with_debt().filter(invoice_date__isnull=True, status='shipped')


class RepairOrder(models.Model):
    """Заказ на ремонт."""
    STATUS_CHOICES = [
        ('accepted', 'Принят'),
        ('diagnostic', 'Диагностика'),
        ('repair', 'Ремонт'),
        ('ready_for_shipment', 'Готов к отгрузке'),
        ('shipped', 'Отгружен'),
        ('unrepairable', 'Ремонт невозможен'),
    ]
    # Незавершённые статусы — заказ ещё «в работе». Используется и в проверке
    # просроченных заказов (SLA), и в подсчёте текущей загрузки по инженерам
    OPEN_STATUSES = ('accepted', 'diagnostic', 'repair', 'ready_for_shipment')
    PAYMENT_STATUS_CHOICES = [
        ('unpaid', 'Не оплачен'),
        ('partially_paid', 'Частично оплачен'),
        ('paid', 'Оплачен'),
    ]

    order_number = models.CharField('Номер заказа', max_length=30, unique=True, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, verbose_name='Заказчик')
    equipments = models.ManyToManyField(
        Equipment, through='RepairOrderEquipment',
        related_name='repair_orders', verbose_name='Оборудование'
    )
    date_received = models.DateTimeField('Дата приёма', auto_now_add=True)
    fault_description = models.TextField('Описание неисправности', blank=True)
    date_completed = models.DateTimeField('Дата завершения', null=True, blank=True)
    shipping_date = models.DateTimeField('Дата отгрузки', null=True, blank=True)
    tracking_number = models.CharField('Трек-номер', max_length=100, blank=True)
    shipping_company = models.CharField('Транспортная компания', max_length=100, blank=True)
    invoice_number = models.CharField('Номер счёта', max_length=50, blank=True)
    invoice_date = models.DateField('Дата счёта', null=True, blank=True)
    payment_status = models.CharField('Статус оплаты', max_length=20, choices=PAYMENT_STATUS_CHOICES, default='unpaid')
    status = models.CharField('Статус', max_length=20, choices=STATUS_CHOICES, default='accepted')

    # --- Счёт, выставленный через API банка ---
    # Номер и дата лежат в тех же invoice_number / invoice_date, что и у счёта,
    # выставленного руками: для долгов и напоминаний разницы нет, и заводить
    # им отдельные поля значило бы раздвоить понятие «счёт выставлен».
    #
    # До v2.50.0 эти три поля назывались tbank_invoice_*: банк был один.
    # Банков стало два, и приставка стала врать — какой именно банк выставил
    # счёт, теперь написано в invoice_provider
    invoice_sent_at = models.DateTimeField(
        'Счёт выставлен через банк', null=True, blank=True
    )
    invoice_pdf_url = models.URLField('Ссылка на PDF счёта', max_length=500, blank=True)
    invoice_error = models.CharField(
        'Последняя ошибка выставления', max_length=500, blank=True
    )
    # Какой банк выставил счёт. Из него же выводится юрлицо заказа —
    # см. legal_entity(): печатные акты и предложение должны быть от того
    # же лица, от которого выставлен счёт, иначе заказчик получает
    # документы от двух разных фирм по одной работе
    invoice_provider = models.CharField(
        'Банк счёта', max_length=20, blank=True, choices=invoicing.PROVIDER_CHOICES
    )
    # Идентификатор счёта в банке. Т-Банк его не возвращает, Точка
    # возвращает documentId — по нему из банка забирается PDF и статус
    invoice_external_id = models.CharField(
        'Идентификатор счёта в банке', max_length=100, blank=True
    )
    # Когда банк сам сообщил, что счёт оплачен (уведомление, см.
    # core/webhooks.py). Это НЕ оплата: суммы в уведомлении Т-Банка нет,
    # поэтому ни Payment, ни payment_status отметка не трогает — деньги
    # по-прежнему разносит бухгалтер по выписке. Отметка нужна затем,
    # чтобы было видно, о чём банк сообщил и когда
    invoice_paid_at = models.DateTimeField(
        'Банк сообщил об оплате счёта', null=True, blank=True
    )

    # --- Коммерческое предложение ---
    # Предложение делается по итогам дефектации: что нашли, во сколько
    # обойдётся и на каких условиях. Поля живут в заказе, а не в отдельной
    # сущности: документ описывает ровно те работы, что уже заведены
    # в заказе, и разъезжаться этим двум спискам незачем
    quote_subject = models.CharField(
        'Предмет предложения', max_length=255, blank=True,
        help_text='Продолжение заголовка: «на ремонт приводов дверей EkoDrive-2.3-1.3»'
    )
    quote_date = models.DateField(
        'Дата предложения', null=True, blank=True,
        help_text='Пусто — в документе встанет сегодняшнее число'
    )
    quote_valid_until = models.DateField('Действительно до', null=True, blank=True)
    quote_payment_terms = models.CharField(
        'Условия оплаты', max_length=255, blank=True,
        default='100% предоплата покупателем'
    )
    quote_delivery_terms = models.CharField(
        'Условия доставки', max_length=255, blank=True,
        default='до склада Покупателя включена в Предложение'
    )
    quote_lead_time = models.CharField(
        'Сроки выполнения, дней', max_length=50, blank=True, default='3-14',
        help_text='Ставится в каждой строке предложения: «3-14»'
    )

    objects = RepairOrderQuerySet.as_manager()

    class Meta:
        verbose_name = 'Заказ на ремонт'
        verbose_name_plural = 'Заказы на ремонт'
        ordering = ['-date_received']

    def __str__(self):
        return self.order_number

    @property
    def paid_amount(self):
        """Сколько денег по заказу уже поступило."""
        return self.payments.aggregate(total=Sum('amount'))['total'] or 0

    @property
    def debt(self):
        """Сколько осталось получить.

        Заказ со статусом «оплачен» долга не имеет, даже если суммы
        не вносили: статус ставят руками, и он главнее арифметики —
        иначе программа спорила бы с человеком, который видел платёжку.
        """
        if self.payment_status == 'paid':
            return 0
        remaining = self.total_repair_cost - self.paid_amount
        return remaining if remaining > 0 else 0

    def payment_status_from_payments(self):
        """Каким должен быть статус оплаты по внесённым суммам."""
        paid = self.paid_amount
        if paid <= 0:
            return 'unpaid'
        if paid >= self.total_repair_cost:
            return 'paid'
        return 'partially_paid'

    def refresh_payment_status(self):
        """Приводит статус в соответствие с оплатами. Возвращает новый статус,
        если он изменился, иначе None."""
        status = self.payment_status_from_payments()
        if status == self.payment_status:
            return None
        self.payment_status = status
        self.save(update_fields=['payment_status'])
        return status

    @property
    def days_overdue(self):
        """Сколько дней прошло с даты счёта. 0, если счёта нет."""
        if not self.invoice_date:
            return 0
        return max((timezone.localdate() - self.invoice_date).days, 0)

    @property
    def is_overdue(self):
        """Пора ли напоминать об оплате. То же условие, что в `overdue()`,
        но для одного заказа — в отчёте оно нужно построчно."""
        return bool(self.invoice_date) and self.days_overdue >= debt_overdue_days()

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = self.generate_order_number()
        super().save(*args, **kwargs)

    def generate_order_number(self):
        """Генерация номера заказа: LT-YYYY-MM-XXX с защитой от race condition.

        Три цифры вместо пяти — номер короче и лучше читается на этикетке.
        Это не жёсткий предел: при превышении 999 заказов за месяц номер просто
        станет четырёхзначным, ничего не сломается. Заказы, созданные до смены
        формата, сохраняют свои прежние номера — уникальность от этого
        не страдает, а сравнение строк продолжает находить последний номер
        верно, поскольку более длинная запись меньше более короткой
        с большей первой цифрой.
        """
        now = timezone.now()
        prefix = f"LT-{now.year:04d}-{now.month:02d}"
        with transaction.atomic():
            last = RepairOrder.objects.select_for_update().filter(
                order_number__startswith=prefix
            ).order_by('-order_number').first()
            if last:
                last_num = int(last.order_number.split('-')[-1])
                new_num = last_num + 1
            else:
                new_num = 1
            return f"{prefix}-{new_num:03d}"

    @property
    def total_repair_cost(self):
        """Общая стоимость ремонта по сумме стоимостей всех единиц оборудования в заказе."""
        total = self.order_equipments.aggregate(
            total=Sum('repair_cost')
        )['total']
        return total or 0

    @property
    def parts_cost(self):
        """Себестоимость деталей, списанных на этот заказ (сумма по партиям
        прихода, из которых они фактически списаны — см. `StockAllocation`).

        None — неизвестна целиком: хотя бы одна запись затрат по деталям
        (`OrderCost`, category='parts') не смогла посчитать сумму, потому
        что задействованная партия без цены или часть расхода осталась
        без партии (нехватка остатка на момент списания). Это не то же
        самое, что 0 — как и `SparePart.price`, пустая сумма не означает
        бесплатно.

        Заказов без единой записи `OrderCost` (детали не списывались, либо
        списание было до появления этого учёта — см. CHANGELOG) считается
        0, а не None: по ним достоверно известно, что запись затрат
        отсутствует, а не что она не смогла посчитать сумму.
        """
        amounts = list(self.costs.filter(category='parts').values_list('amount', flat=True))
        if not amounts:
            return Decimal('0')
        if any(amount is None for amount in amounts):
            return None
        return sum(amounts, Decimal('0'))

    @property
    def profit(self):
        """Прибыль по заказу: реально поступившие деньги (`paid_amount`,
        не выставленная/оценочная `total_repair_cost`) минус себестоимость
        списанных деталей. None — себестоимость неизвестна, см. `parts_cost`.
        """
        parts_cost = self.parts_cost
        if parts_cost is None:
            return None
        return self.paid_amount - parts_cost

    def assign_equipment_owners(self):
        """Проставить заказчика этого заказа владельцем его оборудования.

        Только тем единицам, у которых владельца ещё нет. **Непустого
        владельца не перезаписываем**: прибор может приехать от другого
        обслуживающего предприятия, и надо ли тогда менять владельца —
        вопрос к владельцу программы, он записан открытым в PLAN.md.
        Пока он не решён, молча менять чужую запись нельзя.

        Вызывается после сохранения состава заказа: оборудование заводят
        прямо из формы заказа, и там заказчик уже выбран — но выбран он
        может быть и позже, чем создана единица.

        Возвращает число проставленных.
        """
        if not self.client_id:
            return 0
        return Equipment.objects.filter(
            repairorderequipment__repair_order=self,
            current_client__isnull=True,
        ).update(current_client_id=self.client_id)

    @property
    def sole_equipment(self):
        """Единственная единица в заказе — или None, если их не одна.

        Когда прибор в заказе один, спрашивать «в какую железку ушла
        деталь» незачем: ответ очевиден, и лишний выбор только мешал бы
        мастеру. Когда их несколько, программа не угадывает — деталь
        остаётся общей по заказу, пока не укажут.
        """
        units = list(self.order_equipments.all()[:2])
        return units[0] if len(units) == 1 else None

    def quote_rows(self):
        """Строки коммерческого предложения — по единице оборудования.

        Единицы без цены не пропускаются, в отличие от счёта: предложение
        нередко составляют, когда часть позиций ещё не оценена, и потерять
        их молча хуже, чем напечатать без суммы. В документе на их месте
        стоит прочерк.
        """
        rows = []
        for order_equipment in self.order_equipments.select_related('equipment__model').order_by('id'):
            rows.append({
                'equipment': order_equipment.equipment,
                'name': order_equipment.quote_line,
                'serial_number': order_equipment.equipment.serial_number,
                # Проставленная руками сложность, а если её не проставляли —
                # выведенная из выбранных типовых неисправностей
                'complexity': order_equipment.effective_complexity_display,
                'price': order_equipment.quote_price,
            })
        return rows

    @property
    def quote_total(self):
        """Итог предложения. Позиции без цены в него просто не входят."""
        total = Decimal('0')
        for order_equipment in self.order_equipments.all():
            price = order_equipment.quote_price
            if price is not None:
                total += price
        return total

    @property
    def has_quote(self):
        """Заполняли ли предложение. Пустой бланк печатать незачем."""
        return bool(self.quote_subject or self.quote_date or self.quote_valid_until)

    @classmethod
    def next_invoice_number(cls):
        """Какой номер счёта предложить следующим.

        Считается по номерам, которые видела программа. Про счета,
        выставленные руками в личном кабинете банка, она не знает и знать
        не может — поэтому номер на странице выставления открыт для правки,
        а не подставлен молча. Банк номер не выдаёт: он приходит от нас
        и попадает в документ как есть.

        Ряд один на всю программу и при двух юрлицах: так решил владелец.
        Разводить ряды по юрлицам не надо — за сквозной нумерацией всё
        равно следит человек, и два ряда он свёл бы труднее одного.
        """
        biggest = 0
        numbers = cls.objects.exclude(invoice_number='').values_list(
            'invoice_number', flat=True
        )
        for number in numbers:
            # Только чисто числовые: «942-01» и «б/н» в сквозной ряд не встают,
            # и угадывать по ним следующий номер — значит угадывать неверно
            digits = str(number).strip()
            if digits.isdigit():
                biggest = max(biggest, int(digits))

        start = getattr(settings, 'TBANK_INVOICE_NUMBER_START', 1)
        return str(max(biggest + 1, start))

    def legal_entity(self):
        """От какого юрлица идут документы по этому заказу.

        Одно место на всю программу, а не по ветке в каждом печатном
        представлении: акты приёма, выполненных работ, дефектации и
        коммерческое предложение обязаны быть от того же лица, что и счёт.
        Иначе заказчик получает по одной работе документы от двух разных
        фирм — и вправе не принять ни те, ни другие.

        Порядок такой:

        1. выставлен счёт — берём юрлицо того банка, через который он
           выставлен;
        2. счёта нет (обычное состояние нового заказа и всех заказов
           до v2.50.0) — основное юрлицо, ровно как печаталось раньше;
        3. банк записан, а юрлица за ним нет — снова основное. Пустая
           шапка на печатном документе хуже, чем чужая: с чужой заметят
           и позовут, с пустой отправят заказчику как есть.
        """
        if self.invoice_provider:
            bound = Organization.for_provider(self.invoice_provider)
            if bound is not None:
                return bound
        return Organization.get_solo()

    def invoice_items(self):
        """Позиции счёта — по единице оборудования на строку.

        Формулировка повторяет ту, что стоит в счетах, выставленных руками:
        «Ремонт <тип и модель> SN:<серийник> (<что сделали>)». Единицы
        без стоимости пропускаются: строка счёта на ноль рублей — это
        не строка счёта.
        """
        unit = getattr(settings, 'TBANK_INVOICE_UNIT', 'шт.')
        vat = getattr(settings, 'TBANK_INVOICE_VAT', 'None')

        items = []
        for order_equipment in self.order_equipments.select_related('equipment__model'):
            price = order_equipment.repair_cost
            if price is None or price <= 0:
                continue
            items.append({
                'name': order_equipment.invoice_line,
                'price': float(price),
                'unit': unit,
                'vat': vat,
                'amount': 1,
            })
        return items


class Payment(models.Model):
    """Поступление денег по заказу.

    До этого «оплачено» было только статусом, и при частичной оплате долгом
    считалась вся стоимость ремонта: в отчёте и в напоминании заказчику
    стояла сумма, часть которой он уже перевёл.

    Отдельные записи, а не одно поле «оплачено»: деньги приходят частями
    и в разные дни, и при разговоре с заказчиком важно не только сколько,
    но и когда.
    """
    repair_order = models.ForeignKey(
        RepairOrder, on_delete=models.CASCADE,
        related_name='payments', verbose_name='Заказ'
    )
    amount = models.DecimalField(
        'Сумма', max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    payment_date = models.DateField('Дата поступления', default=timezone.localdate)
    note = models.CharField('Примечание', max_length=255, blank=True,
                            help_text='Платёжное поручение, наличные и т.п.')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='Кем внесено'
    )
    created_at = models.DateTimeField('Внесено', auto_now_add=True)

    class Meta:
        verbose_name = 'Оплата'
        verbose_name_plural = 'Оплаты'
        ordering = ['-payment_date', '-id']

    def __str__(self):
        return f'{self.amount} ₽ по заказу {self.repair_order.order_number}'


class BankOperation(models.Model):
    """Поступление из выписки Т-Банка.

    Хранится отдельно от `Payment` и не превращается в оплату само.
    Причин две. Первая: выписку тянут по расписанию, и разнести деньги
    не по тому заказу автоматом — ошибка, которую потом ищут неделю.
    Вторая: не всякий приход относится к заказу вообще — возвраты,
    переводы между своими счетами, поступления не за ремонт.

    Сотрудник видит поступление, программа подсказывает вероятный заказ,
    решение принимает человек.
    """
    STATUS_CHOICES = [
        ('new', 'Новое'),
        ('applied', 'Разнесено'),
        ('skipped', 'Не по заказам'),
    ]

    # Идентификатор операции в банке. Уникален — на нём держится вся защита
    # от повторного разнесения одних и тех же денег
    external_id = models.CharField('Идентификатор в банке', max_length=100, unique=True)
    operation_date = models.DateField('Дата операции', null=True, blank=True)
    amount = models.DecimalField('Сумма', max_digits=12, decimal_places=2)
    purpose = models.TextField('Назначение платежа', blank=True)
    counterparty = models.CharField('Плательщик', max_length=255, blank=True)
    counterparty_inn = models.CharField('ИНН плательщика', max_length=20, blank=True, db_index=True)
    document_number = models.CharField('Номер документа', max_length=50, blank=True)

    status = models.CharField('Состояние', max_length=20, choices=STATUS_CHOICES, default='new')
    payment = models.OneToOneField(
        Payment, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='bank_operation', verbose_name='Созданная оплата'
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='Кем обработано'
    )
    processed_at = models.DateTimeField('Когда обработано', null=True, blank=True)
    loaded_at = models.DateTimeField('Загружено', auto_now_add=True)

    class Meta:
        verbose_name = 'Поступление из банка'
        verbose_name_plural = 'Поступления из банка'
        ordering = ['-operation_date', '-id']

    def __str__(self):
        return f'{self.amount} ₽ от {self.counterparty or "неизвестно кого"}'

    @property
    def amount_text(self):
        return format_amount(self.amount)

    def guess_orders(self):
        """Заказы, к которым это поступление вероятно относится.

        Три признака, от надёжного к слабому: номер заказа в назначении
        платежа, номер счёта в нём же, ИНН плательщика. Совпадения
        не смешиваются — если нашлось по номеру заказа, ИНН уже не смотрим:
        иначе к точному попаданию примешались бы все прочие долги заказчика.
        """
        text = self.purpose or ''

        by_number = RepairOrder.objects.filter(
            order_number__in=_order_numbers_in(text)
        )
        if by_number.exists():
            return list(by_number.select_related('client'))

        invoices = _invoice_numbers_in(text)
        if invoices:
            by_invoice = RepairOrder.objects.exclude(invoice_number='').filter(
                invoice_number__in=invoices
            ).select_related('client')
            if by_invoice.exists():
                return list(by_invoice)

        if self.counterparty_inn:
            return list(
                RepairOrder.objects.with_debt()
                .filter(client__inn=self.counterparty_inn)
                .select_related('client').order_by('invoice_date', 'id')
            )
        return []


# Номер заказа — LT-2026-08-001, регистр в платёжке бывает любой
ORDER_NUMBER_RE = re.compile(r'\bLT-\d{4}-\d{2}-\d+\b', re.IGNORECASE)
# Номер счёта в назначении: «счет 942», «счёт № 942-01», «по сч. №942»
INVOICE_NUMBER_RE = re.compile(
    r'сч[её]?т?\.?\s*(?:№|N|#)?\s*([0-9][0-9\-/]*)', re.IGNORECASE
)


def _order_numbers_in(text):
    return [match.group(0).upper() for match in ORDER_NUMBER_RE.finditer(text)]


def _invoice_numbers_in(text):
    """Номера счетов из назначения платежа.

    Возвращает и найденное целиком, и обрезанное по разделителю: в счёте
    номер бывает «942», а в платёжке его пишут «942-01» — и наоборот.
    """
    found = []
    for match in INVOICE_NUMBER_RE.finditer(text):
        number = match.group(1).strip('-/')
        if not number:
            continue
        found.append(number)
        head = re.split(r'[-/]', number)[0]
        if head and head != number:
            found.append(head)
    return found


class RepairOrderEquipment(models.Model):
    """Оборудование в заказе на ремонт с индивидуальными полями."""
    repair_order = models.ForeignKey(
        RepairOrder, on_delete=models.CASCADE,
        related_name='order_equipments', verbose_name='Заказ'
    )
    equipment = models.ForeignKey(
        Equipment, on_delete=models.CASCADE, verbose_name='Оборудование'
    )
    fault_description = models.TextField('Описание неисправности', blank=True)
    # Типовые неисправности из справочника — необязательные и не заменяют
    # fault_description: свободный текст остаётся для случаев, которых
    # в справочнике ещё нет («не из списка»). Выбор отсюда лишь предлагает
    # типовой рецепт деталей (см. repair_order_apply_fault_template) — сам
    # список выбранных неисправностей ни на что другое не влияет.
    faults = models.ManyToManyField(
        FaultType, blank=True, related_name='order_equipments',
        verbose_name='Типовые неисправности'
    )
    # Что сделали — не то же самое, что было заявлено сломанным. В акте
    # выполненных работ должно стоять именно это; пока поля не было,
    # в акт попадала неисправность, то есть неправда в подписываемом документе
    work_performed = models.TextField('Выполненные работы', blank=True)
    seal_numbers = models.CharField('Номера пломб', max_length=255, blank=True)
    initial_condition = models.TextField('Начальное состояние', blank=True)
    repair_cost = models.DecimalField('Стоимость ремонта', max_digits=12, decimal_places=2, null=True, blank=True)
    yandex_disk_folder = models.URLField('Папка на Яндекс.Диске', blank=True)

    # --- Акт дефектации ---
    # Заполняется после диагностики, до того как заказчик решил, чинить ли.
    # Отдельно от fault_description: там записано, с чем привезли, здесь —
    # что нашли внутри, и подписывать эти две вещи одним текстом нельзя.
    defect_act_date = models.DateField(
        'Дата акта дефектации', null=True, blank=True,
        help_text='День диагностики. Пусто — в акте встанет сегодняшнее число.'
    )
    diagnosis = models.TextField(
        'Результаты диагностики', blank=True,
        help_text='Что вышло из строя. Печатается в акте дефектации.'
    )
    error_codes = models.TextField(
        'Коды ошибок', blank=True,
        help_text='По одному в строке, вместе с расшифровкой: '
                  '«F2340» — короткое замыкание в IGBT модуле.'
    )
    warranty_case = models.CharField(
        'Гарантийный случай', max_length=20, blank=True,
        choices=[('warranty', 'Гарантийный'), ('non_warranty', 'Не гарантийный')]
    )
    non_warranty_reason = models.TextField(
        'Причина, по которой случай не гарантийный', blank=True,
        help_text='Продолжение фразы «неисправность вызвана …».'
    )
    estimated_cost = models.DecimalField(
        'Ориентировочная стоимость ремонта', max_digits=12, decimal_places=2,
        null=True, blank=True,
        help_text='Оценка для заказчика по итогам дефектации. Не то же самое, '
                  'что стоимость ремонта: та ставится по факту.'
    )

    # --- Строка коммерческого предложения ---
    # Предлагаемые работы — не то же самое, что выполненные: первое пишут
    # до согласия заказчика, второе после ремонта. Одним полем не обойтись,
    # иначе предложение либо пустое, либо в нём стоит прошедшее время
    proposed_work = models.TextField(
        'Предлагаемые работы', blank=True,
        help_text='Что предлагаем сделать. Идёт в коммерческое предложение.'
    )
    repair_complexity = models.CharField(
        'Сложность ремонта', max_length=20, blank=True,
        choices=[('simple', 'Простой'), ('medium', 'Средний'), ('complex', 'Сложный')]
    )
    # Цена, которую предложил прайс в тот момент, когда назначали
    # стоимость. Хранится отдельно от согласованной (`estimated_cost`),
    # потому что мастер вправе её поправить, а знать потом надо обе:
    # по одной видно, о чём договорились, по другой — от чего отступили
    # и насколько. Отдельной истории цен при этом не заводится: цена
    # заморожена здесь, в самом заказе.
    list_price = models.DecimalField(
        'Цена по прайсу', max_digits=12, decimal_places=2, null=True, blank=True
    )

    class Meta:
        verbose_name = 'Оборудование в заказе'
        verbose_name_plural = 'Оборудование в заказе'

    def __str__(self):
        return f"{self.equipment} в {self.repair_order.order_number}"

    @property
    def has_defect_act(self):
        """Есть ли что печатать в акте дефектации."""
        return bool(self.diagnosis_document_text or self.error_codes or self.warranty_case)

    # --- Неисправности в документах ---
    # Короткое название неисправности (`FaultType.name`) не попадает
    # в документы никогда: в них идёт только полное описание
    # (`FaultType.description`). Ниже — единственное место, где текст
    # неисправностей для документа собирается; печатные формы зовут его,
    # а не перебирают неисправности сами.

    @property
    def fault_document_lines(self):
        """Полные описания выбранных типовых неисправностей.

        Неисправности без описания пропускаются молча: подставить вместо
        описания короткое название — ровно то, что запрещено, а печатать
        пустую строку незачем.
        """
        return [text for text in (fault.document_text for fault in self.faults.all()) if text]

    def document_fault_text(self, free_text=''):
        """Текст неисправности для документа: сначала полные описания
        выбранных типовых неисправностей, потом свободный текст мастера.

        Если типовых неисправностей не выбрано, возвращается ровно
        свободный текст — документ печатается так же, как печатался
        до появления справочных описаний.
        """
        parts = list(self.fault_document_lines)
        free_text = (free_text or '').strip()
        if free_text:
            parts.append(free_text)
        return '\n'.join(parts)

    @property
    def diagnosis_document_text(self):
        """Результаты диагностики для акта дефектации.

        Описания выбранных неисправностей, затем то, что мастер дописал
        руками в поле диагностики.
        """
        return self.document_fault_text(self.diagnosis)

    # --- Сложность ремонта ---
    # Поле `repair_complexity` осталось на месте и правится руками: мастер
    # видит прибор, а справочник — нет. Пустое поле означает не «простой»,
    # а «не задавали», и тогда сложность выводится из выбранных
    # неисправностей.

    @property
    def derived_complexity(self):
        """Сложность по выбранным неисправностям: '' — выводить не из чего.

        Достаточно одной сложной неисправности, чтобы весь ремонт единицы
        стал сложным: простую часть работы всё равно делает тот же человек
        за тем же столом, и лёгкой она от этого не становится.
        """
        faults = list(self.faults.all())
        if not faults:
            return ''
        if any(fault.complexity == 'complex' for fault in faults):
            return 'complex'
        return 'simple'

    @property
    def effective_complexity(self):
        """Что считать сложностью этой единицы: заданное руками, иначе
        выведенное из неисправностей."""
        return self.repair_complexity or self.derived_complexity

    @property
    def complexity_is_derived(self):
        """Сложность выведена из неисправностей, а не проставлена руками."""
        return not self.repair_complexity and bool(self.derived_complexity)

    @property
    def effective_complexity_display(self):
        """Название сложности для показа и для печати."""
        value = self.effective_complexity
        if not value:
            return ''
        names = dict(self._meta.get_field('repair_complexity').choices)
        return names.get(value, value)

    @property
    def error_code_lines(self):
        """Коды ошибок построчно — в акте это маркированный список."""
        return [line.strip() for line in self.error_codes.splitlines() if line.strip()]

    @property
    def estimated_cost_text(self):
        """Оценка для акта: «54 000». None — если оценки нет."""
        if self.estimated_cost is None:
            return None
        return format_amount(self.estimated_cost)

    @property
    def price_list_line(self):
        """Строка прайса под эту единицу — или None.

        Считается на лету: это «сколько стоит сегодня». То, о чём
        договорились по этому ремонту, лежит рядом в `list_price`
        и не меняется, сколько бы прайс потом ни правили.
        """
        return PriceList.line_for(
            self.repair_order.client,
            self.equipment.model.equipment_type,
            self.effective_complexity,
        )

    @property
    def suggested_price(self):
        """Что прайс предлагает за этот ремонт сейчас, или None."""
        line = self.price_list_line
        return line.price if line else None

    def freeze_list_price(self):
        """Запомнить цену прайса на момент назначения стоимости.

        Вызывается при сохранении дефектации: там назначают цену, и
        только там известно, от чего мастер отступил. Уже замороженную
        не переписываем — иначе правка прайса задним числом меняла бы
        то, о чём договорились.
        """
        if self.list_price is not None:
            return False
        price = self.suggested_price
        if price is None:
            return False
        self.list_price = price
        self.save(update_fields=['list_price'])
        return True

    @property
    def price_differs_from_list(self):
        """Согласованная цена отличается от прайсовой. Пусто у любой
        из двух означает «не с чем сравнивать», а не «совпадает»."""
        if self.list_price is None or self.estimated_cost is None:
            return False
        return self.estimated_cost != self.list_price

    @property
    def quote_price(self):
        """Цена этой единицы в коммерческом предложении.

        Оценка из дефектации, а если её нет — проставленная стоимость
        ремонта. Именно в таком порядке: предложение делается до ремонта,
        и оценка ближе к тому, что обсуждают с заказчиком.
        """
        if self.estimated_cost is not None:
            return self.estimated_cost
        return self.repair_cost

    @property
    def quote_line(self):
        """Наименование работ в предложении.

        Сначала полные описания выбранных типовых неисправностей, затем
        предлагаемые работы, если их записали; иначе выполненные (бывает,
        что предложение печатают задним числом). Если не выбрано и не
        записано ничего — просто «Ремонт <тип и модель>», без выдумок
        о том, чего никто не писал.
        """
        work = self.proposed_work.strip() or self.work_performed.strip()
        # Переводы строк в наименовании работ схлопываются, как и раньше:
        # это ячейка таблицы предложения, а не абзац
        work = ' '.join(work.split())
        # Описания выбранных типовых неисправностей идут перед текстом
        # мастера — заказчик читает в предложении ту же формулировку,
        # что и в акте дефектации
        text = self.document_fault_text(work)
        if text:
            return text
        return f'Ремонт {self.equipment.model.full_name}'

    @property
    def invoice_line(self):
        """Наименование позиции в счёте.

        «Ремонт Преобразователь частоты Emotron … SN:001272 (Замена IGBT
        модуля)» — так эта строка написана в счетах, выставленных руками,
        и заказчик сверяет её с актом слово в слово.
        """
        name = f'Ремонт {self.equipment.model.full_name} SN:{self.equipment.serial_number}'
        work = self.work_performed.strip()
        if work:
            # Перевод строки в наименовании позиции банку ни к чему:
            # он попадёт в PDF как есть и разорвёт ячейку таблицы
            name += f' ({" ".join(work.split())})'
        return name[:1000]

    @property
    def warranty_until(self):
        """Дата окончания гарантии на этот ремонт.

        None, пока заказ не завершён: гарантия отсчитывается от даты
        завершения, а у незакрытого заказа её ещё нет.
        """
        completed = self.repair_order.date_completed
        months = warranty_months()
        if not completed or not months:
            return None
        return add_months(completed, months)

    @property
    def is_under_warranty(self):
        until = self.warranty_until
        return bool(until and timezone.now() <= until)

    @property
    def warranty_days_left(self):
        """Сколько дней гарантии осталось; None — если гарантии нет."""
        until = self.warranty_until
        if not until:
            return None
        return (until - timezone.now()).days


class OrderStatusHistory(models.Model):
    """История изменения статуса заказа и оплаты."""
    order = models.ForeignKey(RepairOrder, on_delete=models.CASCADE, related_name='status_history', verbose_name='Заказ')
    status = models.CharField('Статус ремонта', max_length=20, choices=RepairOrder.STATUS_CHOICES, blank=True)
    payment_status = models.CharField('Статус оплаты', max_length=20, choices=RepairOrder.PAYMENT_STATUS_CHOICES, blank=True)
    changed_at = models.DateTimeField('Дата изменения', auto_now_add=True)
    changed_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, verbose_name='Кем изменён')
    notes = models.TextField('Примечания', blank=True)

    class Meta:
        verbose_name = 'История статуса'
        verbose_name_plural = 'История статусов'
        ordering = ['-changed_at']

    def __str__(self):
        return f"{self.order.order_number} → {self.get_status_display()}"


class SparePartQuerySet(models.QuerySet):
    """Отбор по остатку. Единственное место, где записаны эти условия:
    раньше «мало на складе» считалось в разных местах по-разному — где-то
    строго ниже минимума, где-то с учётом равенства."""

    def below_minimum(self):
        """Остаток меньше минимального — деталь попадает в план закупок."""
        return self.filter(current_stock__lt=F('min_stock'))

    def at_minimum(self):
        """Остаток ровно на минимуме: заказывать ещё не обязательно,
        но следующее списание уводит деталь в дефицит."""
        return self.filter(min_stock__gt=0, current_stock=F('min_stock'))


class SparePart(models.Model):
    """Радиодеталь / запчасть."""
    part_number = models.CharField('Артикул', max_length=100, unique=True)
    name = models.CharField('Название', max_length=255)
    component_type = models.CharField('Тип компонента', max_length=100, blank=True,
                                      help_text='Резистор, конденсатор, транзистор и т.д.')
    # Корпус — то, по чему деталь опознают в руках и по чему подбирают замену:
    # 0805 вместо 1206 не встанет на плату, как бы ни совпадали характеристики.
    # Короткое поле: обозначения корпусов не бывают длинными, а на этикетке
    # значение печатается рядом с артикулом
    package = models.CharField('Тип корпуса', max_length=50, blank=True,
                               help_text='0805, DIP-8, TO-220, SOT-23 и т.д.')
    resistance = models.DecimalField('Сопротивление', max_digits=15, decimal_places=6, null=True, blank=True)
    resistance_unit = models.CharField('Ед. изм. сопротивления', max_length=10, blank=True, default='Ом',
                                       help_text='Ом, кОм, МОм')
    power = models.DecimalField('Мощность', max_digits=15, decimal_places=6, null=True, blank=True)
    power_unit = models.CharField('Ед. изм. мощности', max_length=10, blank=True, default='Вт',
                                  help_text='Вт, мВт, кВт')
    voltage = models.DecimalField('Напряжение', max_digits=15, decimal_places=6, null=True, blank=True)
    voltage_unit = models.CharField('Ед. изм. напряжения', max_length=10, blank=True, default='В',
                                    help_text='В, кВ, мВ')
    current = models.DecimalField('Ток', max_digits=15, decimal_places=6, null=True, blank=True)
    current_unit = models.CharField('Ед. изм. тока', max_length=10, blank=True, default='А',
                                    help_text='А, мА, мкА')
    capacitance = models.DecimalField('Ёмкость', max_digits=15, decimal_places=6, null=True, blank=True)
    capacitance_unit = models.CharField('Ед. изм. ёмкости', max_length=10, blank=True, default='Ф',
                                        help_text='Ф, мкФ, нФ, пФ')
    current_stock = models.IntegerField('Текущий остаток', default=0, validators=[MinValueValidator(0)])
    min_stock = models.IntegerField('Минимальный остаток', default=0, validators=[MinValueValidator(0)])
    lead_time_days = models.IntegerField('Срок поставки (дней)', default=0, validators=[MinValueValidator(0)])
    # Закупочная цена за штуку. Пустая — не то же самое, что ноль: у детали,
    # цену которой ни разу не вносили, стоимость запаса неизвестна, и в план
    # закупок она попадает без суммы, а не с нулевой
    price = models.DecimalField(
        'Цена закупки, ₽', max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0'))],
        help_text='За штуку. Обновляется сама при приходе с указанной ценой'
    )
    preferred_supplier = models.CharField('Предпочтительный поставщик', max_length=255, blank=True)
    # Где эта деталь стоит: «Otis», «ABB», «БУАД», «Повсеместно». Отдельным
    # полем, а не строкой в описании: применимость печатается на этикетке
    # своим местом, по ней ищут («что у нас есть под Altivar») и она короткая,
    # а описание — свободный текст, который на этикетку помещается не всегда
    application = models.CharField(
        'Применимость', max_length=100, blank=True,
        help_text='Где применяется: Otis, ABB, БУАД, Altivar'
    )
    description = models.TextField('Описание', blank=True)

    class Meta:
        verbose_name = 'Радиодеталь'
        verbose_name_plural = 'Радиодетали'
        ordering = ['part_number']

    def __str__(self):
        return f"{self.part_number} — {self.name}"

    objects = SparePartQuerySet.as_manager()

    STOCK_BELOW = 'below'
    STOCK_AT_MINIMUM = 'at_minimum'
    STOCK_OK = 'ok'

    @property
    def stock_state(self):
        """Состояние остатка одним значением — чтобы везде показывать одно
        и то же: `below` (дефицит), `at_minimum` (ровно на минимуме), `ok`."""
        if self.current_stock < self.min_stock:
            return self.STOCK_BELOW
        if self.min_stock > 0 and self.current_stock == self.min_stock:
            return self.STOCK_AT_MINIMUM
        return self.STOCK_OK

    def is_below_min_stock(self):
        return self.stock_state == self.STOCK_BELOW

    @property
    def purchase_cost(self):
        """Во сколько обойдётся закупка недостающего. None — цена неизвестна."""
        if self.price is None:
            return None
        return self.price * self.stock_deficit

    @property
    def stock_value(self):
        """Стоимость того, что лежит на складе. None — цена неизвестна."""
        if self.price is None:
            return None
        return self.price * self.current_stock

    @property
    def stock_deficit(self):
        """Дефицит запчастей (сколько не хватает до минимального остатка)."""
        if self.min_stock > self.current_stock:
            return self.min_stock - self.current_stock
        return 0

    @property
    def specs_display(self):
        """Компактная строка основных характеристик (для этикеток)."""
        field_pairs = [
            (self.voltage, self.voltage_unit),
            (self.current, self.current_unit),
            (self.resistance, self.resistance_unit),
            (self.power, self.power_unit),
            (self.capacitance, self.capacitance_unit),
        ]
        return ', '.join(
            f'{format_spec(value)}{unit}' for value, unit in field_pairs if value is not None
        )

    @property
    def label_text(self):
        """Пояснение под характеристиками на этикетке.

        Описание, а если его не заполнили — название. Название, дословно
        равное артикулу, не печатаем: артикул уже стоит на этикетке сверху
        и самым крупным шрифтом, а второй раз то же самое место занимает,
        но ничего не добавляет. Так заполнена половина присланных каталогов.
        """
        text = (self.description or '').strip()
        if text:
            return text
        name = (self.name or '').strip()
        return '' if name == self.part_number else name

    @property
    def current_cell(self):
        """Текущая ячейка хранения детали (деталь может находиться только в одной ячейке)."""
        return self.storage_cells.first()


class Cabinet(models.Model):
    """Кассетница — физический органайзер с ячейками.

    Раньше кассетницы как объекта не было: ячейка хранила её номер числом,
    а геометрия (12 штук по 8×8) была зашита в коде. Реальные органайзеры
    так не устроены — у одного 64 одинаковых ячейки, у другого 44, где
    верхние ряды мелкие, а нижние крупные, у третьего 8 больших ящиков.

    Поэтому раскладка задаётся по рядам: сколько ячеек в каждом. Ряд
    из четырёх ячеек — это четыре крупных ящика, из восьми — восемь мелких.
    Отдельного поля «размер» нет намеренно: ширина ячейки на экране и так
    выходит обратной их числу в ряду, и это ровно то, что видно у стены.
    """
    number = models.PositiveIntegerField(
        'Номер', unique=True, validators=[MinValueValidator(1)],
        help_text='Печатается в адресе ячейки: К1-Р1-Я1'
    )
    name = models.CharField(
        'Название', max_length=100, blank=True,
        help_text='Необязательно: «Резисторы», «Крепёж», «Над столом»'
    )
    note = models.CharField('Примечание', max_length=255, blank=True)

    class Meta:
        verbose_name = 'Кассетница'
        verbose_name_plural = 'Кассетницы'
        ordering = ['number']

    def __str__(self):
        return f'Кассетница {self.number}' + (f' — {self.name}' if self.name else '')

    @property
    def label(self):
        """Как называть в списках: «1 — Резисторы» либо просто «1»."""
        return f'{self.number} — {self.name}' if self.name else str(self.number)

    def layout(self):
        """Сколько ячеек в каждом ряду: [8, 8, 8, 8, 8, 4, 4, 4].

        Считается по самим ячейкам, а не по отдельному полю: ячейки —
        источник истины, и расхождению между «как записано» и «что есть»
        взяться неоткуда.
        """
        counts = {}
        for row_number, cell_row in self.cells.values_list('row_number', 'cell_row'):
            counts[row_number] = max(counts.get(row_number, 0), cell_row)
        return [counts[row] for row in sorted(counts)]

    @property
    def layout_text(self):
        return ', '.join(str(count) for count in self.layout())

    @property
    def cell_count(self):
        return self.cells.count()

    @transaction.atomic
    def apply_layout(self, counts):
        """Приводит ячейки к заданной раскладке.

        Лишние ячейки удаляются, недостающие создаются, существующие
        не трогаются — вместе с разложенными в них деталями. Проверку,
        что в удаляемых ячейках пусто, делает вызывающий код: здесь
        не место решать, спросить ли человека.
        """
        wanted = {
            (row, cell)
            for row, count in enumerate(counts, start=1)
            for cell in range(1, count + 1)
        }
        existing = {
            (row, cell): pk
            for pk, row, cell in self.cells.values_list('pk', 'row_number', 'cell_row')
        }

        extra = [pk for key, pk in existing.items() if key not in wanted]
        if extra:
            StorageCell.objects.filter(pk__in=extra).delete()

        StorageCell.objects.bulk_create([
            StorageCell(cabinet=self, row_number=row, cell_row=cell)
            for row, cell in sorted(wanted - set(existing))
        ])
        return len(wanted - set(existing)), len(extra)

    def occupied_outside(self, counts):
        """Занятые ячейки, которые не переживут переход на новую раскладку.

        Нужны до сохранения: удалить ячейку с деталями молча — значит
        потерять сведения о том, где лежит железка.
        """
        wanted = {
            (row, cell)
            for row, count in enumerate(counts, start=1)
            for cell in range(1, count + 1)
        }
        return [
            cell for cell in self.cells.prefetch_related('parts')
            if (cell.row_number, cell.cell_row) not in wanted and cell.parts.exists()
        ]

    @classmethod
    def next_number(cls):
        biggest = cls.objects.aggregate(top=models.Max('number'))['top'] or 0
        return biggest + 1


def parse_layout(text):
    """«8, 8, 8, 4, 4» → [8, 8, 8, 4, 4].

    Разделители любые нецифровые: человек напишет и через запятую,
    и через пробел, и через «х» — все три способа встречаются.
    """
    return [int(part) for part in re.findall(r'\d+', text or '')]


class StorageCell(models.Model):
    """Ячейка хранения в кассетнице. Может содержать несколько разных деталей."""
    cabinet = models.ForeignKey(
        Cabinet, on_delete=models.CASCADE, related_name='cells',
        verbose_name='Кассетница'
    )
    row_number = models.IntegerField('Номер ряда', validators=[MinValueValidator(1)])
    cell_row = models.IntegerField('Номер ячейки в ряду', validators=[MinValueValidator(1)])
    parts = models.ManyToManyField(
        SparePart, blank=True,
        related_name='storage_cells', verbose_name='Детали'
    )

    @property
    def qr_data(self):
        """Данные для QR-кода: адрес ячейки. Специально не включаем список деталей —
        иначе для ячеек с несколькими деталями QR разрастается и не помещается на этикетку."""
        return self.address

    class Meta:
        verbose_name = 'Ячейка хранения'
        verbose_name_plural = 'Ячейки хранения'
        unique_together = [['cabinet', 'row_number', 'cell_row']]
        ordering = ['cabinet__number', 'row_number', 'cell_row']

    def __str__(self):
        return self.address

    @property
    def address(self):
        return f"К{self.cabinet.number}-Р{self.row_number}-Я{self.cell_row}"

    def get_status(self):
        """Статус ячейки для раскраски сетки.

        Дефицит важнее, чем «на минимуме», поэтому проверяется первым:
        в ячейке с несколькими деталями цвет показывает худшее из состояний.
        Раньше оба случая красились одинаково красным, и деталь ровно
        на минимуме выглядела как дефицитная, хотя в план закупок не попадала.
        """
        states = {p.stock_state for p in self.parts.all()}
        if not states:
            return 'free'
        if SparePart.STOCK_BELOW in states:
            return 'low_stock'
        if SparePart.STOCK_AT_MINIMUM in states:
            return 'at_minimum'
        return 'normal'


class RepairOrderDetail(models.Model):
    """Детали, использованные в заказе на ремонт."""
    repair_order = models.ForeignKey(RepairOrder, on_delete=models.CASCADE, related_name='details', verbose_name='Заказ')
    # В какую именно железку ушла деталь. Пусто — «на заказ целиком»:
    # так лежат все списания до v2.65.0 и те, где мастер не стал уточнять.
    # Выдумывать привязку задним числом нельзя: в заказе из пяти приборов
    # программа не знает, в который поставили конденсатор.
    #
    # SET_NULL, а не CASCADE: если единицу убрали из заказа, деталь
    # со склада всё равно списана, и терять запись о ней нельзя.
    order_equipment = models.ForeignKey(
        'RepairOrderEquipment', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='details', verbose_name='Единица оборудования'
    )
    part = models.ForeignKey(SparePart, on_delete=models.CASCADE, verbose_name='Деталь')
    quantity_used = models.IntegerField('Количество', validators=[MinValueValidator(1)])
    # Расход со склада, которым эта деталь списана. Нужен возврату: чтобы
    # вернуть деталь в её партию, надо знать, из каких партий её брали,
    # а это записано в распределении расхода. У списаний до v2.66.0 связи
    # нет — их можно вернуть только без разбора по партиям, и программа
    # об этом говорит, а не догадывается.
    movement = models.ForeignKey(
        'StockMovement', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='order_details', verbose_name='Расход со склада'
    )
    # Деталь нужна по ремонту, но со склада ещё не взята: в приборе её нет,
    # и трогать остаток рано. Так записывают то, чего на полке не оказалось,
    # и то, что мастер наметил по рецепту, ещё не вскрыв прибор.
    #
    # False по умолчанию — значит все записи, сделанные раньше, остаются
    # списанными, как оно и было. Запланированная деталь ничего не стоит
    # заказу, пока её не списали: в себестоимость она не входит.
    is_planned = models.BooleanField('Только запланирована', default=False)

    class Meta:
        verbose_name = 'Деталь в заказе'
        verbose_name_plural = 'Детали в заказе'

    def __str__(self):
        return f"{self.part.name} x{self.quantity_used} в {self.repair_order.order_number}"

    @property
    def returnable(self):
        # Запланированную возвращать нечего: со склада её не брали.
        # Её просто убирают из плана.
        """Можно ли вернуть эту деталь в её партию.

        Нельзя, если расход не записан: у списаний до v2.66.0 связи
        с движением нет, и в какие партии возвращать — неизвестно.
        Возвращать «куда-нибудь» нельзя: это разъедет себестоимость
        по FIFO, а заметят это через полгода в отчёте о прибыли.
        """
        return self.movement_id is not None and not self.is_planned

    def return_to_stock(self, quantity, employee=None, notes=''):
        """Вернуть `quantity` штук этой детали на склад — в те самые партии,
        из которых её брали.

        Партии разбираются **в обратном порядке**: сначала та, из которой
        брали последней. Это отменяет списание ровно наоборот тому, как оно
        делалось, и сохраняет главное свойство FIFO — что старые партии
        израсходованы раньше новых. Разбирать с начала значило бы вернуть
        деталь в старую партию, оставив израсходованной новую, то есть
        перевернуть очередь.

        Возврат не создаёт новой партии. Приход — это партия, из которой
        потом списывают; возвращённая деталь в новую партию не превращается,
        она возвращается в свою. Движение с типом «возврат» заводится
        только затем, чтобы возврат был виден в журнале.

        Себестоимость возвращённого считается по тем же партиям и по тому же
        правилу, что и списание: часть без партии или партия без цены делают
        сумму неизвестной целиком, а не нулевой. На эту сумму по заказу
        заводится **отрицательная** затрата — правка прошлой записи стёрла бы
        след, а по затратам считают прибыль.

        Возвращает созданное движение.
        """
        if quantity < 1:
            raise ValidationError('Вернуть можно хотя бы одну штуку')
        if quantity > self.quantity_used:
            raise ValidationError(
                f'В заказе списано {self.quantity_used} шт., вернуть больше нельзя'
            )
        if not self.returnable:
            raise ValidationError(
                'Это списание сделано до появления возвратов: неизвестно, '
                'из каких партий брали деталь. Верните её приходом на склад.'
            )

        with transaction.atomic():
            returned_cost = self._unwind_allocations(quantity)

            self.part.current_stock += quantity
            self.part.save(update_fields=['current_stock'])

            movement = StockMovement.objects.create(
                part=self.part,
                quantity=quantity,
                movement_type='return',
                repair_order=self.repair_order,
                notes=notes or f'Возврат по заказу {self.repair_order.order_number}',
                created_by=employee,
            )

            OrderCost.objects.create(
                repair_order=self.repair_order,
                category='parts',
                amount=-returned_cost if returned_cost is not None else None,
            )

            self.quantity_used -= quantity
            if self.quantity_used:
                self.save(update_fields=['quantity_used'])
            else:
                # Вернули всё — записи об использовании больше нет.
                # Движения при этом остаются: со склада деталь уходила
                # и возвращалась, и в журнале это видно.
                self.delete()

        return movement

    def _unwind_allocations(self, quantity):
        """Разобрать распределение расхода на `quantity` штук, начиная
        с партии, из которой брали последней.

        Возвращает себестоимость возвращённого или None, если она неизвестна
        хотя бы частично: партия без цены или часть, списанная вовсе без
        партии (в минус). Неизвестное остаётся неизвестным — средних цен
        и нулей здесь быть не должно.
        """
        remaining = quantity
        cost = Decimal('0')
        unknown = False

        allocations = list(
            self.movement.allocations
            .select_related('incoming')
            .order_by('-incoming__movement_date', '-incoming_id')
        )
        for allocation in allocations:
            if remaining <= 0:
                break
            take = min(allocation.quantity, remaining)
            price = allocation.incoming.unit_price
            if price is None:
                unknown = True
            else:
                cost += price * take
            if take == allocation.quantity:
                allocation.delete()
            else:
                allocation.quantity -= take
                allocation.save(update_fields=['quantity'])
            remaining -= take

        if remaining > 0:
            # Эта часть списывалась без партии — не хватало остатка.
            # Её себестоимость и тогда была неизвестна.
            unknown = True

        return None if unknown else cost

    @property
    def cost(self):
        """Во что обошлись эти детали. None — цена детали не заполнена
        или деталь только запланирована.

        Запланированная заказу ещё ничего не стоила: со склада её не брали.
        Ноль тут был бы не лучше — он попал бы в сумму как настоящая цифра.

        Это себестоимость, внутренняя цифра: в акт заказчику она
        не попадает, там только стоимость работ.
        """
        if self.is_planned or self.part.price is None:
            return None
        return self.part.price * self.quantity_used

    @property
    def enough_in_stock(self):
        """Хватает ли остатка, чтобы списать запланированное."""
        return self.part.current_stock >= self.quantity_used


class Notification(models.Model):
    """Оповещение в очереди на отправку.

    Письма не отправляются прямо из обработчика страницы. SMTP через домашний
    канал отвечает секундами, а иногда не отвечает вовсе: сохранение заказа
    не должно ни ждать почтовый сервер, ни падать из-за него. Событие
    записывается в очередь, а отправкой занимается отдельная команда
    по расписанию — она же умеет повторять неудачные попытки.

    Очередь наполняется всегда, даже когда отправка выключена: по ней видно,
    что программа *собиралась* отправить, и можно посмотреть на это до того,
    как включать письма заказчикам.
    """
    EVENT_CHOICES = [
        ('order_status', 'Смена статуса заказа'),
        ('low_stock', 'Деталь ушла в дефицит'),
        ('debt_reminder', 'Напоминание об оплате'),
        ('debt_digest', 'Сводка по задолженностям'),
        ('order_overdue', 'Заказ завис в статусе'),
        ('invoice_paid', 'Банк сообщил об оплате счёта'),
    ]
    STATUS_CHOICES = [
        ('pending', 'В очереди'),
        ('sent', 'Отправлено'),
        ('failed', 'Не удалось'),
        ('skipped', 'Пропущено'),
    ]
    CHANNEL_EMAIL = 'email'
    CHANNEL_MAX = 'max'
    CHANNEL_TELEGRAM = 'telegram'
    CHANNEL_CHOICES = [
        (CHANNEL_EMAIL, 'Почта'),
        (CHANNEL_MAX, 'MAX'),
        (CHANNEL_TELEGRAM, 'Telegram'),
    ]
    # Каналы мессенджеров: у них общий порядок настройки и общий вид строки
    # в очереди, поэтому в нескольких местах их удобно перечислить разом
    MESSENGER_CHANNELS = (CHANNEL_MAX, CHANNEL_TELEGRAM)

    event = models.CharField('Событие', max_length=30, choices=EVENT_CHOICES)
    # Канал определяет и способ отправки, и то, как читать `recipient`:
    # для почты это адрес, для MAX — «user:12345» или «chat:-98765»
    channel = models.CharField('Канал', max_length=10, choices=CHANNEL_CHOICES,
                               default=CHANNEL_EMAIL, db_index=True)
    # Раньше здесь был EmailField. Получатель в MAX — не адрес, а число,
    # так что проверка формата переехала в код, который ставит в очередь
    recipient = models.CharField('Получатель', max_length=254)
    subject = models.CharField('Тема', max_length=255)
    body = models.TextField('Текст')

    status = models.CharField('Состояние', max_length=10, choices=STATUS_CHOICES,
                              default='pending', db_index=True)
    attempts = models.IntegerField('Попыток отправки', default=0)
    last_error = models.TextField('Последняя ошибка', blank=True)

    created_at = models.DateTimeField('Создано', auto_now_add=True)
    sent_at = models.DateTimeField('Отправлено', null=True, blank=True)

    # Ссылки на то, из-за чего оповещение появилось: по ним в списке видно,
    # к какому заказу или детали относится письмо
    repair_order = models.ForeignKey(
        RepairOrder, on_delete=models.CASCADE, null=True, blank=True,
        related_name='notifications', verbose_name='Заказ'
    )
    part = models.ForeignKey(
        'SparePart', on_delete=models.CASCADE, null=True, blank=True,
        related_name='notifications', verbose_name='Деталь'
    )

    class Meta:
        verbose_name = 'Оповещение'
        verbose_name_plural = 'Оповещения'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_event_display()} → {self.recipient} ({self.get_status_display()})'

    @property
    def recipient_display(self):
        """Получатель в читаемом виде.

        Адрес почты понятен сам по себе, а «user:842910» в списке ни о чём
        не говорит: подставляем имя сотрудника, если такой в базе найдётся.
        """
        if self.channel == self.CHANNEL_MAX:
            kind, _, value = self.recipient.partition(':')
            if kind != 'user':
                return f'MAX, чат {value}'
            return self._employee_name('max_user_id', value, 'MAX')

        if self.channel == self.CHANNEL_TELEGRAM:
            # Идентификатор группы в Telegram отрицательный — этим она
            # и отличается от человека
            if self.recipient.startswith('-'):
                return f'Telegram, чат {self.recipient}'
            return self._employee_name('telegram_chat_id', self.recipient, 'Telegram')

        return self.recipient

    def _employee_name(self, field, value, channel_name):
        employee = Employee.objects.filter(**{field: value}).first()
        if employee:
            return f'{employee.full_name} ({channel_name})'
        return f'{channel_name}, пользователь {value}'


class StockMovement(models.Model):
    """Движение деталей на складе."""
    MOVEMENT_TYPE_CHOICES = [
        ('incoming', 'Приход'),
        ('outgoing', 'Расход'),
        # Возврат детали из заказа. Отдельный тип, а не приход: приход —
        # это партия, из которой потом списывают по FIFO, а возвращённая
        # деталь в новую партию не превращается — она возвращается
        # в свою прежнюю, и делается это уменьшением распределения
        # (`StockAllocation`), а не новой записью. Здесь движение нужно
        # только затем, чтобы возврат был виден в журнале.
        ('return', 'Возврат из заказа'),
    ]

    part = models.ForeignKey(SparePart, on_delete=models.CASCADE, related_name='movements', verbose_name='Деталь')
    movement_date = models.DateTimeField('Дата движения', auto_now_add=True)
    quantity = models.IntegerField('Количество', validators=[MinValueValidator(1)])
    movement_type = models.CharField('Тип движения', max_length=10, choices=MOVEMENT_TYPE_CHOICES)
    document_number = models.CharField('Номер документа', max_length=100, blank=True)
    # Цена этой поставки. Хранится у движения, а не только у детали: цены
    # меняются, и на вопрос «почему деталь подорожала вдвое» отвечает
    # именно история приходов
    unit_price = models.DecimalField(
        'Цена за штуку, ₽', max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0'))]
    )
    repair_order = models.ForeignKey(
        RepairOrder, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='Заказ на ремонт'
    )
    notes = models.TextField('Примечания', blank=True)
    created_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='Кем создано', related_name='stock_movements'
    )

    class Meta:
        verbose_name = 'Движение запчасти'
        verbose_name_plural = 'Движения запчастей'
        ordering = ['-movement_date']

    def __str__(self):
        sign = '-' if self.movement_type == 'outgoing' else '+'
        return f"{self.part.part_number} {sign}{self.quantity} ({self.get_movement_type_display()})"

    @property
    def total_price(self):
        """Сумма движения. None, если цену не вносили."""
        if self.unit_price is None:
            return None
        return self.unit_price * self.quantity

    @property
    def remaining_in_batch(self):
        """Остаток этой партии (только для приходов) — сколько из неё ещё
        не списано ни одним расходом. None — для расходов остаток не имеет
        смысла.

        Считается на лету по `StockAllocation`, не хранится отдельным полем:
        объём данных в проекте небольшой, а хранимое поле означало бы
        синхронизацию с распределением по партиям в двух местах.
        """
        if self.movement_type != 'incoming':
            return None
        used = self.batch_allocations.aggregate(total=Sum('quantity'))['total'] or 0
        return self.quantity - used


class StockAllocation(models.Model):
    """Из какой партии прихода физически списан расход (полностью или
    частично) — себестоимость считается по конкретной партии, а не по
    средней/текущей цене детали.

    Партия — это сама входящая запись `StockMovement`: у неё уже есть
    `unit_price` и дата поступления, отдельная модель `Batch` не нужна.
    Одному расходу может соответствовать несколько таких записей, если
    для него не хватило одной партии — расход разбивается по нескольким.

    Распределение всегда идёт по датам поступления (FIFO, см.
    `StockAllocation.allocate`): деталь не бывает «свежее» физически
    в одной партии против другой, так что естественный порядок — списывать
    то, что лежит дольше.
    """
    outgoing = models.ForeignKey(
        StockMovement, on_delete=models.CASCADE, related_name='allocations',
        limit_choices_to={'movement_type': 'outgoing'}, verbose_name='Расход'
    )
    incoming = models.ForeignKey(
        StockMovement, on_delete=models.PROTECT, related_name='batch_allocations',
        limit_choices_to={'movement_type': 'incoming'}, verbose_name='Партия прихода'
    )
    quantity = models.PositiveIntegerField(
        'Списано из партии', validators=[MinValueValidator(1)]
    )

    class Meta:
        verbose_name = 'Распределение расхода по партии'
        verbose_name_plural = 'Распределения расхода по партиям'
        ordering = ['incoming__movement_date']

    def __str__(self):
        return f'{self.quantity} шт. из партии №{self.incoming_id} на расход №{self.outgoing_id}'

    @staticmethod
    def allocate(outgoing):
        """Распределяет уже созданную исходящую запись `outgoing` по партиям
        прихода той же детали, строго по датам поступления (FIFO, старейшая
        первая), создавая по одной `StockAllocation` на каждую задействованную
        партию, пока не наберётся всё запрошенное количество.

        Если остатка по всем партиям вместе не хватает (в проекте это уже
        разрешённая ситуация — списание в минус, не блокировка), известная
        часть распределяется как обычно, а недостающая остаётся без партии:
        себестоимость этой части неизвестна, а не нулевая и не средняя по
        прошлым партиям.

        Остаток каждой партии считается заново при каждом вызове — поэтому
        повторный вызов в той же транзакции (например, применение шаблона
        неисправности несколькими деталями подряд, каждая — свой вызов
        `_use_repair_order_part`) видит списания, сделанные предыдущими
        вызовами, и не может списать с партии больше, чем в ней осталось.

        Возвращает True, если распределено полностью, False — если часть
        количества осталась без партии.
        """
        remaining = outgoing.quantity
        batches = (
            StockMovement.objects
            .filter(part_id=outgoing.part_id, movement_type='incoming')
            .annotate(used=Coalesce(Sum('batch_allocations__quantity'), Value(0)))
            .order_by('movement_date', 'pk')
        )
        for batch in batches:
            if remaining <= 0:
                break
            available = batch.quantity - batch.used
            if available <= 0:
                continue
            take = min(available, remaining)
            StockAllocation.objects.create(outgoing=outgoing, incoming=batch, quantity=take)
            remaining -= take
        return remaining <= 0


class OrderCost(models.Model):
    """Затрата по заказу — обобщённая запись, а не жёстко «стоимость заказа
    = сумма партий деталей»: структура должна оставлять место под труд
    и накладные расходы позже без переделки.

    Сейчас заполняется автоматически только категория `parts` — себестоимость
    деталей, списанных на заказ через `_use_repair_order_part` (одна запись
    на каждое такое списание, сумма — по фактически задействованным партиям).
    `labor` и `overhead` зарезервированы под будущие этапы и пока нигде
    не создаются и не показываются как интерфейс.
    """
    CATEGORY_CHOICES = [
        ('parts', 'Детали'),
        ('labor', 'Труд'),
        ('overhead', 'Накладные расходы'),
    ]

    repair_order = models.ForeignKey(
        RepairOrder, on_delete=models.CASCADE, related_name='costs', verbose_name='Заказ'
    )
    category = models.CharField('Категория', max_length=20, choices=CATEGORY_CHOICES)
    # Пусто — сумма неизвестна (партия без цены, либо часть расхода осталась
    # без партии из-за нехватки остатка), а не 0: см. `SparePart.price`
    amount = models.DecimalField(
        'Сумма, ₽', max_digits=12, decimal_places=2, null=True, blank=True
    )
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'Затрата по заказу'
        verbose_name_plural = 'Затраты по заказу'
        ordering = ['-created_at']

    def __str__(self):
        amount_text = f'{self.amount} ₽' if self.amount is not None else 'сумма неизвестна'
        return f'{self.get_category_display()} по {self.repair_order.order_number}: {amount_text}'


class InventorySession(models.Model):
    """Сессия инвентаризации одной кассетницы целиком.

    Область — всегда одна `Cabinet`, а не склад целиком и не произвольная
    «зона»: в проекте нет отдельного понятия зоны, а кассетница уже задаёт
    физическую границу пересчёта. Одновременно на одну кассетницу может
    идти только одна незавершённая сессия — это проверяет вызывающий код
    при старте, а не модель.
    """
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_COMPLETED = 'completed'
    STATUS_CHOICES = [
        (STATUS_IN_PROGRESS, 'В процессе'),
        (STATUS_COMPLETED, 'Завершена'),
    ]

    cabinet = models.ForeignKey(
        Cabinet, on_delete=models.CASCADE, related_name='inventory_sessions',
        verbose_name='Кассетница'
    )
    status = models.CharField(
        'Статус', max_length=15, choices=STATUS_CHOICES,
        default=STATUS_IN_PROGRESS, db_index=True
    )
    started_at = models.DateTimeField('Начата', auto_now_add=True)
    completed_at = models.DateTimeField('Завершена', null=True, blank=True)
    started_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inventory_sessions_started', verbose_name='Кем начата'
    )
    completed_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inventory_sessions_completed', verbose_name='Кем завершена'
    )

    class Meta:
        verbose_name = 'Сессия инвентаризации'
        verbose_name_plural = 'Сессии инвентаризации'
        ordering = ['-started_at']

    def __str__(self):
        return f'Инвентаризация кассетницы {self.cabinet.number} от {self.started_at:%d.%m.%Y}'

    @property
    def is_in_progress(self):
        return self.status == self.STATUS_IN_PROGRESS

    @property
    def can_be_deleted(self):
        """Удалить можно только черновик: сессию в процессе, по которой
        ещё ничего не применено. Применённые движения склада — это уже
        аудиторский след, и его не отменяют удалением сессии."""
        return self.is_in_progress and not self.lines.filter(movement__isnull=False).exists()


class InventorySessionLine(models.Model):
    """Одна деталь в сессии инвентаризации: сколько числилось на начало
    и сколько фактически насчитали.

    `expected_quantity` — снимок `SparePart.current_stock` на момент
    создания строки (начало сессии), нужен только для информации и для
    предупреждения об «утечке» остатка за время сессии. Применяемое
    расхождение считается не по этому снимку, а по живому остатку на
    момент подтверждения (см. `views._apply_inventory_discrepancy`) — это
    гарантирует, что после применения `current_stock` детали равен ровно
    посчитанному, даже если между началом сессии и подтверждением прошло
    другое движение по той же детали (в проекте есть параллельные
    пользователи по Tailscale).
    """
    session = models.ForeignKey(
        InventorySession, on_delete=models.CASCADE, related_name='lines',
        verbose_name='Сессия'
    )
    part = models.ForeignKey(
        SparePart, on_delete=models.CASCADE, related_name='inventory_lines',
        verbose_name='Деталь'
    )
    # Ячейка на момент начала сессии. SET_NULL, а не CASCADE: перекладка
    # раскладки кассетницы не должна стирать уже готовую строку аудита
    cell = models.ForeignKey(
        StorageCell, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inventory_lines', verbose_name='Ячейка'
    )
    expected_quantity = models.IntegerField('Учтено на начало сессии', validators=[MinValueValidator(0)])
    counted_quantity = models.IntegerField(
        'Посчитано фактически', null=True, blank=True, validators=[MinValueValidator(0)]
    )
    # Обязателен только при избытке (проверяет представление при подтверждении,
    # не форма и не модель — на момент ввода количества знак расхождения ещё
    # не известен окончательно, он пересчитывается против живого остатка)
    comment = models.CharField('Комментарий при избытке', max_length=255, blank=True)
    # Движение склада, которым расхождение применено. Пусто, пока не
    # применено, и пусто навсегда, если расхождения не было (посчитанное
    # совпало с остатком на момент подтверждения) — специальной
    # parallel-сущности корректировки в проекте нет, только обычный
    # StockMovement с пояснением в notes
    movement = models.ForeignKey(
        StockMovement, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inventory_lines', verbose_name='Движение склада'
    )

    class Meta:
        verbose_name = 'Строка инвентаризации'
        verbose_name_plural = 'Строки инвентаризации'
        ordering = ['cell__row_number', 'cell__cell_row', 'part__part_number']

    def __str__(self):
        return f'{self.part.part_number} в сессии №{self.session_id}'

    @property
    def discrepancy(self):
        """Расхождение против учтённого на начало сессии (посчитано минус
        учтено). None, пока деталь не посчитана.

        Это информационное число для экрана ввода — окончательное
        расхождение, которое действительно применяется, считается против
        живого остатка на момент подтверждения и может от этого отличаться,
        если остаток успел измениться (см. docstring класса)."""
        if self.counted_quantity is None:
            return None
        return self.counted_quantity - self.expected_quantity




class WebhookDelivery(models.Model):
    """Уведомление, доставленное банком на публичный адрес программы.

    Зачем запись. Уведомление приходит один раз, а банк при неудачном
    ответе присылает его повторно — иногда несколько раз подряд. Без
    следа о том, что именно и когда пришло, невозможно ни разобрать
    жалобу «оплата не разнеслась», ни отличить повтор от нового события.
    Поэтому каждая **принятая** доставка записывается целиком, до того
    как с ней что-то делают.

    Защита от повтора — ограничение уникальности на пару «банк + ключ
    доставки». Ключ берётся из идентификатора события, если банк его
    присылает, а если нет — из хеша тела запроса (`body_hash`). Хеш
    надёжен как запасной вариант: два разных события с побайтово
    одинаковым телом означали бы, что банк не сообщает ни времени,
    ни номера, — тогда различить их всё равно нечем.

    Чего здесь нет. Суммы: её нет и в самом уведомлении Т-Банка —
    событие «счёт оплачен» состоит из идентификатора счёта и слова PAID.
    Поэтому доставка не создаёт `Payment` и не трогает `payment_status`:
    она ставит на заказе `invoice_paid_at` и зовёт бухгалтера внести
    поступление по выписке. Ссылки на заказ в записи тоже нет — заказ
    находится по идентификатору счёта, а связь с ним видна из `result`.

    От Точки записей не будет вовсе: проверять подлинность её уведомлений
    нечем, и её проверяющий отказывает на каждом запросе.
    """
    STATUS_RECEIVED = 'received'
    STATUS_PROCESSED = 'processed'
    STATUS_UNMATCHED = 'unmatched'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_RECEIVED, 'Принято'),
        (STATUS_PROCESSED, 'Разобрано'),
        # Уведомление настоящее и понятное, но счёт из него не нашёлся.
        # Это не ошибка доставки: банку отвечено «принято», повторять
        # ему нечего, а разбираться надо у нас
        (STATUS_UNMATCHED, 'Счёт не найден'),
        (STATUS_FAILED, 'Разобрать не удалось'),
    ]

    provider = models.CharField('Банк', max_length=20,
                                choices=invoicing.PROVIDER_CHOICES, db_index=True)
    # Идентификатор события у банка. Пусто — банк его не прислал либо
    # мы ещё не знаем, в каком поле он лежит (см. `core/webhooks.py`)
    event_id = models.CharField('Событие у банка', max_length=200, blank=True)
    # Ключ, по которому доставка считается повтором: идентификатор события,
    # а без него — хеш тела. Хранится отдельным полем, потому что
    # ограничение уникальности должно работать в обоих случаях одинаково
    dedup_key = models.CharField('Ключ доставки', max_length=200)
    body_hash = models.CharField('Хеш тела', max_length=64, db_index=True)
    # Тело запроса как пришло. Нужно ровно для разбора неполадок: понять,
    # что именно прислал банк, по одному хешу нельзя
    body = models.TextField('Тело запроса', blank=True)

    received_at = models.DateTimeField('Получено', auto_now_add=True)
    processed_at = models.DateTimeField('Разобрано', null=True, blank=True)
    status = models.CharField('Состояние', max_length=10, choices=STATUS_CHOICES,
                              default=STATUS_RECEIVED, db_index=True)
    # Чем кончилось: причина отказа или что сделано. Секретов здесь быть
    # не должно — текст пишется и в журнал
    result = models.TextField('Итог', blank=True)

    class Meta:
        verbose_name = 'Уведомление банка'
        verbose_name_plural = 'Уведомления банков'
        ordering = ['-received_at']
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'dedup_key'],
                name='unique_webhook_delivery',
            ),
        ]

    def __str__(self):
        return f'{self.get_provider_display()} → {self.dedup_key} ({self.get_status_display()})'
