from django.db import migrations, models


def migrate_part_to_parts(apps, schema_editor):
    StorageCell = apps.get_model('core', 'StorageCell')
    for cell in StorageCell.objects.exclude(part__isnull=True):
        cell.parts.add(cell.part)


def reverse_parts_to_part(apps, schema_editor):
    StorageCell = apps.get_model('core', 'StorageCell')
    for cell in StorageCell.objects.all():
        first_part = cell.parts.first()
        if first_part:
            cell.part = first_part
            cell.save(update_fields=['part'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_stockmovement_created_by'),
    ]

    operations = [
        migrations.AddField(
            model_name='storagecell',
            name='parts',
            field=models.ManyToManyField(blank=True, related_name='storage_cells', to='core.sparepart', verbose_name='Детали'),
        ),
        migrations.RunPython(migrate_part_to_parts, reverse_parts_to_part),
        migrations.RemoveField(
            model_name='storagecell',
            name='part',
        ),
    ]
