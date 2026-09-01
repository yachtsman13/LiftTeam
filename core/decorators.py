"""
Декораторы для контроля доступа.

С v2.98.0 доступ проверяется по **праву**, а не по роли: роли были
зашиты в код четырьмя штуками, и каждая закрытая страница перечисляла
те из них, кому она открыта. Права даёт должность (`core.models.Position`),
и заводит должности администратор, а не программист.

Имя права — из явного списка `models.PERMISSIONS`. Незнакомое имя
не проходит никогда: `Position.allows` сверяется с выданными правами,
а выдать можно только то, что в списке есть.
"""
from functools import wraps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def permission_required(code):
    """Требует, чтобы у сотрудника было право `code`.

    Должность с полным доступом (`is_admin`) проходит всегда — так же,
    как прежняя роль «Администратор». Это не просто «выданы все права
    из списка»: право, добавленное в следующем выпуске, у неё появится
    само, и новая страница не окажется закрытой для владельца до того,
    как он вспомнит про галочку.
    """
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.allows(code):
                messages.error(request, 'Недостаточно прав для этого действия')
                return redirect('dashboard')
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
