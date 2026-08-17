"""Прогонщик тестов LiftTeam.

Существует ради одной настройки — быстрого хеширования паролей. Django по
умолчанию считает PBKDF2 в сотни тысяч итераций: на рабочей установке это
защита от подбора пароля по украденной базе, в тестах — чистая трата.
В наборе 68 классов, и почти каждый заводит сотрудника в setUp; на хеши
уходило 346 секунд из 356.

Отдельный прогонщик, а не файл настроек: `python manage.py test` должен
работать без ключей — иначе половина запусков пойдёт мимо ускорения.
Подмена живёт только внутри тестового окружения и на настройки рабочей
установки не влияет никак.
"""
from django.conf import settings
from django.test.runner import DiscoverRunner

# MD5 здесь не про безопасность: пароли тестовых сотрудников существуют
# несколько секунд в базе, которую сразу же удаляют. Пароль пользователя
# этим хешером не хранится нигде и никогда — см. PASSWORD_HASHERS
# в lifteam/settings.py.
FAST_HASHER = 'django.contrib.auth.hashers.MD5PasswordHasher'


class FastPasswordTestRunner(DiscoverRunner):
    """Обычный прогонщик Django с быстрым хешером паролей."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        settings.PASSWORD_HASHERS = [FAST_HASHER]
