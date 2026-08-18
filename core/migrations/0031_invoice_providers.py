"""Два банка вместо одного: справочник юрлиц и выбор банка у счёта.

Поля счёта переименованы, а не заведены заново: в них лежат ссылки
на уже выставленные счета, и потерять их нельзя. RenameField сохраняет
содержимое, AddField+RemoveField потеряло бы его.
"""
from django.db import migrations, models


def bind_the_existing_entity(apps, schema_editor):
    """Прежняя единственная запись становится основным юрлицом.

    До этой версии запись была одна и банк был один — Т-Банк. Значит,
    именно она и есть юрлицо Т-Банка, и все прежние счета выставлены
    от неё. Так её и помечаем, чтобы печатные акты не заметили разницы:
    основное юрлицо у них то же самое, что и было единственным.
    """
    Organization = apps.get_model('core', 'Organization')
    first = Organization.objects.order_by('pk').first()
    if first is None:
        return
    Organization.objects.filter(pk=first.pk).update(is_default=True, provider='tbank')


def unbind(apps, schema_editor):
    """Откат: пометки снимаются, записи остаются."""
    Organization = apps.get_model('core', 'Organization')
    Organization.objects.update(is_default=False, provider='')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_employee_last_seen'),
    ]

    operations = [
        migrations.RenameField(
            model_name='repairorder',
            old_name='tbank_invoice_sent_at',
            new_name='invoice_sent_at',
        ),
        migrations.RenameField(
            model_name='repairorder',
            old_name='tbank_invoice_pdf_url',
            new_name='invoice_pdf_url',
        ),
        migrations.RenameField(
            model_name='repairorder',
            old_name='tbank_invoice_error',
            new_name='invoice_error',
        ),
        migrations.AddField(
            model_name='repairorder',
            name='invoice_provider',
            field=models.CharField(
                blank=True,
                choices=[('tbank', 'Т-Банк'), ('tochka', 'Точка Банк')],
                max_length=20, verbose_name='Банк счёта'),
        ),
        migrations.AddField(
            model_name='repairorder',
            name='invoice_external_id',
            field=models.CharField(
                blank=True, max_length=100,
                verbose_name='Идентификатор счёта в банке'),
        ),
        migrations.AddField(
            model_name='organization',
            name='provider',
            field=models.CharField(
                blank=True,
                choices=[('tbank', 'Т-Банк'), ('tochka', 'Точка Банк')],
                help_text='Через какой банк это юрлицо выставляет счета. '
                          'Один банк можно закрепить только за одним юрлицом.',
                max_length=20, verbose_name='Банк для счетов'),
        ),
        migrations.AddField(
            model_name='organization',
            name='is_default',
            field=models.BooleanField(
                default=False,
                help_text='Его реквизиты стоят в актах и коммерческом предложении',
                verbose_name='Основное юрлицо'),
        ),
        migrations.AddField(
            model_name='employee',
            name='default_provider',
            field=models.CharField(
                blank=True,
                choices=[('tbank', 'Т-Банк'), ('tochka', 'Точка Банк')],
                help_text='Подставляется в форму счёта. '
                          'Поменять можно на самой форме.',
                max_length=20, verbose_name='Банк по умолчанию'),
        ),
        migrations.AlterModelOptions(
            name='organization',
            options={'ordering': ['-is_default', 'name'],
                     'verbose_name': 'Юрлицо',
                     'verbose_name_plural': 'Юрлица'},
        ),
        migrations.RunPython(bind_the_existing_entity, unbind),
        migrations.AddConstraint(
            model_name='organization',
            constraint=models.UniqueConstraint(
                condition=models.Q(('provider', ''), _negated=True),
                fields=('provider',),
                name='unique_organization_per_provider'),
        ),
    ]
