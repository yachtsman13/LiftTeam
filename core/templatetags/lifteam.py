"""Фильтры шаблонов LiftTeam."""
from django import template

from ..models import format_power, format_spec

register = template.Library()


@register.filter(name='spec')
def spec(value):
    """Значение характеристики без хвостовых нулей: «0.15», а не «0.150000».

    Django хранит характеристики с шестью знаками после точки и дописывает
    нули при каждой записи; `floatformat:"-6"` их не убирает — отрицательная
    точность отбрасывает дробную часть только у целых чисел.
    """
    return format_spec(value)


@register.filter(name='power')
def power(value):
    """Мощность в кВт без хвостовых нулей: «7,5», не «7.50», «11», не «11,00».

    `{{ value }}` в шаблоне и так меняет точку на запятую по локали
    `ru-ru`, но хвостовые нули не убирает — этим и отличается от `power`,
    который нужен именно там.
    """
    if value is None:
        return ''
    return format_power(value)
