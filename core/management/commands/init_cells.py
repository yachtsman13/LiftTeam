"""
Management command: init_cells
LiftTeam v2.21.0
"""
from django.core.management.base import BaseCommand
from core.models import StorageCell


class Command(BaseCommand):
    help = 'Создание ячеек хранения (кассетницы)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--cabinets',
            type=int,
            default=12,
            help='Количество кассетниц (по умолчанию 12)'
        )
        parser.add_argument(
            '--rows',
            type=int,
            default=8,
            help='Количество рядов в кассетнице (по умолчанию 8)'
        )
        parser.add_argument(
            '--cells',
            type=int,
            default=8,
            help='Количество ячеек в ряду (по умолчанию 8)'
        )

    def handle(self, *args, **options):
        cabinets = options['cabinets']
        rows = options['rows']
        cells = options['cells']

        total = 0
        for cabinet in range(1, cabinets + 1):
            for row in range(1, rows + 1):
                for cell in range(1, cells + 1):
                    obj, created = StorageCell.objects.get_or_create(
                        cabinet_number=cabinet,
                        row_number=row,
                        cell_row=cell
                    )
                    if created:
                        total += 1

        self.stdout.write(self.style.SUCCESS(
            f'Создано {total} ячеек хранения ({cabinets} кассетниц x {rows} рядов x {cells} ячеек)'
        ))




