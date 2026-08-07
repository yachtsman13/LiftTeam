"""
Декораторы для контроля доступа по ролям сотрудника.
"""
from functools import wraps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def role_required(*roles):
    """Требует, чтобы пользователь был авторизован и имел одну из указанных ролей.
    Роль 'admin' допускается всегда, независимо от переданного списка."""
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.user.role != 'admin' and request.user.role not in roles:
                messages.error(request, 'Недостаточно прав для этого действия')
                return redirect('dashboard')
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
