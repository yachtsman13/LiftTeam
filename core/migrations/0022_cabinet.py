"""
Кассетница становится объектом.

Раньше ячейка хранила номер кассетницы числом, а геометрия (12 штук по 8×8)
была зашита в коде. Миграция заводит модель, переносит существующие ячейки
на ссылку и убирает число — без потери разложенных деталей.
"""
import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


def create_cabinets(apps, schema_editor):
    """Из номеров, что стоят у ячеек, делаем сами кассетницы."""
    Cabinet = apps.get_model('core', 'Cabinet')
    StorageCell = apps.get_model('core', 'StorageCell')

    numbers = sorted(
        StorageCell.objects.values_list('cabinet_number', flat=True).distinct()
    )
    for number in numbers:
        cabinet, _ = Cabinet.objects.get_or_create(number=number)
        StorageCell.objects.filter(cabinet_number=number).update(cabinet=cabinet)


def restore_numbers(apps, schema_editor):
    StorageCell = apps.get_model('core', 'StorageCell')
    for cell in StorageCell.objects.select_related('cabinet'):
        cell.cabinet_number = cell.cabinet.number
        cell.save(update_fields=['cabinet_number'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_organization_city'),
    ]

    operations = [
        migrations.CreateModel(
            name='Cabinet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('number', models.PositiveIntegerField(
                    help_text='Печатается в адресе ячейки: К1-Р1-Я1', unique=True,
                    validators=[django.core.validators.MinValueValidator(1)],
                    verbose_name='Номер')),
                ('name', models.CharField(
                    blank=True,
                    help_text='Необязательно: «Резисторы», «Крепёж», «Над столом»',
                    max_length=100, verbose_name='Название')),
                ('note', models.CharField(blank=True, max_length=255,
                                          verbose_name='Примечание')),
            ],
            options={
                'verbose_name': 'Кассетница',
                'verbose_name_plural': 'Кассетницы',
                'ordering': ['number'],
            },
        ),
        # Уникальность снимаем до переноса: пока живут оба поля, старое
        # ограничение мешает промежуточному состоянию
        migrations.AlterUniqueTogether(
            name='storagecell',
            unique_together=set(),
        ),
        migrations.AddField(
            model_name='storagecell',
            name='cabinet',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='cells', to='core.cabinet', verbose_name='Кассетница'),
        ),
        migrations.RunPython(create_cabinets, restore_numbers),
        migrations.AlterField(
            model_name='storagecell',
            name='cabinet',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='cells', to='core.cabinet', verbose_name='Кассетница'),
        ),
        migrations.RemoveField(
            model_name='storagecell',
            name='cabinet_number',
        ),
        migrations.AlterUniqueTogether(
            name='storagecell',
            unique_together={('cabinet', 'row_number', 'cell_row')},
        ),
        migrations.AlterModelOptions(
            name='storagecell',
            options={
                'ordering': ['cabinet__number', 'row_number', 'cell_row'],
                'verbose_name': 'Ячейка хранения',
                'verbose_name_plural': 'Ячейки хранения',
            },
        ),
    ]
