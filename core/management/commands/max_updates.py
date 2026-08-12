"""
Кто писал боту в MAX и с какими идентификаторами.

Нужна один раз при настройке. Бот не может написать человеку первым и не
знает его по логину: пока сотрудник сам не напишет боту хоть слово, слать
ему нечего и некуда. Эта команда показывает, кто уже написал, — числа из
колонки «ID» вписываются сотрудникам в карточку.

Запуск: python manage.py max_updates
"""
from django.core.management.base import BaseCommand

from core import messengers
from core.models import Employee


class Command(BaseCommand):
    help = 'Показывает идентификаторы тех, кто писал боту MAX'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=100,
            help='Сколько событий запросить (по умолчанию 100)',
        )
        parser.add_argument(
            '--raw', action='store_true',
            help='Вывести ответ MAX как есть, без разбора',
        )

    def handle(self, *args, **options):
        if not messengers.max_is_configured():
            self.stderr.write(
                'Не задан MAX_BOT_TOKEN. Токен выдаёт @MasterBot в MAX '
                'по команде /create, вписывается в .env (см. DEPLOY.md).'
            )
            return

        try:
            data = messengers.get_max_updates(limit=options['limit'])
        except messengers.MessengerError as error:
            self.stderr.write(str(error))
            return

        if options['raw']:
            import json
            self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
            return

        senders, chats = self._collect_senders(data)
        if not senders and not chats:
            self.stdout.write(
                'Боту никто не писал. Попросите сотрудников найти бота в MAX '
                'и отправить ему любое сообщение, потом повторите команду.'
            )
            return

        known = dict(
            Employee.objects.exclude(max_user_id='')
            .values_list('max_user_id', 'full_name')
        )
        if senders:
            self.stdout.write(f'{"ID":<16} {"Имя в MAX":<30} В программе')
            for user_id, name in sorted(senders.items()):
                self.stdout.write(f'{user_id:<16} {name[:30]:<30} {known.get(user_id, "—")}')
            self.stdout.write(
                '\nВпишите нужный ID в карточку сотрудника '
                '(Администрирование → Пользователи → ID в MAX).'
            )

        if chats:
            self.stdout.write('\nЧаты, где бот состоит: ' + ', '.join(sorted(chats)))
            self.stdout.write(
                'Идентификатор общего чата вписывается в MAX_GROUP_CHAT_ID — '
                'тогда оповещение уходит один раз в чат, а не каждому отдельно.'
            )

    def _collect_senders(self, data):
        """Достаёт из ответа отправителей и чаты.

        Формат разбираем осторожно: событий у бота несколько видов, и в них
        лежат разные вложенные объекты. Берём то, что нашлось, остальное
        молча пропускаем — падать на незнакомом событии команде незачем.
        """
        senders = {}
        chats = set()
        updates = data.get('updates', []) if isinstance(data, dict) else []
        for update in updates:
            if not isinstance(update, dict):
                continue

            candidates = [update.get('user')]
            message = update.get('message')
            if isinstance(message, dict):
                candidates.append(message.get('sender'))
                recipient = message.get('recipient')
                if isinstance(recipient, dict) and recipient.get('chat_id'):
                    chats.add(str(recipient['chat_id']))
            if update.get('chat_id'):
                chats.add(str(update['chat_id']))

            for sender in candidates:
                if isinstance(sender, dict) and sender.get('user_id'):
                    user_id = str(sender['user_id'])
                    senders[user_id] = sender.get('name') or sender.get('username') or ''
        return senders, chats
