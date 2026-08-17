"""
Создание кассетниц с ячейками — для первого запуска на пустой базе.

Дальше кассетницы заводят в программе: «Кассетницы → Новая кассетница»,
там же задаётся раскладка по рядам. Команда нужна ровно затем, чтобы
свежая установка не начиналась с пустого склада.
"""
from django.core.management.base import BaseCommand

from core.models import Cabinet, parse_layout


class Command(BaseCommand):
    help = 'Создаёт кассетницы с ячейками'

    def add_arguments(self, parser):
        parser.add_argument(
            '--cabinets', type=int, default=12,
            help='Сколько кассетниц завести (по умолчанию 12)',
        )
        parser.add_argument(
            '--layout', default='',
            help='Ячеек в рядах: «8,8,8,8,8,4,4». По умолчанию восемь рядов '
                 'по восемь ячеек',
        )
        parser.add_argument(
            '--rows', type=int, default=8,
            help='Рядов в кассетнице, если не задан --layout (по умолчанию 8)',
        )
        parser.add_argument(
            '--cells', type=int, default=8,
            help='Ячеек в ряду, если не задан --layout (по умолчанию 8)',
        )

    def handle(self, *args, **options):
        counts = parse_layout(options['layout'])
        if not counts:
            counts = [options['cells']] * options['rows']

        created_cabinets = 0
        created_cells = 0
        for number in range(1, options['cabinets'] + 1):
            cabinet, created = Cabinet.objects.get_or_create(number=number)
            if created:
                created_cabinets += 1
            # Существующие кассетницы не перекраиваем: раскладку могли
            # поменять руками, и повторный запуск команды не должен
            # затирать эту работу
            if created or not cabinet.cells.exists():
                added, _ = cabinet.apply_layout(counts)
                created_cells += added

        self.stdout.write(self.style.SUCCESS(
            f'Кассетниц создано: {created_cabinets}, ячеек: {created_cells} '
            f'(раскладка {", ".join(str(c) for c in counts)})'
        ))
