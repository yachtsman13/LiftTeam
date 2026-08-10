"""
Модели данных для LiftTeam v2.7.2.
Сущности: Client, EquipmentModel, Equipment, RepairOrder, RepairOrderEquipment,
          RepairOrderDetail, SparePart, StorageCell, StockMovement, Employee (User extension).
"""
from django.db import models, transaction
from django.db.models import Sum
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MinValueValidator
from django.utils import timezone


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


class Client(models.Model):
    """Заказчик."""
    name = models.CharField('Название', max_length=255)
    inn = models.CharField('ИНН', max_length=20, blank=True)
    kpp = models.CharField('КПП', max_length=20, blank=True)
    contact_person = models.CharField('Контактное лицо', max_length=255, blank=True)
    phone = models.CharField('Телефон', max_length=50, blank=True)
    email = models.EmailField('Email', blank=True)

    class Meta:
        verbose_name = 'Заказчик'
        verbose_name_plural = 'Заказчики'
        ordering = ['name']

    def __str__(self):
        return self.name


class EquipmentModel(models.Model):
    """Модель оборудования."""
    name = models.CharField('Название модели', max_length=255, unique=True)

    class Meta:
        verbose_name = 'Модель оборудования'
        verbose_name_plural = 'Модели оборудования'
        ordering = ['name']

    def __str__(self):
        return self.name


class Equipment(models.Model):
    """Единица оборудования."""
    model = models.ForeignKey(EquipmentModel, on_delete=models.CASCADE, verbose_name='Модель')
    serial_number = models.CharField('Серийный номер', max_length=100, unique=True)
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

    class Meta:
        verbose_name = 'Заказ на ремонт'
        verbose_name_plural = 'Заказы на ремонт'
        ordering = ['-date_received']

    def __str__(self):
        return self.order_number

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
    seal_numbers = models.CharField('Номера пломб', max_length=255, blank=True)
    initial_condition = models.TextField('Начальное состояние', blank=True)
    repair_cost = models.DecimalField('Стоимость ремонта', max_digits=12, decimal_places=2, null=True, blank=True)
    yandex_disk_folder = models.URLField('Папка на Яндекс.Диске', blank=True)

    class Meta:
        verbose_name = 'Оборудование в заказе'
        verbose_name_plural = 'Оборудование в заказе'

    def __str__(self):
        return f"{self.equipment} в {self.repair_order.order_number}"


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


class SparePart(models.Model):
    """Радиодеталь / запчасть."""
    part_number = models.CharField('Артикул', max_length=100, unique=True)
    name = models.CharField('Название', max_length=255)
    component_type = models.CharField('Тип компонента', max_length=100, blank=True,
                                      help_text='Резистор, конденсатор, транзистор и т.д.')
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
    preferred_supplier = models.CharField('Предпочтительный поставщик', max_length=255, blank=True)
    description = models.TextField('Описание', blank=True)

    class Meta:
        verbose_name = 'Радиодеталь'
        verbose_name_plural = 'Радиодетали'
        ordering = ['part_number']

    def __str__(self):
        return f"{self.part_number} — {self.name}"

    def is_below_min_stock(self):
        return self.current_stock < self.min_stock

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
        """Возвращает статус ячейки для визуализации."""
        cell_parts = list(self.parts.all())
        if not cell_parts:
            return 'free'
        if any(p.current_stock <= p.min_stock and p.min_stock > 0 for p in cell_parts):
            return 'low_stock'
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




