"""
Отправка накопившихся оповещений. Запускается по расписанию systemd-таймером
(deploy/lifteam-notify.timer), а не из обработчика страницы.

Почему отдельной командой: SMTP через домашний канал отвечает секундами,
а бывает, что не отвечает вовсе. Сохранение заказа не должно ни ждать почту,
ни падать из-за неё, а неудачную отправку нужно уметь повторить.
"""
from datetime import timedelta

from django.conf import settings
from django.core.mail import get_connection, EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone

from core import messengers
from core.models import Notification

# Чем отправлять сообщение каждого канала мессенджера. Почта устроена иначе:
# ей нужно соединение, общее на всю пачку, поэтому она обрабатывается отдельно
MESSENGERS = {
    Notification.CHANNEL_MAX:
        lambda item: messengers.send_max_message(item.recipient, item.body),
    Notification.CHANNEL_TELEGRAM:
        lambda item: messengers.send_telegram_message(item.recipient, item.body),
}


class Command(BaseCommand):
    help = 'Отправляет оповещения из очереди'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=50,
            help='Сколько оповещений отправить за один запуск (по умолчанию 50)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что было бы отправлено, ничего не отправляя',
        )

    def handle(self, *args, **options):
        if not getattr(settings, 'NOTIFICATIONS_ENABLED', False):
            self.stdout.write(
                'Отправка оповещений выключена (NOTIFICATIONS_ENABLED). '
                'Очередь продолжает наполняться.'
            )
            return

        max_attempts = getattr(settings, 'NOTIFICATIONS_MAX_ATTEMPTS', 5)
        max_age_hours = getattr(settings, 'NOTIFICATIONS_MAX_AGE_HOURS', 24)

        # Просроченные не отправляем: включив отправку через месяц после
        # накопления очереди, заказчик получил бы пачку новостей о давно
        # закрытых заказах
        stale_before = timezone.now() - timedelta(hours=max_age_hours)
        stale = Notification.objects.filter(status='pending', created_at__lt=stale_before)
        stale_count = stale.update(
            status='skipped',
            last_error=f'Старше {max_age_hours} ч на момент отправки',
        )

        queued = list(
            Notification.objects
            .filter(status='pending', attempts__lt=max_attempts)
            .order_by('created_at')[:options['limit']]
        )

        if options['dry_run']:
            # Про то, что это была проверка, нужно сказать словами и в конце:
            # квадратные скобки в начале строк люди пролистывают, а потом
            # ищут причину, по которой «письма не уходят»
            for item in queued:
                self.stdout.write(
                    f'[проверка] {item.get_channel_display()} → {item.recipient}: {item.subject}'
                )
            self.stdout.write(f'В очереди: {len(queued)}, просрочено: {stale_count}')
            self.stdout.write(self.style.WARNING(
                'Это была проверка (--dry-run): НИЧЕГО НЕ ОТПРАВЛЕНО, '
                'очередь не тронута. Чтобы отправить, запустите ту же команду '
                'без --dry-run.'
            ))
            return

        sent = failed = 0
        connection = None
        try:
            # Соединение с почтой открываем лениво и одно на всю пачку:
            # открывать SMTP-сессию на каждое письмо — лишние секунды каждый
            # раз, а если писем в пачке нет вовсе, она и не понадобится
            for item in queued:
                messenger = MESSENGERS.get(item.channel)
                if messenger is not None:
                    ok = self._deliver(item, max_attempts, messenger)
                else:
                    if connection is None:
                        connection = get_connection()
                    ok = self._deliver(
                        item, max_attempts,
                        lambda entry: self._send_email(entry, connection),
                    )
                if ok:
                    sent += 1
                else:
                    failed += 1
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

        self.stdout.write(
            f'Отправлено: {sent}, не удалось: {failed}, просрочено: {stale_count}'
        )

    def _send_email(self, item, connection):
        EmailMessage(
            subject=item.subject,
            body=item.body,
            to=[item.recipient],
            connection=connection,
        ).send(fail_silently=False)

    def _deliver(self, item, max_attempts, send):
        item.attempts += 1
        try:
            send(item)
        except Exception as error:
            # Текст ошибки обрезаем: в него попадает ответ сервера целиком,
            # а в списке нужна причина, а не простыня
            item.last_error = str(error)[:500]
            item.status = 'failed' if item.attempts >= max_attempts else 'pending'
            item.save(update_fields=['attempts', 'last_error', 'status'])
            self.stderr.write(f'{item.recipient}: {item.last_error}')
            return False

        item.status = 'sent'
        item.sent_at = timezone.now()
        item.last_error = ''
        item.save(update_fields=['attempts', 'status', 'sent_at', 'last_error'])
        return True
