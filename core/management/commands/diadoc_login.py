"""
Вход в Диадок для API — Device Authorization Flow.

Разовая команда: запускается один раз при настройке (и снова, если
Диадок когда-нибудь отзовёт refresh_token). Устроена так же по духу,
как аутентификация Т-Банка и Точки одним обращением, но сам вход у
Диадока интерактивный — нужен человек с браузером, который залогинится
под своей учётной записью Диадока. Программа на Pi браузера не имеет
и им быть не должна: вместо этого она печатает ссылку, человек открывает
её на любом устройстве (хоть на телефоне), а команда ждёт результата.

После входа команда сама узнаёт boxId (через GetMyOrganizations) и
предлагает вписать его в настройки — угадывать его незачем, он приходит
в том же ответе.
"""
import time

from django.core.management.base import BaseCommand, CommandError

from core import diadoc, envfile


class Command(BaseCommand):
    help = 'Войти в Диадок (Device Authorization Flow) и получить DIADOC_REFRESH_TOKEN'

    def handle(self, *args, **options):
        if not diadoc.client_id() or not diadoc.client_secret():
            raise CommandError(
                'Сначала задайте DIADOC_CLIENT_ID (страница «Настройки») '
                'и DIADOC_CLIENT_SECRET (manage.py setsecret DIADOC_CLIENT_SECRET).'
            )

        try:
            start = diadoc.start_device_authorization()
        except diadoc.DiadocError as exc:
            raise CommandError(str(exc))

        device_code = start.get('device_code')
        user_code = start.get('user_code')
        link = start.get('verification_uri_complete') or start.get('verification_uri')
        interval = int(start.get('interval') or 5)
        expires_in = int(start.get('expires_in') or 300)
        if not device_code or not link:
            raise CommandError(f'Диадок не вернул ожидаемые поля: {start}')

        self.stdout.write('Откройте на любом устройстве и войдите под своей учётной записью Диадока:')
        self.stdout.write(self.style.WARNING(link))
        if user_code:
            self.stdout.write(f'Код, если он не подставился в ссылку сам: {user_code}')
        self.stdout.write('Жду подтверждения…')

        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                answer = diadoc.poll_device_token(device_code)
            except diadoc.DiadocError as exc:
                text = str(exc)
                if 'authorization_pending' in text:
                    continue
                if 'slow_down' in text:
                    interval += 5
                    continue
                raise CommandError(f'Вход не удался: {exc}')

            refresh = answer.get('refresh_token')
            if not refresh:
                raise CommandError(f'Диадок не вернул refresh_token: {answer}')
            envfile.set_value('DIADOC_REFRESH_TOKEN', refresh, allow_secrets=True)
            self.stdout.write(self.style.SUCCESS('DIADOC_REFRESH_TOKEN записан.'))
            self._show_box_id()
            return

        raise CommandError('Время на вход истекло. Запустите команду заново.')

    def _show_box_id(self):
        try:
            payload = diadoc.get_my_organizations()
        except diadoc.DiadocError as exc:
            self.stderr.write(f'Список организаций не получен: {exc}')
            return

        organizations = payload.get('Organizations') if isinstance(payload, dict) else None
        if not organizations:
            self.stdout.write(f'Диадок не вернул организаций в ожидаемом виде: {payload}')
            return

        self.stdout.write('Ящики (впишите нужный в настройку DIADOC_BOX_ID):')
        for org in organizations:
            name = org.get('ShortName') or org.get('FullName') or '?'
            for box in org.get('Boxes') or []:
                self.stdout.write(f'{box.get("BoxId", "?")}  {name}')
