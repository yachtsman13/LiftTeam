"""Общее описание неисправности у заказа убрано.

Поле спрашивали при приёме, но заполняли его вместо описания по прибору —
то есть спрашивали одно и то же дважды. По базе владельца оно не было
заполнено ни в одном заказе, и он решил его убрать.

Прежде чем удалять столбец, непустой текст перекладывается в те единицы,
у которых своего описания нет. Ровно так его и печатал акт приёма:
описание единицы, а если его нет — общее по заказу. Так что ничей текст
не пропадает, а оказывается там, где его и читали.
"""
from django.db import migrations, models


def move_description_to_units(apps, schema_editor):
    RepairOrder = apps.get_model('core', 'RepairOrder')
    RepairOrderEquipment = apps.get_model('core', 'RepairOrderEquipment')

    for order in RepairOrder.objects.exclude(fault_description='').iterator():
        text = (order.fault_description or '').strip()
        if not text:
            continue
        RepairOrderEquipment.objects.filter(
            repair_order=order, fault_description=''
        ).update(fault_description=text)


def put_it_back(apps, schema_editor):
    """Разъединить обратно нельзя, и делать вид, что можно, не надо.

    После обратной миграции описания останутся у единиц — там, где им
    и место. Пустое поле заказа при этом верно: общего описания больше
    нет ни у одного из них.
    """


def medium_becomes_complex(apps, schema_editor):
    """«Средний» ремонт убран: владелец сказал, что такого не бывает.

    Уже проставленный переводится в «сложный», а не в «простой»: занизить
    сложность значит занизить цену по прайсу, а завысить — всего лишь
    попросить перечитать. По базе владельца таких строк нет ни одной,
    но на рабочей установке заказы старше.
    """
    RepairOrderEquipment = apps.get_model('core', 'RepairOrderEquipment')
    RepairOrderEquipment.objects.filter(repair_complexity='medium').update(
        repair_complexity='complex'
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0046_faulttype_work_description'),
    ]

    operations = [
        migrations.RunPython(move_description_to_units, put_it_back),
        migrations.RunPython(medium_becomes_complex, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='repairorder',
            name='fault_description',
        ),
        migrations.AlterField(
            model_name='repairorderequipment',
            name='repair_complexity',
            field=models.CharField(
                blank=True,
                choices=[('simple', 'Простой'), ('complex', 'Сложный')],
                max_length=20,
                verbose_name='Сложность ремонта',
            ),
        ),
    ]
