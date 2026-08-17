"""Применимость детали — отдельным полем.

До этого она жила внутри описания строкой «… | Применение: Otis»: так её
записывали каталоги, из которых детали загружали. Читать оттуда неудобно,
искать невозможно, а на этикетке она терялась в общем тексте. Переносим
готовые значения в поле и убираем их из описания.
"""
import re

from django.db import migrations, models

APPLICATION_RE = re.compile(r'[|,;.]?\s*Применение:\s*(?P<value>[^|]+)$', re.IGNORECASE)


def split_application(apps, schema_editor):
    SparePart = apps.get_model('core', 'SparePart')
    updated = []
    for part in SparePart.objects.exclude(description=''):
        match = APPLICATION_RE.search(part.description)
        if not match:
            continue
        part.application = match.group('value').strip()[:100]
        part.description = part.description[:match.start()].strip(' .,;|')
        updated.append(part)
    SparePart.objects.bulk_update(updated, ['application', 'description'], batch_size=200)


def merge_back(apps, schema_editor):
    """Обратный ход: возвращаем применимость в конец описания."""
    SparePart = apps.get_model('core', 'SparePart')
    updated = []
    for part in SparePart.objects.exclude(application=''):
        tail = f'Применение: {part.application}'
        part.description = f'{part.description}. {tail}'.strip(' .') if part.description else tail
        updated.append(part)
    SparePart.objects.bulk_update(updated, ['description'], batch_size=200)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0022_cabinet"),
    ]

    operations = [
        migrations.AddField(
            model_name="sparepart",
            name="application",
            field=models.CharField(
                blank=True,
                help_text="Где применяется: Otis, ABB, БУАД, Altivar",
                max_length=100,
                verbose_name="Применимость",
            ),
        ),
        migrations.RunPython(split_application, merge_back),
    ]
