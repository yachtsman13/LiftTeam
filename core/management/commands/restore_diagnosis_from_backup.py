"""
Разовое восстановление RepairOrderEquipment.diagnosis из резервной копии,
снятой до ошибочной миграции v2.111.0 (см. CHANGELOG v2.114.1 и раздел
о `diagnosis`/`error_codes` в CLAUDE.md). Эта миграция стёрла столбец
`diagnosis` на установке, где успела развернуться; сам столбец возвращает
миграция `0059_restore_diagnosis_if_missing` (пустым), а прежний текст
берётся уже отсюда — из значений, вытащенных из копии, снятой до потери.

Команда общая, а данные — нет: файл со значениями (id единицы, номер
заказа и серийный номер для сверки, сам текст) готовится отдельно и сюда
не входит — в нём настоящие записи заказчиков. После восстановления
и сам файл, и эта команда своё дело сделали; удалять их не обязательно,
но и держать вечно незачем — это лекарство от одного случая, а не часть
обычной работы с программой.

Без --apply команда только показывает, что сделала бы (безопасно звать
как угодно часто). Запись уже существующего текста не трогает никогда —
если после инцидента диагностику успели вписать заново, это решение
мастера, а не резервной копии.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.models import RepairOrderEquipment


class Command(BaseCommand):
    help = ('Восстанавливает RepairOrderEquipment.diagnosis из JSON, '
            'выгруженного из резервной копии до потери (см. docstring файла)')

    def add_arguments(self, parser):
        parser.add_argument(
            'json_file',
            help='Путь к файлу вида [{"id":, "order_number":, "serial_number":, "diagnosis":}, ...]',
        )
        parser.add_argument(
            '--apply', action='store_true',
            help='Записать в базу. Без флага — только показать, что будет сделано',
        )

    def handle(self, *args, **options):
        path = Path(options['json_file'])
        if not path.exists():
            raise CommandError(f'Файл не найден: {path}')

        try:
            records = json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CommandError(f'Файл не разобран как JSON: {exc}')

        restored = skipped_filled = not_found = mismatched = 0

        for record in records:
            try:
                unit = RepairOrderEquipment.objects.select_related(
                    'repair_order', 'equipment'
                ).get(pk=record['id'])
            except RepairOrderEquipment.DoesNotExist:
                not_found += 1
                self.stdout.write(self.style.WARNING(
                    f"№{record['id']}: единицы с таким номером больше нет, пропущено"
                ))
                continue

            # Номер заказа и серийник — не для поиска (искали по id), а для
            # проверки, что это та же самая единица, а не другая запись,
            # занявшая тот же номер после удаления и повторного заведения
            if (unit.repair_order.order_number != record['order_number']
                    or unit.equipment.serial_number != record['serial_number']):
                mismatched += 1
                self.stdout.write(self.style.WARNING(
                    f"№{record['id']}: заказ или серийный номер не совпадает "
                    f"с копией (сейчас {unit.repair_order.order_number}/"
                    f"{unit.equipment.serial_number}, в копии "
                    f"{record['order_number']}/{record['serial_number']}) — "
                    f"пропущено, разбираться руками"
                ))
                continue

            if unit.diagnosis:
                skipped_filled += 1
                continue

            restored += 1
            self.stdout.write(
                f"№{record['id']} ({unit.repair_order.order_number}, "
                f"SN {unit.equipment.serial_number}): "
                f"{record['diagnosis'][:60]}{'…' if len(record['diagnosis']) > 60 else ''}"
            )
            if options['apply']:
                unit.diagnosis = record['diagnosis']
                unit.save(update_fields=['diagnosis'])

        self.stdout.write('')
        self.stdout.write(
            f'Восстановлено: {restored}, уже было заполнено: {skipped_filled}, '
            f'не найдено: {not_found}, не совпало: {mismatched}'
        )
        if not options['apply'] and restored:
            self.stdout.write(self.style.WARNING(
                'Это была проверка (--apply не указан) — в базу ничего не записано.'
            ))
