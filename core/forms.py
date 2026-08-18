"""
Формы для LiftTeam v2.42.0.
"""
from django import forms
from django.contrib.auth import authenticate
from django.core import validators
from django.forms import inlineformset_factory
from .models import (
    Cabinet, Client, EquipmentModel, Equipment, RepairOrder, RepairOrderEquipment,
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


class EquipmentModelForm(forms.ModelForm):
    class Meta:
        model = EquipmentModel
        fields = ['name', 'kind']
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
        }
        labels = {
            'name': 'Название модели',
            'kind': 'Тип оборудования',
        }


class EquipmentForm(forms.ModelForm):
    class Meta:
        model = Equipment
        fields = ['model', 'serial_number', 'current_client']
        widgets = {
            'model': forms.Select(attrs={'class': 'form-select'}),
            'serial_number': forms.TextInput(attrs={'class': 'form-control'}),
            'current_client': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'model': 'Модель',
            'serial_number': 'Серийный номер',
            'current_client': 'Текущий заказчик',
        }


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


class RepairOrderEquipmentForm(forms.ModelForm):
    class Meta:
        model = RepairOrderEquipment
        fields = ['equipment', 'fault_description', 'work_performed', 'seal_numbers', 'initial_condition', 'repair_cost', 'yandex_disk_folder']
        widgets = {
            'equipment': forms.Select(attrs={'class': 'form-select'}),
            'fault_description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Описание неисправности'}),
            'work_performed': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Что сделали — попадёт в акт выполненных работ'}),
            'seal_numbers': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Номера пломб'}),
            'initial_condition': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Начальное состояние'}),
            'repair_cost': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'yandex_disk_folder': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://disk.yandex.ru/...'}),
        }
        labels = {
            'equipment': 'Оборудование',
            'fault_description': 'Неисправность',
            'work_performed': 'Выполненные работы',
            'seal_numbers': 'Номера пломб',
            'initial_condition': 'Начальное состояние',
            'repair_cost': 'Стоимость ремонта',
            'yandex_disk_folder': 'Папка на Яндекс.Диске',
        }


RepairOrderEquipmentFormSet = inlineformset_factory(
    RepairOrder, RepairOrderEquipment, form=RepairOrderEquipmentForm,
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
    """Реквизиты своей фирмы — шапка и подписи печатных актов."""

    class Meta:
        model = Organization
        fields = ['name', 'inn', 'kpp', 'ogrn', 'address', 'city', 'phone', 'email',
                  'signatory_position', 'signatory_name',
                  'bank_name', 'bank_bik', 'bank_account', 'corr_account',
                  'tax_note']
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
    """Подтверждение выставления счёта через API Т-Банка.

    Обычная форма, а не ModelForm: часть полей уходит в банк и в заказе
    не хранится, а номер счёта человек правит перед самой отправкой —
    программа не знает, какие номера уже заняты счетами из личного кабинета.
    """
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
                  'role', 'is_active',
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
            'notify_by_email': 'Оповещения на почту',
            'notify_by_max': 'Оповещения в MAX',
            'notify_by_telegram': 'Оповещения в Telegram',
        }
        help_texts = {
            'max_user_id': 'Число. Узнаётся командой max_updates после того, '
                           'как сотрудник напишет боту (см. DEPLOY.md)',
            'telegram_chat_id': 'Число. Узнаётся командой telegram_updates',
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




