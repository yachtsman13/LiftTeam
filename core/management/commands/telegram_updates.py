"""
Кто писал боту в Telegram и с какими идентификаторами.

Нужна один раз при настройке — как max_updates, но для Telegram. Бот не
может написать человеку первым: пока сотрудник сам не напишет боту, слать
ему нечего и некуда. Эта команда показывает, кто уже написал.

Запуск: python manage.py telegram_updates
"""
from django.core.management.base import BaseCommand

from core import messengers
from core.models import Employee


class Command(BaseCommand):
    help = 'Показывает chat_id тех, кто писал боту Telegram'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=100,
            help='Сколько событий запросить (по умолчанию 100)',
        )
        parser.add_argument(
            '--raw', action='store_true',
            help='Вывести ответ Telegram как есть, без разбора',
        )

    def handle(self, *args, **options):
        if not messengers.telegram_is_configured():
            self.stderr.write(
                'Не задан TELEGRAM_BOT_TOKEN. Токен выдаёт @BotFather '
                'по команде /newbot, вписывается в .env (см. DEPLOY.md).'
            )
            return

        try:
            data = messengers.get_telegram_updates(limit=options['limit'])
        except messengers.MessengerError as error:
            self.stderr.write(str(error))
            return

        if options['raw']:
            import json
            self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
            return

        people, chats = self._collect_chats(data)
        if not people and not chats:
            self.stdout.write(
                'Боту никто не писал. Попросите сотрудников найти бота '
                'в Telegram, нажать «Запустить» и отправить любое сообщение, '
                'потом повторите команду.\n'
                'Важно: Telegram отдаёт только события за последние сутки.'
            )
            return

        known = dict(
            Employee.objects.exclude(telegram_chat_id='')
            .values_list('telegram_chat_id', 'full_name')
        )
        if people:
            self.stdout.write(f'{"chat_id":<16} {"Имя в Telegram":<30} В программе')
            for chat_id, name in sorted(people.items()):
                self.stdout.write(f'{chat_id:<16} {name[:30]:<30} {known.get(chat_id, "—")}')
            self.stdout.write(
                '\nВпишите нужный chat_id в карточку сотрудника '
                '(Администрирование → Пользователи → ID в Telegram).'
            )

        if chats:
            self.stdout.write('\nГруппы, где бот состоит: ' + ', '.join(sorted(chats)))
            self.stdout.write(
                'Идентификатор группы вписывается в TELEGRAM_GROUP_CHAT_ID — '
                'тогда оповещение уходит один раз в группу, а не каждому отдельно.'
            )

    def _collect_chats(self, data):
        """Достаёт из ответа личные диалоги и группы.

        Формат разбираем осторожно: событий у бота много видов, и в них
        лежат разные вложенные объекты. Берём то, что нашлось, остальное
        молча пропускаем — падать на незнакомом событии команде незачем.
        """
        people = {}
        chats = set()
        for update in (data.get('result', []) if isinstance(data, dict) else []):
            if not isinstance(update, dict):
                continue
            # Обычное сообщение, правка, сообщение в канале — у всех внутри
            # один и тот же объект message
            for key in ('message', 'edited_message', 'channel_post', 'my_chat_member'):
                message = update.get(key)
                if not isinstance(message, dict):
                    continue
                chat = message.get('chat')
                if not isinstance(chat, dict) or not chat.get('id'):
                    continue

                chat_id = str(chat['id'])
                if chat.get('type') == 'private':
                    name = ' '.join(
                        part for part in (chat.get('first_name'), chat.get('last_name'))
                        if part
                    ) or chat.get('username') or ''
                    people[chat_id] = name
                else:
                    chats.add(chat_id)
        return people, chats
