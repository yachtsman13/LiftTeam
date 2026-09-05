# Тот же ремонт, что и в 0059, только для error_codes и в обратную
# сторону: на большинстве баз (не заставших ошибочную версию 0057) её
# уже убрала сама 0057 — здесь для них нечего делать. На Pi, где 0057
# под этим именем уже отмечена применённой и потому не перевыполнится,
# столбец всё ещё живой — эта миграция и снимает его по-настоящему.
#
# `state_operations` пуст по той же причине, что и в 0059: из состояния
# моделей error_codes уже убран самой 0057, второй раз убирать оттуда
# нечего.
from django.db import migrations


def drop_error_codes_if_present(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(core_repairorderequipment)")
        columns = {row[1] for row in cursor.fetchall()}
    if 'error_codes' in columns:
        schema_editor.execute(
            "ALTER TABLE core_repairorderequipment DROP COLUMN \"error_codes\""
        )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0059_restore_diagnosis_if_missing"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[],
            database_operations=[
                migrations.RunPython(drop_error_codes_if_present, migrations.RunPython.noop),
            ],
        ),
    ]
