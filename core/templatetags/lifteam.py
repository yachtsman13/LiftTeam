"""Фильтры шаблонов LiftTeam."""
from django import template

from ..models import format_spec

register = template.Library()


@register.filter(name='spec')
def spec(value):
    """Значение характеристики без хвостовых нулей: «0.15», а не «0.150000».

    Django хранит характеристики с шестью знаками после точки и дописывает
    нули при каждой записи; `floatformat:"-6"` их не убирает — отрицательная
    точность отбрасывает дробную часть только у целых чисел.
    """
    return format_spec(value)
