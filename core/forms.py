"""
Формы для LiftTeam v2.8.0.
"""
from django import forms
from django.contrib.auth import authenticate
from django.forms import inlineformset_factory
from .models import (
    Client, EquipmentModel, Equipment, RepairOrder, RepairOrderEquipment,
    RepairOrderDetail, SparePart, StockMovement, Employee
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
        fields = ['name', 'inn', 'kpp', 'contact_person', 'phone', 'email']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'inn': forms.TextInput(attrs={'class': 'form-control'}),
            'kpp': forms.TextInput(attrs={'class': 'form-control'}),
            'contact_person': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'name': 'Название',
            'inn': 'ИНН',
            'kpp': 'КПП',
            'contact_person': 'Контактное лицо',
            'phone': 'Телефон',
            'email': 'Email',
        }


class EquipmentModelForm(forms.ModelForm):
    class Meta:
        model = EquipmentModel
        fields = ['name']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'name': 'Название модели',
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
        fields = ['equipment', 'fault_description', 'seal_numbers', 'initial_condition', 'repair_cost', 'yandex_disk_folder']
        widgets = {
            'equipment': forms.Select(attrs={'class': 'form-select'}),
            'fault_description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Описание неисправности'}),
            'seal_numbers': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Номера пломб'}),
            'initial_condition': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Начальное состояние'}),
            'repair_cost': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'yandex_disk_folder': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://disk.yandex.ru/...'}),
        }
        labels = {
            'equipment': 'Оборудование',
            'fault_description': 'Неисправность',
            'seal_numbers': 'Номера пломб',
            'initial_condition': 'Начальное состояние',
            'repair_cost': 'Стоимость ремонта',
            'yandex_disk_folder': 'Папка на Яндекс.Диске',
        }


RepairOrderEquipmentFormSet = inlineformset_factory(
    RepairOrder, RepairOrderEquipment, form=RepairOrderEquipmentForm,
    extra=1, can_delete=True
)


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
            'part_number', 'name', 'component_type',
            'resistance', 'resistance_unit',
            'power', 'power_unit',
            'voltage', 'voltage_unit',
            'current', 'current_unit',
            'capacitance', 'capacitance_unit',
            'min_stock', 'lead_time_days',
            'preferred_supplier', 'description'
        ]
        widgets = {
            'part_number': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'component_type': forms.TextInput(attrs={'class': 'form-control', 'list': 'component-types'}),
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
            'preferred_supplier': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }
        labels = {
            'part_number': 'Артикул',
            'name': 'Название',
            'component_type': 'Тип компонента',
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
            'description': 'Описание',
        }


class StockMovementForm(forms.ModelForm):
    """Форма для прихода на складе."""
    class Meta:
        model = StockMovement
        fields = ['quantity', 'document_number', 'notes']
        widgets = {
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'document_number': forms.TextInput(attrs={'class': 'form-control'}),
            'notes': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Примечание'}),
        }
        labels = {
            'quantity': 'Количество',
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
        fields = ['username', 'full_name', 'email', 'role', 'is_active']
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'full_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'username': 'Логин',
            'full_name': 'ФИО',
            'email': 'Email',
            'role': 'Роль',
            'is_active': 'Активен',
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




