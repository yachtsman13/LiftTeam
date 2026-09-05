# Ремонт для баз, уже прошедших ошибочную версию 0057 (см. её комментарий
# и CLAUDE.md): там `diagnosis` физически удалена из таблицы, и Джанго
# эту миграцию у себя не перевыполнит — только новая миграция и заставит
# его тронуть такую базу ещё раз.
#
# `state_operations` пуст: в состоянии моделей `diagnosis` и так стоит
# с 0017 без перерыва (0057 в её нынешнем виде поле не трогает) — второй
# раз объявлять то же самое поле в состоянии Джанго не даст (ошибка
# «поле уже есть»). Настоящая проверка — по факту, через PRAGMA
# table_info: столбец есть — не трогаем (это как раз случай баз вроде
# этой, разработческой, где данные диагностики не терялись ни разу);
# столбца нет — досоздаём пустым. Восстановить сами записи, стёртые
# ошибочной миграцией на Pi, отсюда нельзя — их взять неоткуда, кроме
# резервной копии, снятой до той миграции.
from django.db import migrations


def add_diagnosis_if_missing(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(core_repairorderequipment)")
        columns = {row[1] for row in cursor.fetchall()}
    if 'diagnosis' not in columns:
        schema_editor.execute(
            "ALTER TABLE core_repairorderequipment "
            "ADD COLUMN \"diagnosis\" text NOT NULL DEFAULT ''"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0058_repair_impossible_and_repairer_check"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[],
            database_operations=[
                migrations.RunPython(add_diagnosis_if_missing, migrations.RunPython.noop),
            ],
        ),
    ]
