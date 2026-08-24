"""
Формы для LiftTeam v2.59.0.
"""
from django import forms
from django.contrib.auth import authenticate
from django.core import validators
from django.forms import inlineformset_factory
from . import invoicing
from .models import (
    Cabinet, Client, EquipmentModel, EquipmentType, EquipmentVersion,
    Equipment, FaultType, FaultTypePart,
    RepairOrder, RepairOrderEquipment,
    PriceList, PriceListLine, EquipmentMaterial, available_equipment_for_order,
    RepairOrderDetail, SparePart, StockMovement, Employee, Payment, Organization,
    parse_layout, format_spec,
)


class LoginForm(forms.Form):
    """Кастомная форма входа по логину (username)."""
    username = forms.CharField(
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Логин', 'autofocus': True}),
        label='Логин'
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Пароль'}),
        label='Пароль'
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if username and password:
            self.user_cache = authenticate(self.request, username=username, password=password)
            if self.user_cache is None:
                raise forms.ValidationError('Неверный логин или пароль')
            else:
                self.confirm_login_allowed(self.user_cache)
        return self.cleaned_data

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise forms.ValidationError('Аккаунт неактивен')

    def get_user(self):
        return self.user_cache


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ['name', 'inn', 'kpp', 'address', 'contact_person', 'phone', 'email']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'inn': forms.TextInput(attrs={'class': 'form-control'}),
            'kpp': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '305048, г. Курск, проспект Дружбы, д. 9А'}),
            'contact_person': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'name': 'Название',
            'inn': 'ИНН',
            'kpp': 'КПП',
            'address': 'Адрес',
            'contact_person': 'Контактное лицо',
            'phone': 'Телефон',
            'email': 'Email',
        }


class EquipmentTypeForm(forms.ModelForm):
    class Meta:
        model = EquipmentType
        fields = ['name', 'description']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Привод дверей',
            }),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }
        labels = {
            'name': 'Название типа',
            'description': 'Описание',
        }


class EquipmentVersionForm(forms.ModelForm):
    class Meta:
        model = EquipmentVersion
        fields = ['equipment_model', 'name', 'note']
        widgets = {
            'equipment_model': forms.Select(attrs={'class': 'form-select'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '1.1'}),
            'note': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }
        labels = {
            'equipment_model': 'Модель оборудования',
            'name': 'Версия',
            'note': 'Комментарий',
        }


class EquipmentModelForm(forms.ModelForm):
    class Meta:
        model = EquipmentModel
        fields = ['name', 'kind', 'equipment_type']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'kind': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Преобразователь частоты',
                # Родовых названий немного, и печатаются они в акте слово
                # в слово — подсказка из уже введённых бережёт от разнобоя
                # «Привод дверей» / «привод дверей» / «Приводы дверей».
                'list': 'equipment-kinds',
            }),
            'equipment_type': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'name': 'Название модели',
            'kind': 'Родовое название для акта',
            'equipment_type': 'Тип оборудования',
        }


class FaultTypeForm(forms.ModelForm):
    class Meta:
        model = FaultType
        fields = ['equipment_model', 'name', 'description', 'complexity']
        widgets = {
            'equipment_model': forms.Select(attrs={'class': 'form-select'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'complexity': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'equipment_model': 'Модель оборудования',
            'name': 'Неисправность (коротко, для списков)',
            'description': 'Описание для документов',
            'complexity': 'Сложность ремонта',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Сложность появилась позже самой неисправности, и форма без неё
        # обязана сохраняться: незаполненная сложность — «простой», как
        # и по умолчанию у поля
        self.fields['complexity'].required = False

    def clean_complexity(self):
        return self.cleaned_data.get('complexity') or 'simple'


class FaultTypePartForm(forms.ModelForm):
    """Строка рецепта типовой неисправности.

    Поле `part`, как и в заказе, рисуется общим выбором детали, а не
    виджетом формы; имя поля при этом прежнее (`parts-N-part`).
    """
    class Meta:
        model = FaultTypePart
        fields = ['part', 'quantity', 'version']
        widgets = {
            'part': forms.Select(attrs={'class': 'form-select'}),
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'version': forms.Select(attrs={'class': 'form-select fault-part-version'}),
        }
        labels = {
            'part': 'Деталь',
            'quantity': 'Типовое количество',
            'version': 'Только для версии',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Пустой вариант читается как «строка общая», а не как «не выбрано»:
        # общих строк в рецепте большинство, и человек должен видеть,
        # что оставленное пустым поле — это осмысленный ответ
        self.fields['version'].empty_label = 'Для всех версий'
        self.fields['version'].queryset = (
            EquipmentVersion.objects.select_related('equipment_model')
            .order_by('equipment_model__name', 'name')
        )


class BaseFaultTypePartFormSet(forms.BaseInlineFormSet):
    """Проверяет, что уточнение рецепта стоит на версии той же модели.

    Версия из чужой модели в рецепте — это строка, которая не сработает
    никогда: `FaultType.recipe_lines` сверяет версию с версией единицы,
    а у единицы другой модели её быть не может. Молча хранить такую
    строку хуже, чем не дать её сохранить.
    """

    def clean(self):
        super().clean()
        model = getattr(self.instance, 'equipment_model_id', None)
        if not model:
            return
        for form in self.forms:
            if not hasattr(form, 'cleaned_data') or form.cleaned_data.get('DELETE'):
                continue
            version = form.cleaned_data.get('version')
            if version and version.equipment_model_id != model:
                form.add_error(
                    'version',
                    'Версия относится к другой модели оборудования.'
                )


FaultTypePartFormSet = inlineformset_factory(
    FaultType, FaultTypePart, form=FaultTypePartForm,
    formset=BaseFaultTypePartFormSet, extra=1, can_delete=True
)


def make_fault_type_part_formset(extra=1):
    """Формсет рецепта на заданное число пустых строк.

    Нужен копированию: строки рецепта-образца подставляются как initial,
    а initial показывается только в добавочных формах — с обычным extra=1
    из копии доехала бы одна строка рецепта вместо всех.
    """
    return inlineformset_factory(
        FaultType, FaultTypePart, form=FaultTypePartForm,
        formset=BaseFaultTypePartFormSet, extra=extra, can_delete=True
    )


class EquipmentForm(forms.ModelForm):
    class Meta:
        model = Equipment
        fields = [
            'model', 'version', 'serial_number', 'manufacture_date', 'current_client'
        ]
        widgets = {
            'model': forms.Select(attrs={'class': 'form-select'}),
            'version': forms.Select(attrs={'class': 'form-select'}),
            'serial_number': forms.TextInput(attrs={'class': 'form-control'}),
            'manufacture_date': forms.DateInput(
                attrs={'class': 'form-control', 'type': 'date'}, format='%Y-%m-%d'
            ),
            'current_client': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'model': 'Модель',
            'version': 'Версия',
            'serial_number': 'Серийный номер',
            'manufacture_date': 'Дата изготовления',
            'current_client': 'Текущий заказчик',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Версий у большинства оборудования нет вовсе — поле необязательное,
        # и пустой вариант об этом прямо говорит
        self.fields['version'].empty_label = 'Без версии'
        self.fields['version'].queryset = (
            EquipmentVersion.objects.select_related('equipment_model')
            .order_by('equipment_model__name', 'name')
        )

    def clean(self):
        cleaned = super().clean()
        model = cleaned.get('model')
        version = cleaned.get('version')
        if model and version and version.equipment_model_id != model.pk:
            self.add_error('version', 'Эта версия относится к другой модели.')
        return cleaned


class RepairOrderForm(forms.ModelForm):
    """Форма заказа на ремонт. Поле status исключено — статус меняется через отдельный механизм."""
    class Meta:
        model = RepairOrder
        fields = [
            'client', 'fault_description', 'invoice_number', 'invoice_date',
            'payment_status'
        ]
        widgets = {
            'client': forms.Select(attrs={'class': 'form-select'}),
            'fault_description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'invoice_number': forms.TextInput(attrs={'class': 'form-control'}),
            'invoice_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}, format='%Y-%m-%d'),
            'payment_status': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'client': 'Заказчик',
            'fault_description': 'Общее описание неисправности',
            'invoice_number': 'Номер счёта',
            'invoice_date': 'Дата счёта',
            'payment_status': 'Статус оплаты',
        }


class FaultSelectMultiple(forms.SelectMultiple):
    """Как обычный multiple-select, но с одним не относящимся к модели
    вариантом — «Другое (не из списка)».

    Он существует только ради интерфейса: сигнализирует, что случая ещё нет
    в справочнике, и по нему кнопка «Применить шаблон» в форме заказа
    становится неактивной. Значение выбрасывается до того, как поле формы
    проверит присланные id по базе — иначе этот псевдо-вариант проверки
    не прошёл бы и ошибочно завернул сохранение всей строки оборудования.
    """
    OTHER_VALUE = 'other'

    def value_from_datadict(self, data, files, name):
        values = super().value_from_datadict(data, files, name)
        return [v for v in values if v != self.OTHER_VALUE]


class RepairOrderEquipmentForm(forms.ModelForm):
    """Единица оборудования в заказе.

    В списке — только свободное оборудование: то, что сейчас лежит
    в другом незакрытом заказе, не предлагается (см.
    `available_equipment_for_order`). Единицы самого правимого заказа
    в списке остаются, иначе форма потеряла бы уже выбранное.
    """

    def __init__(self, *args, order=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['equipment'].queryset = (
            available_equipment_for_order(order)
            .select_related('model', 'version')
            .order_by('model__name', 'serial_number')
        )

    class Meta:
        model = RepairOrderEquipment
        fields = ['equipment', 'fault_description', 'faults', 'work_performed', 'seal_numbers', 'initial_condition', 'repair_cost', 'yandex_disk_folder']
        widgets = {
            'equipment': forms.Select(attrs={'class': 'form-select'}),
            'fault_description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Описание неисправности'}),
            'faults': FaultSelectMultiple(attrs={'class': 'form-select fault-select', 'size': 4}),
            'work_performed': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Что сделали — попадёт в акт выполненных работ'}),
            'seal_numbers': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Номера пломб'}),
            'initial_condition': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Начальное состояние'}),
            'repair_cost': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'yandex_disk_folder': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://disk.yandex.ru/...'}),
        }
        labels = {
            'equipment': 'Оборудование',
            'fault_description': 'Неисправность',
            'faults': 'Типовые неисправности',
            'work_performed': 'Выполненные работы',
            'seal_numbers': 'Номера пломб',
            'initial_condition': 'Начальное состояние',
            'repair_cost': 'Стоимость ремонта',
            'yandex_disk_folder': 'Папка на Яндекс.Диске',
        }

    def clean(self):
        """Типовая неисправность выбирается по модели оборудования этой
        строки; выбор чужой (например, после ручной подмены запроса)
        не должен молча осесть в базе."""
        cleaned = super().clean()
        faults = cleaned.get('faults')
        equipment = cleaned.get('equipment')
        if faults and equipment:
            mismatched = [f for f in faults if f.equipment_model_id != equipment.model_id]
            if mismatched:
                names = ', '.join(f.name for f in mismatched)
                self.add_error(
                    'faults',
                    f'Не относится к модели «{equipment.model.name}»: {names}'
                )
        return cleaned


RepairOrderEquipmentFormSet = inlineformset_factory(
    RepairOrder, RepairOrderEquipment, form=RepairOrderEquipmentForm,
    extra=1, can_delete=True
)


# Приём заказа и работа по нему — разные моменты, и знают о заказе они
# разное. Когда прибор только привезли, известно: от кого, что с ним
# со слов заказчика и в каком он виде приехал. Ни счёта, ни оплаты,
# ни диагноза, ни стоимости, ни выполненных работ, ни номеров пломб
# тогда ещё не существует: счёт выставляют после согласования, пломбы
# ставят при выдаче, стоимость считают после диагностики. Показывать
# их при приёме — значит просить заполнить то, чего человек не знает,
# и прятать за ними те три поля, которые он знает.
#
# Приём — тот же приём, что и у DefectActForm: поля, которые заполняют
# в другой момент, живут в форме своего момента. Полная форма никуда
# не делась и открывается по «Редактировать» — на приёме её просто
# не показывают, и ни одно поле при этом не потеряно.

class RepairOrderIntakeForm(RepairOrderForm):
    """Заказ в момент приёма: заказчик и что он рассказал.

    Общее описание остаётся: оно печатается в акте приёма строкой
    «Со слов заказчика» — то есть это как раз поле приёмки, а не работы.
    """
    class Meta(RepairOrderForm.Meta):
        fields = ['client', 'fault_description']


class RepairOrderEquipmentIntakeForm(RepairOrderEquipmentForm):
    """Единица оборудования в момент приёма.

    Начальное состояние — про то, что видно при осмотре: комплектность,
    следы вскрытия, повреждения корпуса. Отсюда его и заполняют, а после
    ремонта уже не восстановить.
    """
    class Meta(RepairOrderEquipmentForm.Meta):
        fields = ['equipment', 'fault_description', 'initial_condition']


RepairOrderEquipmentIntakeFormSet = inlineformset_factory(
    RepairOrder, RepairOrderEquipment, form=RepairOrderEquipmentIntakeForm,
    extra=1, can_delete=True
)


# Формулировка, которая стоит почти в каждом акте дефектации. Подставляется
# в пустое поле как заготовка, а не как значение по умолчанию в модели:
# инженер должен её прочитать и при необходимости поправить, а не подписать
# не глядя.
DEFAULT_NON_WARRANTY_REASON = (
    'перепадами напряжения в питающей сети и(или) естественной деградацией '
    'электронных компонентов'
)


class PriceListForm(forms.ModelForm):
    """Прайс: чей он и примечание. Строки — отдельным набором форм."""
    class Meta:
        model = PriceList
        fields = ['client', 'note']
        widgets = {
            'client': forms.Select(attrs={'class': 'form-select'}),
            'note': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }
        labels = {'client': 'Заказчик', 'note': 'Примечание'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # У заказчика прайс один: занятых в списке быть не должно, иначе
        # сохранение упрётся в ограничение уже после заполнения строк
        taken = PriceList.objects.exclude(client__isnull=True)
        if self.instance.pk:
            taken = taken.exclude(pk=self.instance.pk)
        self.fields['client'].queryset = Client.objects.exclude(
            pk__in=taken.values('client_id')
        ).order_by('name')
        self.fields['client'].empty_label = 'Базовый прайс (для всех)'


class PriceListLineForm(forms.ModelForm):
    class Meta:
        model = PriceListLine
        fields = ['equipment_type', 'complexity', 'price']
        widgets = {
            'equipment_type': forms.Select(attrs={'class': 'form-select'}),
            'complexity': forms.Select(attrs={'class': 'form-select'}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        }
        labels = {
            'equipment_type': 'Тип оборудования',
            'complexity': 'Сложность',
            'price': 'Цена, ₽',
        }


PriceListLineFormSet = inlineformset_factory(
    PriceList, PriceListLine, form=PriceListLineForm, extra=1, can_delete=True
)


class EquipmentMaterialForm(forms.ModelForm):
    """Одна ссылка на материал модели."""

    class Meta:
        model = EquipmentMaterial
        fields = ['kind', 'title', 'url', 'version', 'note']
        widgets = {
            'kind': forms.Select(attrs={'class': 'form-select'}),
            'title': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Схема принципиальная'
            }),
            'url': forms.URLInput(attrs={
                'class': 'form-control', 'placeholder': 'https://disk.yandex.ru/...'
            }),
            'version': forms.Select(attrs={'class': 'form-select'}),
            'note': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, equipment_model=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Только исполнения этой же модели. Модель приходит извне — так же,
        # как заказ приходит в форму единицы: у пустой строки набора связь
        # с моделью проставляется уже после того, как форма собрана,
        # и спрашивать её у instance тут рано.
        #
        # Чужое исполнение означало бы материал, который не покажется
        # никогда: отбор идёт от модели той единицы, что лежит на столе.
        self.fields['version'].queryset = (
            EquipmentVersion.objects.filter(equipment_model=equipment_model)
            if equipment_model is not None else EquipmentVersion.objects.none()
        )
        self.fields['version'].empty_label = 'Для всех исполнений'


EquipmentMaterialFormSet = inlineformset_factory(
    EquipmentModel, EquipmentMaterial, form=EquipmentMaterialForm,
    extra=1, can_delete=True
)


class DefectActForm(forms.ModelForm):
    """Данные акта дефектации — то, что нашли при диагностике.

    Отдельная форма и отдельная страница: в форме заказа этих полей было бы
    шесть штук на каждую единицу оборудования, а заполняют их один раз
    и не тогда, когда заводят заказ.
    """
    class Meta:
        model = RepairOrderEquipment
        fields = [
            'defect_act_date', 'diagnosis', 'error_codes',
            'warranty_case', 'non_warranty_reason', 'estimated_cost',
        ]
        widgets = {
            'defect_act_date': forms.DateInput(
                attrs={'class': 'form-control', 'type': 'date'}, format='%Y-%m-%d'
            ),
            'diagnosis': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3,
                'placeholder': 'В результате диагностики устройства выявлен '
                               'выход из строя IGBT модуля и его обвязки.',
            }),
            'error_codes': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 4,
                'placeholder': '«F2340» — короткое замыкание в IGBT модуле',
            }),
            'warranty_case': forms.Select(attrs={'class': 'form-select'}),
            'non_warranty_reason': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'estimated_cost': forms.NumberInput(
                attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Пустой вариант ModelForm подставляет сам, но подписывает его
        # прочерками: в акте это состояние осмысленное, а не «не выбрано».
        self.fields['warranty_case'].choices = [
            ('', 'Не определено') if not value else (value, label)
            for value, label in self.fields['warranty_case'].choices
        ]
        # Именно self.initial, а не field.initial: у формы, связанной
        # с объектом, значения полей берутся из initial объекта, и пустая
        # строка оттуда перебила бы заготовку.
        if not self.instance.non_warranty_reason:
            self.initial['non_warranty_reason'] = DEFAULT_NON_WARRANTY_REASON

    def clean(self):
        cleaned = super().clean()
        # Гарантийному случаю причина негарантийности не нужна, и оставленная
        # заготовка попала бы в акт прямым противоречием самой себе.
        if cleaned.get('warranty_case') == 'warranty':
            cleaned['non_warranty_reason'] = ''
        return cleaned


class RepairOrderDetailForm(forms.ModelForm):
    """Деталь в заказе.

    Поле `part` в карточке заказа рисуется не виджетом формы, а общим
    выбором детали (`core/templates/core/_part_picker.html`): каталог
    в несколько сотен записей списком не листается. Наружу выбор отдаёт
    то же самое имя поля, поэтому проверка здесь не изменилась.
    """
    class Meta:
        model = RepairOrderDetail
        fields = ['part', 'quantity_used']
        widgets = {
            'part': forms.Select(attrs={'class': 'form-select'}),
            'quantity_used': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
        }
        labels = {
            'part': 'Деталь',
            'quantity_used': 'Количество',
        }


class SparePartForm(forms.ModelForm):
    class Meta:
        model = SparePart
        fields = [
            'part_number', 'name', 'component_type', 'package',
            'resistance', 'resistance_unit',
            'power', 'power_unit',
            'voltage', 'voltage_unit',
            'current', 'current_unit',
            'capacitance', 'capacitance_unit',
            'min_stock', 'lead_time_days',
            'price', 'preferred_supplier', 'application', 'description'
        ]
        widgets = {
            'part_number': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'component_type': forms.TextInput(attrs={'class': 'form-control', 'list': 'component-types'}),
            'package': forms.TextInput(attrs={'class': 'form-control', 'list': 'packages'}),
            'resistance': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any', 'placeholder': '0'}),
            'resistance_unit': forms.TextInput(attrs={'class': 'form-control', 'style': 'max-width: 90px', 'list': 'resistance-units', 'placeholder': 'Ом'}),
            'power': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any', 'placeholder': '0'}),
            'power_unit': forms.TextInput(attrs={'class': 'form-control', 'style': 'max-width: 90px', 'list': 'power-units', 'placeholder': 'Вт'}),
            'voltage': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any', 'placeholder': '0'}),
            'voltage_unit': forms.TextInput(attrs={'class': 'form-control', 'style': 'max-width: 90px', 'list': 'voltage-units', 'placeholder': 'В'}),
            'current': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any', 'placeholder': '0'}),
            'current_unit': forms.TextInput(attrs={'class': 'form-control', 'style': 'max-width: 90px', 'list': 'current-units', 'placeholder': 'А'}),
            'capacitance': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any', 'placeholder': '0'}),
            'capacitance_unit': forms.TextInput(attrs={'class': 'form-control', 'style': 'max-width: 90px', 'list': 'capacitance-units', 'placeholder': 'Ф'}),
            'min_stock': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'lead_time_days': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01',
                                              'min': '0', 'placeholder': '0.00'}),
            'preferred_supplier': forms.TextInput(attrs={'class': 'form-control'}),
            'application': forms.TextInput(attrs={'class': 'form-control', 'list': 'applications',
                                                  'placeholder': 'Otis, ABB, БУАД'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }
        labels = {
            'part_number': 'Артикул',
            'name': 'Название',
            'component_type': 'Тип компонента',
            'package': 'Тип корпуса',
            'resistance': 'Сопротивление',
            'resistance_unit': 'Ед. изм.',
            'power': 'Мощность',
            'power_unit': 'Ед. изм.',
            'voltage': 'Напряжение',
            'voltage_unit': 'Ед. изм.',
            'current': 'Ток',
            'current_unit': 'Ед. изм.',
            'capacitance': 'Ёмкость',
            'capacitance_unit': 'Ед. изм.',
            'min_stock': 'Минимальный остаток',
            'lead_time_days': 'Срок поставки (дней)',
            'preferred_supplier': 'Предпочтительный поставщик',
            'application': 'Применимость',
            'description': 'Описание',
        }

    # Характеристики хранятся с шестью знаками после точки, и в поле формы
    # значение приходит как «0.150000». Править его никто не станет, но
    # выглядит это как точность до микроампера, которой нет
    SPEC_FIELDS = ('resistance', 'power', 'voltage', 'current', 'capacitance')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.SPEC_FIELDS:
            value = self.initial.get(name)
            if value is not None:
                self.initial[name] = format_spec(value)


class OrganizationForm(forms.ModelForm):
    """Реквизиты юрлица — шапка и подписи печатных актов, счета банка."""

    def clean_provider(self):
        """Один банк — одно юрлицо.

        Иначе по выбранному на форме счёта банку нельзя понять, чьи
        реквизиты ставить в документ, и он ушёл бы заказчику от чужого
        имени.
        """
        provider = self.cleaned_data.get('provider') or ''
        if not provider:
            return provider
        taken = Organization.objects.filter(provider=provider)
        if self.instance.pk:
            taken = taken.exclude(pk=self.instance.pk)
        other = taken.first()
        if other is not None:
            raise forms.ValidationError(
                f'Этот банк уже закреплён за юрлицом «{other}». '
                f'Сначала освободите его там.'
            )
        return provider

    class Meta:
        model = Organization
        fields = ['name', 'inn', 'kpp', 'ogrn', 'address', 'city', 'phone', 'email',
                  'signatory_position', 'signatory_name',
                  'bank_name', 'bank_bik', 'bank_account', 'corr_account',
                  'tax_note', 'provider', 'is_default']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'ООО «Название»'}),
            'inn': forms.TextInput(attrs={'class': 'form-control'}),
            'kpp': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'signatory_position': forms.TextInput(attrs={'class': 'form-control'}),
            'signatory_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Иванов И. И.'}),
            'ogrn': forms.TextInput(attrs={'class': 'form-control'}),
            'city': forms.TextInput(attrs={'class': 'form-control'}),
            'bank_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'АО «ТБанк»'}),
            'bank_bik': forms.TextInput(attrs={'class': 'form-control'}),
            'bank_account': forms.TextInput(attrs={'class': 'form-control'}),
            'corr_account': forms.TextInput(attrs={'class': 'form-control'}),
            'provider': forms.Select(attrs={'class': 'form-select'}),
            'is_default': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'tax_note': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Без НДС, применяется УСН'}),
        }


class PaymentForm(forms.ModelForm):
    """Внесение поступивших денег по заказу."""

    class Meta:
        model = Payment
        fields = ['amount', 'payment_date', 'note']
        widgets = {
            'amount': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'min': '0.01',
                'placeholder': '0.00',
            }),
            'payment_date': forms.DateInput(
                attrs={'class': 'form-control', 'type': 'date'}, format='%Y-%m-%d'),
            'note': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Платёжное поручение, наличные…'}),
        }
        labels = {
            'amount': 'Сумма, ₽',
            'payment_date': 'Дата поступления',
            'note': 'Примечание',
        }


class CabinetForm(forms.ModelForm):
    """Кассетница и её раскладка по рядам.

    Раскладка вводится строкой «8, 8, 8, 8, 8, 4, 4, 4»: сверху мелкие
    ячейки, снизу крупные — ровно так устроены органайзеры, и ряд из
    четырёх ячеек означает четыре крупных ящика.
    """
    MAX_ROWS = 30
    MAX_CELLS_PER_ROW = 24

    layout = forms.CharField(
        label='Ячеек в рядах',
        # required=False, чтобы пустое поле дошло до clean_layout: там текст
        # ошибки объясняет, что вписать, а стандартное «Обязательное поле»
        # не объясняет ничего
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'placeholder': '8, 8, 8, 8, 8, 4, 4, 4'}),
        help_text='По числу на каждый ряд, сверху вниз. «8, 8, 4» — два ряда '
                  'по восемь мелких ячеек и один из четырёх крупных.'
    )

    class Meta:
        model = Cabinet
        fields = ['number', 'name', 'note']
        widgets = {
            'number': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Резисторы'}),
            'note': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['layout'].initial = self.instance.layout_text
        else:
            self.fields['number'].initial = Cabinet.next_number()

    def clean_layout(self):
        counts = parse_layout(self.cleaned_data['layout'])
        if not counts:
            raise forms.ValidationError(
                'Укажите, сколько ячеек в каждом ряду: например «8, 8, 4»')
        if len(counts) > self.MAX_ROWS:
            raise forms.ValidationError(f'Рядов не больше {self.MAX_ROWS}')
        if any(count < 1 for count in counts):
            raise forms.ValidationError('В ряду не может быть ноль ячеек')
        if any(count > self.MAX_CELLS_PER_ROW for count in counts):
            raise forms.ValidationError(
                f'В ряду не больше {self.MAX_CELLS_PER_ROW} ячеек')
        return counts

    def clean(self):
        """Не даём молча выбросить ячейки, в которых лежат детали."""
        cleaned = super().clean()
        counts = cleaned.get('layout')
        if counts and self.instance.pk:
            occupied = self.instance.occupied_outside(counts)
            if occupied:
                addresses = ', '.join(cell.address for cell in occupied[:10])
                more = f' и ещё {len(occupied) - 10}' if len(occupied) > 10 else ''
                self.add_error('layout', (
                    f'В отрезаемых ячейках лежат детали: {addresses}{more}. '
                    'Сначала переложите их, иначе сведения о месте хранения '
                    'потеряются.'
                ))
        return cleaned


class QuoteForm(forms.ModelForm):
    """Условия коммерческого предложения по заказу."""

    class Meta:
        model = RepairOrder
        fields = [
            'quote_subject', 'quote_date', 'quote_valid_until',
            'quote_lead_time', 'quote_payment_terms', 'quote_delivery_terms',
        ]
        widgets = {
            'quote_subject': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'на ремонт приводов дверей EkoDrive-2.3-1.3'}),
            'quote_date': forms.DateInput(
                attrs={'class': 'form-control', 'type': 'date'}, format='%Y-%m-%d'),
            'quote_valid_until': forms.DateInput(
                attrs={'class': 'form-control', 'type': 'date'}, format='%Y-%m-%d'),
            'quote_lead_time': forms.TextInput(attrs={'class': 'form-control'}),
            'quote_payment_terms': forms.TextInput(attrs={'class': 'form-control'}),
            'quote_delivery_terms': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def clean(self):
        cleaned = super().clean()
        quote_date = cleaned.get('quote_date')
        valid_until = cleaned.get('quote_valid_until')
        if quote_date and valid_until and valid_until < quote_date:
            self.add_error('quote_valid_until', 'Срок действия раньше даты предложения')
        return cleaned


class QuoteLineForm(forms.ModelForm):
    """Строка предложения: что предлагаем сделать и почём."""

    class Meta:
        model = RepairOrderEquipment
        fields = ['proposed_work', 'repair_complexity', 'estimated_cost']
        widgets = {
            'proposed_work': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2,
                'placeholder': 'Ремонт импульсного блока питания, замена транзисторов'}),
            'repair_complexity': forms.Select(attrs={'class': 'form-select'}),
            'estimated_cost': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
        }


QuoteLineFormSet = inlineformset_factory(
    RepairOrder, RepairOrderEquipment, form=QuoteLineForm,
    extra=0, can_delete=False,
)


class InvoiceSendForm(forms.Form):
    """Подтверждение выставления счёта через API банка.

    Обычная форма, а не ModelForm: часть полей уходит в банк и в заказе
    не хранится, а номер счёта человек правит перед самой отправкой —
    программа не знает, какие номера уже заняты счетами из личного кабинета.

    Банк — обычное поле выбора: оно подставляется по карточке сотрудника,
    но остаётся видимым и доступным для правки. Спрятать его при наличии
    подстановки нельзя: бухгалтеры подменяют друг друга, и тогда счёт
    молча ушёл бы не от того юрлица.
    """
    provider = forms.ChoiceField(
        label='Банк', choices=invoicing.PROVIDER_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
        help_text='Подставлен банк, указанный у вас в карточке сотрудника. '
                  'Счёт уйдёт от того юрлица, за которым закреплён этот банк.'
    )
    invoice_number = forms.CharField(
        label='Номер счёта', max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
        help_text='Проверьте по личному кабинету банка: программа не видит '
                  'счета, выставленные там руками, и сквозной ряд держите вы.'
    )
    invoice_date = forms.DateField(
        label='Дата счёта',
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'},
                               format='%Y-%m-%d')
    )
    due_date = forms.DateField(
        label='Оплатить до', required=False,
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'},
                               format='%Y-%m-%d')
    )
    emails = forms.CharField(
        label='Кому отправить', required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'placeholder': 'buh@example.ru, director@example.ru'}),
        help_text='Через запятую. Пусто — банк счёт создаст, но никому не пошлёт.'
    )

    def clean_invoice_number(self):
        number = self.cleaned_data['invoice_number'].strip()
        if not number:
            raise forms.ValidationError('Номер счёта обязателен')
        return number

    def clean_emails(self):
        """Адреса списком, каждый проверен по отдельности.

        Одна опечатка в списке не должна молча съесть весь список: счёт
        уйдёт не туда, а человек будет уверен, что отправил.
        """
        raw = self.cleaned_data.get('emails', '')
        addresses = [part.strip() for part in raw.replace(';', ',').split(',') if part.strip()]
        validator = validators.EmailValidator()
        for address in addresses:
            try:
                validator(address)
            except forms.ValidationError:
                raise forms.ValidationError(f'Непохоже на адрес почты: {address}')
        return addresses

    def clean(self):
        cleaned = super().clean()
        invoice_date = cleaned.get('invoice_date')
        due_date = cleaned.get('due_date')
        if invoice_date and due_date and due_date < invoice_date:
            self.add_error('due_date', 'Срок оплаты раньше даты счёта')
        return cleaned


class StockMovementForm(forms.ModelForm):
    """Форма для прихода на складе."""
    class Meta:
        model = StockMovement
        fields = ['quantity', 'unit_price', 'document_number', 'notes']
        widgets = {
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'unit_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01',
                                                   'min': '0', 'placeholder': '0.00'}),
            'document_number': forms.TextInput(attrs={'class': 'form-control'}),
            'notes': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Примечание'}),
        }
        labels = {
            'quantity': 'Количество',
            'unit_price': 'Цена за штуку, ₽',
            'document_number': 'Номер документа',
            'notes': 'Примечания',
        }


class StockOutgoingForm(forms.Form):
    """Форма для ручного списания деталей (расход / инвентаризация)."""
    quantity = forms.IntegerField(
        widget=forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
        label='Количество',
        min_value=1
    )
    document_number = forms.CharField(
        widget=forms.TextInput(attrs={'class': 'form-control'}),
        label='Номер документа',
        required=False
    )
    notes = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        label='Основание (фактический расход / инвентаризация)',
        required=False
    )
    reason = forms.ChoiceField(
        choices=[
            ('consumption', 'Фактический расход'),
            ('inventory', 'Инвентаризация'),
            ('defect', 'Брак'),
            ('other', 'Другое'),
        ],
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Причина списания'
    )


class EmployeeForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
        label='Пароль', required=False,
        help_text='Оставьте пустым, чтобы не менять пароль при редактировании'
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
        label='Подтверждение пароля', required=False
    )

    class Meta:
        model = Employee
        fields = ['username', 'full_name', 'email', 'max_user_id', 'telegram_chat_id',
                  'role', 'is_active', 'default_provider',
                  'notify_by_email', 'notify_by_max', 'notify_by_telegram']
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'full_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'max_user_id': forms.TextInput(attrs={'class': 'form-control',
                                                  'inputmode': 'numeric'}),
            'telegram_chat_id': forms.TextInput(attrs={'class': 'form-control',
                                                       'inputmode': 'numeric'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'default_provider': forms.Select(attrs={'class': 'form-select'}),
            'notify_by_email': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_by_max': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_by_telegram': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'username': 'Логин',
            'full_name': 'ФИО',
            'email': 'Email',
            'max_user_id': 'ID в MAX',
            'telegram_chat_id': 'ID в Telegram',
            'role': 'Роль',
            'is_active': 'Активен',
            'default_provider': 'Банк по умолчанию',
            'notify_by_email': 'Оповещения на почту',
            'notify_by_max': 'Оповещения в MAX',
            'notify_by_telegram': 'Оповещения в Telegram',
        }
        help_texts = {
            'max_user_id': 'Число. Узнаётся командой max_updates после того, '
                           'как сотрудник напишет боту (см. DEPLOY.md)',
            'telegram_chat_id': 'Число. Узнаётся командой telegram_updates',
            'default_provider': 'Из какого банка этот бухгалтер обычно '
                                'выставляет счета. Только подсказка: банк '
                                'на форме счёта можно поменять',
            'notify_by_email': 'Внутренние оповещения — дефицит деталей, '
                               'задолженности. Личных оповещений заказчикам '
                               'это не касается',
        }

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password_confirm = cleaned_data.get('password_confirm')
        if password and password != password_confirm:
            self.add_error('password_confirm', 'Пароли не совпадают')
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get('password'):
            user.set_password(self.cleaned_data['password'])
        if commit:
            user.save()
        return user


class MyNotificationsForm(forms.ModelForm):
    """Личный выбор канала внутренних оповещений — страница «Мои оповещения».

    Только три чекбокса, без роли и пароля: это форма сотрудника про себя,
    а не карточка пользователя, которую правит администратор.
    """
    class Meta:
        model = Employee
        fields = ['notify_by_email', 'notify_by_max', 'notify_by_telegram']
        widgets = {
            'notify_by_email': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_by_max': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_by_telegram': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'notify_by_email': 'Оповещения на почту',
            'notify_by_max': 'Оповещения в MAX',
            'notify_by_telegram': 'Оповещения в Telegram',
        }
        help_texts = {
            'notify_by_email': 'Дефицит деталей, задолженности — то, что вам '
                               'сейчас приходит по вашей роли. Если канал '
                               'выключен для всех в настройках программы '
                               'или у вас не заполнен ID, галочка здесь '
                               'ничего не изменит',
        }


class StatusChangeForm(forms.Form):
    new_status = forms.ChoiceField(
        choices=RepairOrder.STATUS_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Новый статус'
    )
    notes = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        label='Примечания', required=False
    )


class PartImportForm(forms.Form):
    file = forms.FileField(
        label='Excel файл (.xlsx)',
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.xlsx'})
    )
    update_existing = forms.BooleanField(
        label='Обновлять существующие детали',
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )




