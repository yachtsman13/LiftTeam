"""
Модели данных для LiftTeam v2.36.0.
Сущности: Client, EquipmentModel, Equipment, RepairOrder, RepairOrderEquipment,
          RepairOrderDetail, SparePart, StorageCell, StockMovement, Employee (User extension).
"""
import calendar
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MinValueValidator
from django.utils import timezone


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


def format_amount(value):
    """Сумма с разделителями разрядов: «54 000».

    Пробел неразрывный: и в письме, и в печатном акте перенос строки
    посреди числа превращает 54 000 в «54» на одной строке и «000»
    на другой.
    """
    return f'{value:,.0f}'.replace(',', ' ')


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
    role = models.CharField('Роль', max_length=20, choices=ROLE_CHOICES, default='repair_manager')
    is_active = models.BooleanField('Активен', default=True)
    is_staff = models.BooleanField('Сотрудник', default=False)
    date_joined = models.DateTimeField('Дата регистрации', auto_now_add=True)

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


class Organization(models.Model):
    """Реквизиты своей фирмы — шапка печатных документов.

    Одна запись на всю программу: фирма одна. Отдельная модель, а не строки
    в настройках, потому что менять их приходится владельцу — переехали,
    сменился директор, — а лазить для этого по SSH в файл настроек он не
    должен.
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

    class Meta:
        verbose_name = 'Реквизиты организации'
        verbose_name_plural = 'Реквизиты организации'

    def __str__(self):
        return self.name or 'Реквизиты не заполнены'

    @classmethod
    def get_solo(cls):
        """Единственная запись. Создаётся пустой, если её ещё нет: печатать
        документы можно и с незаполненной шапкой — хуже, чем не напечатать
        вовсе, это не будет."""
        organization = cls.objects.first()
        if organization is None:
            organization = cls.objects.create()
        return organization

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


class EquipmentModel(models.Model):
    """Модель оборудования."""
    name = models.CharField('Название модели', max_length=255, unique=True)
    # Родовое название — «Преобразователь частоты», «Привод дверей». В списках
    # и на этикетках оно только съедает место, поэтому там печатается одно
    # name; но в акте дефектации первая строка — «Тип: <родовое> <модель>»,
    # и без него документ выглядит как записка для своих, а не как акт.
    kind = models.CharField(
        'Тип оборудования', max_length=100, blank=True,
        help_text='Родовое название для акта дефектации: «Преобразователь '
                  'частоты», «Привод дверей». Если оно уже есть в начале '
                  'названия модели, поле оставьте пустым.'
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


class Equipment(models.Model):
    """Единица оборудования."""
    model = models.ForeignKey(EquipmentModel, on_delete=models.CASCADE, verbose_name='Модель')
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
        return f"{self.model.name} — {self.serial_number}"

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


class RepairOrderQuerySet(models.QuerySet):
    """Отбор по долгам. Условия записаны здесь, а не в каждом отчёте:
    «должник» встречается на дашборде, в отчёте, в выгрузке и в напоминаниях,
    и расходиться эти четыре определения не должны."""

    def with_debt(self):
        """Заказы, по которым остались деньги."""
        return self.filter(payment_status__in=['unpaid', 'partially_paid'])

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
    ]
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

    # --- Счёт, выставленный через API Т-Банка ---
    # Номер и дата лежат в тех же invoice_number / invoice_date, что и у счёта,
    # выставленного руками: для долгов и напоминаний разницы нет, и заводить
    # им отдельные поля значило бы раздвоить понятие «счёт выставлен»
    tbank_invoice_sent_at = models.DateTimeField(
        'Счёт выставлен через банк', null=True, blank=True
    )
    tbank_invoice_pdf_url = models.URLField('Ссылка на PDF счёта', max_length=500, blank=True)
    tbank_invoice_error = models.CharField(
        'Последняя ошибка выставления', max_length=500, blank=True
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
                'complexity': order_equipment.get_repair_complexity_display(),
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

    class Meta:
        verbose_name = 'Оборудование в заказе'
        verbose_name_plural = 'Оборудование в заказе'

    def __str__(self):
        return f"{self.equipment} в {self.repair_order.order_number}"

    @property
    def has_defect_act(self):
        """Есть ли что печатать в акте дефектации."""
        return bool(self.diagnosis or self.error_codes or self.warranty_case)

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

        Предлагаемые работы, если их записали; иначе выполненные (бывает,
        что предложение печатают задним числом); иначе просто «Ремонт
        <тип и модель>» — без выдумок о том, чего никто не писал.
        """
        work = self.proposed_work.strip() or self.work_performed.strip()
        if work:
            return ' '.join(work.split())
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
        return ', '.join(f'{float(value):g}{unit}' for value, unit in field_pairs if value is not None)

    @property
    def label_text(self):
        """Пояснение под характеристиками на этикетке.

        Описание, а если его не заполнили — название. Пустая строка на
        этикетке не нужна никому, а название есть у каждой детали.
        """
        return (self.description or '').strip() or self.name

    @property
    def current_cell(self):
        """Текущая ячейка хранения детали (деталь может находиться только в одной ячейке)."""
        return self.storage_cells.first()


class StorageCell(models.Model):
    """Ячейка хранения в кассетнице. Может содержать несколько разных деталей."""
    cabinet_number = models.IntegerField('Номер кассетницы', validators=[MinValueValidator(1)])
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
        unique_together = [['cabinet_number', 'row_number', 'cell_row']]
        ordering = ['cabinet_number', 'row_number', 'cell_row']

    def __str__(self):
        return self.address

    @property
    def address(self):
        return f"К{self.cabinet_number}-Р{self.row_number}-Я{self.cell_row}"

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
    part = models.ForeignKey(SparePart, on_delete=models.CASCADE, verbose_name='Деталь')
    quantity_used = models.IntegerField('Количество', validators=[MinValueValidator(1)])

    class Meta:
        verbose_name = 'Деталь в заказе'
        verbose_name_plural = 'Детали в заказе'

    def __str__(self):
        return f"{self.part.name} x{self.quantity_used} в {self.repair_order.order_number}"

    @property
    def cost(self):
        """Во что обошлись эти детали. None — цена детали не заполнена.

        Это себестоимость, внутренняя цифра: в акт заказчику она
        не попадает, там только стоимость работ.
        """
        if self.part.price is None:
            return None
        return self.part.price * self.quantity_used


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
        sign = '+' if self.movement_type == 'incoming' else '-'
        return f"{self.part.part_number} {sign}{self.quantity} ({self.get_movement_type_display()})"

    @property
    def total_price(self):
        """Сумма движения. None, если цену не вносили."""
        if self.unit_price is None:
            return None
        return self.unit_price * self.quantity




