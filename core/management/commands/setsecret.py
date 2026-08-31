"""
Management command: setsecret
LiftTeam v2.97.0

Ввод секретов у самого Raspberry Pi.

Почему не из браузера. Страница настроек доступна с любого устройства
в Tailscale, а разделить «из офиса» и «издалека» не выходит: сертификат
HTTPS выдан на имя в Tailscale, и по локальному адресу 192.168.1.x браузер
пойдёт без него — то есть токен отправился бы по офисной сети открытым
текстом. Ввод у Pi под ssh снимает вопрос целиком: шифрование даёт ssh,
а «физически рядом» проверяется само собой.

Значение **не принимается аргументом** — оно осело бы в истории оболочки
и в списке процессов. Спрашивается скрытым вводом и нигде не печатается.
"""
import getpass

from django.core.management.base import BaseCommand, CommandError

from core import envfile, selfcheck


class Command(BaseCommand):
    help = 'Записать секрет (токен, пароль) в файл настроек .env'

    def add_arguments(self, parser):
        parser.add_argument(
            'name', nargs='?', type=str,
            help='Имя настройки, например TBANK_TOKEN. Без имени — список.',
        )
        parser.add_argument(
            '--list', action='store_true',
            help='Показать, какие секреты заданы (значения не показываются)',
        )
        parser.add_argument(
            '--clear', action='store_true',
            help='Стереть значение (записывается пустое)',
        )
        parser.add_argument(
            '--no-check', action='store_true',
            help='Не проверять связь после записи',
        )

    def handle(self, *args, **options):
        name = (options.get('name') or '').strip().upper()
        if options['list'] or not name:
            self._show_list()
            return

        if name not in envfile.SECRET_NAMES:
            raise CommandError(
                'Неизвестный секрет: %s. Список: %s'
                % (name, ', '.join(envfile.SECRET_NAMES))
            )

        self.stdout.write('Файл настроек: %s' % envfile.path())
        if options['clear']:
            value = ''
            self.stdout.write('Значение %s будет стёрто.' % name)
        else:
            value = self._ask(name)

        try:
            envfile.set_value(name, value, allow_secrets=True)
        except envfile.EnvFileError as error:
            raise CommandError(str(error))

        self.stdout.write(self.style.SUCCESS(
            '%s %s.' % (name, 'стёрт' if not value else 'записан')
        ))
        if name in envfile.SECRETS_NEEDING_RESTART:
            self.stdout.write(self.style.WARNING(
                'Эта настройка читается не нашим кодом, а Django, поэтому '
                'подхватится только после перезапуска: '
                'sudo systemctl restart lifteam'
            ))
        else:
            self.stdout.write('Действует сразу, перезапуск не нужен.')

        if value and not options['no_check']:
            self._check(name)

    def _ask(self, name):
        """Скрытый ввод с подтверждением.

        Подтверждение здесь не формальность: токен вставляют из письма
        или из личного кабинета банка, и потерянный при вставке знак
        не видно — ввод не отображается.
        """
        title = envfile.SECRET_TITLES.get(name, name)
        self.stdout.write('%s (%s). Ввод не отображается.' % (title, name))
        first = getpass.getpass('Значение: ')
        if not first.strip():
            raise CommandError(
                'Пустое значение. Чтобы стереть настройку, укажите --clear.'
            )
        second = getpass.getpass('Повторите: ')
        if first != second:
            raise CommandError('Значения не совпали. Ничего не записано.')
        return first.strip()

    def _check(self, name):
        self.stdout.write('Проверяю связь…')
        result = selfcheck.check(name)
        if result.ok:
            self.stdout.write(self.style.SUCCESS(result.message))
        elif result.state == selfcheck.CheckResult.SKIPPED:
            self.stdout.write(result.message)
        else:
            # Не CommandError: записать-то записали, и откатывать нечего.
            # Человеку нужно знать оба факта сразу
            self.stdout.write(self.style.ERROR(result.message))

    def _show_list(self):
        self.stdout.write('Файл настроек: %s%s' % (
            envfile.path(),
            '' if envfile.exists() else ' (пока не существует)',
        ))
        self.stdout.write('')
        for name in envfile.SECRET_NAMES:
            state = envfile.describe_secret(name)
            if state['filled']:
                mark = self.style.SUCCESS('задан, %d знаков' % state['length'])
                if state['source'] == 'settings':
                    mark += ' (не из файла — из окружения службы)'
            else:
                mark = self.style.WARNING('не задан')
            self.stdout.write('  %-24s %-28s %s' % (
                name, state['title'], mark,
            ))
        self.stdout.write('')
        self.stdout.write(
            'Записать: python manage.py setsecret ИМЯ. '
            'Значение спрашивается скрытым вводом — аргументом оно '
            'не принимается, чтобы не осесть в истории команд.'
        )
